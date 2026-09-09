import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { ROOT, resolveConfig } from './config.js';
import { VaneError } from './errors.js';
import type { HarnessContext } from './harness.js';
import { createTools } from './tools.js';
import { WorkspaceManager } from './workspace-manager.js';

export const name = 'dsh-vane';
export const inject = ['tools','systemPrompt'] as const;
export function apply(ctx: HarnessContext, config: unknown = {}) {
  if (typeof ctx.tools?.register !== 'function' || typeof ctx.systemPrompt?.section !== 'function' || typeof ctx.effect !== 'function') throw new VaneError('HOST_VERSION_UNSUPPORTED', 'Host requires tools.register, systemPrompt.section and effect lifecycle.');
  const manager = new WorkspaceManager(resolveConfig(config));
  const disposers: (() => void)[] = [];
  try {
    for (const tool of createTools(manager)) disposers.push(ctx.tools.register(tool));
    for (const [file, section, order] of [['prompts/data-agent.md','dsh-vane:analysis',120],['skills/data-analysis/SKILL.md','dsh-vane:skill',121]] as const) {
      disposers.push(ctx.systemPrompt.section({ name: section, order, text: readFileSync(join(ROOT,file),'utf8') }));
    }
  } catch (e) { disposers.reverse().forEach(d => d()); void manager.dispose(); throw e; }
  ctx.effect(() => async () => { disposers.reverse().forEach(d => d()); await manager.dispose(); });
  ctx.logger?.info('dsh-vane: six tools and analysis guidance registered (Vane runtime starts on vane_open).');
}
