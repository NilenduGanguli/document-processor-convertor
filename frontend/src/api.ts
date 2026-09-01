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
  /* Doctree fields (SPEC-DOCTREE-1 §6.1) — absent on older deployments or tree_mode=off,
     so every one of them is optional and the console renders fine without them. */
  tree_status?: string | null;
  tree_source?: string | null;
  tree_nodes?: number | null;
  sha256_tree?: string | null;
  sha256_tree_markdown?: string | null;
  passes?: Record<string, string> | null;
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
  /* Doctree columns (nullable in the DB; absent from older API builds). */
  tree_status?: string | null;
  tree_source?: string | null;
  tree_nodes?: number | null;
  sha256_tree?: string | null;
}

/* ------------------------------------------------------- doctree types */
/*
 * Shape of the stored doctree.json artifact — SPEC-DOCTREE-1 §2.2, schema "dpc.doctree/1".
 * Everything the renderer reads is optional or null-tolerant: the artifact is produced by a
 * separate workstream and this viewer must degrade to "shape unrecognised", never crash.
 * Invariant I5 means NO field here ever carries document text — the console can only show
 * structure (kinds, paths, counts), which is exactly what the Tree tab renders.
 */

export interface TreeMetrics {
  char_count?: number;
  line_count?: number;
  height_mu?: number;
  ends_terminal_punct?: boolean;
  starts_lowercase?: boolean | null;
  ends_hyphen?: boolean;
  script_class?: string;
  digit_ratio_class?: string;
  alignment?: string;
}

export interface TreeProv {
  source?: string;
  provider_ref?: string | null;
  provider_role?: string | null;
  band_ix?: number | null;
  frame_ix?: number | null;
  region_ix?: number | null;
}

export interface TreeNode {
  id: number;
  kind: string;
  path: string;
  parent: number | null;
  children?: number[];
  page?: number | null;
  bbox?: [number, number, number, number] | null;
  level?: number | null;
  block_ixs?: number[];
  table_ix?: number | null;
  kv_ix?: number | null;
  mark_ix?: number | null;
  figure_id?: string | null;
  metrics?: TreeMetrics | null;
  prov?: TreeProv | null;
}

export interface FlowEdge {
  src: number;
  dst: number;
  kind: string;
  score?: number;
  evidence?: string[];
}

export interface DocTree {
  schema: string;
  doc_id?: string;
  view_sha256?: string;
  builder?: string;
  pages?: Array<{ page: number; width_mu: number; height_mu: number }>;
  body?: number;
  furniture?: number;
  nodes: TreeNode[];
  flow?: FlowEdge[];
  report?: {
    order_ties?: Array<[number, number, number]>;
    coverage_fallback_pages?: number[];
    declined_pages?: number[];
  };
  passes?: Record<string, string>;
  counters?: Record<string, number>;
}

/** Minimal structural check before rendering — enough to trust nodes[] indexing. */
export function isDocTree(body: unknown): body is DocTree {
  if (!body || typeof body !== 'object') return false;
  const b = body as Record<string, unknown>;
  return typeof b.schema === 'string' && Array.isArray(b.nodes);
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

/**
 * The stored doctree artifact. 404 means "no tree for this conversion" — a normal state
 * (tree_mode=off, an older conversion, or a build that fell back), not a failure; the spec's
 * 404 body is `{"error":"no_tree","tree_status":…}` and the caller renders it calm.
 */
export function getTree(id: string): Promise<DocTree> {
  return request<DocTree>(`${API}/conversions/${id}/tree`);
}

/** Pull tree_status out of a getTree() 404 detail, when the service sent one. */
export function treeStatusFrom(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  if (error.detail && typeof error.detail === 'object') {
    const d = error.detail as Record<string, unknown>;
    if (typeof d.tree_status === 'string') return d.tree_status;
  }
  return null;
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
