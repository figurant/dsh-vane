import test from 'node:test';
import assert from 'node:assert/strict';
import {harness} from './helpers.mjs';

test('malformed/oversized fd3 frames fail pending requests and retire the instance',async t=>{
  const h=await harness({enabledPackages:[]});t.after(()=>h.manager.dispose());
  for(const code of ["import os\nos.write(3,b'not-json\\n')","import os\nos.write(3,b'x'*1100000)"]){
    const w=(await h.success('vane_open',{})).workspace_id;
    const result=await h.finish(await h.call('vane_execute',{workspace_id:w,mode:'python',code}));
    assert.ok(['IPC_PROTOCOL_ERROR','IPC_FRAME_LIMIT'].includes(result.error.code),JSON.stringify(result));
    assert.equal((await h.call('vane_describe',{workspace_id:w,target:'workspace'})).error.code,'PROCESS_LOST');
  }
});

test('oversized requests fail before execution; the healthy workspace remains usable',async t=>{
  const h=await harness({enabledPackages:[],limits:{maxFrameBytes:70000,maxResponseBytes:65536}});t.after(()=>h.manager.dispose());
  const w=(await h.success('vane_open',{})).workspace_id;
  const tooBig=await h.finish(await h.call('vane_execute',{workspace_id:w,mode:'python',code:'#'+'x'.repeat(71000)}));
  assert.equal(tooBig.error.code,'IPC_FRAME_LIMIT');
  const healthy=await h.success('vane_execute',{workspace_id:w,mode:'sql',sql:'SELECT 1 AS n'});
  assert.equal(healthy.data.tables[0].preview[0].n,1);
});

test('open deduplicates a session and reserves global capacity before awaits',async t=>{
  const h=await harness({enabledPackages:[],limits:{maxWorkspaces:1}});t.after(()=>h.manager.dispose());
  const [a,b,c]=await Promise.all([h.call('vane_open'),h.call('vane_open'),h.call('vane_open',{},'different-session')]);
  assert.equal(a.ok,true);assert.equal(a.workspace_id,b.workspace_id);assert.equal(c.error.code,'WORKSPACE_LIMIT');
});

test('unloading during workspace creation does not leave a live runtime',async()=>{
  const h=await harness({enabledPackages:[]});
  const opening=h.call('vane_open');
  await h.manager.dispose();
  const result=await opening;
  assert.equal(result.ok,false);
  assert.ok(['PLUGIN_CLOSED','PROCESS_LOST'].includes(result.error.code),JSON.stringify(result));
  assert.equal((await h.call('vane_open')).error.code,'PLUGIN_CLOSED');
});
