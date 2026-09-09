// Structural public API verified against DSH 0.1.2-rc.1 / source 49a606bc.
export type JsonSchema = Record<string, unknown>;
export interface Execution {
  signal: AbortSignal;
  agent?: { id: string; session?: { header?: { cwd?: string } } };
}
export interface ToolDefinition {
  name: string;
  description: string;
  parameters: JsonSchema;
  output: { schema: JsonSchema; render(args: unknown, value: unknown): { type: 'text'; text: string }[] };
  execute(args: unknown, exec: Execution): Promise<unknown>;
  isConcurrencySafe(args: unknown): boolean;
}
export interface HarnessContext {
  tools: { register(tool: ToolDefinition): () => void };
  systemPrompt: { section(section: { name: string; order: number; text: string }): () => void };
  effect(callback: () => (() => Promise<void>)): unknown;
  logger?: { info(message: string): void };
}
