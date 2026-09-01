"""Tests for :mod:`dpc.doctree.harvest` — SPEC-DOCTREE-1 §3.1, the provider-structure seed.

Two recorded-shape fixtures (synthetic text, DI 2024-11-30 shape) carry the defensive cases
on purpose: ``sections_two_column.json`` has a dangling ref and a double-claim,
``sections_figure_caption.json`` has one caption that maps to its paragraph and one that maps
to nothing (bbox-only). The tests assert the §3.1 rules — dropped-and-counted, first claim
wins, cycles cut — and the two properties everything downstream leans on: the resolved
structure contains zero surviving dangling refs, and ``to_raw``/``from_raw`` round-trip
through JSON to an identical dataclass.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dpc.adapters import from_azure_layout
from dpc.doctree.harvest import (
    ProviderStructure,
    SectionRef,
    from_raw,
    harvest_structure,
    to_raw,
)

FIXTURES = Path(__file__).parent / "fixtures" / "di"


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def walk_sections(section: SectionRef) -> list[SectionRef]:
    out = [section]
    for child in section.children:
        out.extend(walk_sections(child))
    return out


def assert_no_surviving_dangling(structure: ProviderStructure, payload: dict[str, Any]) -> None:
    """Every index in the resolved structure is in range of its payload array."""
    result = payload["analyzeResult"]
    limits = {
        "section": len(result.get("sections", [])),
        "paragraph": len(result.get("paragraphs", [])),
        "table": len(result.get("tables", [])),
        "figure": len(result.get("figures", [])),
    }
    for section in walk_sections(structure.root):
        assert 0 <= section.section_ix < limits["section"]
        for kind, ix in section.elements:
            assert 0 <= ix < limits[kind], (kind, ix)
    for figure in structure.figures:
        assert 0 <= figure.figure_ix < limits["figure"]
        if figure.caption_paragraph_ix is not None:
            assert 0 <= figure.caption_paragraph_ix < limits["paragraph"]


# ---------------------------------------------------------------------------
# Fixture 1 — two columns, dangling ref, double-claim
# ---------------------------------------------------------------------------
def test_two_column_fixture_resolves() -> None:
    payload = load("sections_two_column.json")
    structure = harvest_structure(payload)
    assert structure is not None
    assert_no_surviving_dangling(structure, payload)
    # Root: title, both column sections, the table — the dangling /paragraphs/99 is gone.
    assert structure.root.elements == (
        ("paragraph", 0), ("section", 1), ("section", 2), ("table", 0),
    )
    assert [c.section_ix for c in structure.root.children] == [1, 2]


def test_dangling_ref_dropped_and_counted() -> None:
    structure = harvest_structure(load("sections_two_column.json"))
    assert structure is not None
    assert structure.dangling_dropped == 1
    assert ("paragraph", 99) not in structure.root.elements


def test_double_claim_keeps_first_document_order_claimant() -> None:
    """Paragraph 6 is claimed by both columns; section 1 walks first, so it keeps the claim."""
    structure = harvest_structure(load("sections_two_column.json"))
    assert structure is not None
    left, right = structure.root.children
    assert ("paragraph", 6) in left.elements
    assert ("paragraph", 6) not in right.elements
    assert structure.double_claims == 1
    # And exactly once overall — the exactly-once discipline invariant I3 will lean on.
    claims = [
        ref for s in walk_sections(structure.root) for ref in s.elements if ref[0] != "section"
    ]
    assert len(claims) == len(set(claims))


def test_sibling_order_is_provider_order_verbatim() -> None:
    structure = harvest_structure(load("sections_two_column.json"))
    assert structure is not None
    left, right = structure.root.children
    assert left.elements == (
        ("paragraph", 1), ("paragraph", 2), ("paragraph", 3), ("paragraph", 6),
    )
    assert right.elements == (("paragraph", 4), ("paragraph", 5))


# ---------------------------------------------------------------------------
# Fixture 2 — figures and captions
# ---------------------------------------------------------------------------
def test_figure_caption_fixture_resolves() -> None:
    payload = load("sections_figure_caption.json")
    structure = harvest_structure(payload)
    assert structure is not None
    assert_no_surviving_dangling(structure, payload)
    assert structure.dangling_dropped == 0
    assert structure.double_claims == 0
    assert len(structure.figures) == 2


def test_caption_maps_to_paragraph_index_never_text() -> None:
    structure = harvest_structure(load("sections_figure_caption.json"))
    assert structure is not None
    mapped = structure.figures[0]
    assert mapped.caption_paragraph_ix == 2
    assert mapped.bbox is not None
    assert mapped.caption_bbox is not None
    # Azure's undocumented id is carried verbatim for prov.provider_ref only (R20).
    assert mapped.provider_id == "1.1"


def test_unmatched_caption_is_bbox_only() -> None:
    """The orphan caption's spans match no paragraph -> index None, geometry kept."""
    structure = harvest_structure(load("sections_figure_caption.json"))
    assert structure is not None
    orphan = structure.figures[1]
    assert orphan.caption_paragraph_ix is None
    assert orphan.caption_bbox is not None


