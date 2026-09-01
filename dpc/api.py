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

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

import dpc
from dpc import adapters, db, storage
from dpc.config import get_settings
from dpc.doctree import build as doctree_build
from dpc.doctree import models as doctree_models
from dpc.emitter import to_pmd
from dpc.models import LayoutView

# One rubric, one place: dpc.treemd owns §5.4's tree_source (it writes the served front
# matter), and the conversions row COPIES that verdict rather than re-deriving it — two
# implementations of the same rubric is exactly how the audit spine and the index came to
# disagree on conflict_demoted/declined manifests. Imported at module load (never through
# the lazy importlib seam) so a test that plants a fake ``dpc.treemd`` in ``sys.modules``
# cannot split the two surfaces apart again.
from dpc.treemd import _tree_source

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
    #: Optional per-request doctree override (SPEC-DOCTREE-1 §6.1): ``None`` lets
    #: ``settings.tree_mode`` decide, ``False`` opts this conversion out, ``True`` opts in.
    tree: bool | None = None


def _sha256_json(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolve_document(
    request: ConvertRequest, settings: Any
) -> tuple[LayoutView, str, str, str | None, bytes]:
    """Raw bytes -> (view, provider, sha256_input, media_type, bytes) via the reader.

    The decoded bytes ride along because the multimodal arrange payload (§10) rasterizes
    page images from them in-request; provider-JSON inputs have no pixels and never reach
    this function.
    """
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
    return view, provider, sha_input, media_type, data


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
    # UnsupportedFormat subclasses ValueError, which buys nothing here because the lookup is
    # keyed by exception NAME: without its own row every §2.4 refusal — .gif, .msg, a DOCX
    # under office_route=local, a render route with no DPC_RENDER_CMD — was served as 500
    # 'internal' with the remedy text discarded. That pages an on-call for a client error.
    status, error = {
        "OcrTimeout": (504, "ocr_timeout"),
        "OcrError": (502, "ocr_failed"),
        "UnsupportedFormat": (415, "unsupported_media_type"),
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


# ---------------------------------------------------------------------------
# Doctree + arrange wiring (SPEC-DOCTREE-1 §6). The conversion's contract is the PMD 2.0
# markdown; everything in this block degrades to a recorded status and NEVER fails the
# request — a builder crash is a 200 with tree_status=error:<Name>, and an absent or broken
# dpc.treemd / dpc.arrange package costs only its own artifact.
# ---------------------------------------------------------------------------

#: The closed set of conversions-row keys the tree pipeline may fill (schema §6.2).
_TREE_ROW_KEYS = (
    "tree_s3_key", "sha256_tree", "tree_source", "tree_nodes", "tree_status",
    "tree_md_s3_key", "sha256_tree_markdown", "passes",
)

#: Response keys copied from the tree fields when the tree ran (§6.1).
_TREE_BODY_KEYS = (
    "tree_source", "sha256_tree", "tree_nodes", "tree_status", "sha256_tree_markdown",
    "passes",
)

#: Single-image inputs the raster step passes through as page 1 (§10).
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp")


def _effective_tree_mode(requested: bool | None, configured: str) -> str:
    """Resolve the request's optional ``tree`` flag against ``settings.tree_mode`` (§6.1).

    ``None`` lets settings decide; ``False`` opts a single conversion out; ``True`` opts in
    at ``build`` when the deployment default is ``off``. ``True`` never promotes ``build``
    to ``emit``: emitting PMD 3.0 is gated on the §8 corpus measurements, a deployment
    decision, not a caller choice.
    """
    if requested is False:
        return "off"
    if requested is True and configured == "off":
        return "build"
    return configured


def _tree_artifacts(
    view: LayoutView,
    conversion_id: str,
    sha_input: str,
    mode: str,
    settings: Any,
    *,
    source: str,
    provider: str,
) -> tuple[Any, dict[str, Any]]:
    """build -> validate -> dump -> sha -> S3, plus the PMD 3.0 flatten under ``emit``.

    Never raises: returns ``(tree_or_None, fields)`` where ``fields`` carries whichever of
    ``_TREE_ROW_KEYS`` were produced (``passes`` as a dict; the row writer canonicalizes).
    ``sha256_tree`` is hashed from the exact bytes handed to storage, so the recorded sha
    can never diverge from the stored object.
    """
    fields: dict[str, Any] = {}
    try:
        tree = doctree_build.build_doctree(view)
        verdict = doctree_models.validate_tree(tree, view)
        if not verdict.ok:
            # build_doctree self-validates and degrades to a flat tree, so reaching here
            # means even the fallback failed. Record the first named rule, store nothing —
            # the degradation matrix's "invalid:*, no 3.0 object" row (§8.7).
            fields["tree_status"] = f"invalid:{verdict.violations[0]}"
            return None, fields
        data = doctree_models.dump_tree(tree)
        fields["tree_s3_key"] = storage.put_tree(conversion_id, data)
        fields["sha256_tree"] = hashlib.sha256(data).hexdigest()
        fields["tree_source"] = _tree_source(tree)
        fields["tree_nodes"] = len(tree.nodes)
        fields["tree_status"] = "built"
        fields["passes"] = tree.passes.model_dump()
    except Exception as exc:  # noqa: BLE001 - never fails the request; name only, no text.
        logger.warning("tree build failed id=%s error=%s", conversion_id, type(exc).__name__)
        fields["tree_status"] = f"error:{type(exc).__name__}"
        return None, fields
    if mode == "emit":
        try:
            treemd = importlib.import_module("dpc.treemd")
            # §5.4 front matter carries doc_id/source/provider "as 2.0", and §5.2's figure
            # URIs are ``figure://{conversion_id}/…`` — flatten's ``doc_id`` argument is
            # both (its docstring: "Also the conversion_id of figure placeholder URIs"),
            # so the call site must hand all three over or the served front matter lies by
            # omission and every figure URI comes out with an empty, unresolvable authority.
            text, report = treemd.flatten(
                tree, view, doc_id=conversion_id, source=source, provider=provider,
                extra={"sha256_input": sha_input},
            )
            if report.error or not text:
                # §5.1: flatten REFUSES by returning ``("", report)`` with ``report.error``
                # set — it never raises. Store nothing: an empty PMD 3.0 served as a 200
                # would launder a refusal into a valid-looking artifact (and stamp
                # sha256_tree_markdown with the empty-string sha). /tree.md 404s instead;
                # the tree itself stays "built". ``report.error`` is a closed code
                # (TreeInvalid:*/error:<Name>), never document text.
                logger.warning(
                    "treemd flatten refused id=%s error=%s",
                    conversion_id, report.error or "empty_output",
                )
            else:
                fields["tree_md_s3_key"] = storage.put_tree_markdown(conversion_id, text)
                fields["sha256_tree_markdown"] = hashlib.sha256(
                    text.encode("utf-8")
                ).hexdigest()
        except Exception as exc:  # noqa: BLE001 - fail closed on the NEW artifact only.
            # Defense for a treemd that violates its never-raises contract (or is absent):
            # PMD 2.0 plus the stored tree are already safe; /tree.md 404s with the reason.
            logger.warning(
                "treemd flatten failed id=%s error=%s", conversion_id, type(exc).__name__
            )
    return tree, fields


#: Leading bytes of the raster formats the pass accepts verbatim (content sniff, §10).
_IMAGE_MAGICS = (
    b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"II*\x00", b"MM\x00*", b"BM", b"GIF8",
)


def _looks_like_image(data: bytes) -> bool:
    """Magic-byte sniff for the single-image pass-through (PNG/JPEG/TIFF/BMP/GIF/WEBP)."""
    if data.startswith(_IMAGE_MAGICS):
        return True
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _raster_pages(
    data: bytes, media_type: str | None, filename: str | None, dpi: int
) -> dict[int, bytes] | None:
    """Page PNGs for the multimodal arrange payload (§10), keyed by page. Never raises.

    A single-image input passes through as page 1 verbatim; PDFs render one PNG per page at
    ``dpi`` via pymupdf. CONTENT decides the route, not the filename: a PDF uploaded as
    ``scan.png`` must be rasterized, never shipped to the LLM raw under an image label —
    the suffix/media-type hint only breaks ties for image formats whose magic isn't known
    here. The float zoom factor is fine: raster pixels feed the LLM request only, never a
    hashed artifact — the arrangement records ``image_sha256`` of whatever actually left,
    so the disclosure stays auditable without a copy.
    """
    name = (filename or "").lower()
    declared_image = (media_type or "").startswith("image/") or name.endswith(
        _IMAGE_SUFFIXES
    )
    if not data.startswith(b"%PDF") and (_looks_like_image(data) or declared_image):
        return {1: data}
    try:
        import pymupdf

        with pymupdf.open(stream=data, filetype="pdf") as document:
            matrix = pymupdf.Matrix(dpi / 72.0, dpi / 72.0)
            return {
                index + 1: document[index].get_pixmap(matrix=matrix).tobytes("png")
                for index in range(document.page_count)
            }
    except Exception as exc:  # noqa: BLE001 - no images is a recorded fallback, not a 500.
        logger.warning("arrange raster failed error=%s", type(exc).__name__)
        return None


def _arrange_and_persist(
    runner: Any,
    *,
    conversion_id: str,
    tree: Any,
    view: LayoutView,
    page_images: dict[int, bytes] | None,
    settings: Any,
    pmd_sha256: str,
) -> None:
    """The background task: run the pass, then persist its result. Never raises.

    ``run_arrange_pass`` is a pure pass — it RETURNS an :class:`ArrangementResult` and its
    own ``storage``/``db`` parameters are optional duck-typed seams it best-effort pokes.
    Persistence is this side's job (this workstream owns ``storage.py``/``db.py``): the
    artifact bytes go to S3 under §6.2's ``arr/`` key and the index row is inserted here,
    in the task itself, so a pass that ran can never silently evaporate — an arrange pass
    whose artifact never lands is a measurement that never happened, and Phase 3's shadow
    gate reads exactly these rows.
    """
    try:
        result = runner.run_arrange_pass(
            conversion_id=conversion_id,
            tree=tree,
            view=view,
            page_images=page_images,
            settings=settings,
            pmd_sha256=pmd_sha256,
        )
        if not getattr(result, "artifact_bytes", b""):
            return  # skipped:off — mode off runs nothing and stores nothing by contract.
        # One pass is enqueued per conversion, so the §6.2 key's ``n`` is 0 — a future
        # re-run surface would count existing rows first.
        s3_key = storage.put_arrangement(conversion_id, result.artifact_bytes, 0)
        artifact: dict[str, Any] = result.artifact
        row: dict[str, Any] = {
            "suggestion_id": str(uuid.uuid4()),
            "conversion_id": conversion_id,
            "artifact_sha256": result.artifact_sha256,
            "s3_key": s3_key,
            "status": result.status,
            "model_id": artifact.get("model_id"),
            "prompt_template_version": artifact.get("prompt_template_version"),
            "verifier_version": artifact.get("verifier_version"),
            "n_accepted": len(result.accepted_ops),
            "n_rejected": sum(
                1
                for window in artifact.get("windows", ())
                for verdict in window.get("verdicts", ())
                # The verifier's vocabulary is ACCEPTED | ADVISORY | REJECT_<RULE> —
                # there is no bare "REJECTED" literal anywhere in dpc/arrange, so matching
                # it counted zero forever (found by replaying a recorded artifact whose
                # REJECT_NO_MAJORITY verdicts produced n_rejected=0).
                if str(verdict.get("verdict", "")).startswith("REJECT")
            ),
        }
        variant = getattr(result, "variant_markdown", None)
        if variant is not None:
            # Active mode: the derived PMD 3.0 variant, stored under its own §6.2 address
            # (never overwriting the heuristic file). The sha8 is the artifact's — the
            # exact tag ``decided_by: heuristics+patch@{sha8}`` carries inside the bytes.
            variant_key = storage.put_tree_markdown(
                conversion_id, variant, result.artifact_sha256[:8]
            )
            row["variant_s3_key"] = variant_key
            row["variant_sha256"] = hashlib.sha256(variant.encode("utf-8")).hexdigest()
            # R17: the exact ``generated`` string the variant was flattened with — the
            # runner flattens with its default (empty), recorded verbatim.
            row["variant_generated"] = ""
        db.insert_arrangement(row)
        logger.info(
            "arrange.persisted id=%s status=%s accepted=%d",
            conversion_id, result.status, len(result.accepted_ops),
        )
    except Exception as exc:  # noqa: BLE001 - advisory boundary; class name only (PII).
        logger.warning(
            "arrange persist failed id=%s error=%s", conversion_id, type(exc).__name__
        )


def _queue_arrange(
    background_tasks: BackgroundTasks,
    *,
    conversion_id: str,
    tree: Any,
    view: LayoutView,
    source: str,
    raw_bytes: bytes | None,
    media_type: str | None,
    filename: str | None,
    settings: Any,
    pmd_sha256: str,
) -> None:
    """Hand the conversion to the async arrange pass (§4.7). Never raises.

    Lazy import plus broad catch by charter: the arrange pass is advisory, and an advisory
    pass that can break ingestion is not advisory — a broken or absent ``dpc.arrange``
    package must cost only its own artifact. Page images are rasterized in-request for
    raw-document inputs under the multimodal payload; provider-JSON inputs hand off
    ``page_images=None`` and the runner records ``payload_mode:
    structure(fallback_no_images)`` (§10). The enqueued task is
    :func:`_arrange_and_persist`, which stores the returned artifact through the real
    storage/db seams.
    """
    try:
        runner = importlib.import_module("dpc.arrange.runner")
    except Exception as exc:  # noqa: BLE001 - a missing advisory pass is a log line.
        logger.warning("arrange unavailable error=%s", type(exc).__name__)
        return
    page_images: dict[int, bytes] | None = None
    if (
        source == "document"
        and raw_bytes is not None
        and settings.arrange_payload == "multimodal"
    ):
        page_images = _raster_pages(
            raw_bytes, media_type, filename, settings.arrange_raster_dpi
        )
    try:
        background_tasks.add_task(
            _arrange_and_persist,
            runner,
            conversion_id=conversion_id,
            tree=tree,
            view=view,
            page_images=page_images,
            settings=settings,
            pmd_sha256=pmd_sha256,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("arrange enqueue failed error=%s", type(exc).__name__)


@app.post("/api/v1/process")
async def process(
    background_tasks: BackgroundTasks,
    file: UploadFile,
    doc_id: str = Form(""),
) -> dict[str, Any]:
    """One file in, one conversion out — the upload-form face of ``/convert``.

    ``/convert`` speaks JSON-with-base64 because service callers already hold bytes in
    memory; a person (or the console, or a curl one-liner) holds a FILE, and making them
    base64 it first is friction with no safety payoff. This endpoint accepts exactly one
    multipart file plus an optional ``doc_id`` form field and DELEGATES to :func:`convert`
    with the same semantics — one pipeline, so the two faces cannot drift: same routing,
    same tree/arrange wiring, same refusals (415/422/413), same response row.

        curl -F file=@statement.pdf -F doc_id=case-42 http://localhost:8300/api/v1/process
    """
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    request = ConvertRequest(
        doc_id=doc_id,
        filename=file.filename,
        content_base64=base64.b64encode(data).decode("ascii"),
    )
    return convert(request, background_tasks)


@app.post("/api/v1/convert")
def convert(request: ConvertRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
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

    raw_bytes: bytes | None = None
    if source == "document":
        view, provider, sha_input, media_type, raw_bytes = _resolve_document(
            request, settings
        )
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
        layout=settings.pmd_layout,
        rect_scale=settings.pmd_rect_scale,
        tab_snap=settings.canvas_tab_snap,
    )
    sha_markdown = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    s3_key = storage.put_markdown(conversion_id, markdown)

    # Doctree (§6): built AFTER the 2.0 store so the primary artifact is safe whatever the
    # tree pipeline does. tree=None with a status means the pipeline degraded, never raised.
    tree: Any = None
    tree_fields: dict[str, Any] = {}
    tree_mode = _effective_tree_mode(request.tree, settings.tree_mode)
    if tree_mode != "off":
        tree, tree_fields = _tree_artifacts(
            view, conversion_id, sha_input, tree_mode, settings,
            source=source, provider=provider,
        )
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
    for key in _TREE_ROW_KEYS:
        value = tree_fields.get(key)
        if key == "passes" and value is not None:
            # The DB column is text; canonical JSON (sorted, compact) so equal manifests
            # are equal strings — the same discipline as every other stored byte.
            value = json.dumps(value, sort_keys=True, separators=(",", ":"))
        row[key] = value
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
    for key in _TREE_BODY_KEYS:
        if tree_fields.get(key) is not None:
            body[key] = tree_fields[key]
    if request.echo:
        body["markdown"] = markdown

    # Arrange (§4.7): async, post-store, advisory; precondition tree_mode=emit. Only a
    # successfully built tree is worth reviewing — an invalid or errored tree already
    # recorded its own status.
    if tree is not None and tree_mode == "emit" and settings.arrange_mode != "off":
        _queue_arrange(
            background_tasks,
            conversion_id=conversion_id,
            tree=tree,
            view=view,
            source=source,
            raw_bytes=raw_bytes,
            media_type=media_type,
            filename=request.filename,
            settings=settings,
            pmd_sha256=sha_markdown,
        )
    return body


# ---------------------------------------------------------------------------
# Conversions index
# ---------------------------------------------------------------------------
#: One uuid rubric for the whole service: db.py owns it because the constraint is
#: POSTGRES's grammar, not Python's — ``uuid.UUID`` accepts ``urn:uuid:…`` forms that 500
#: on a real ``uuid`` column, and two private copies of one rubric is how the row and the
#: front matter came to disagree about ``_tree_source`` (same defect class, same review).
_valid_uuid = db._valid_uuid


def _conversion_row(conversion_id: str) -> dict[str, Any] | None:
    """The conversion row, or ``None`` — treating a non-uuid id as simply not found."""
    if not _valid_uuid(conversion_id):
        return None
    return db.get_conversion(conversion_id)


def _serialise(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key == "passes" and isinstance(value, str):
            # The row stores canonical JSON text; the API's one vocabulary for ``passes``
            # is the dict the /convert response already uses — one shape on both surfaces.
            try:
                out[key] = json.loads(value)
            except ValueError:
                out[key] = value
        elif value is None or isinstance(value, (str, int, float, bool)):
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
    row = _conversion_row(conversion_id)
    if row is None:
        raise HTTPException(status_code=404, detail="conversion not found")
    return _serialise(row)


@app.get("/api/v1/conversions/{conversion_id}/markdown")
def get_conversion_markdown(conversion_id: str) -> PlainTextResponse:
    row = _conversion_row(conversion_id)
    if row is None or not row.get("s3_key"):
        raise HTTPException(status_code=404, detail="conversion not found")
    text = storage.get_markdown(str(row["s3_key"]))
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")


# ---------------------------------------------------------------------------
# Doctree artifacts (SPEC-DOCTREE-1 §6.1). Distinct artifacts get distinct addresses —
# /markdown stays "the stored primary artifact, byte-exact", and the tree family gets its
# own routes rather than a ?output= parameter that would break caches and recorded URLs.
# ---------------------------------------------------------------------------
@app.get("/api/v1/conversions/{conversion_id}/tree")
def get_conversion_tree(conversion_id: str) -> Response:
    """The stored ``doctree.json``, byte-exact (re-hash it and it matches ``sha256_tree``)."""
    row = _conversion_row(conversion_id)
    if row is None:
        raise HTTPException(status_code=404, detail="conversion not found")
    key = row.get("tree_s3_key")
    if not key:
        # The 404 body carries tree_status so a caller can tell "tree off" (null) from
        # "builder degraded" (error:*/invalid:*) without a second request.
        return JSONResponse(
            status_code=404,
            content={"error": "no_tree", "tree_status": row.get("tree_status")},
        )
    return Response(content=storage.get_tree(str(key)), media_type="application/json")


@app.get("/api/v1/conversions/{conversion_id}/tree.md")
def get_conversion_tree_markdown(
    conversion_id: str, arrangement: str | None = None
) -> Response:
    """PMD 3.0 — the tree-flattened markdown; ``?arrangement=`` serves a stored variant.

    The variant is addressed by suggestion id and must belong to this conversion; an
    arrangement that produced no variant (shadow mode, or all ops rejected) is a 409
    ``arrangement_rejected`` — the caller asked for bytes that deliberately do not exist.
    """
    row = _conversion_row(conversion_id)
    if row is None:
        raise HTTPException(status_code=404, detail="conversion not found")
    if arrangement:
        # ``arrangement`` is caller-controlled text addressing a uuid column: a malformed
        # value names no row by construction — 404, never an InvalidTextRepresentation 500.
        if not _valid_uuid(arrangement):
            return JSONResponse(status_code=404, content={"error": "arrangement_not_found"})
        arr = db.get_arrangement(arrangement)
        if arr is None or str(arr.get("conversion_id")) != conversion_id:
            return JSONResponse(status_code=404, content={"error": "arrangement_not_found"})
        variant_key = arr.get("variant_s3_key")
        if not variant_key:
            return JSONResponse(
                status_code=409,
                content={"error": "arrangement_rejected", "status": arr.get("status")},
            )
        text = storage.get_tree_markdown(str(variant_key))
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")
    key = row.get("tree_md_s3_key")
    if not key:
        return JSONResponse(
            status_code=404,
            content={"error": "no_tree_markdown", "tree_status": row.get("tree_status")},
        )
    text = storage.get_tree_markdown(str(key))
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")


@app.get("/api/v1/conversions/{conversion_id}/arrangement")
def get_conversion_arrangement(conversion_id: str) -> Response:
    """The newest stored ``arrangement.json`` for this conversion (skips are artifacts too)."""
    row = _conversion_row(conversion_id)
    if row is None:
        raise HTTPException(status_code=404, detail="conversion not found")
    arr = db.latest_arrangement(conversion_id)
    if arr is None or not arr.get("s3_key"):
        return JSONResponse(status_code=404, content={"error": "no_arrangement"})
    return Response(
        content=storage.get_arrangement(str(arr["s3_key"])),
        media_type="application/json",
    )


@app.get("/api/v1/conversions/{conversion_id}/figures/{figure_id}")
def get_conversion_figure(conversion_id: str, figure_id: str) -> JSONResponse:
    """Reserved (§5.2): the ``figure://`` URI scheme is stable now, so PMD bytes never
    change when crop persistence lights up; until then this address answers honestly."""
    return JSONResponse(
        status_code=404, content={"error": "figure_extraction", "detail": "not_stored"}
    )


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
