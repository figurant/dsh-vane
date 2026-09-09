#!/usr/bin/env node
// An explicitly configured live read-only check; never mutates an existing KB.
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {resolveConfig,WorkspaceManager,createTools} from '../dist/index.js';
const out=resolve('receipts/weknora');await mkdir(out,{recursive:true});
const required=['DSH_VANE_TEST_WEKNORA_URL','DSH_VANE_TEST_WEKNORA_KEY','DSH_VANE_TEST_WEKNORA_KB','DSH_VANE_TEST_WEKNORA_KNOWLEDGE'];
const missing=required.filter(k=>!process.env[k]);
const receipt={kind:'live-weknora-read',started_at:new Date().toISOString()};
if(missing.length){
  receipt.status='blocked';receipt.missing_environment_variables=missing;
}else{
  const config=resolveConfig({workspaceRoot:resolve('receipts/local/weknora-workspaces'),weknoraSources:[{alias:'test',baseURL:process.env.DSH_VANE_TEST_WEKNORA_URL,apiKeyEnv:'DSH_VANE_TEST_WEKNORA_KEY',knowledgeBaseIds:[process.env.DSH_VANE_TEST_WEKNORA_KB]}]});
  const manager=new WorkspaceManager(config),tools=new Map(createTools(manager).map(t=>[t.name,t]));
  const exec={agent:{id:'dedicated-weknora-verification'},signal:new AbortController().signal};
  try{
    const opened=await tools.get('vane_open').execute({},exec);
    if(!opened.ok)throw new Error(opened.error.message);
    let result=await tools.get('vane_load').execute({workspace_id:opened.workspace_id,source:{kind:'weknora',source_alias:'test',knowledge_id:process.env.DSH_VANE_TEST_WEKNORA_KNOWLEDGE}},exec);
    while(['queued','running'].includes(result.status)){await new Promise(r=>setTimeout(r,100));result=await tools.get('vane_control').execute({workspace_id:opened.workspace_id,action:'status',operation_id:result.operation_id},exec);}
    receipt.status=result.ok?'passed':'failed';receipt.result=result;
  }catch(e){receipt.status='failed';receipt.reason=e.message;}finally{await manager.dispose();}
}
receipt.finished_at=new Date().toISOString();await writeFile(out+'/receipt.json',JSON.stringify(receipt,null,2));console.log(JSON.stringify(receipt,null,2));
if(receipt.status!=='passed')process.exitCode=2;