def test_caption_span_match_when_elements_absent() -> None:
    """Without a caption ``elements`` ref, span overlap against paragraphs[] resolves it."""
    payload = load("sections_figure_caption.json")
    caption = payload["analyzeResult"]["figures"][0]["caption"]
    caption["elements"] = []
    structure = harvest_structure(payload)
    assert structure is not None
    assert structure.figures[0].caption_paragraph_ix == 2


def test_no_text_in_harvest() -> None:
    """No document string survives into the structure — I5 starts at the source.

    The only strings anywhere in the raw form are the schema tag, the closed ref-kind enum,
    and the provider figure id — never a value that appears in the fixture's text.
    """
    payload = load("sections_figure_caption.json")
    structure = harvest_structure(payload)
    assert structure is not None
    blob = json.dumps(to_raw(structure))
    for paragraph in payload["analyzeResult"]["paragraphs"]:
        assert paragraph["content"] not in blob
    assert "Orphan" not in blob and "Synthetic" not in blob


# ---------------------------------------------------------------------------
# Absence, cycles, and hostile shapes
# ---------------------------------------------------------------------------
def test_no_sections_yields_none() -> None:
    """A Tesseract-mock/Read-shaped payload (pages+lines, no sections) is None, no error."""
    payload = {
        "analyzeResult": {
            "apiVersion": "2024-11-30",
            "modelId": "prebuilt-layout",
            "content": "MOCK OCR LINE",
            "pages": [
                {
                    "pageNumber": 1, "width": 8.5, "height": 11.0, "unit": "inch",
                    "lines": [{"content": "MOCK OCR LINE", "polygon": [1, 1, 4, 1, 4, 2, 1, 2]}],
                }
            ],
            "paragraphs": [
                {
                    "content": "MOCK OCR LINE",
                    "boundingRegions": [{"pageNumber": 1, "polygon": [1, 1, 4, 1, 4, 2, 1, 2]}],
                }
            ],
        }
    }
    assert harvest_structure(payload) is None
    assert harvest_structure(payload["analyzeResult"]) is None


def test_hostile_shapes_never_raise() -> None:
    for garbage in ({}, {"analyzeResult": None}, {"sections": "nope"},
                    {"sections": [None]}, {"sections": [{"elements": [3, None, "///"]}]}):
        harvest_structure(garbage)  # must not raise; None or an empty-ish structure is fine


def test_cycle_is_cut_at_the_revisit() -> None:
    payload = {
        "sections": [
            {"elements": ["/sections/1", "/sections/0"]},          # self via child + cycle
            {"elements": ["/sections/0", "/paragraphs/0"]},        # back-edge to the root
        ],
        "paragraphs": [{"content": "x", "spans": [{"offset": 0, "length": 1}]}],
    }
    structure = harvest_structure(payload)
    assert structure is not None
    assert structure.cycles_cut == 2  # 0 -> ... -> 0 back-edge, and root's own /sections/0
    assert structure.root.elements == (("section", 1),)
    assert structure.root.children[0].elements == (("paragraph", 0),)


# ---------------------------------------------------------------------------
# Adapter wiring + round-trip + determinism
# ---------------------------------------------------------------------------
def test_from_azure_layout_parks_structure_in_raw() -> None:
    payload = load("sections_two_column.json")
    view = from_azure_layout(payload)
    assert "structure" in view.raw
    # The parked form is plain JSON types and rebuilds the identical dataclass.
    json.dumps(view.raw["structure"])
    assert from_raw(view.raw["structure"]) == harvest_structure(payload)
    # Existing raw keys are untouched by the addition.
    assert view.raw["provider"] == "azure-prebuilt-layout"
    assert view.raw["_line_join"][1] >= view.raw["_line_join"][0] >= 0


def test_from_azure_layout_key_absent_when_no_sections() -> None:
    view = from_azure_layout(
        {"analyzeResult": {"content": "x", "pages": [], "paragraphs": []}}
    )
    assert "structure" not in view.raw


def test_raw_round_trip_identity() -> None:
    for name in ("sections_two_column.json", "sections_figure_caption.json"):
        structure = harvest_structure(load(name))
        assert structure is not None
        raw = to_raw(structure)
        # Through actual JSON bytes, exactly as a stored view travels.
        rebuilt = from_raw(json.loads(json.dumps(raw)))
        assert rebuilt == structure
        assert to_raw(rebuilt) == raw


def test_from_raw_refuses_foreign_shapes() -> None:
    assert from_raw(None) is None
    assert from_raw({}) is None
    assert from_raw({"schema": "someone-elses/9", "root": {}}) is None
    assert from_raw({"schema": "dpc.provider-structure/1"}) is None  # no root
    assert from_raw({"schema": "dpc.provider-structure/1", "root": {"elements": [["p", "x"]]}}
                    ) is None  # malformed element pair


def test_harvest_is_deterministic() -> None:
    for name in ("sections_two_column.json", "sections_figure_caption.json"):
        first = harvest_structure(load(name))
        second = harvest_structure(load(name))
        assert first == second
        assert first is not None and to_raw(first) == to_raw(second)  # type: ignore[arg-type]
