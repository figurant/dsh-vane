#!/usr/bin/env node
// Deterministic transport/registration regression only; not an autonomous research claim.
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {spawn} from 'node:child_process';
import {mkdtemp,mkdir,readFile,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve,join} from 'node:path';

const root=resolve('.'),home=await mkdtemp(join(tmpdir(),'dsh-vane-profile-'));
const dsh=process.env.DSH_BIN??join(root,'node_modules/.bin/dsh');
const out=resolve('receipts/host');await mkdir(out,{recursive:true});
const env={...process.env,DSH_HOME:home,DSH_VANE_PYTHON:join(root,'.venv/bin/python'),DSH_VANE_WORKSPACES:join(home,'workspaces'),DSH_VANE_FILE_ROOTS:join(root,'examples/materials'),DSH_VANE_HOST_MODEL_KEY:'test-only'};
function run(bin,args){return new Promise((resolveRun,reject)=>{const child=spawn(bin,args,{cwd:root,env,stdio:['ignore','pipe','pipe']});let stdout='',stderr='';const timer=setTimeout(()=>child.kill('SIGKILL'),120000);child.stdout.on('data',c=>stdout+=c);child.stderr.on('data',c=>stderr+=c);child.on('error',reject);child.on('close',code=>{clearTimeout(timer);resolveRun({code,stdout,stderr});});});}
const requests=[];
const server=createServer((req,res)=>{const parts=[];req.on('data',c=>parts.push(c));req.on('end',()=>{
  const body=JSON.parse(Buffer.concat(parts));requests.push(body);
  const toolResults=body.messages.filter(m=>m.role==='tool').map(m=>{try{return JSON.parse(typeof m.content==='string'?m.content:m.content.map(p=>p.text??'').join(''));}catch{return null;}}).filter(Boolean);
  let call;
  const opened=toolResults.find(r=>r.workspace_id&&r.data?.vane_version);
  const active=toolResults.at(-1);
  if(!opened)call={name:'vane_open',arguments:{}};
  else if(active&&['queued','running'].includes(active.status))call={name:'vane_control',arguments:{workspace_id:active.workspace_id,action:'status',operation_id:active.operation_id}};
  else if(!toolResults.some(r=>r.data?.tables?.some(t=>t.preview?.some(row=>row.answer===42))))call={name:'vane_execute',arguments:{workspace_id:opened.workspace_id,mode:'sql',sql:'SELECT 42 AS answer',wait_ms:2000}};
  res.writeHead(200,{'Content-Type':'text/event-stream'});
  const chunk=(delta,finish_reason=null)=>res.write('data: '+JSON.stringify({id:'host-contract',object:'chat.completion.chunk',created:1,model:body.model,choices:[{index:0,delta,finish_reason}]})+'\n\n');
  if(call){chunk({role:'assistant',tool_calls:[{index:0,id:'call-'+requests.length,type:'function',function:{name:call.name,arguments:JSON.stringify(call.arguments)}}]});chunk({},'tool_calls');}
  else{chunk({role:'assistant',content:'Host contract verified: the Vane SQL result is 42.'});chunk({},'stop');}
  res.end('data: [DONE]\n\n');
});});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const receipt={kind:'deterministic-host-contract',home,started_at:new Date().toISOString()};
try{
  const version=await run(dsh,['--version']);assert.equal(version.code,0);receipt.host_version=version.stdout.trim();
  const pack=await run('npm',['pack','--json','--pack-destination',home]);assert.equal(pack.code,0,pack.stderr);
  const packageName=JSON.parse(pack.stdout.slice(pack.stdout.indexOf('[\n')))[0].filename;
  const installed=await run(dsh,['plugin','--profile','headless','add',join(home,packageName)]);
  assert.equal(installed.code,0,installed.stderr);receipt.install_output=installed.stdout;
  const manifest=JSON.parse(await readFile(join(home,'profiles/headless/package.json'),'utf8'));assert.ok(manifest.dsh.profile.bundles.includes('dsh-vane'));
  const patch=[{id:'agent-default-model',config:{provider:'vane-host',model:'vane-host'}},{id:'llm-pi-ai',config:{providers:{'vane-host':{api:'openai-completions',baseURL:`http://127.0.0.1:${server.address().port}/v1`,apiKeyEnv:'DSH_VANE_HOST_MODEL_KEY',models:[{id:'vane-host',name:'Contract test',contextWindow:65536,maxTokens:4096}]}}}}];
  await writeFile(join(home,'profiles/headless/cordis.patch.yml'),JSON.stringify(patch,null,2));
  const dump=await run(dsh,['--profile','headless','--dump-config']);assert.equal(dump.code,0,dump.stderr);assert.match(dump.stdout,/name: dsh-vane/);
  const boot=await run(dsh,['--profile','headless','Verify the Vane plugin contract.']);
  await writeFile(join(out,'cli-output.txt'),boot.stdout+'\n'+boot.stderr);assert.equal(boot.code,0,boot.stderr);
  assert.equal(requests[0].tools.filter(t=>t.function.name.startsWith('vane_')).length,6);
  const actualPrompt=requests[0].messages.filter(m=>m.role==='system').map(m=>m.content).join('\n');
  assert.match(actualPrompt,/You own the research plan/);assert.match(actualPrompt,/Frame the decision/);
  assert.match(boot.stdout,/42/);receipt.status='passed';receipt.tools=requests[0].tools.map(t=>t.function.name).filter(n=>n.startsWith('vane_'));
}catch(error){receipt.status='failed';receipt.reason=error.message;process.exitCode=1;}
finally{receipt.finished_at=new Date().toISOString();await writeFile(join(out,'model-inputs.json'),JSON.stringify(requests,null,2));await writeFile(join(out,'receipt.json'),JSON.stringify(receipt,null,2));await new Promise(r=>server.close(r));}
console.log(JSON.stringify(receipt,null,2));
