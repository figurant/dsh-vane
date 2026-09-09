import { Ajv } from 'ajv';
import type { JsonSchema, ToolDefinition } from './harness.js';
import { failure, VaneError } from './errors.js';
import type { WorkspaceManager } from './workspace-manager.js';

const str = { type: 'string' }, integer = { type: 'integer' }, strings = { type: 'array', items: str };
const obj = (properties: Record<string, unknown>, required = Object.keys(properties)): JsonSchema => ({ type: 'object', properties, required, additionalProperties: false });
const literal = (value: string) => ({ type: 'string', const: value });
const source = { oneOf: [
  obj({ kind: literal('file'), path: str }),
  obj({ kind: literal('weknora'), source_alias: str, knowledge_id: str, chunk_ids: strings }, ['kind', 'source_alias', 'knowledge_id']),
  obj({ kind: literal('artifact'), store_alias: str, artifact_id: str, knowledge_id: str, source_alias: str }, ['kind', 'store_alias', 'artifact_id']),
  obj({ kind: literal('database'), source_alias: str, query: str, bindings: { type: 'array', items: {} } }, ['kind', 'source_alias', 'query'])
] };
// DSH requires an object root and supports oneOf, but no numeric bounds/if/then.
// Numeric limits and exact mode/action fields are also checked below and in the runtime.
const executeFields = { workspace_id: str, mode: { type: 'string', enum: ['sql','python','pipeline'] },
  sql: str, bindings: { type: 'array', items: {} }, code: str, package_id: str, pipeline: str,
  asset_ids: strings, params: { type: 'object', additionalProperties: true }, wait_ms: integer };
export const schemas: Record<string, JsonSchema> = {
  vane_open: obj({ new_task: { type: 'boolean' }, package_ids: strings }, []),
  vane_load: obj({ workspace_id: str, source, table_name: str }, ['workspace_id','source']),
  vane_describe: obj({ workspace_id: str, target: { type: 'string', enum: ['workspace','table','functions'] }, name: str }, ['workspace_id','target']),
  vane_execute: obj(executeFields, ['workspace_id','mode']),
  vane_read: obj({ workspace_id: str, result_id: str, offset: integer, limit: integer }, ['workspace_id','result_id']),
  vane_control: obj({ workspace_id: str, action: { type: 'string', enum: ['status','cancel','checkpoint','restore','close'] }, operation_id: str, checkpoint_id: str, tables: strings }, ['workspace_id','action'])
};
export const outputSchema = obj({ ok: { type: 'boolean' }, workspace_id: str, operation_id: str, status: str,
  data: { type: 'object', additionalProperties: true }, error: obj({ code: str, message: str, retryable: { type: 'boolean' }, details: {} }, ['code','message','retryable']) }, ['ok','workspace_id','status']);
const descriptions: Record<string,string> = {
  vane_open: 'Open/reuse the current task workspace. new_task creates an isolated task; IDs are owned by this session.',
  vane_load: 'Load an allowed local file, complete WeKnora chunks, verified artifact, or read-only PostgreSQL query. PDF/images become assets for a selected package/UDF.',
  vane_describe: 'Inspect workspace/table/function catalogs with bounded samples, schemas and provenance. No implicit expensive full-table statistics.',
  vane_execute: 'Compute in a persistent Vane connection. sql needs sql and optional bindings; python needs code with ctx.connection/register_udf/publish; pipeline needs package_id/pipeline/asset_ids and optional params. Only fields for the selected mode are accepted. wait_ms: 0..configured maximum. Long operations return an operation_id.',
  vane_read: 'Read a materialized result, evidence or execution record. offset >= 0; limit defaults to 20, maximum 100. Oversized cells are referenced by the full local result file.',
  vane_control: 'status/cancel require operation_id; checkpoint requires explicit tables; restore requires checkpoint_id and creates a new workspace; close retires this task. Other action-specific fields are rejected.'
};
export function createTools(manager: WorkspaceManager): ToolDefinition[] {
  const ajv = new Ajv({ strict: false, allErrors: true });
  return Object.entries(schemas).map(([name, parameters]) => {
    const validate = ajv.compile(parameters);
    return { name, description: descriptions[name]!, parameters,
      output: { schema: outputSchema, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
      isConcurrencySafe: () => true,
      async execute(args: unknown, exec) {
        let a: Record<string, any> = {};
        try {
          if (!validate(args)) throw new VaneError('INVALID_ARGUMENT', 'Arguments do not match the tool schema.', false, validate.errors?.map(e => ({ path: e.instancePath, message: e.message })));
          a = args as Record<string, any>;
          if (!exec?.agent?.id || typeof exec.agent.id !== 'string') throw new VaneError('HOST_CONTEXT_UNAVAILABLE', 'A trusted exec.agent.id is required.');
          if (!exec.signal || exec.signal.aborted) throw new VaneError('CANCELLED', 'Tool call cancelled.');
          if (name === 'vane_execute') {
            const fields: Record<string,string[]> = { sql: ['sql','bindings'], python: ['code'], pipeline: ['package_id','pipeline','asset_ids','params'] };
            const allowed = ['workspace_id','mode','wait_ms',...fields[a.mode]!];
            const required = a.mode === 'sql' ? ['sql'] : a.mode === 'python' ? ['code'] : ['package_id','pipeline','asset_ids'];
            if (Object.keys(a).some(k => !allowed.includes(k)) || required.some(k => a[k] === undefined || a[k] === '')) throw new VaneError('INVALID_ARGUMENT', 'Missing or inapplicable execute mode fields.');
          }
          if (name === 'vane_control') {
            const fields: Record<string,string[]> = { status: ['operation_id'], cancel: ['operation_id'], checkpoint: ['tables'], restore: ['checkpoint_id'], close: [] };
            if (Object.keys(a).some(k => !['workspace_id','action',...fields[a.action]!].includes(k)) || fields[a.action]!.some(k => a[k] === undefined)) throw new VaneError('INVALID_ARGUMENT', 'Missing or inapplicable control action fields.');
          }
          if (a.wait_ms !== undefined && (a.wait_ms < 0 || a.wait_ms > manager.config.limits.maxWaitMs)) throw new VaneError('INVALID_ARGUMENT', 'wait_ms is out of range.');
          if ((a.offset !== undefined && a.offset < 0) || (a.limit !== undefined && (a.limit < 1 || a.limit > 100))) throw new VaneError('INVALID_ARGUMENT', 'Invalid pagination bounds.');
          if (name === 'vane_describe' && ((a.target === 'table') !== (a.name !== undefined))) throw new VaneError('INVALID_ARGUMENT', 'Only target=table requires name.');
          return await manager.call(name, a, exec);
        } catch (e) { return failure(e, typeof a.workspace_id === 'string' ? a.workspace_id : ''); }
      }
    };
  });
}
