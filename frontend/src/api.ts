/**
 * Typed client for the DPC API. Same-origin only: the console is served by the process it
 * calls, and the vite dev server proxies /api to :8300.
 *
 * Everything non-2xx throws `ApiError`:
 *   err.status    HTTP status
 *   err.detail    parsed body detail, when the service sent one
 *   err.needsOcr  true for the 422 "needs_ocr" refusal — the service declining to guess at a
 *                 scan with no recogniser configured. Render it calm, not red.
 *
 * Nothing here logs: request and response bodies carry document text.
 */

const API = '/api/v1';

/* ---------------------------------------------------------------- types */

/** Kinds the paste-JSON tab accepts, mapped to their request-body field. */
export const JSON_KINDS = [
  ['azure_read', 'azure_read_result', 'Azure Read v3.2'],
  ['azure_layout', 'azure_analyze_result', 'Azure DI analyze (layout/read v4)'],
  ['des_ocr', 'des_ocr', 'DES OCR page payload'],
] as const;

export type JsonKind = (typeof JSON_KINDS)[number][0];

export interface ConvertResponse {
  id: string;
  doc_id: string;
  source: string;
  provider: string;
  pages: number;
  blocks: number;
  tables: number;
  marks: number;
  key_values: number;
  chars: number;
  sha256_markdown: string;
  s3_bucket: string;
  s3_key: string;
  ms: number;
  /** Present only when the request set echo=true. */
  markdown?: string;
}

/** One row of the conversions ledger (subset the console renders). */
export interface ConversionRow {
  id: string;
  doc_id: string | null;
  source: string;
  provider: string;
  filename?: string | null;
  pages: number | null;
  blocks?: number | null;
  tables_n?: number | null;
  chars: number | null;
  status: string;
  error?: string | null;
  ms?: number | null;
  created_at: string;
}

export interface HealthResponse {
  status: string;
  service: string;
  version: string;
}

/* ---------------------------------------------------------------- errors */

export class ApiError extends Error {
  status: number;
  detail: unknown;
  needsOcr: boolean;

  constructor(status: number, message: string, detail: unknown, needsOcr: boolean) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.needsOcr = needsOcr;
  }
}

function messageFrom(status: number, body: unknown): [string, unknown, boolean] {
  if (body && typeof body === 'object') {
    const b = body as Record<string, unknown>;
    const needsOcr = status === 422 && b.error === 'needs_ocr';
    const detail = b.detail ?? b.error ?? b.message;
    if (typeof detail === 'string' && detail) return [detail, b.detail ?? b, needsOcr];
    if (detail !== undefined) return [`request failed (HTTP ${status})`, detail, needsOcr];
  }
  return [`request failed (HTTP ${status})`, body, false];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!res.ok) {
    const [msg, detail, needsOcr] = messageFrom(res.status, body);
    throw new ApiError(res.status, msg, detail, needsOcr);
  }
  return body as T;
}

/* ----------------------------------------------------------------- calls */

export function convertFile(
  file: { name: string; base64: string },
  docId: string,
): Promise<ConvertResponse> {
  return request<ConvertResponse>(`${API}/convert`, {
    method: 'POST',
    body: JSON.stringify({
      ...(docId ? { doc_id: docId } : {}),
      filename: file.name,
      content_base64: file.base64,
      echo: true,
    }),
  });
}

export function convertJson(
  kind: JsonKind,
  payload: unknown,
  docId: string,
): Promise<ConvertResponse> {
  const field = JSON_KINDS.find(([k]) => k === kind)![1];
  return request<ConvertResponse>(`${API}/convert`, {
    method: 'POST',
    body: JSON.stringify({
      ...(docId ? { doc_id: docId } : {}),
      [field]: payload,
      echo: true,
    }),
  });
}

export async function listConversions(limit = 50, offset = 0): Promise<ConversionRow[]> {
  const body = await request<unknown>(`${API}/conversions?limit=${limit}&offset=${offset}`);
  // Defensive about the envelope: a bare array or {items|conversions|rows: [...]} both work.
  if (Array.isArray(body)) return body as ConversionRow[];
  if (body && typeof body === 'object') {
    const b = body as Record<string, unknown>;
    for (const key of ['items', 'conversions', 'rows']) {
      if (Array.isArray(b[key])) return b[key] as ConversionRow[];
    }
  }
  return [];
}

export function getConversion(id: string): Promise<ConversionRow> {
  return request<ConversionRow>(`${API}/conversions/${id}`);
}

export async function getMarkdown(id: string): Promise<string> {
  const res = await fetch(`${API}/conversions/${id}/markdown`);
  const text = await res.text();
  if (!res.ok) {
    let body: unknown = text;
    try {
      body = JSON.parse(text);
    } catch {
      /* plain-text error */
    }
    const [msg, detail, needsOcr] = messageFrom(res.status, body);
    throw new ApiError(res.status, msg, detail, needsOcr);
  }
  return text;
}

export function health(): Promise<HealthResponse> {
  return request<HealthResponse>('/health');
}

/* ---------------------------------------------------------------- files */

/** Read a File as raw base64 (no data: prefix) — what content_base64 wants. */
export function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error('could not read file'));
    reader.onload = () => {
      const url = String(reader.result ?? '');
      const comma = url.indexOf(',');
      resolve(comma >= 0 ? url.slice(comma + 1) : url);
    };
    reader.readAsDataURL(file);
  });
}
