# document-processor-convertor (DPC)

Any supported reading of a document — the document bytes themselves, an Azure Read or Azure
Layout result, or a DES OCR output — becomes **one deterministic, position-preserving
markdown file** (PMD) in S3, with its record in Postgres. Every downstream AI workflow then
consumes documents in exactly one shape: markdown an LLM reads natively, that survives
chunking, that a human can diff, and that does not lose where things sat on the page.

> **Picking this up cold?** Read [`HANDOFF.md`](HANDOFF.md) first — current state (what is
> verified by execution versus merely written), how to get running in ten minutes, the
> decisions not to reverse silently, what to do next, and the traps that cost real time.

The format is specified normatively in [`docs/SPEC-PMD.md`](docs/SPEC-PMD.md), extended by
[`docs/SPEC-PMD-2.md`](docs/SPEC-PMD-2.md) (spatial canvases) and
[`docs/specs/SPEC-DOCTREE-1.md`](docs/specs/SPEC-DOCTREE-1.md) (the logical document tree and
the advisory LLM arrangement pass). Design rationale lives in
[`docs/DESIGN.md`](docs/DESIGN.md). The short version:

```markdown
<!-- @2 [93,319,434,347] title -->
# UNITED STATES OF AMERICA
```

GitHub-flavoured markdown, plus one HTML-comment anchor per element carrying the page and
bounding rectangle it came from. Renderers hide the anchors; RAG chunks keep them.

## How a document gets in

| Input | Read by | Provider recorded |
|---|---|---|
| `content_base64` — PDF with a text layer | PyMuPDF, blocks with rectangles | `pymupdf` |
| `content_base64` — image, or PDF with scanned pages | Azure Document Intelligence (whole document) | `azure_layout` |
| `azure_analyze_result` — Read- or Layout-shaped payload | shape auto-detected | as detected |
| `azure_read_result` — declared Azure Vision Read v3.2 | Read mapper | `azure-read-v3.2` |
| `des_ocr` — Document Enrichment Service OCR output | DES mapper | `des-ocr` |

A scanned document with no Azure DI endpoint configured is a structured `422 needs_ocr`
refusal — never a guess.

## Quickstart

```bash
docker compose up --build
```

That starts the service on **:8300**, Postgres 16 on **:5438**, and MinIO on **:9004**
(console **:9005**, bucket `docmd`, credentials `dpc` / `dpc-secret`).

Convert a PDF:

```bash
curl -s -X POST http://localhost:8300/api/v1/convert \
  -H 'Content-Type: application/json' \
  -d "{\"doc_id\": \"demo-1\", \"filename\": \"sample.pdf\",
       \"content_base64\": \"$(base64 < sample.pdf | tr -d '\n')\", \"echo\": true}"
```

Convert an Azure result you already have (no document bytes needed):

```bash
curl -s -X POST http://localhost:8300/api/v1/convert \
  -H 'Content-Type: application/json' \
  -d '{"doc_id": "demo-2", "azure_analyze_result": '"$(cat analyze_result.json)"'}'
```

Or use the console at **http://localhost:8300** — drag a file (or paste a provider JSON) on
the **Convert** page, read the rendered markdown, raw PMD and parsed anchors in tabs; the
**History** page lists past conversions and reopens any of them.

## API

All endpoints are JSON unless stated. An `X-Request-Id` header is honoured and echoed. When
`DPC_API_KEY` is set, requests must carry it in `X-API-Key`.

### `POST /api/v1/convert`

Body — **exactly one** input field, else `400`:

```jsonc
{
  "doc_id": "optional caller id",
  "filename": "optional, helps type sniffing",
  "content_base64": "…",          // OR
  "azure_read_result": { … },     // OR
  "azure_analyze_result": { … },  // OR
  "des_ocr": { … },
  "echo": false                   // true => response includes the markdown
}
```

Response:

```jsonc
{
  "id": "conversion uuid",
  "doc_id": "…", "source": "document", "provider": "pymupdf",
  "pages": 3, "blocks": 42, "tables": 1, "marks": 2, "key_values": 5, "chars": 6120,
  "sha256_markdown": "…",
  "s3_bucket": "docmd", "s3_key": "pmd/2026/08/<id>.md",
  "ms": 148,
  "markdown": "… (only when echo=true)"
}
```

Errors: `400` (not exactly one input), `422 {"error": "needs_ocr", "detail": …}` (document
needs optical recognition and no Azure DI endpoint is configured).

### Reads

| Endpoint | Returns |
|---|---|
| `GET /api/v1/conversions?limit=50&offset=0` | conversion rows, newest first |
| `GET /api/v1/conversions/{id}` | one conversion row |
| `GET /api/v1/conversions/{id}/markdown` | the PMD itself, `text/markdown`, fetched from S3 |
| `GET /health` | `{status, service, version}` |
| `GET /readyz` | `{ready, checks: {postgres, s3}}` — `503` when not ready |

