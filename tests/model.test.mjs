import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {copyFile,readdir} from 'node:fs/promises';
import {resolve,join,dirname} from 'node:path';
import {harness} from './helpers.mjs';

test('vision model substitute validates extraction, prompt versions and failures independently of real research',async t=>{
  const requests=[];let invalid=false;
  const server=createServer((req,res)=>{const chunks=[];req.on('data',c=>chunks.push(c));req.on('end',()=>{
    const body=JSON.parse(Buffer.concat(chunks));requests.push(body);
    const revised=body.messages[0].content[0].text.includes('Recheck');
    const content=invalid?'not JSON':JSON.stringify({observations:[{item:'A',value:revised?16:15,category:null,quote:'A,15',bbox:[0.1,0.1,0.5,0.5]}]});
    res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({choices:[{message:{content}}]}));
  });});
  await new Promise((r,j)=>{server.once('error',j);server.listen(0,'127.0.0.1',r);});
  t.after(()=>new Promise(r=>server.close(r)));
  const h=await harness({models:[{alias:'vision-test',baseURL:`http://127.0.0.1:${server.address().port}/v1`,model:'regression-substitute',temperature:0}]});t.after(()=>h.manager.dispose());
  const file=join(h.root,'sample.png');await copyFile(resolve('examples/materials/new-report.png'),file);
  const w=(await h.success('vane_open',{})).workspace_id;
  const loaded=await h.success('vane_load',{workspace_id:w,source:{kind:'file',path:file}});
  const args={workspace_id:w,mode:'pipeline',package_id:'document_observations',pipeline:'ingest',asset_ids:[loaded.data.assets[0].asset_id],params:{model_alias:'vision-test'}};
  const first=await h.success('vane_execute',args);
  assert.equal(first.data.tables.find(t=>t.name.endsWith('.facts')).preview[0].value,15);
  const evidence=first.data.tables.find(t=>t.name.endsWith('.evidence')).preview[0];
  assert.deepEqual(JSON.parse(evidence.locator_json).bbox,[0.1,0.1,0.5,0.5]);
  assert.equal(requests[0].stream,false);assert.match(requests[0].messages[0].content[1].image_url.url,/^data:image\/png;base64,/);
  const second=await h.success('vane_execute',{...args,params:{...args.params,prompt:'Recheck the observation'}});
  assert.notEqual(first.data.artifacts[0].artifact_id,second.data.artifacts[0].artifact_id);
  assert.equal(second.data.tables.find(t=>t.name.endsWith('.facts')).preview[0].value,16);
  invalid=true;
  const failed=await h.finish(await h.call('vane_execute',{...args,params:{...args.params,prompt:'invalid response test'}}));
  assert.equal(failed.ok,false);assert.equal(failed.error.code,'MODEL_ERROR');
  const artifactRoot=dirname(dirname(first.data.artifacts[0].manifest));
  assert.equal((await readdir(artifactRoot)).filter(n=>!n.startsWith('.')).length,2);
});
