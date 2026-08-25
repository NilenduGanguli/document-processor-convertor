"""HTTP surface: one convert endpoint, a conversions index, health, and the SPA.

Every input kind funnels into the same pipeline: resolve a ``LayoutView`` (via the adapters,
or workstream A's ``dpc.pdfread`` for raw bytes) -> ``to_pmd`` -> sha256 -> S3 put -> Postgres
insert -> the response row. ``dpc.pdfread`` is imported lazily inside the handler so this
module — and everything that only sends provider JSON — works before that module exists.

KYC posture: no log line and no error detail in this module ever carries document text. Logs
carry ids, kinds, counts and durations only.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import importlib
import json
import logging
import mimetypes
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

import dpc
from dpc import adapters, db, storage
from dpc.config import get_settings
from dpc.emitter import to_pmd
from dpc.models import LayoutView

logger = logging.getLogger("dpc.api")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Apply the schema at startup; tolerate an unreachable database (readyz reports it)."""
    logging.basicConfig(level=get_settings().log_level)
    # Retried, not "deferred": the app and Postgres race at container start, and a deferral
    # that nothing ever picks up is how the first version came up ready and 500'd on the
    # first insert. If it still fails after the retries, /readyz reports postgres=false —
    # the check verifies the table, not the socket.
    db.init_schema_retrying()
    yield


