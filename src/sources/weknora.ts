import type { Config, WeKnoraConfig } from '../config.js';
import { VaneError } from '../errors.js';

export class WeKnoraClient {
  constructor(private config: Config) {}
  source(alias: string): WeKnoraConfig {
    const source = this.config.weknoraSources.find(s => s.alias === alias);
    if (!source) throw new VaneError('SOURCE_NOT_FOUND', 'Unknown WeKnora source alias.');
    return source;
  }
  private async get(source: WeKnoraConfig, route: string, signal: AbortSignal): Promise<any> {
    const key = process.env[source.apiKeyEnv];
    if (!key) throw new VaneError('SOURCE_CONFIG_ERROR', 'The configured WeKnora credential environment variable is missing.');
    const base = source.baseURL.replace(/\/$/, '').replace(/\/api\/v1$/, '') + '/api/v1';
    let response: Response;
    try { response = await fetch(base + route, { headers: { 'X-API-Key': key, Accept: 'application/json' }, signal: AbortSignal.any([signal, AbortSignal.timeout(this.config.limits.httpTimeoutMs)]), redirect: 'error' }); }
    catch { throw new VaneError(signal.aborted ? 'CANCELLED' : 'SOURCE_UNAVAILABLE', 'WeKnora request failed or timed out.', !signal.aborted); }
    if (!response.ok) { await response.body?.cancel(); throw new VaneError([401,403].includes(response.status) ? 'PERMISSION_DENIED' : 'SOURCE_HTTP_ERROR', `WeKnora returned HTTP ${response.status}.`, response.status >= 500); }
    const reader = response.body?.getReader();
    if (!reader) throw new VaneError('SOURCE_RESPONSE_INVALID', 'WeKnora returned no response body.');
    let size = 0; const chunks: Uint8Array[] = [];
    for (;;) { const { done, value } = await reader.read(); if (done) break; size += value.length;
      if (size > this.config.limits.maxFileBytes) { await reader.cancel(); throw new VaneError('LIMIT_EXCEEDED', 'WeKnora response exceeds the byte limit.'); } chunks.push(value); }
    let envelope: any; try { envelope = JSON.parse(Buffer.concat(chunks).toString()); } catch { throw new VaneError('SOURCE_RESPONSE_INVALID', 'WeKnora returned invalid JSON.'); }
    if (envelope.success === false || envelope.data === undefined) throw new VaneError('SOURCE_RESPONSE_INVALID', 'WeKnora reported failure or omitted data.');
    return envelope;
  }
  async authorize(alias: string, knowledgeId: string, signal: AbortSignal) {
    const s = this.source(alias);
    const { data } = await this.get(s, `/knowledge/${encodeURIComponent(knowledgeId)}`, signal);
    if (data.id !== knowledgeId || typeof data.knowledge_base_id !== 'string' || !s.knowledgeBaseIds.includes(data.knowledge_base_id)) throw new VaneError('PERMISSION_DENIED', 'Knowledge is outside the configured allowlist or returned the wrong identity.');
    return data;
  }
  async read(alias: string, knowledgeId: string, chunkIds: string[] | undefined, signal: AbortSignal) {
    const document = await this.authorize(alias, knowledgeId, signal), s = this.source(alias);
    const chunks = new Map<string, any>();
    const requested = chunkIds === undefined ? undefined : new Set(chunkIds);
    let page = 1, seen = 0, total: number | null = null, complete = false, bytes = 0;
    const warnings: string[] = [];
    for (; seen < this.config.limits.maxChunks; page++) {
      const envelope = await this.get(s, `/chunks/${encodeURIComponent(knowledgeId)}?page=${page}&page_size=100`, signal);
      if (!Array.isArray(envelope.data) || (envelope.page !== undefined && envelope.page !== page)) throw new VaneError('SOURCE_RESPONSE_INVALID', 'Invalid chunk page.');
      if (Number.isSafeInteger(envelope.total) && envelope.total >= 0) {
        if (total !== null && total !== envelope.total) warnings.push('Chunk total changed during pagination; completeness is not guaranteed.');
        total = envelope.total;
      }
      let added = 0;
      for (const c of envelope.data) {
        if (typeof c.id !== 'string' || typeof c.content !== 'string' || (c.knowledge_id && c.knowledge_id !== knowledgeId)) throw new VaneError('SOURCE_RESPONSE_INVALID', 'Invalid chunk identity/content.');
        if (!chunks.has(c.id)) { chunks.set(c.id, { chunk_id: c.id, knowledge_id: knowledgeId, chunk_index: c.chunk_index ?? null, content: c.content }); added++; bytes += Buffer.byteLength(c.content); }
      }
      seen += envelope.data.length;
      if (chunks.size > this.config.limits.maxChunks || bytes > this.config.limits.maxFileBytes) throw new VaneError('LIMIT_EXCEEDED', 'WeKnora evidence exceeds configured limits.');
      if (requested && [...requested].every(id => chunks.has(id))) break;
      if (envelope.data.length === 0 || (total !== null && seen >= total)) { complete = total === null ? envelope.data.length === 0 : chunks.size === total; break; }
      if (added === 0) { warnings.push('Pagination repeated chunks; fetch stopped.'); break; }
    }
    const selected = [...chunks.values()].filter(c => !requested || requested.has(c.chunk_id)).sort((a,b) => (a.chunk_index ?? 0) - (b.chunk_index ?? 0));
    const missing = requested ? [...requested].filter(id => !chunks.has(id)) : [];
    if (missing.length) throw new VaneError('CHUNKS_NOT_FOUND', 'Requested chunks were not all readable.', false, { missing });
    if (!requested && !complete) warnings.push('Only a partial set of readable chunks was fetched.');
    const artifactIds = [...new Set(selected.flatMap(c => [...c.content.matchAll(/\bVANE_ARTIFACT:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b/gi)].map(m => m[1])))];
    return { document: { knowledge_id: knowledgeId, title: document.title ?? document.file_name ?? knowledgeId, description: document.description ?? '' }, chunks: selected,
      provenance: { kind: 'weknora', source_alias: alias, knowledge_id: knowledgeId, complete: !requested && complete && warnings.length === 0, scope: requested ? 'selected_chunks' : 'all_readable_chunks', chunk_ids: selected.map(c => c.chunk_id), total_chunks: total, fetched_pages: page },
      artifact_ids: artifactIds, warnings };
  }
}
