import { spawn, type ChildProcess } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import type { Readable } from 'node:stream';
import type { Config } from './config.js';
import { VaneError } from './errors.js';

export const PROTOCOL = 'dsh-vane-ipc/v1';
export class RuntimeProcess {
  child: ChildProcess;
  private pending = new Map<string, { resolve(value: any): void; reject(error: Error): void }>();
  private buffer = Buffer.alloc(0);
  private ended = false;
  private logs = '';
  readonly exited: Promise<void>;
  onLost?: (error: VaneError) => void;

  constructor(readonly config: Config, readonly workspaceId: string, readonly cwd: string) {
    this.child = spawn(config.pythonExecutable, ['-u','-m',config.runtimeModule], {
      cwd, env: { ...process.env, PYTHONUNBUFFERED: '1', DSH_VANE_MAX_FRAME: String(config.limits.maxFrameBytes) },
      detached: process.platform !== 'win32', stdio: ['pipe','pipe','pipe','pipe']
    });
    this.exited = new Promise(resolve => this.child.once('close', () => resolve()));
    for (const stream of [this.child.stdout, this.child.stderr]) stream?.on('data', (chunk: Buffer) => {
      // Kept only in memory, never sent to the model or persisted (UDFs may print secrets).
      this.logs = (this.logs + chunk.toString()).slice(-8192);
    });
    (this.child.stdio[3] as Readable).on('data', (chunk: Buffer) => this.receive(chunk));
    this.child.on('error', () => this.fail(new VaneError('PROCESS_LOST', 'Python could not start. Install the locked runtime and check pythonExecutable.', true)));
    this.child.on('exit', () => this.fail(new VaneError('PROCESS_LOST', 'The task Python process exited. Restore a checkpoint into a new workspace.', true)));
    this.child.stdin?.on('error', () => this.fail(new VaneError('PROCESS_LOST', 'Runtime input channel closed.', true)));
  }
  private receive(chunk: Buffer) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    for (;;) {
      const newline = this.buffer.indexOf(10);
      if (newline < 0) break;
      if (newline > this.config.limits.maxFrameBytes) return this.fail(new VaneError('IPC_FRAME_LIMIT', 'Runtime frame exceeded the configured limit.'));
      const line = this.buffer.subarray(0, newline); this.buffer = this.buffer.subarray(newline + 1);
      let frame: any;
      try { frame = JSON.parse(line.toString('utf8')); } catch { return this.fail(new VaneError('IPC_PROTOCOL_ERROR', 'Malformed runtime frame.')); }
      if (frame.protocol !== PROTOCOL || frame.workspace_id !== this.workspaceId || typeof frame.request_id !== 'string' || typeof frame.ok !== 'boolean') return this.fail(new VaneError('IPC_PROTOCOL_ERROR', 'Unexpected runtime frame identity.'));
      const p = this.pending.get(frame.request_id);
      if (!p) continue; // A request already failed/cancelled; never resurrect it.
      this.pending.delete(frame.request_id);
      if (frame.ok) p.resolve(frame.data);
      else p.reject(new VaneError(frame.error?.code ?? 'RUNTIME_ERROR', frame.error?.message ?? 'Runtime operation failed.', frame.error?.retryable ?? false, frame.error?.details));
    }
    if (this.buffer.length > this.config.limits.maxFrameBytes) this.fail(new VaneError('IPC_FRAME_LIMIT', 'Runtime frame exceeded the configured limit.'));
  }
  request(action: string, data: unknown = {}, operationId?: string): Promise<any> {
    if (this.ended) return Promise.reject(new VaneError('PROCESS_LOST', 'Runtime is no longer available.', true));
    const request_id = randomUUID();
    const frame = JSON.stringify({ protocol: PROTOCOL, request_id, workspace_id: this.workspaceId, action, data, operation_id: operationId });
    if (Buffer.byteLength(frame) > this.config.limits.maxFrameBytes) return Promise.reject(new VaneError('IPC_FRAME_LIMIT', 'Request exceeds the configured frame size.'));
    return new Promise((resolve, reject) => {
      this.pending.set(request_id, { resolve, reject });
      this.child.stdin!.write(frame + '\n', error => { if (error) this.fail(new VaneError('PROCESS_LOST', 'Runtime input channel failed.', true)); });
    });
  }
  private fail(error: VaneError) {
    if (this.ended) return;
    this.ended = true;
    for (const p of this.pending.values()) p.reject(error);
    this.pending.clear();
    this.killGroup();
    this.onLost?.(error);
  }
  private killGroup() {
    if (!this.child.pid) return;
    try { process.kill(process.platform === 'win32' ? this.child.pid : -this.child.pid, 'SIGKILL'); } catch { /* already reaped */ }
  }
  async kill() { this.fail(new VaneError('PROCESS_LOST', 'Runtime terminated.', true)); await this.exited; }
  async close() {
    if (this.ended) { await this.exited; return; }
    const timer = setTimeout(() => this.killGroup(), this.config.limits.cancelGraceMs);
    try { await this.request('close'); } catch { /* exited during close */ }
    finally { this.killGroup(); await this.exited; clearTimeout(timer); }
  }
}
