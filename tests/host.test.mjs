import test from 'node:test';
import assert from 'node:assert/strict';
import { Context } from '@deepseek-ai/cordis';
import SystemPrompt, { renderPrompt } from '@deepseek-ai/dsh-system-prompt';
import ToolRuntime from '@deepseek-ai/dsh-tools';
import * as plugin from '../dist/index.js';
import { harness } from './helpers.mjs';

test('published DSH registry enforces schemas, renders canonical results and assembles both prompt sections', async t => {
  const h = await harness({enabledPackages:[]}); t.after(() => h.manager.dispose());
  const ctx = new Context(); t.after(() => ctx.fiber.dispose());
  await ctx.plugin(SystemPrompt);
  await ctx.plugin(ToolRuntime);
  const fiber = await ctx.plugin(plugin,h.config);
  const assembled = await ctx.systemPrompt.assemble();
  assert.equal(assembled.tools.filter(t => t.name.startsWith('vane_')).length,6);
  const prompt = renderPrompt(assembled);
  assert.match(prompt,/You own the research plan/);
  assert.match(prompt,/Frame the decision/);
  assert.match(prompt,/DeepSeek Harness/);
  const absent = await ctx.tools.execute({name:'vane_open',arguments:{},callId:'absent',signal:new AbortController().signal});
  assert.equal(absent.isError,false);
  assert.equal(absent.value.error.code,'HOST_CONTEXT_UNAVAILABLE');
  const output = await ctx.tools.execute({name:'vane_open',arguments:{},callId:'owned',agent:{id:'real-registry-session',ctx},signal:new AbortController().signal});
  assert.equal(output.isError,false,JSON.stringify(output));
  assert.equal(output.value.ok,true,JSON.stringify(output));
  assert.deepEqual(JSON.parse(output.content[0].text),output.value);
  await fiber.dispose();
  assert.equal(ctx.tools.schemas().filter(t => t.name.startsWith('vane_')).length,0);
  assert.doesNotMatch(renderPrompt(await ctx.systemPrompt.assemble()),/You own the research plan/);
});

test('unsupported host contract and configured version fail explicitly', () => {
  assert.throws(() => plugin.apply({}),/Host requires/);
  assert.throws(() => plugin.resolveConfig({hostVersion:'0.0.0'}),/Supported DSH version/);
});
