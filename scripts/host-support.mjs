import { Context } from '@deepseek-ai/cordis';
import LlmRuntime, { LlmAdapter } from '@deepseek-ai/dsh-llm';
import SessionStore from '@deepseek-ai/dsh-session';
import SessionProjectionRegistry from '@deepseek-ai/dsh-session-projection';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import ToolRuntime from '@deepseek-ai/dsh-tools';
import AgentRegistry from '@deepseek-ai/dsh-agent';
import AgentLoop from '@deepseek-ai/dsh-agent-loop';
import * as plugin from '../dist/index.js';
import { randomUUID } from 'node:crypto';

export async function boot(config, adapter) {
  const ctx = new Context();
  for (const service of [LlmRuntime, SessionStore, SessionProjectionRegistry, SystemPrompt, ToolRuntime, AgentRegistry]) await ctx.plugin(service);
  await ctx.plugin(AgentLoop,{agents:[]});
  await ctx.plugin(plugin,config);
  ctx.llm.registerAdapter(['vane-research'],adapter);
  return ctx;
}

export function* blocks(message) {
  let index=0;
  if (message.content) {
    yield {type:'block-start',index,blockType:'text'};
    yield {type:'text-delta',index,text:message.content};
    yield {type:'block-end',index,block:{type:'text',text:message.content}};
    index++;
  }
  for (const call of message.tool_calls??[]) {
    const id=call.id??randomUUID(),name=call.function.name,args=call.function.arguments;
    yield {type:'block-start',index,blockType:'tool-call'};
    yield {type:'tool-call-delta',index,id,name,argumentsDelta:args};
    yield {type:'block-end',index,block:{type:'tool-call',id,name,arguments:args}};
    index++;
  }
  yield {type:'finish',reason:{kind:message.tool_calls?.length?'tool-calls':'stop'}};
}

/** Non-streaming local-model adapter. It translates wire syntax only, never plans tools. */
export class LocalModelAdapter extends LlmAdapter {
  requests=[];
  constructor({baseURL,model,maxSteps=32,apiKeyEnv}) { super();Object.assign(this,{baseURL,model,maxSteps,apiKeyEnv}); }
  async resolveModel(provider,model) { return {provider,id:model,name:model,context:{contextWindow:32768},defaultMaxTokens:1800,inputModalities:['text']}; }
  async *stream(options) {
    if (this.requests.length>=this.maxSteps) throw new Error('Research step budget exhausted; experiment incomplete.');
    const toolGuide='\n你正在实际执行分析，工具会运行并返回真实结果。每次只选择下一步动作；调用工具时只输出 <tool_call>{"name":"工具名","arguments":{参数}}</tool_call>，然后停下等待结果。不要用代码块介绍计划，不要用占位符或伪造ID。未知ID必须从工具结果获取。@文件是路径提及标记，传给工具的path不带@。完成后用普通文本写完整结论。可用工具如下：\n<tools>\n'+JSON.stringify(options.tools??[])+'\n</tools>';
    const messages=[{role:'system',content:(options.system??'')+toolGuide}];
    for (const message of options.messages) {
      let content='';
      for (const b of message.content) {
        if (b.type==='text') content+=b.text;
        else if (b.type==='tool-call') content+='\n<tool_call>'+JSON.stringify({name:b.name,arguments:JSON.parse(b.arguments)})+'</tool_call>';
        else if (b.type==='tool-result') content+='\n<tool_response>'+b.content.map(c=>c.text??'').join('\n')+'</tool_response>';
      }
      messages.push({role:message.role,content});
    }
    // The local demo endpoint concatenates content and discards role fields.
    // Preserve boundaries as text in the adapter; do not alter the service or host.
    const transcript=messages.map(m=>'['+m.role.toUpperCase()+']\n'+m.content+'\n[/'+m.role.toUpperCase()+']').join('\n\n');
    const body={model:this.model,messages:[{role:'user',content:transcript+'\n\n请继续上面会话中 ASSISTANT 的下一步，只执行下一步或给出已经完成的结论。\n[ASSISTANT]'}],stream:false,temperature:0,max_tokens:options.maxTokens??1000};
    const headers={'Content-Type':'application/json','X-Request-Id':randomUUID()};
    if (this.apiKeyEnv) { const key=process.env[this.apiKeyEnv];if(!key)throw new Error('Missing configured research credential');headers.Authorization='Bearer '+key; }
    const entry={step:this.requests.length+1,started_at:new Date().toISOString(),request:body};this.requests.push(entry);
    console.log(`model step ${entry.step}`);
    const response=await fetch(this.baseURL.replace(/\/$/,'')+'/chat/completions',{method:'POST',headers,body:JSON.stringify(body),signal:AbortSignal.any([options.signal??new AbortController().signal,AbortSignal.timeout(180000)])});
    if(!response.ok)throw new Error('Model returned HTTP '+response.status);
    const raw=await response.json();entry.response=raw;entry.finished_at=new Date().toISOString();
    const message=raw.choices?.[0]?.message;
    if(!message)throw new Error('Model response had no message');
    const parsed={content:message.content??'',tool_calls:message.tool_calls??[]};
    if(!parsed.tool_calls.length){
      const matches=[...parsed.content.matchAll(/<tool_call>\s*([\s\S]*?)\s*<\/tool_call>/g)];
      for(const match of matches){
        const call=JSON.parse(match[1]);
        if(typeof call.name!=='string'||!call.arguments||typeof call.arguments!=='object')throw new Error('Invalid model-authored tool call');
        parsed.tool_calls.push({id:randomUUID(),type:'function',function:{name:call.name,arguments:JSON.stringify(call.arguments)}});
      }
      if(matches.length)parsed.content=parsed.content.replace(/<tool_call>[\s\S]*?<\/tool_call>/g,'').trim();
    }
    if(raw.choices[0].finish_reason==='length'&&!parsed.tool_calls.length){
      yield* [...blocks(parsed)].filter(c=>c.type!=='finish');
      yield {type:'finish',reason:{kind:'max-tokens'}};
    }else yield* blocks(parsed);
  }
}
