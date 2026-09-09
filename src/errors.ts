export class VaneError extends Error {
  constructor(public code: string, message: string, public retryable = false, public details?: unknown) { super(message); }
}
export interface Outcome {
  ok: boolean;
  workspace_id: string;
  operation_id?: string;
  status: string;
  data?: Record<string, any>;
  error?: { code: string; message: string; retryable: boolean; details?: unknown };
}
export function failure(error: unknown, workspace_id = '', operation_id?: string): Outcome {
  const e = error instanceof VaneError ? error : new VaneError('INTERNAL_ERROR', 'Operation failed; inspect local execution records.');
  return { ok: false, workspace_id, ...(operation_id ? { operation_id } : {}), status: e.code === 'CANCELLED' ? 'cancelled' : 'failed',
    error: { code: e.code, message: e.message, retryable: e.retryable, ...(e.details === undefined ? {} : { details: e.details }) } };
}