Anything that is not an API path serves the built frontend (SPA fallback).

## Configuration

Environment variables, prefix `DPC_` (also read from `.env`). See `dpc/config.py`.

| Variable | Default | Meaning |
|---|---|---|
| `DPC_HOST` | `0.0.0.0` | Bind address |
| `DPC_PORT` | `8300` | Bind port |
| `DPC_API_KEY` | *(empty)* | Optional `X-API-Key` gate; empty disables it |
| `DPC_PG_DSN` | `postgresql://dpc:dpc@localhost:5438/dpc` | Postgres DSN |
| `DPC_S3_ENDPOINT` | `http://localhost:9004` | S3/MinIO endpoint |
| `DPC_S3_ACCESS_KEY` | `dpc` | S3 access key |
| `DPC_S3_SECRET_KEY` | `dpc-secret` | S3 secret key |
| `DPC_S3_BUCKET` | `docmd` | Bucket for PMD artifacts |
| `DPC_S3_REGION` | `us-east-1` | S3 region |
| `DPC_MAX_BYTES` | `33554432` | Max upload size (32 MiB) |
| `DPC_MAX_PAGES` | `500` | Pages read from one PDF |
| `DPC_MIN_ALNUM_CHARS` | `40` | Per-page floor: below this many alphanumeric characters the text layer counts as absent (page is a scan) |
| `DPC_AZURE_DI_ENDPOINT` | *(empty)* | Azure Document Intelligence endpoint for OCR; empty ⇒ scans get a `422 needs_ocr` refusal |
| `DPC_AZURE_DI_KEY` | *(empty)* | Azure DI key |
| `DPC_AZURE_DI_API_VERSION` | `2024-11-30` | Azure DI API version |
| `DPC_OCR_TIMEOUT_SECONDS` | `60.0` | Overall OCR wait budget |
| `DPC_OCR_POLL_INTERVAL_SECONDS` | `0.5` | Operation-Location poll interval |
| `DPC_OCR_MAX_POLLS` | `120` | Poll count cap |
| `DPC_LOG_LEVEL` | `DEBUG` | Log level. No log line ever carries document text — counts and hashes only |

## Storage layout

**S3** (MinIO locally): bucket `docmd`, one object per conversion at

```
pmd/{yyyy}/{mm}/{conversion-uuid}.md
```

The markdown is the artifact; S3 is where artifacts live, so future consumers need no
service call to read one.

**Postgres**: one `conversions` row per artifact — the index and audit trail (the file is
the truth):

`id` (uuid pk) · `doc_id` · `source` · `provider` · `filename` · `media_type` · `pages` ·
`blocks` · `tables_n` · `marks` · `key_values` · `chars` · `sha256_input` ·
`sha256_markdown` · `s3_bucket` · `s3_key` · `status` · `error` · `ms` · `created_at`
(indexed, descending).

The schema is applied at startup (`CREATE TABLE IF NOT EXISTS`, see `dpc/schema.sql`).

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q       # tests (emitter golden suite, API suite)
.venv/bin/ruff check dpc tests      # lint
```

Frontend: `cd frontend && npm run build` emits `frontend/dist`, which the service serves.

## Security posture

- **No document text in any log line** — counts and hashes only. This is a KYC-adjacent
  service; treat every document as sensitive.
- `doc_id` is caller data, stored verbatim, never interpreted.
- Optional `X-API-Key` gate (`DPC_API_KEY`); fine to disable behind a service mesh.
- Credentials come from the environment, never the image. The only outbound call is the
  optional in-network Azure DI endpoint.

## Corpus verification

The service is graded against the sibling DCE reference corpus — 124 real files: 98 PDFs
(including 119- and 354-page scanned regulatory filings), 22 EDGAR HTML filings, 2 XLSX
registers and 2 photo IDs. `tools/corpus_sweep.py` converts every file **twice** and grades
the output, not the status code: anchor grammar, rectangle-inside-page geometry, page-marker
monotonicity, alnum fidelity against an independent PyMuPDF read, and byte-determinism
across the pair.

Current result: **124 / 124**, zero determinism failures, ~28,000 anchors all grammar-valid
and in-bounds. Determinism holds even through two full OCR round-trips of a 119-page scan
(`sha256 b00fa10ed00e57a9…` both times), because the artifact deliberately carries no wall
clock — conversion time lives in the database row, which is what makes `sha256_markdown` a
content-addressed dedupe key.

Reproduce with the stack running:

    .venv/bin/python tools/corpus_sweep.py

Large scanned documents are budget-bound: the local mock recognises at ~4 s/page (the
compose file sets `DPC_OCR_TIMEOUT_SECONDS=900` for it); real Document Intelligence at
~1 s/page fits the code default of 180.
