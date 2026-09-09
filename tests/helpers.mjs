import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve, join } from 'node:path';
import { resolveConfig, WorkspaceManager, createTools } from '../dist/index.js';

export async function harness(overrides = {}) {
  const root = await mkdtemp(join(tmpdir(),'dsh-vane-test-'));
  const config = resolveConfig({ workspaceRoot: root, allowedFileRoots: [resolve('tests/fixtures'), root], ...overrides });
  const manager = new WorkspaceManager(config);
  const tools = new Map(createTools(manager).map(t => [t.name,t]));
  async function call(name, args = {}, owner = 'session-a', signal = new AbortController().signal) {
    return tools.get(name).execute(args, { agent: { id: owner, session: { header: { cwd: resolve('tests/fixtures') } } }, signal });
  }
  async function finish(result, owner = 'session-a') {
    const deadline = Date.now() + 30000;
    while (['queued','running'].includes(result.status)) {
      if (Date.now() > deadline) throw new Error('operation did not finish: ' + JSON.stringify(result));
      await new Promise(r => setTimeout(r,25));
      result = await call('vane_control', { workspace_id: result.workspace_id, action:'status', operation_id:result.operation_id }, owner);
    }
    return result;
  }
  async function success(name, args, owner) { const r = await finish(await call(name,args,owner),owner); assert.equal(r.ok,true,JSON.stringify(r)); return r; }
  return { root, config, manager, tools, call, finish, success };
}
