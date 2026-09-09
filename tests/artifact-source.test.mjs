import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile,cp,writeFile} from 'node:fs/promises';
import {resolve,join} from 'node:path';
import {harness} from './helpers.mjs';

test('shared artifacts require live knowledge authorization, marker linkage, scope and SHA',async t=>{
  const golden=resolve('tests/fixtures/golden');
  const receipt=JSON.parse(await readFile(join(golden,'receipt.json'),'utf8'));
  let revoked=false,hasMarker=true;
  const server=createServer((req,res)=>{
    if(revoked){res.writeHead(403);res.end('{}');return;}
    const data=req.url.startsWith('/api/v1/knowledge/')?{success:true,data:{id:'doc',knowledge_base_id:'kb'}}:{success:true,total:1,data:[{id:'c1',chunk_index:0,content:hasMarker?'VANE_ARTIFACT:'+receipt.artifact_id:'ordinary authorized document'}]};
    res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify(data));
  });
  await new Promise((r,j)=>{server.once('error',j);server.listen(0,'127.0.0.1',r);});
  t.after(()=>new Promise(r=>server.close(r)));
  process.env.DSH_VANE_ARTIFACT_TEST_KEY='fixture-key';t.after(()=>delete process.env.DSH_VANE_ARTIFACT_TEST_KEY);
  const h=await harness({weknoraSources:[{alias:'test',baseURL:`http://127.0.0.1:${server.address().port}`,apiKeyEnv:'DSH_VANE_ARTIFACT_TEST_KEY',knowledgeBaseIds:['kb']}],artifactStores:[{alias:'shared',root:golden,scope:'golden-v1'}]});t.after(()=>h.manager.dispose());
  const w=(await h.success('vane_open',{})).workspace_id;
  const source={kind:'artifact',store_alias:'shared',artifact_id:receipt.artifact_id,source_alias:'test',knowledge_id:'doc'};
  const loaded=await h.success('vane_load',{workspace_id:w,source});
  assert.equal(loaded.data.tables.find(t=>t.name.endsWith('.facts')).row_count,2);
  const auto=await h.success('vane_load',{workspace_id:w,source:{kind:'weknora',source_alias:'test',knowledge_id:'doc'}});
  assert.equal(auto.data.artifact_status[0].status,'loaded');
  hasMarker=false;
  assert.equal((await h.finish(await h.call('vane_load',{workspace_id:w,source}))).error.code,'PERMISSION_DENIED');
  const body=await h.success('vane_load',{workspace_id:w,source:{kind:'weknora',source_alias:'test',knowledge_id:'doc'}});
  assert.equal(body.data.artifact_status[0].status,'no_marker');
  revoked=true;
  assert.equal((await h.finish(await h.call('vane_load',{workspace_id:w,source}))).error.code,'PERMISSION_DENIED');
});
