# Build contracts — read before touching anything

Four workstreams build this repo in parallel. Each OWNS the files listed for it and touches
nothing else. `dpc/models.py`, `dpc/adapters.py`, `dpc/emitter.py`, `dpc/config.py` are DONE —
read them, code against them, never edit them. Do not git commit; the integrator commits.

## Shared facts

- Service: FastAPI on port 8300. Env prefix `DPC_` (see dpc/config.py for every setting).
- Input kinds: `document` (base64 bytes), `azure_read_result`, `azure_analyze_result`,
  `des_ocr` — mapped by `dpc.adapters.from_azure_read / from_azure / from_des_ocr`
  (`from_azure` auto-detects read-vs-layout shape and records which in `view.raw["provider"]`).
- `dpc.emitter.to_pmd(view, *, source, provider, doc_id, generated, extra)` -> markdown str.
- Postgres 16 at 5438, db/user/pass `dpc`. MinIO S3 at 9004 (console 9005), bucket `docmd`,
  creds dpc / dpc-secret.
- S3 key layout: `pmd/{yyyy}/{mm}/{id}.md` where id is the conversion uuid.

## A. dpc/pdfread.py + dpc/ocr_client.py + tests/test_pdfread.py

`read_document(data: bytes, *, filename: str | None, settings: Settings) -> tuple[LayoutView, str]`
returning (view, provider). PDF with text layer -> PyMuPDF `get_text("dict")`: one TextBlock
per block WITH bbox=[x0,y0,x1,y0,x1,y1,x0,y1], page sizes from page.rect, provider "pymupdf".
Per-page alnum floor `settings.min_alnum_chars`: a page below it is a scan. Any scanned page
or a non-PDF image => whole document to Azure DI via ocr_client (202 + Operation-Location
poll, bounded by settings) then `from_azure(...)`, provider "azure_layout". No endpoint
configured => raise `NeedsRecognition(reason)` (define in pdfread.py; api maps it to 422).
Never log document text.

## B. dpc/storage.py + dpc/db.py + dpc/schema.sql + dpc/api.py + Dockerfile + docker-compose.yml + pyproject.toml + tests/test_api.py

- schema: table `conversions` (id uuid pk, doc_id text, source text, provider text, filename
  text, media_type text, pages int, blocks int, tables_n int, marks int, key_values int,
  chars int, sha256_input text, sha256_markdown text, s3_bucket text, s3_key text, status
  text, error text, ms int, created_at timestamptz default now()). Index (created_at desc).
  Apply at startup with CREATE TABLE IF NOT EXISTS (db.py).
- storage.py: boto3 client from settings; put_markdown(id, text) -> s3_key; get_markdown(key).
- api.py: POST /api/v1/convert  body: {doc_id?, filename?, content_base64? | azure_read_result?
  | azure_analyze_result? | des_ocr?, echo?: bool} — exactly one input; 400 otherwise.
  Pipeline: resolve view (adapters or A's read_document) -> to_pmd(generated=utcnow isoZ,
  extra={"sha256_input": ...}) -> sha256 markdown -> S3 put -> pg insert -> response row
  {id, doc_id, source, provider, pages, blocks, tables, marks, key_values, chars,
  sha256_markdown, s3_bucket, s3_key, ms, markdown? (only when echo)}.
  GET /api/v1/conversions?limit=50&offset=0 (list, newest first), GET /api/v1/conversions/{id},
  GET /api/v1/conversions/{id}/markdown (text/markdown fetched from S3),
  GET /health {status,service,version}, GET /readyz {ready, checks: {postgres, s3}} (503 if not),
  X-Request-Id honoured + echoed. Serve ../frontend/dist as SPA fallback like DCE (API paths
  never fall through to HTML). NeedsRecognition -> 422 {error:"needs_ocr", detail}.
  tests/test_api.py: FastAPI TestClient with storage+db faked in-memory (monkeypatch), covering
  each input kind (tiny fixture payloads), exactly-one-input 400, echo, and 422 path.
- Dockerfile: python:3.12-slim, install ".", copy frontend/dist, uvicorn dpc.api:app on 8300,
  HEALTHCHECK curl /health. compose: app (build ., 8300:8300, env wired to pg+minio services),
  postgres:16 (5438:5432, POSTGRES_USER/PASSWORD/DB dpc), minio (9004:9000 9005:9001,
  MINIO_ROOT_USER dpc MINIO_ROOT_PASSWORD dpc-secret), minio-init (mc mb --ignore-existing).
  pyproject: fastapi, uvicorn[standard], pydantic>=2, pydantic-settings, boto3,
  psycopg[binary], httpx, pymupdf; dev extra: pytest, ruff.

## C. frontend/ (everything under it)

Vite + React 18 + TS, dark enterprise console visually consistent with the sibling service at
~/document-classification-extraction/frontend (read its src/index.css tokens and Panel
component for the look; do not import from it — copy what you need). Pages: **Convert**
(drag/drop or file pick; OR paste-JSON tab with kind selector azure_read/azure_layout/des_ocr;
calls POST /api/v1/convert with echo=true; result: summary chips (pages/blocks/tables/chars),
tabs "Rendered" (react-markdown + remark-gfm) / "Raw PMD" (pre, anchors visible) / "Anchors"
(parsed table of anchor comments: page, rect, tag)) and **History** (GET /conversions table:
created_at, doc_id, source, provider, pages, chars, status; row click -> fetch
/conversions/{id}/markdown into the same viewer). Router: /convert (default), /history.
`npm run build` MUST emit frontend/dist and dist is committed. No external network at runtime
except same-origin /api. Keep bundle lean; react, react-dom, react-router-dom, react-markdown,
remark-gfm only.

## D. docs/SPEC-PMD.md + README.md + tests/test_emitter.py

SPEC: the PMD format, normatively — front matter fields, page marker, anchor grammar
`<!-- @<page> [x0,y0,x1,y1] <tag> -->`, tag vocabulary (title, heading, p, verbatim provider
roles, furniture[:role], mark, kv, table RxC), element renderings, escaping rules (from
emitter.py docstrings — read them, they are the rationale), determinism guarantee, chunking
property (why anchors are inline), and the "no bbox -> no anchor" honesty rule.
README: what the service is, quickstart (compose up, curl convert, UI), API reference, env
table from config.py, S3/PG layout. tests/test_emitter.py: golden-file determinism (two calls
byte-equal), table pipe/newline escaping, comment-opener sanitisation, zone=table skip,
y-splice ordering, no-bbox-no-anchor, heading escape of leading '#'.
