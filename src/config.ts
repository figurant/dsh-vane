import { readFileSync } from 'node:fs';
import { resolve, join, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';
import { homedir } from 'node:os';
import { VaneError } from './errors.js';

export const ROOT = fileURLToPath(new URL('../', import.meta.url));
export const HOST_VERSION = '0.1.2-rc.1';
export interface PackageConfig { id: string; version: string; digest: string; path: string }
export interface StoreConfig { alias: string; root: string; scope: string }
export interface WeKnoraConfig { alias: string; baseURL: string; apiKeyEnv: string; knowledgeBaseIds: string[] }
export interface DatabaseConfig { alias: string; kind: 'postgresql'; dsnEnv: string }
export interface ModelConfig { alias: string; baseURL: string; model: string; apiKeyEnv?: string; temperature?: number; maxTokens?: number; revision?: string }
export interface Config {
  pythonExecutable: string; runtimeModule: string; workspaceRoot: string; allowedFileRoots: string[];
  enabledPackages: PackageConfig[]; artifactStores: StoreConfig[]; weknoraSources: WeKnoraConfig[];
  databases: DatabaseConfig[]; models: ModelConfig[]; hostVersion: string;
  limits: { maxWorkspaces: number; maxQueue: number; idleMs: number; operationTimeoutMs: number; cancelGraceMs: number;
    waitMs: number; maxWaitMs: number; maxFrameBytes: number; maxResponseBytes: number; maxFileBytes: number;
    maxRows: number; maxMemoryMB: number; maxDiskBytes: number; httpTimeoutMs: number; maxChunks: number };
}
export function resolveConfig(input: unknown = {}): Config {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new VaneError('CONFIG_ERROR', 'Configuration must be an object.');
  const raw = input as Partial<Config>;
  const defaults: Config = {
    pythonExecutable: join(ROOT, '.venv/bin/python'), runtimeModule: 'dsh_vane_runtime',
    workspaceRoot: join(homedir(), '.local/share/dsh-vane/workspaces'), allowedFileRoots: [process.cwd()],
    enabledPackages: [{ id: 'document_observations', version: '1.0.0', path: join(ROOT, 'packages/document_observations'),
      digest: readFileSync(join(ROOT, 'packages/document_observations/digest.sha256'), 'utf8').trim() }],
    artifactStores: [], weknoraSources: [], databases: [], models: [], hostVersion: HOST_VERSION,
    limits: { maxWorkspaces: 4, maxQueue: 32, idleMs: 1800000, operationTimeoutMs: 120000, cancelGraceMs: 1500,
      waitMs: 150, maxWaitMs: 2000, maxFrameBytes: 1048576, maxResponseBytes: 65536, maxFileBytes: 104857600,
      maxRows: 100000, maxMemoryMB: 1024, maxDiskBytes: 1073741824, httpTimeoutMs: 30000, maxChunks: 10000 }
  };
  for (const k of Object.keys(raw)) if (!(k in defaults)) throw new VaneError('CONFIG_ERROR', `Unknown configuration field: ${k}`);
  const c = { ...defaults, ...Object.fromEntries(Object.entries(raw).filter(([,v]) => v !== undefined)), limits: { ...defaults.limits, ...raw.limits } } as Config;
  if (c.hostVersion !== HOST_VERSION) throw new VaneError('HOST_VERSION_UNSUPPORTED', `Supported DSH version is ${HOST_VERSION}; verify a new version before enabling it.`);
  for (const k of ['pythonExecutable', 'runtimeModule', 'workspaceRoot'] as const) if (typeof c[k] !== 'string' || !c[k]) throw new VaneError('CONFIG_ERROR', `${k} must be a nonempty string.`);
  c.pythonExecutable = resolve(c.pythonExecutable); c.workspaceRoot = resolve(c.workspaceRoot);
  for (const [k, v] of Object.entries(c.limits)) if (!(k in defaults.limits) || !Number.isSafeInteger(v) || v < 1) throw new VaneError('CONFIG_ERROR', `Invalid limit ${k}`);
  if (c.limits.maxResponseBytes < 2048 || c.limits.maxResponseBytes >= c.limits.maxFrameBytes || c.limits.waitMs > c.limits.maxWaitMs || c.limits.maxWaitMs > 5000) throw new VaneError('CONFIG_ERROR', 'Response/frame/wait limits are inconsistent (maximum wait 5000 ms).');
  if (!Array.isArray(c.allowedFileRoots) || c.allowedFileRoots.some(p => typeof p !== 'string' || !isAbsolute(p))) throw new VaneError('CONFIG_ERROR', 'allowedFileRoots must contain absolute paths.');
  const lists = [c.enabledPackages, c.artifactStores, c.weknoraSources, c.databases, c.models];
  for (const entries of lists) {
    if (!Array.isArray(entries)) throw new VaneError('CONFIG_ERROR', 'Sources, packages and models must be arrays.');
    const names = entries.map(e => 'alias' in e ? e.alias : e.id);
    if (new Set(names).size !== names.length || names.some(n => typeof n !== 'string' || !/^[a-zA-Z][\w-]*$/.test(n))) throw new VaneError('CONFIG_ERROR', 'Aliases/IDs must be unique identifiers.');
  }
  for (const p of c.enabledPackages) if (!isAbsolute(p.path) || !p.version || !/^[a-f0-9]{64}$/.test(p.digest)) throw new VaneError('CONFIG_ERROR', 'Packages require absolute path, version and SHA-256 digest.');
  for (const s of c.artifactStores) if (!isAbsolute(s.root) || !s.scope) throw new VaneError('CONFIG_ERROR', 'Artifact stores require absolute root and scope.');
  for (const s of [...c.weknoraSources, ...c.models]) {
    let url: URL; try { url = new URL(s.baseURL); } catch { throw new VaneError('CONFIG_ERROR', 'Invalid source/model URL.'); }
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new VaneError('CONFIG_ERROR', 'Use HTTP(S) base URLs without embedded credentials/query.');
  }
  for (const s of c.weknoraSources) if (!s.apiKeyEnv || !Array.isArray(s.knowledgeBaseIds) || s.knowledgeBaseIds.length === 0) throw new VaneError('CONFIG_ERROR', 'WeKnora requires a key environment reference and explicit knowledge base allowlist.');
  for (const s of c.databases) if (s.kind !== 'postgresql' || !s.dsnEnv) throw new VaneError('CONFIG_ERROR', 'Only configured PostgreSQL sources are implemented.');
  return c;
}
