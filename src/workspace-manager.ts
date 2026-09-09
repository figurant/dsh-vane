import { createHash, randomUUID } from 'node:crypto';
import { mkdir, realpath, writeFile } from 'node:fs/promises';
import { join, resolve, relative, isAbsolute } from 'node:path';
import type { Config } from './config.js';
import type { Execution } from './harness.js';
import { failure, VaneError, type Outcome } from './errors.js';
import { RuntimeProcess } from './ipc.js';
import { WeKnoraClient } from './sources/weknora.js';

type State = 'opening'|'ready'|'busy'|'closing'|'closed'|'lost';
interface Operation { id: string; status: 'queued'|'running'|'succeeded'|'failed'|'cancelled'; outcome?: Outcome;
  done: Promise<void>; finish(): void; abort: AbortController; running?: Promise<void>; cancelling?: Promise<void> }
interface Workspace { id: string; owner: string; scope: string; root: string; state: State; runtime: RuntimeProcess;
  queue: Promise<void>; operations: Map<string, Operation>; active?: Operation; pending: number; lastUsed: number; capabilities?: any }
export class WorkspaceManager {
  private workspaces = new Map<string, Workspace>();
  private current = new Map<string,string>();
  private opening = new Map<string,Promise<Workspace>>();
  private timer: NodeJS.Timeout;
  private disposed = false;
  private weknora: WeKnoraClient;
  constructor(readonly config: Config) {
    this.weknora = new WeKnoraClient(config);
    this.timer = setInterval(() => void this.reap(), Math.min(config.limits.idleMs, 30000)); this.timer.unref();
  }
  private get(id: string, owner: string, allowClosed = false) {
    const ws = this.workspaces.get(id);
    if (!ws || ws.owner !== owner) throw new VaneError('WORKSPACE_NOT_FOUND', 'Workspace is not in this session.');
    if (!allowClosed && !['ready','busy'].includes(ws.state)) throw new VaneError(ws.state === 'lost' ? 'PROCESS_LOST' : 'WORKSPACE_CLOSED', `Workspace is ${ws.state}.`, true);
    ws.lastUsed = Date.now(); return ws;
  }
  private async open(owner: string, packageIds?: string[], newTask = false): Promise<Workspace> {
    if (this.disposed) throw new VaneError('PLUGIN_CLOSED', 'Plugin is unloading.');
    if (packageIds?.some(id => !this.config.enabledPackages.some(p => p.id === id))) throw new VaneError('PACKAGE_NOT_ALLOWED', 'Package is not enabled by the host.');
    if (!newTask) {
      const pending = this.opening.get(owner); if (pending) return pending;
      const existing = this.current.get(owner);
      if (existing) { const ws = this.workspaces.get(existing)!; if (['ready','busy'].includes(ws.state)) {
        if (packageIds?.some(id => !ws.capabilities?.packages.some((p: any) => p.id === id))) throw new VaneError('PACKAGE_NOT_ENABLED', 'Use new_task=true to change the package set.');
        return ws;
      } }
    }
    const promise = this.create(owner, packageIds);
    if (!newTask) this.opening.set(owner, promise);
    try { return await promise; } finally { if (!newTask) this.opening.delete(owner); }
  }
  private async create(owner: string, packageIds?: string[]) {
    if ([...this.workspaces.values()].filter(w => !['closed','lost'].includes(w.state)).length >= this.config.limits.maxWorkspaces) throw new VaneError('WORKSPACE_LIMIT', 'Close an existing workspace before opening another.', true);
    const id = randomUUID(), scope = createHash('sha256').update(owner).digest('hex');
    const root = join(this.config.workspaceRoot, scope, id);
    const ws = { id, owner, scope, root, state: 'opening', queue: Promise.resolve(), operations: new Map(), pending: 0, lastUsed: Date.now() } as Workspace;
    // Reserve capacity before any await, including concurrent opens from other sessions.
    this.workspaces.set(id, ws);
    try {
      await mkdir(root, { recursive: true, mode: 0o700 });
      if (this.disposed) throw new VaneError('PLUGIN_CLOSED', 'Plugin unloaded while the workspace was opening.');
      ws.runtime = new RuntimeProcess(this.config, id, root);
      ws.runtime.onLost = error => {
        if (ws.state === 'closed' || ws.state === 'closing') return;
        ws.state = 'lost';
        for (const op of ws.operations.values()) if (!op.outcome) { op.abort.abort(); op.status = 'failed'; op.outcome = failure(error, ws.id, op.id); op.finish(); }
      };
      const deadline = setTimeout(() => void ws.runtime.kill(), 30000);
      try { ws.capabilities = await ws.runtime.request('hello', { config: { ...this.config, enabledPackages: this.config.enabledPackages.filter(p => !packageIds || packageIds.includes(p.id)) }, workspace_root: root, scope_id: scope }); }
      finally { clearTimeout(deadline); }
      if (this.disposed) throw new VaneError('PLUGIN_CLOSED', 'Plugin unloaded while the runtime was starting.');
      ws.state = 'ready'; this.current.set(owner, id); return ws;
    } catch (error) { ws.state = 'lost'; if (ws.runtime) await ws.runtime.kill(); throw error; }
  }
  private status(ws: Workspace, op: Operation): Outcome {
    return op.outcome ?? { ok: true, workspace_id: ws.id, operation_id: op.id, status: op.status, data: { workspace_state: ws.state } };
  }
  private submit(ws: Workspace, work: (signal: AbortSignal, id: string) => Promise<any>): Operation {
    if (ws.pending >= this.config.limits.maxQueue) throw new VaneError('QUEUE_LIMIT', 'Workspace operation queue is full.', true);
    let finish!: () => void; const done = new Promise<void>(r => { finish = r; });
    const op: Operation = { id: randomUUID(), status: 'queued', done, finish, abort: new AbortController() };
    ws.operations.set(op.id, op); ws.pending++; ws.state = 'busy';
    const run = async () => {
      if (op.outcome) return;
      if (['lost','closing','closed'].includes(ws.state)) { op.outcome = failure(new VaneError('PROCESS_LOST', 'Workspace is unavailable.', true), ws.id, op.id); op.finish(); return; }
      ws.active = op; op.status = 'running';
      const timeout = setTimeout(() => void this.cancel(ws, op), this.config.limits.operationTimeoutMs);
      try {
        const data = await work(op.abort.signal, op.id);
        if (!op.outcome) { op.status = 'succeeded'; op.outcome = { ok: true, workspace_id: ws.id, operation_id: op.id, status: 'succeeded', data }; }
      } catch (error) { if (!op.outcome) { op.status = 'failed'; op.outcome = failure(error, ws.id, op.id); } }
      finally { clearTimeout(timeout); if (ws.active === op) ws.active = undefined; op.finish(); }
    };
    op.running = ws.queue.then(run).finally(() => {
      ws.pending--; ws.lastUsed = Date.now(); if (ws.pending === 0 && ws.state === 'busy') ws.state = 'ready';
    });
    ws.queue = op.running.catch(() => {});
    return op;
  }
  private async wait(ws: Workspace, op: Operation, waitMs: number, signal: AbortSignal) {
    let abortDone: Promise<void> | undefined;
    const abort = () => { abortDone = this.cancel(ws, op); };
    signal.addEventListener('abort', abort, { once: true });
    if (signal.aborted) abort();
    let timer: NodeJS.Timeout | undefined;
    try {
      await Promise.race([op.done, new Promise<void>(r => { timer = setTimeout(r, waitMs); })]);
      if (abortDone) await abortDone;
      return this.status(ws, op);
    } finally { clearTimeout(timer); signal.removeEventListener('abort', abort); }
  }
  private async cancel(ws: Workspace, op: Operation) {
    if (op.outcome) return;
    if (op.cancelling) return op.cancelling;
    op.cancelling = this.cancelOperation(ws, op);
    return op.cancelling;
  }
  private async cancelOperation(ws: Workspace, op: Operation) {
    const running = op.status === 'running';
    op.abort.abort();
    if (running) {
      // A runtime that has already committed owns its terminal result. A late
      // cancellation must not relabel a successfully published artifact.
      let timer: NodeJS.Timeout | undefined;
      try {
        const ack = await Promise.race([ws.runtime.request('cancel', { operation_id: op.id }).catch(() => null),
          new Promise<null>(r => { timer = setTimeout(() => r(null), this.config.limits.cancelGraceMs); })]);
        if (ack?.accepted === false) { await op.running; return; }
      } finally { clearTimeout(timer); }
    }
    if (!op.outcome) {
      op.status = 'cancelled'; op.outcome = failure(new VaneError('CANCELLED', 'Operation cancelled; external model/file side effects may not be reversible.'), ws.id, op.id); op.finish();
    }
    if (running) {
      let timer: NodeJS.Timeout | undefined;
      try { await Promise.race([op.running, new Promise<void>(r => { timer = setTimeout(r, this.config.limits.cancelGraceMs); })]); }
      finally { clearTimeout(timer); }
      if (ws.active === op) { ws.state = 'lost'; await ws.runtime.kill(); }
    }
  }
  private async close(ws: Workspace) {
    if (ws.state === 'closed') return;
    ws.state = 'closing';
    await Promise.all([...ws.operations.values()].filter(o => !o.outcome).map(o => this.cancel(ws, o)));
    await ws.queue;
    await ws.runtime.close(); ws.state = 'closed';
  }
  async call(name: string, a: Record<string, any>, exec: Execution): Promise<Outcome> {
    const owner = exec.agent!.id;
    if (name === 'vane_open') {
      const ws = await this.open(owner, a.package_ids, a.new_task);
      return { ok: true, workspace_id: ws.id, status: ws.state, data: { ...ws.capabilities, limits: this.config.limits,
        sources: { weknora: this.config.weknoraSources.map(s => s.alias), databases: this.config.databases.map(s => s.alias), artifact_stores: ['workspace',...this.config.artifactStores.map(s => s.alias)], models: this.config.models.map(s => s.alias) } } };
    }
    const ws = this.get(a.workspace_id, owner, name === 'vane_control');
    if (name === 'vane_control') {
      if (['status','cancel'].includes(a.action)) {
        const op = ws.operations.get(a.operation_id);
        if (!op) throw new VaneError('OPERATION_NOT_FOUND', 'Operation does not belong to this workspace.');
        if (a.action === 'cancel') await this.cancel(ws, op);
        return this.status(ws, op);
      }
      if (a.action === 'close') { await this.close(ws); return { ok: true, workspace_id: ws.id, status: 'closed' }; }
      if (a.action === 'restore') {
        if (!/^[0-9a-f-]{36}$/.test(a.checkpoint_id)) throw new VaneError('INVALID_ARGUMENT', 'Invalid checkpoint ID.');
        const restored = await this.open(owner, undefined, true);
        const op = this.submit(restored, (_signal, id) => restored.runtime.request('restore', { checkpoint_id: a.checkpoint_id }, id));
        return this.wait(restored, op, this.config.limits.waitMs, exec.signal);
      }
      this.get(ws.id, owner);
      const op = this.submit(ws, (_signal,id) => ws.runtime.request('checkpoint', { tables: a.tables }, id));
      return this.wait(ws, op, this.config.limits.waitMs, exec.signal);
    }
    const op = this.submit(ws, async (signal, id) => {
      let data = { ...a };
      if (name === 'vane_load') data = await this.prepareLoad(ws, data, exec, signal);
      return ws.runtime.request(name.slice(5), data, id);
    });
    return this.wait(ws, op, a.wait_ms ?? this.config.limits.waitMs, exec.signal);
  }
  private async prepareLoad(ws: Workspace, data: Record<string, any>, exec: Execution, signal: AbortSignal) {
    const source = data.source;
    if (source.kind === 'file') {
      const cwd = exec.agent?.session?.header?.cwd;
      if (!isAbsolute(source.path) && (!cwd || !isAbsolute(cwd))) throw new VaneError('HOST_CONTEXT_UNAVAILABLE', 'Relative files require trusted agent.session.header.cwd; use an allowed absolute path.');
      let path: string;
      try { path = await realpath(resolve(cwd ?? '/', source.path)); } catch { throw new VaneError('FILE_NOT_FOUND', 'Source file does not exist.'); }
      const roots = await Promise.all(this.config.allowedFileRoots.map(r => realpath(r)));
      if (!roots.some(r => { const rel = relative(r,path); return rel === '' || (rel !== '..' && !rel.startsWith('../') && !rel.startsWith('..\\') && !isAbsolute(rel)); })) throw new VaneError('PERMISSION_DENIED', 'Source file is outside allowedFileRoots after symlink resolution.');
      return { ...data, source: { kind: 'file', path } };
    }
    if (source.kind === 'weknora') {
      const knowledge = await this.weknora.read(source.source_alias, source.knowledge_id, source.chunk_ids, signal);
      if (signal.aborted) throw new VaneError('CANCELLED', 'Loading cancelled.');
      // Stage large HTTP results as host-owned data, not in an unbounded IPC frame.
      const path = join(ws.root, `knowledge-${randomUUID()}.json`);
      await writeFile(path, JSON.stringify(knowledge), { mode: 0o600 });
      return { ...data, source: { ...source, staged_path: path } };
    }
    if (source.kind === 'artifact' && source.store_alias !== 'workspace') {
      if (!source.knowledge_id || !source.source_alias) throw new VaneError('PERMISSION_DENIED', 'Shared artifacts require a readable WeKnora knowledge reference.');
      // Re-read evidence so a readable, unrelated document cannot authorize a guessed artifact ID.
      const knowledge = await this.weknora.read(source.source_alias, source.knowledge_id, undefined, signal);
      if (!knowledge.artifact_ids.includes(source.artifact_id)) throw new VaneError('PERMISSION_DENIED', 'Artifact is not referenced by the authorized knowledge.');
    }
    return data;
  }
  async reap(now = Date.now()) {
    await Promise.all([...this.workspaces.values()].filter(w => w.state === 'ready' && w.pending === 0 && now - w.lastUsed >= this.config.limits.idleMs).map(w => this.close(w)));
  }
  async dispose() { this.disposed = true; clearInterval(this.timer); await Promise.all([...this.workspaces.values()].filter(w => w.runtime).map(w => this.close(w))); }
}
