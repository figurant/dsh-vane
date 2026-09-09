import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { WeKnoraClient } from '../dist/sources/weknora.js';
import { resolveConfig } from '../dist/config.js';

async function server(t, responder) {
  const requests=[];
  const s=createServer((req,res)=>{
    requests.push({url:req.url,key:req.headers['x-api-key']});
    const [status,data]=responder(new URL(req.url,'http://localhost'));
    res.writeHead(status,{'Content-Type':'application/json'});res.end(typeof data==='string'?data:JSON.stringify(data));
  });
  await new Promise(r=>s.listen(0,'127.0.0.1',r));
  t.after(()=>new Promise(r=>s.close(r)));
  process.env.DSH_VANE_TEST_KEY='fixture-not-a-secret';
  t.after(()=>delete process.env.DSH_VANE_TEST_KEY);
  const config=resolveConfig({weknoraSources:[{alias:'test',baseURL:`http://127.0.0.1:${s.address().port}`,apiKeyEnv:'DSH_VANE_TEST_KEY',knowledgeBaseIds:['kb']}],enabledPackages:[]});
  return {client:new WeKnoraClient(config),requests};
}
const signal=()=>new AbortController().signal;
const metadata={success:true,data:{id:'doc',knowledge_base_id:'kb',description:'SUMMARY ONLY',title:'Operational record'}};

test('WeKnora reads real body pages, retains evidence, marks selected ranges and parses artifact markers', async t=>{
  const {client,requests}=await server(t,url=>url.pathname.startsWith('/api/v1/knowledge/')?[200,metadata]:[200,{success:true,total:3,page:Number(url.searchParams.get('page')),data:Number(url.searchParams.get('page'))===1?[{id:'c1',chunk_index:0,content:'body one'},{id:'c2',chunk_index:1,content:'body two'}]:[{id:'c3',chunk_index:2,content:'VANE_ARTIFACT:12345678-1234-1234-1234-123456789012'}]}]);
  const all=await client.read('test','doc',undefined,signal());
  assert.equal(all.chunks.length,3);assert.equal(all.provenance.complete,true);
  assert.equal(all.chunks.some(c=>c.content==='SUMMARY ONLY'),false);
  assert.equal(all.artifact_ids.length,1);assert.ok(requests.every(r=>r.key==='fixture-not-a-secret'));
  const selected=await client.read('test','doc',['c2'],signal());
  assert.deepEqual(selected.chunks.map(c=>c.chunk_id),['c2']);assert.equal(selected.provenance.complete,false);
  await assert.rejects(client.read('test','doc',['missing'],signal()),e=>e.code==='CHUNKS_NOT_FOUND');
});

test('WeKnora permission revocation is checked on every load and errors are not successful empty tables', async t=>{
  let revoked=false;
  const {client}=await server(t,url=>revoked?[403,{message:'no access'}]:url.pathname.startsWith('/api/v1/knowledge/')?[200,metadata]:[200,{success:true,data:[],total:0}]);
  assert.equal((await client.read('test','doc',undefined,signal())).provenance.complete,true);
  revoked=true;
  await assert.rejects(client.read('test','doc',undefined,signal()),e=>e.code==='PERMISSION_DENIED');
});

test('WeKnora malformed responses and incomplete/repeated pagination are distinguished',async t=>{
  const {client}=await server(t,url=>url.pathname.startsWith('/api/v1/knowledge/')?[200,metadata]:[200,{success:true,total:5,data:[{id:'c',content:'partial',chunk_index:0}]}]);
  const value=await client.read('test','doc',undefined,signal());
  assert.equal(value.provenance.complete,false);assert.equal(value.chunks.length,1);assert.ok(value.warnings.length);
});

test('WeKnora abort and invalid envelope fail explicitly',async t=>{
  const {client}=await server(t,()=>[200,{success:false,data:[]}]);
  await assert.rejects(client.read('test','doc',undefined,signal()),e=>e.code==='SOURCE_RESPONSE_INVALID');
  const abort=new AbortController();abort.abort();
  await assert.rejects(client.read('test','doc',undefined,abort.signal),e=>e.code==='CANCELLED');
});
