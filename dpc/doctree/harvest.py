"""Provider-structure harvest — Azure DI sections/figures as index refs, never text.

``from_azure_layout`` currently keeps only provenance scalars in ``LayoutView.raw`` and drops
``analyzeResult.sections`` and ``analyzeResult.figures`` on the floor — the verified adapter
gap SPEC-DOCTREE-1 §3.1 exists to close. This module extracts that structure into a frozen
dataclass tree of **index references only**: which paragraph/table/figure each section claims
and in what order, how sections nest, where each figure sits and which paragraph is its
caption. No document string ever enters the result — caption *content* is deliberately
reduced to the index of the paragraph it maps to (invariant I5 starts at the source, not at
the tree serializer).

The harvest runs inside :func:`dpc.adapters.from_azure_layout` (the only code that sees the
raw payload) and parks its JSON form under ``view.raw["structure"]`` via :func:`to_raw`, so a
stored view round-trips through JSON and the tree builder (Phase 1) never re-fetches the
provider payload. :func:`from_raw` is the inverse; round-trip identity is pinned by test.

Defensive resolution, per §3.1 — the provider's ref graph is treated as untrusted:

* a ref that does not parse as ``/sections|paragraphs|tables|figures/N`` or whose index is
  out of range is **dropped and counted** (``dangling_dropped``);
* a paragraph/table/figure claimed by two sections keeps its **first claimant in document
  order** (pre-order walk from ``sections[0]``, elements in provider array order) — the
  duplicate is dropped and counted (``double_claims``);
* a section reachable twice — self-reference, shared child or true cycle — is **cut at the
  revisit** and counted (``cycles_cut``);
* a payload with no usable ``sections`` yields ``None``, which downstream reads as
  ``passes.provider_sections = "absent"`` — the honest state for Read/Tesseract-shaped
  payloads that simply have no section stream.

This module intentionally does NOT import :mod:`dpc.adapters` (the import runs the other
way), so the handful of defensive coercions it needs are local copies — a dozen lines of
duplication beats an import cycle in the conversion path.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Ref kinds a section may claim, keyed by the collection name in the JSON-pointer ref.
#: Closed on purpose: DI 2024-11-30 sections reference exactly these four collections, and an
#: unknown collection is a dangling ref, not a new feature.
_REF_KINDS: dict[str, str] = {
    "sections": "section",
    "paragraphs": "paragraph",
    "tables": "table",
    "figures": "figure",
}


# ---------------------------------------------------------------------------
# The harvested structure — frozen, index-refs only (invariant I5 at the source)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SectionRef:
    """One provider section: its index, its ordered claims, its child sections.

    ``elements`` preserves the provider's array order verbatim (§3.2 step 3: sibling order =
    provider order) as ``(kind, index)`` pairs; a ``("section", k)`` entry corresponds
    positionally to the next entry of ``children``, so both the flat claim order and the
    nesting survive without a second bookkeeping structure.
    """

    section_ix: int
    elements: tuple[tuple[str, int], ...]
    children: tuple[SectionRef, ...]


@dataclass(frozen=True, slots=True)
class FigureRef:
    """One provider figure: geometry plus the caption's paragraph *index*, never its text.

    ``provider_id`` is Azure's undocumented figure id, carried verbatim for
    ``prov.provider_ref`` only (R20) — nothing may key off it. ``bbox`` quads stay in the
    page's own float unit, exactly as every other quad in :class:`dpc.models.LayoutView`
    does; mu quantisation happens once, at emit, through :mod:`dpc.geom`.
    """

    figure_ix: int
    page: int
    bbox: tuple[float, ...] | None
    provider_id: str | None
    caption_paragraph_ix: int | None
    caption_bbox: tuple[float, ...] | None


@dataclass(frozen=True, slots=True)
class ProviderStructure:
    """The harvest result: section tree, figures, and the honesty counters.

    The counters are the only evidence a reader gets that defensive resolution fired — a
    dropped ref that is not counted is a silent skip, which this codebase does not do.
    """

    root: SectionRef
    figures: tuple[FigureRef, ...]
    dangling_dropped: int
    double_claims: int
    cycles_cut: int


# ---------------------------------------------------------------------------
# Local defensive coercions (see module docstring for why they are not imported)
# ---------------------------------------------------------------------------
def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _quad(polygon: Any) -> tuple[float, ...] | None:
    """An 8-float quad from a polygon list, or ``None`` — same tolerance as the adapters."""
    if not isinstance(polygon, list):
        return None
    try:
        values = [float(v) for v in polygon]
    except (TypeError, ValueError):
        return None
    if len(values) == 8:
        return tuple(values)
    if len(values) == 4:
        x0, y0, x1, y1 = values
        return (x0, y0, x1, y0, x1, y1, x0, y1)
    if len(values) >= 6 and len(values) % 2 == 0:
        xs, ys = values[0::2], values[1::2]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        return (x0, y0, x1, y0, x1, y1, x0, y1)
    return None


def _region_page_and_quad(node: dict[str, Any]) -> tuple[int, tuple[float, ...] | None]:
    """Page (1-based) and quad of the first bounding region, tolerating absence of both."""
    regions = _dicts(node.get("boundingRegions"))
    if not regions:
        return 1, None
    page = _as_int(regions[0].get("pageNumber"), 1) or 1
    return page, _quad(regions[0].get("polygon"))


def _spans(node: dict[str, Any]) -> list[tuple[int, int]]:
    """Character spans as ``(offset, length)``; accepts ``spans: [...]`` and ``span: {...}``."""
    raw = node.get("spans")
    if raw is None and isinstance(node.get("span"), dict):
        raw = [node["span"]]
    out: list[tuple[int, int]] = []
    for span in _dicts(raw):
        offset = _as_int(span.get("offset"), -1)
        if offset >= 0:
            out.append((offset, _as_int(span.get("length"))))
    return out


def _spans_overlap(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> bool:
    return any(
        a_off < b_off + max(b_len, 1) and b_off < a_off + max(a_len, 1)
        for a_off, a_len in a
        for b_off, b_len in b
    )


def _parse_ref(ref: Any) -> tuple[str, int] | None:
    """``"/paragraphs/3"`` -> ``("paragraph", 3)``, or ``None`` for anything else."""
    if not isinstance(ref, str):
        return None
    parts = ref.split("/")
    if len(parts) != 3 or parts[0] != "":
        return None
    kind = _REF_KINDS.get(parts[1])
    if kind is None or not parts[2].isdigit():
        return None
    return (kind, int(parts[2]))


# ---------------------------------------------------------------------------
# Harvest
# ---------------------------------------------------------------------------
def harvest_structure(analyze_result: dict[str, Any]) -> ProviderStructure | None:
    """Extract sections/figures refs, or None when absent.

    Runs inside ``from_azure_layout`` (the only module that sees the raw payload) and parks
    the result under ``view.raw["structure"]``, so the tree stays constructible from recorded
    payloads with no re-fetch. Defensive resolution: dangling refs dropped; an element
    claimed by two sections keeps its first (document-order) claimant; a section reachable
    twice (cycle) is cut at the revisit. Only refs and geometry are kept — caption TEXT is
    kept as a paragraph INDEX, never a string.

    Args:
        analyze_result: The ``analyzeResult`` object, or the whole job JSON — both accepted,
            mirroring ``from_azure_layout`` itself.

    Returns:
        The harvested structure, or ``None`` when the payload carries no usable sections
        (Read v3.2, the Tesseract-shaped mock, pre-sections API versions).
    """
    # The conversion entrypoint above this call never raises (inherited contract), and an
    # advisory harvest that can break ingestion is not advisory. A defect here must cost the
    # structure, not the conversion — and it must not raise through a path that could put
    # payload text into an exception message, so nothing is logged either: the absent key is
    # the visible symptom, surfaced downstream as ``passes.provider_sections = "absent"``.
    try:
        return _harvest(analyze_result)
    except Exception:  # noqa: BLE001 - see the never-raises contract above
        return None


def _harvest(analyze_result: dict[str, Any]) -> ProviderStructure | None:
    node = _as_dict(analyze_result)
    result = _as_dict(node.get("analyzeResult")) or node
    sections = _dicts(result.get("sections"))
    if not sections:
        return None
    limits = {
        "section": len(sections),
        "paragraph": len(_dicts(result.get("paragraphs"))),
        "table": len(_dicts(result.get("tables"))),
        "figure": len(_dicts(result.get("figures"))),
    }
    counters = {"dangling_dropped": 0, "double_claims": 0, "cycles_cut": 0}
    # ``sections[0]`` is the document root by DI convention. Pre-order from it, elements in
    # provider array order, IS document order — which is what makes first-claim-wins a
    # deterministic rule rather than an arrival-order accident.
    visited: set[int] = {0}
    claimed: set[tuple[str, int]] = set()
    root = _resolve_section(0, sections, limits, visited, claimed, counters)
    figures = tuple(
        _harvest_figure(ix, figure, limits, result)
        for ix, figure in enumerate(_dicts(result.get("figures")))
    )
    return ProviderStructure(
        root=root,
        figures=figures,
        dangling_dropped=counters["dangling_dropped"],
        double_claims=counters["double_claims"],
        cycles_cut=counters["cycles_cut"],
    )


def _resolve_section(
    section_ix: int,
    sections: list[dict[str, Any]],
    limits: dict[str, int],
    visited: set[int],
    claimed: set[tuple[str, int]],
    counters: dict[str, int],
) -> SectionRef:
    """One section, resolved depth-first with the §3.1 defensive rules.

    Recursion depth is bounded by ``len(sections)`` — every section is entered at most once
    (``visited`` is checked before descending), so a payload would need thousands of real
    nested sections to approach the interpreter limit; if one ever does, the entrypoint guard
    degrades the whole harvest to ``None`` rather than raising out of the conversion.
    """
    elements: list[tuple[str, int]] = []
    children: list[SectionRef] = []
    for ref in _as_dict(sections[section_ix]).get("elements") or []:
        parsed = _parse_ref(ref)
        if parsed is None or parsed[1] >= limits[parsed[0]]:
            counters["dangling_dropped"] += 1
            continue
        kind, ix = parsed
        if kind == "section":
            if ix in visited:
                counters["cycles_cut"] += 1
                continue
            visited.add(ix)
            elements.append(parsed)
            children.append(_resolve_section(ix, sections, limits, visited, claimed, counters))
            continue
        if parsed in claimed:
            counters["double_claims"] += 1
            continue
        claimed.add(parsed)
        elements.append(parsed)
    return SectionRef(
        section_ix=section_ix, elements=tuple(elements), children=tuple(children)
    )


def _harvest_figure(
    figure_ix: int,
    figure: dict[str, Any],
    limits: dict[str, int],
    result: dict[str, Any],
) -> FigureRef:
    page, bbox = _region_page_and_quad(figure)
    provider_id = figure.get("id")
    caption = _as_dict(figure.get("caption"))
    caption_bbox: tuple[float, ...] | None = None
    caption_ix: int | None = None
    if caption:
        _, caption_bbox = _region_page_and_quad(caption)
        caption_ix = _caption_paragraph_ix(caption, limits, result)
    return FigureRef(
        figure_ix=figure_ix,
        page=page,
        bbox=bbox,
        provider_id=str(provider_id) if isinstance(provider_id, str) and provider_id else None,
        caption_paragraph_ix=caption_ix,
        caption_bbox=caption_bbox,
    )


def _caption_paragraph_ix(
    caption: dict[str, Any], limits: dict[str, int], result: dict[str, Any]
) -> int | None:
    """The paragraph index a caption maps to, or ``None`` (bbox-only caption).

    The caption's own ``content`` is NEVER copied — the paragraph index is the whole record.
    Resolution order: (1) the caption's own ``elements`` ref, the provider's explicit claim,
    when it parses and is in range; (2) span overlap against ``paragraphs[]``, first match by
    ascending paragraph index — an intrinsic, deterministic tiebreak. A caption matching
    neither stays bbox-only, which is honest: the geometry is real, the mapping is not.
    """
    for ref in caption.get("elements") or []:
        parsed = _parse_ref(ref)
        if parsed is not None and parsed[0] == "paragraph" and parsed[1] < limits["paragraph"]:
            return parsed[1]
    caption_spans = _spans(caption)
    if not caption_spans:
        return None
    for ix, paragraph in enumerate(_dicts(result.get("paragraphs"))):
        if _spans_overlap(caption_spans, _spans(paragraph)):
            return ix
    return None


# ---------------------------------------------------------------------------
# JSON round-trip — ``view.raw["structure"]`` must survive store-and-reload
# ---------------------------------------------------------------------------
#: Versioned so a future harvest change can be told apart from a recorded one. ``from_raw``
#: refuses unknown schemas rather than guessing — a misread structure is worse than none.
RAW_SCHEMA = "dpc.provider-structure/1"


def to_raw(structure: ProviderStructure) -> dict[str, Any]:
    """The structure as plain JSON types, for ``LayoutView.raw["structure"]``.

    Lists for tuples, ``None`` kept explicit — every key always present, so the stored shape
    is stable and a reader never distinguishes "absent" from "null" by accident.
    """
    return {
        "schema": RAW_SCHEMA,
        "root": _section_to_raw(structure.root),
        "figures": [
            {
                "figure_ix": f.figure_ix,
                "page": f.page,
                "bbox": list(f.bbox) if f.bbox is not None else None,
                "provider_id": f.provider_id,
                "caption_paragraph_ix": f.caption_paragraph_ix,
                "caption_bbox": list(f.caption_bbox) if f.caption_bbox is not None else None,
            }
            for f in structure.figures
        ],
        "counters": {
            "dangling_dropped": structure.dangling_dropped,
            "double_claims": structure.double_claims,
            "cycles_cut": structure.cycles_cut,
        },
    }


def _section_to_raw(section: SectionRef) -> dict[str, Any]:
    return {
        "section_ix": section.section_ix,
        "elements": [[kind, ix] for kind, ix in section.elements],
        "children": [_section_to_raw(child) for child in section.children],
    }


def from_raw(raw: Any) -> ProviderStructure | None:
    """Rebuild a :class:`ProviderStructure` from its :func:`to_raw` form.

    Defensive like the harvest itself: a shape this module did not write (wrong schema,
    hand-edited JSON, a future version) yields ``None`` rather than a half-read structure.
    """
    try:
        node = _as_dict(raw)
        if node.get("schema") != RAW_SCHEMA:
            return None
        root = _section_from_raw(node["root"])
        counters = _as_dict(node.get("counters"))
        return ProviderStructure(
            root=root,
            figures=tuple(
                FigureRef(
                    figure_ix=_as_int(f.get("figure_ix")),
                    page=_as_int(f.get("page"), 1) or 1,
                    bbox=_raw_quad(f.get("bbox")),
                    provider_id=(
                        f["provider_id"] if isinstance(f.get("provider_id"), str) else None
                    ),
                    caption_paragraph_ix=(
                        f["caption_paragraph_ix"]
                        if isinstance(f.get("caption_paragraph_ix"), int)
                        else None
                    ),
                    caption_bbox=_raw_quad(f.get("caption_bbox")),
                )
                for f in _dicts(node.get("figures"))
            ),
            dangling_dropped=_as_int(counters.get("dangling_dropped")),
            double_claims=_as_int(counters.get("double_claims")),
            cycles_cut=_as_int(counters.get("cycles_cut")),
        )
    except Exception:  # noqa: BLE001 - a foreign shape yields None, never an exception
        return None


def _section_from_raw(raw: Any) -> SectionRef:
    node = _as_dict(raw)
    elements: list[tuple[str, int]] = []
    for pair in node.get("elements") or []:
        kind, ix = pair
        if kind not in _REF_KINDS.values() or not isinstance(ix, int):
            raise ValueError("malformed element ref")  # caught by from_raw -> None
        elements.append((kind, ix))
    return SectionRef(
        section_ix=_as_int(node.get("section_ix")),
        elements=tuple(elements),
        children=tuple(_section_from_raw(child) for child in node.get("children") or []),
    )


def _raw_quad(value: Any) -> tuple[float, ...] | None:
    if not isinstance(value, list):
        return None
    return tuple(float(v) for v in value)


__all__ = [
    "RAW_SCHEMA",
    "FigureRef",
    "ProviderStructure",
    "SectionRef",
    "from_raw",
    "harvest_structure",
    "to_raw",
]