app = FastAPI(title=dpc.SERVICE_NAME, version=dpc.__version__, lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Request id + optional API key gate
# ---------------------------------------------------------------------------
@app.middleware("http")
async def _request_context(request: Request, call_next: Any) -> Response:
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
    settings = get_settings()
    if (
        settings.api_key
        and request.url.path.startswith("/api/")
        and request.headers.get("X-API-Key") != settings.api_key
    ):
        response: Response = JSONResponse(
            status_code=401, content={"detail": "invalid or missing API key"}
        )
    else:
        response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    return response


# ---------------------------------------------------------------------------
# Convert
# ---------------------------------------------------------------------------
class ConvertRequest(BaseModel):
    """Exactly one of the four input fields must be present."""

    doc_id: str = ""
    filename: str | None = None
    content_base64: str | None = None
    azure_read_result: dict[str, Any] | None = None
    azure_analyze_result: dict[str, Any] | None = None
    des_ocr: dict[str, Any] | None = None
    echo: bool = False


def _sha256_json(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolve_document(
    request: ConvertRequest, settings: Any
) -> tuple[LayoutView, str, str, str | None]:
    """Raw bytes -> (view, provider, sha256_input, media_type) via workstream A's reader."""
    assert request.content_base64 is not None
    try:
        data = base64.b64decode(request.content_base64, validate=True)
    except ValueError:  # binascii.Error is a ValueError
        raise HTTPException(status_code=400, detail="content_base64 is not valid base64")
    if len(data) > settings.max_bytes:
        raise HTTPException(
            status_code=413, detail=f"document exceeds max_bytes={settings.max_bytes}"
        )
    sha_input = hashlib.sha256(data).hexdigest()
    media_type = mimetypes.guess_type(request.filename or "")[0]
    try:
        pdfread = importlib.import_module("dpc.pdfread")
    except ImportError:
        raise HTTPException(
            status_code=503, detail="document input not yet available"
        )
    needs_recognition = getattr(pdfread, "NeedsRecognition", ())
    try:
        view, provider = pdfread.read_document(
            data, filename=request.filename, settings=settings
        )
    except needs_recognition as exc:
        # Structured refusal, not an error: the document needs optical recognition and no
        # endpoint is configured (or recognition itself declined). Detail carries the
        # reason, never document content.
        raise _NeedsOcr(str(exc))
    return view, provider, sha_input, media_type


class _NeedsOcr(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@app.exception_handler(_NeedsOcr)
async def _needs_ocr_handler(request: Request, exc: _NeedsOcr) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "needs_ocr", "detail": exc.detail})


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    # Every failure leaves as JSON with a name and the request id -- never bare text. The
    # corpus sweep drove 124 real files through /convert and ten came back as the literal
    # string "Internal Server Error": OcrError/OcrTimeout had no mapping, so callers got a
    # body they could not parse and no id to quote. Mapping is by exception NAME rather
    # than import so this handler cannot itself fail on a build without ocr_client.
    name = type(exc).__name__
    status, error = {
        "OcrTimeout": (504, "ocr_timeout"),
        "OcrError": (502, "ocr_failed"),
        "ValueError": (400, "invalid_input"),
    }.get(name, (500, "internal"))
    detail = str(exc)[:300] if status != 500 else f"unhandled {name}"
    logger.error("convert failed error=%s status=%d", name, status)
    return JSONResponse(
        status_code=status,
        content={
            "error": error,
            "detail": detail,
            "request_id": request.headers.get("X-Request-Id", ""),
        },
    )


@app.post("/api/v1/convert")
def convert(request: ConvertRequest) -> dict[str, Any]:
    settings = get_settings()
    supplied = {
        "document": request.content_base64,
        "azure_read": request.azure_read_result,
        "azure_layout": request.azure_analyze_result,
        "des_ocr": request.des_ocr,
    }
    kinds = [kind for kind, value in supplied.items() if value is not None]
    if len(kinds) != 1:
        raise HTTPException(
            status_code=400,
            detail=(
                "exactly one of content_base64, azure_read_result, azure_analyze_result, "
                f"des_ocr is required (got {len(kinds)})"
            ),
        )
    source = kinds[0]
    started = time.perf_counter()

    if source == "document":
        view, provider, sha_input, media_type = _resolve_document(request, settings)
    else:
        payload = supplied[source]
        assert isinstance(payload, dict)
        sha_input = _sha256_json(payload)
        media_type = "application/json"
        if source == "azure_read":
            view = adapters.from_azure_read(payload)
        elif source == "azure_layout":
            view = adapters.from_azure(payload)
        else:
            view = adapters.from_des_ocr(payload)
        provider = str(view.raw.get("provider") or "")

    conversion_id = str(uuid.uuid4())
    # No wall clock in the artifact -- deliberately. The corpus sweep caught the first
    # version: a per-second `generated` stamp made any two conversions that straddled a
    # second boundary produce different bytes, so exactly the SLOW documents (OCR round
    # trips, 100-page filings) failed the format's determinism guarantee while fast ones
    # passed by luck. Conversion time is a fact about the conversion, not the document; it
    # lives in the row's created_at. Identical input now yields identical bytes, which is
    # what makes sha256_markdown a dedupe key.
    markdown = to_pmd(
        view,
        source=source,
        provider=provider,
        doc_id=request.doc_id,
        extra={"sha256_input": sha_input},
    )
    sha_markdown = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    s3_key = storage.put_markdown(conversion_id, markdown)
    ms = int((time.perf_counter() - started) * 1000)

    row: dict[str, Any] = {
        "id": conversion_id,
        "doc_id": request.doc_id,
        "source": source,
        "provider": provider,
        "filename": request.filename,
        "media_type": media_type,
        "pages": view.page_count,
        "blocks": len(view.blocks),
        "tables_n": len(view.tables),
        "marks": len(view.marks),
        "key_values": len(view.key_values),
        "chars": sum(len(b.text) for b in view.blocks),
        "sha256_input": sha_input,
        "sha256_markdown": sha_markdown,
        "s3_bucket": settings.s3_bucket,
        "s3_key": s3_key,
        "status": "ok",
        "error": None,
        "ms": ms,
    }
    db.insert_conversion(row)
    logger.debug(
        "converted id=%s source=%s provider=%s pages=%s blocks=%s tables=%s chars=%s ms=%s",
        conversion_id, source, provider, row["pages"], row["blocks"], row["tables_n"],
        row["chars"], ms,
    )

    body: dict[str, Any] = {
        "id": conversion_id,
        "doc_id": request.doc_id,
        "source": source,
        "provider": provider,
        "pages": row["pages"],
        "blocks": row["blocks"],
        "tables": row["tables_n"],
        "marks": row["marks"],
        "key_values": row["key_values"],
        "chars": row["chars"],
        "sha256_markdown": sha_markdown,
        "s3_bucket": settings.s3_bucket,
        "s3_key": s3_key,
        "ms": ms,
    }
    if request.echo:
        body["markdown"] = markdown
    return body


# ---------------------------------------------------------------------------
# Conversions index
# ---------------------------------------------------------------------------
def _serialise(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if value is None or isinstance(value, (str, int, float, bool)):
            out[key] = value
        elif isinstance(value, dt.datetime):
            out[key] = value.isoformat()
        else:
            out[key] = str(value)
    return out


@app.get("/api/v1/conversions")
def list_conversions(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    return [_serialise(row) for row in db.list_conversions(limit=limit, offset=offset)]


@app.get("/api/v1/conversions/{conversion_id}")
def get_conversion(conversion_id: str) -> dict[str, Any]:
    row = db.get_conversion(conversion_id)
    if row is None:
        raise HTTPException(status_code=404, detail="conversion not found")
    return _serialise(row)


@app.get("/api/v1/conversions/{conversion_id}/markdown")
def get_conversion_markdown(conversion_id: str) -> PlainTextResponse:
    row = db.get_conversion(conversion_id)
    if row is None or not row.get("s3_key"):
        raise HTTPException(status_code=404, detail="conversion not found")
    text = storage.get_markdown(str(row["s3_key"]))
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": dpc.SERVICE_NAME, "version": dpc.__version__}


@app.get("/readyz")
def readyz() -> JSONResponse:
    checks = {"postgres": db.check(), "s3": storage.check()}
    ready = all(checks.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"ready": ready, "checks": checks},
    )


# ---------------------------------------------------------------------------
# SPA (frontend/dist) — API paths never fall through to HTML
# ---------------------------------------------------------------------------
def _dist_dir() -> Path | None:
    for root in (
        Path(__file__).resolve().parent.parent / "frontend" / "dist",
        Path.cwd() / "frontend" / "dist",
    ):
        if root.is_dir():
            return root
    return None


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str) -> FileResponse:
    if path == "api" or path.startswith("api/"):
        raise HTTPException(status_code=404, detail="not found")
    root = _dist_dir()
    if root is not None:
        if path:
            candidate = (root / path).resolve()
            if candidate.is_file() and root.resolve() in candidate.parents:
                return FileResponse(candidate)
        index = root / "index.html"
        if index.is_file():
            return FileResponse(index)
    raise HTTPException(status_code=404, detail="frontend not built")


__all__ = ["app"]
