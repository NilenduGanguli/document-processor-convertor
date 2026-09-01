"""``run_arrange_pass`` — the async pass's orchestration (SPEC-DOCTREE-1 §4.7, §10).

Async, post-store, ADVISORY: the heuristic artifacts are stored before this runs and their
shas never depend on it. Accordingly this function catches EVERYTHING at its boundary — an
advisory pass that can break ingestion is not advisory — and every outcome is stamped: a run
artifact, a skip artifact with a closed-set reason, or an ``error:<Type>`` artifact. No log
line and no artifact byte ever carries document text; budgets are measured on the MONOTONIC
clock and go to logs only, never into hashed bytes (R8).

Storage/DB are duck-typed seams: the pass stores through ``storage.put_arrangement`` /
``db.insert_arrangement`` WHEN the deployment provides them, and reports ``stored=False``
otherwise — integration (which owns ``dpc/storage.py``/``dpc/db.py``) reconciles the
signatures; the pass itself must run offline from fixtures with neither.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from dpc import canvas
from dpc.arrange.artifact import (
    artifact_sha256,
    canonical_bytes,
    error_artifact,
    ran_artifact,
    skip_artifact,
)
from dpc.arrange.client import (
    SAMPLE_TEMPS,
    ArrangeCallError,
    ArrangeLlmClient,
    ArrangeUnavailable,
)
from dpc.arrange.ops import ParsedSample, parse_sample
from dpc.arrange.payload import Window, make_windows
from dpc.arrange.verifier import AcceptedOp, verify_window
from dpc.doctree.models import DocTree, NodeKind, tree_sha256, walk_body
from dpc.doctree.patch import PatchInvalid, apply_patch
from dpc.models import LayoutView

logger = logging.getLogger(__name__)

#: §4.7 budgets (spec defaults; ``arrange_window_timeout_seconds`` / ``_doc_timeout``
#: override).
WINDOW_TIMEOUT_SECONDS = 20.0
DOC_TIMEOUT_SECONDS = 120.0

#: §4.7 (d): a section-less page needs this many body nodes before the pass bothers.
_MIN_BODY_NODES = 10


def _now() -> float:
    """Monotonic seconds — a seam so tests can drive the budget without sleeping."""
    return time.monotonic()


def _get(settings: Any, name: str, default: Any) -> Any:
    value = getattr(settings, name, default)
    return default if value is None else value


@dataclass(slots=True)
class ArrangementResult:
    """Everything the caller (API background hook) needs to record one pass outcome."""

    status: str
    artifact: dict[str, Any]
    artifact_bytes: bytes = b""
    artifact_sha256: str = ""
    payload_mode: str = "structure"
    accepted_ops: list[dict[str, Any]] = field(default_factory=list)
    review_queue: list[dict[str, Any]] = field(default_factory=list)
    windows_run: int = 0
    windows_skipped: int = 0
    #: Active mode only: the derived PMD 3.0 variant text, when derivation succeeded.
    variant_markdown: str | None = None
    #: Why the variant is absent (``unavailable(treemd_missing)``, ``patch_invalid(rule)``,
    #: ``flatten_refused(code)``, ``flatten_failed(ExcName)``) or None when no derivation
    #: was attempted / it succeeded (``derived``).
    variant_status: str | None = None
    stored: bool = False


# ---------------------------------------------------------------------------------------
# should_run — §4.7's trigger
# ---------------------------------------------------------------------------------------
def should_run(tree: DocTree, view: LayoutView | None = None) -> tuple[bool, str]:
    """Whether the pass has anything to add for this tree, and the (loggable) reason.

    Run iff: (a) any page holds >= 2 frames; (b) coverage fell back anywhere; (c) the
    builder confessed order ties; or (d) no provider sections while some page has more than
    one band and the body has more than 10 nodes. A clean single-column letter skips —
    there is nothing for a reviewer to reorder.

    Gate (a) is answered from BOTH representations when ``view`` is given. Frame nodes
    exist in the tree only when the geometry rung built it — on a provider-seeded tree the
    columns the canvas saw are recorded in node ``prov`` but no ``frame`` node exists, so a
    tree-only gate (the first shipped version) could never fire on exactly the flagship
    case: provider reading order on a multi-column page, which is where a validator adds
    the most value. Found live: a two-column terms page, ``page_layout`` reporting a
    2-frame spatial region, ``skipped(clean_single_column)``. The spec's signature was
    always ``should_run(tree, layouts)``; this restores the second argument.

    Returns:
        ``(True, why)`` or ``(False, skip_reason)`` with the skip reason from the closed
        §4.7 set (``no_geometry`` | ``clean_single_column``).
    """
    body_nodes = [n for n in walk_body(tree) if n.kind is not NodeKind.body]
    if not any(n.prov.band_ix is not None for n in body_nodes):
        return False, "no_geometry"
    frames_per_page: dict[int, int] = {}
    max_band: dict[int, int] = {}
    for node in body_nodes:
        if node.kind is NodeKind.frame:
            frames_per_page[node.page] = frames_per_page.get(node.page, 0) + 1
        if node.prov.band_ix is not None:
            max_band[node.page] = max(max_band.get(node.page, 0), node.prov.band_ix)
    if any(count >= 2 for count in frames_per_page.values()):
        return True, "multi_frame"
    if view is not None:
        pages = sorted({p.page for p in view.pages} | {b.page for b in view.blocks})
        for page_number in pages:
            layout = canvas.page_layout(view, page_number)
            if any(len(region.frames) >= 2 for region in layout.regions):
                return True, "multi_frame_geometry"
    if tree.report.coverage_fallback_pages:
        return True, "coverage_fallback"
    if tree.report.order_ties:
        return True, "order_ties"
    if (
        tree.passes.provider_sections == "absent"
        and any(band >= 1 for band in max_band.values())
        and len(body_nodes) > _MIN_BODY_NODES
    ):
        return True, "sectionless_multiband"
    return False, "clean_single_column"


# ---------------------------------------------------------------------------------------
# run_arrange_pass
# ---------------------------------------------------------------------------------------
def run_arrange_pass(
    conversion_id: str,
    tree: DocTree,
    view: LayoutView,
    page_images: dict[int, bytes] | None,
    settings: Any,
    storage: Any = None,
    db: Any = None,
    *,
    pmd_sha256: str = "",
) -> ArrangementResult:
    """The whole pass for one document. NEVER raises — the boundary catch is the contract.

    Args:
        conversion_id: The conversion this pass reviews (names the stored artifact).
        tree: The stored heuristic tree.
        view: The stored layout view the tree indexes into.
        page_images: §10 multimodal — PNG per page from the source rasterisation; None or
            empty for provider-JSON inputs (no pixels), which falls back to structure mode
            with ``payload_mode: structure(fallback_no_images)`` recorded.
        settings: ``arrange_*``/``coin_*`` config (read via getattr with spec defaults).
        storage: Optional object with ``put_arrangement(conversion_id, data) -> key``.
        db: Optional object with ``insert_arrangement(row)``.
        pmd_sha256: The stored PMD's sha, quoted in the artifact when known.

    Returns:
        An :class:`ArrangementResult`; ``status`` is ``ran``, ``skipped:<reason>``,
        ``skipped:off`` (mode off — nothing stored) or ``error:<Type>``.
    """
    started = _now()
    try:
        result = _run(conversion_id, tree, view, page_images, settings, storage, db,
                      pmd_sha256=pmd_sha256)
    except Exception as exc:  # noqa: BLE001 - the advisory boundary; class name only (PII).
        document = error_artifact(
            doc_id=_safe_id(conversion_id),
            sha256_tree=_tree_sha_or_blank(tree),
            exc_name=type(exc).__name__,
        )
        result = ArrangementResult(
            status=document["status"],
            artifact=document,
            artifact_bytes=canonical_bytes(document),
            artifact_sha256=artifact_sha256(document),
        )
        result.stored = _store(storage, db, conversion_id, result)
    logger.info(
        "arrange.done doc=%s status=%s windows=%d skipped=%d accepted=%d ms=%d",
        conversion_id, result.status, result.windows_run, result.windows_skipped,
        len(result.accepted_ops), int((_now() - started) * 1000),
    )
    return result


def _tree_sha_or_blank(tree: DocTree) -> str:
    try:
        return tree_sha256(tree)
    except Exception:  # noqa: BLE001 - the error artifact must not depend on a dumpable tree.
        return "0" * 64


def _safe_id(conversion_id: str) -> str:
    """Conversion ids are uuids; anything else is dropped rather than stored."""
    return conversion_id if conversion_id.replace("-", "").isalnum() else ""


def _skip(
    conversion_id: str,
    tree: DocTree,
    reason: str,
    storage: Any,
    db: Any,
    *,
    payload_mode: str | None = None,
) -> ArrangementResult:
    document = skip_artifact(
        doc_id=_safe_id(conversion_id), sha256_tree=tree_sha256(tree), reason=reason,
        payload_mode=payload_mode,
    )
    result = ArrangementResult(
        status=f"skipped:{reason}",
        artifact=document,
        artifact_bytes=canonical_bytes(document),
        artifact_sha256=artifact_sha256(document),
        payload_mode=payload_mode or "structure",
    )
    result.stored = _store(storage, db, conversion_id, result)
    return result


def _run(
    conversion_id: str,
    tree: DocTree,
    view: LayoutView,
    page_images: dict[int, bytes] | None,
    settings: Any,
    storage: Any,
    db: Any,
    *,
    pmd_sha256: str,
) -> ArrangementResult:
    mode = str(_get(settings, "arrange_mode", "off")).strip().lower()
    if mode == "off":
        # Mode off means the hook should not have fired; nothing runs, nothing is stored.
        return ArrangementResult(status="skipped:off", artifact={})

    run, reason = should_run(tree, view)
    if not run:
        return _skip(conversion_id, tree, reason, storage, db)

    client = ArrangeLlmClient(settings)
    if not client.available():
        return _skip(conversion_id, tree, "no_llm_configured", storage, db)

    requested = str(_get(settings, "arrange_payload", "multimodal")).strip().lower()
    images = dict(page_images or {}) if requested == "multimodal" else {}
    if requested == "multimodal" and images:
        payload_mode = "multimodal"
    elif requested == "multimodal":
        payload_mode = "structure(fallback_no_images)"  # provider-JSON input: no pixels.
        images = {}
    else:
        payload_mode = "structure"

    max_window = int(_get(settings, "arrange_max_window", 48))
    samples_k = int(_get(settings, "arrange_samples", len(SAMPLE_TEMPS)))
    window_timeout = float(_get(settings, "arrange_window_timeout_seconds",
                                WINDOW_TIMEOUT_SECONDS))
    doc_timeout = float(_get(settings, "arrange_doc_timeout_seconds", DOC_TIMEOUT_SECONDS))

    windows = make_windows(tree, view, page_images=images or None, max_window=max_window)
    if not windows:
        return _skip(conversion_id, tree, "clean_single_column", storage, db,
                     payload_mode=payload_mode)

    deadline = _now() + doc_timeout
    window_records: list[dict[str, Any]] = []
    accepted: list[AcceptedOp] = []
    review_queue: list[dict[str, Any]] = []
    windows_run = 0
    windows_skipped = 0

    for window in windows:
        if _now() >= deadline:
            windows_skipped += 1
            window_records.append({
                "window_ix": window.window_ix,
                "page": window.page,
                "payload_sha256": window.payload_sha256,
                "skipped": "budget_exhausted",
            })
            continue
        samples = _collect_samples(
            client, window, samples_k, window_timeout, deadline,
        )
        verified = verify_window(tree, view, window, samples)
        record: dict[str, Any] = {
            "window_ix": window.window_ix,
            "page": window.page,
            "node_span": list(window.node_span),
            "payload_sha256": window.payload_sha256,
            "raw": _raw_records(samples, verified.sample_discards),
            "verdicts": verified.verdicts,
        }
        if window.image_sha256 is not None:
            # §10: the disclosure's fingerprint, never the bytes.
            record["image_sha256"] = window.image_sha256
        window_records.append(record)
        accepted.extend(verified.accepted)
        review_queue.extend(verified.review)
        windows_run += 1

    if windows_run == 0:
        # The budget expired before a single window completed — the pass timed out whole.
        return _skip(conversion_id, tree, "timeout", storage, db, payload_mode=payload_mode)

    accepted_ops = _dedupe_sorted(accepted)
    document = ran_artifact(
        doc_id=_safe_id(conversion_id),
        pmd_sha256=pmd_sha256,
        sha256_tree=tree_sha256(tree),
        model_id=str(_get(settings, "arrange_model", "gemini-2.5-flash")),
        payload_mode=payload_mode,
        samples=samples_k,
        windows=window_records,
        accepted_ops=accepted_ops,
        review_queue=review_queue,
    )
    result = ArrangementResult(
        status="ran",
        artifact=document,
        artifact_bytes=canonical_bytes(document),
        artifact_sha256=artifact_sha256(document),
        payload_mode=payload_mode,
        accepted_ops=accepted_ops,
        review_queue=review_queue,
        windows_run=windows_run,
        windows_skipped=windows_skipped,
    )
    result.stored = _store(storage, db, conversion_id, result)

    if mode == "active" and accepted_ops:
        _derive_variant(result, tree, view)
    return result


def _collect_samples(
    client: ArrangeLlmClient,
    window: Window,
    samples_k: int,
    window_timeout: float,
    deadline: float,
) -> list[ParsedSample]:
    """k samples for one window; every failure is a named per-sample discard, never a raise."""
    samples: list[ParsedSample] = []
    payload_text = window.payload_bytes.decode()
    for sample_ix in range(max(1, samples_k)):
        remaining = deadline - _now()
        if remaining <= 0:
            samples.append(ParsedSample(ops=None, discarded="budget_exhausted"))
            continue
        temperature = SAMPLE_TEMPS[min(sample_ix, len(SAMPLE_TEMPS) - 1)]
        try:
            text = client.complete(
                payload_text=payload_text,
                payload_sha256=window.payload_sha256,
                sample_ix=sample_ix,
                temperature=temperature,
                image_png=window.image_png,
                timeout=min(window_timeout, remaining),
            )
        except (ArrangeCallError, ArrangeUnavailable):
            samples.append(ParsedSample(ops=None, discarded="unreachable"))
            continue
        samples.append(parse_sample(text))
    return samples


def _raw_records(
    samples: list[ParsedSample], v9_discards: list[str | None]
) -> list[dict[str, Any]]:
    """§4.6 ``raw``: verbatim schema-valid ops per sample, or the named discard.

    A V9-discarded sample keeps its ops verbatim PLUS the discard reason, so replaying the
    verifier over the recorded artifact re-discards it identically; a parse-level discard
    stores no ops at all (its raw text never reaches any artifact).
    """
    records: list[dict[str, Any]] = []
    for sample_ix, sample in enumerate(samples):
        discarded = sample.discarded
        if discarded is None and sample_ix < len(v9_discards):
            discarded = v9_discards[sample_ix]
        records.append({
            "sample_ix": sample_ix,
            "ops": [op.dump() for op in sample.ops] if sample.ops is not None else None,
            "discarded": discarded,
        })
    return records


def _dedupe_sorted(accepted: list[AcceptedOp]) -> list[dict[str, Any]]:
    """Global canonical application order + first-wins dedupe across window seams.

    The same (op, node, ref) can be accepted in two adjacent windows when the pair sits in
    a context overlap; applying it twice would be a double move, so identity keeps its
    first (lowest-key) acceptance. The key is intrinsic (page, rank, tree ids) — never an
    arrival index.
    """
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in sorted(accepted, key=lambda a: a.key):
        identity = (item.op["op"], item.op["node"], item.op.get("ref", ""))
        if identity in seen:
            continue
        seen.add(identity)
        out.append(item.op)
    return out


def _derive_variant(result: ArrangementResult, tree: DocTree, view: LayoutView) -> None:
    """Active mode (§4.7): accepted ops -> patched tree -> PMD 3.0 variant.

    ``treemd`` is Phase 2's module and may not exist on disk yet in this workstream's
    branch; its absence is recorded as a status, never raised — integration reconciles.
    """
    try:
        patched, flow_joins = apply_patch(tree, result.accepted_ops)
    except PatchInvalid as exc:
        result.variant_status = f"patch_invalid({exc.rule})"
        return
    try:
        from dpc import treemd  # deliberately lazy: Phase-2 module, may not exist yet.
    except ImportError:
        result.variant_status = "unavailable(treemd_missing)"
        return
    try:
        markdown, report = treemd.flatten(
            patched, view,
            decided_by=f"heuristics+patch@{result.artifact_sha256[:8]}",
            flow_joins=flow_joins,
        )
    except Exception as exc:  # noqa: BLE001 - variant failure never costs the artifact.
        result.variant_status = f"flatten_failed({type(exc).__name__})"
        return
    if report.error is not None:
        # ``flatten`` NEVER raises — a refusal is a typed ``report.error`` with empty
        # markdown. An empty string stored under a success status would serve nothing as
        # the variant; the refusal must travel as the status (codes only, never text).
        result.variant_status = f"flatten_refused({report.error})"
        return
    result.variant_markdown = markdown
    result.variant_status = "derived"


def _store(storage: Any, db: Any, conversion_id: str, result: ArrangementResult) -> bool:
    """Best-effort persistence through the duck-typed seams; counts-only logging."""
    stored = False
    put = getattr(storage, "put_arrangement", None)
    if callable(put):
        try:
            put(conversion_id, result.artifact_bytes)
            stored = True
        except Exception as exc:  # noqa: BLE001 - storage failure must not kill the pass.
            logger.warning("arrange.store_failed doc=%s error=%s",
                           conversion_id, type(exc).__name__)
    insert = getattr(db, "insert_arrangement", None)
    if callable(insert):
        try:
            insert({
                "conversion_id": conversion_id,
                "artifact_sha256": result.artifact_sha256,
                "status": result.status,
                "n_accepted": len(result.accepted_ops),
            })
        except Exception as exc:  # noqa: BLE001 - as above.
            logger.warning("arrange.db_failed doc=%s error=%s",
                           conversion_id, type(exc).__name__)
    return stored


__all__ = [
    "DOC_TIMEOUT_SECONDS",
    "WINDOW_TIMEOUT_SECONDS",
    "ArrangementResult",
    "run_arrange_pass",
    "should_run",
]
