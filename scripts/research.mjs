#!/usr/bin/env node
// Real DSH AgentLoop with real model decisions. No predetermined tool sequence.
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { createUserMessage } from '@deepseek-ai/dsh-llm';
import { boot, LocalModelAdapter } from './host-support.mjs';

const root=resolve('.');
const outputArg=process.argv.indexOf('--output');
const destination=resolve(outputArg>=0?process.argv[outputArg+1]:process.env.DSH_VANE_RESEARCH_OUTPUT??'receipts/research');
await mkdir(destination,{recursive:true});
const baseURL=process.env.DSH_VANE_RESEARCH_URL??'http://127.0.0.1:8001/v1';
const model=process.env.DSH_VANE_RESEARCH_MODEL??'Qwen2.5-VL-3B-Instruct';
const receipt={kind:'real-model-research',model,baseURL,started_at:new Date().toISOString(),status:'incomplete',criteria:{}};
try {
  const probe=await fetch(baseURL+'/models',{signal:AbortSignal.timeout(5000)});
  if(!probe.ok)throw new Error('model service HTTP '+probe.status);
}catch(e){
  receipt.status='blocked';receipt.reason='Configured model service unavailable: '+e.message;
  await writeFile(join(destination,'receipt.json'),JSON.stringify(receipt,null,2));console.log(receipt.reason);process.exit(2);
}
const adapter=new LocalModelAdapter({baseURL,model,maxSteps:Number(process.env.DSH_VANE_RESEARCH_MAX_STEPS??32),apiKeyEnv:process.env.DSH_VANE_RESEARCH_KEY_ENV});
const ctx=await boot({workspaceRoot:join(destination,'workspaces'),allowedFileRoots:[join(root,'examples/materials')],
  models:[{alias:'local-vision',baseURL,model,temperature:0,maxTokens:1800}],limits:{waitMs:1000,maxWaitMs:2000}},adapter);
const trace=[];
ctx.on('tools/result',(exec,result)=>{trace.push({name:exec.name,arguments:exec.arguments,agent_id:exec.agent?.id,value:result.value??null,isError:result.isError});console.log('tool '+exec.name+' '+(result.value?.status??'error'));});
const handle=await ctx.agents.create({sessionId:'vane-research-'+randomUUID(),meta:{cwd:join(root,'examples/materials')},agentOptions:{provider:'vane-research',model,maxTokens:1000}});
const agent=handle.agent;
const prompts=[
  '比较现有记录与新报告，识别变化及证据不足处，并给出完整分析。历史记录是 @history.csv，新报告是 @new-report.pdf；如需补充，另有 @supplement.csv。请自行检查材料、选择方法和工具，保留可核验的来源。',
  '补充一份 @follow-up.csv，请沿用刚才的任务工作区更新比较，说明新增材料如何改变结论以及还有哪些证据不足。'
];
try{
  for(const prompt of prompts){
    agent.followup(createUserMessage({content:[{type:'text',text:prompt}],source:{kind:'user'}}));
    await agent.whenIdle();
  }
  const events=agent.session.snapshotEvents();
  const messages=events.filter(e=>e.type==='assistant/message').map(e=>e.data.message);
  const conclusions=messages.filter(m=>!m.content?.some(b=>b.type==='tool-call')).map(m=>m.content?.filter(b=>b.type==='text').map(b=>b.text).join('\n')).filter(Boolean);
  const workspaces=[...new Set(trace.filter(t=>t.name==='vane_open'&&t.value?.ok).map(t=>t.value.workspace_id))];
  receipt.criteria={model_participated:adapter.requests.some(r=>r.response),tools_called:trace.length,loaded_files:trace.filter(t=>t.name==='vane_load'&&t.arguments.source.kind==='file').map(t=>t.arguments.source.path),
    sql_executed:trace.some(t=>t.name==='vane_execute'&&t.arguments.mode==='sql'&&t.value?.ok),
    pipeline_executed:trace.some(t=>t.name==='vane_execute'&&t.arguments.mode==='pipeline'&&t.value?.ok),
    workspace_ids:workspaces,followup_same_workspace:workspaces.length===1&&trace.some(t=>t.name==='vane_load'&&t.arguments.source.path?.includes('follow-up')),
    final_messages:conclusions.length,manual_review_required:true};
  receipt.status=trace.length?'recorded':'incomplete';receipt.agent_id=agent.id;
  if(!trace.length)receipt.reason='Model returned proposed tool calls as prose without executing tools; autonomous analysis did not pass.';
  await writeFile(join(destination,'session-events.json'),JSON.stringify(events,null,2));
  await writeFile(join(destination,'conclusions.md'),conclusions.join('\n\n---\n\n')||'模型未给出完整结论。请查看原始响应与工具轨迹。\n');
}catch(e){receipt.status='incomplete';receipt.reason=e.message;}
finally{
  receipt.finished_at=new Date().toISOString();
  await writeFile(join(destination,'model-raw.json'),JSON.stringify(adapter.requests,null,2));
  await writeFile(join(destination,'tool-trace.json'),JSON.stringify(trace,null,2));
  await writeFile(join(destination,'receipt.json'),JSON.stringify(receipt,null,2));
  await handle.dispose();await ctx.fiber.dispose();
}
console.log(JSON.stringify(receipt,null,2));
