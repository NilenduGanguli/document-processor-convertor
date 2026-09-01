"""The doctree builder, tested through the ladder a real corpus climbs.

Three rungs, three fixtures: a provider-seeded single column (Azure sections + a figure), a
two-column statement the canvas machinery decomposes (geometry), and a geometry-less
seq-only view (flat fallback). Every rung must produce a tree that validates I1-I5 and —
the product contract — byte-identical ``dump_tree`` output across process boundaries and
hash-seed changes, because the stored artifact is sha256-stamped.

The builders below follow ``tests/test_emitter_spatial.py``'s style (TextBlock with lines,
inch pages) so the canvas fires exactly as it does for the emitter's own fixtures.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from dpc.doctree.build import build_doctree
from dpc.doctree.harvest import FigureRef, ProviderStructure, SectionRef, to_raw
from dpc.doctree.models import (
    DocTree,
    NodeKind,
    ProvSource,
    dump_tree,
    tree_sha256,
    validate_tree,
    walk_body,
)
from dpc.models import KeyValue, LayoutView, Mark, PageInfo, TextBlock, TextLine, Zone

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# View builders (shared with the continuity/patch test modules)
# ---------------------------------------------------------------------------
def quad(x0: float, y0: float, x1: float, y1: float) -> list[float]:
    return [x0, y0, x1, y0, x1, y1, x0, y1]


def block(text: str, box: tuple[float, float, float, float], **kw: object) -> TextBlock:
    """A block that carries its own single line — the Azure-layout shape."""
    q = quad(*box)
    return TextBlock(text=text, bbox=q, lines=[TextLine(text=text, bbox=q)], **kw)  # type: ignore[arg-type]


def page(n: int = 1) -> PageInfo:
    return PageInfo(page=n, width=8.5, height=11.0, unit="inch")


COLUMN_ROWS = [
    ("Account Holder", "Statement Period", 1.53, 1.78),
    ("Jane Q. Public", "01 Jun 2026 - 30 Jun 2026", 1.88, 2.10),
    ("14 Rivermill Lane", "Currency  USD", 2.18, 2.40),
    ("Portland, OR 97205", "Closing Balance", 2.48, 2.70),
    ("Account  ****-****-4417", "USD 12,480.55", 2.78, 3.00),
]


def two_column_view() -> LayoutView:
    """The geometry rung: a title plus a two-column body the canvas splits into frames."""
    blocks = [block("QUARTERLY ACCOUNT STATEMENT", (0.83, 0.69, 7.78, 1.04), zone=Zone.title)]
    for left, right, y0, y1 in COLUMN_ROWS:
        blocks.append(block(left, (0.83, y0, 4.03, y1)))
        blocks.append(block(right, (4.58, y0, 7.78, y1)))
    return LayoutView(pages=[page()], blocks=blocks)


def provider_view() -> LayoutView:
    """The provider rung: sections/figures harvested into ``raw["structure"]``."""
    blocks = [
        block("Annual Report", (0.8, 0.7, 7.8, 1.0), zone=Zone.title, role="title"),
        block("Introduction", (0.8, 1.2, 3.0, 1.45), zone=Zone.heading, role="sectionHeading"),
        block("This report presents the results.", (0.8, 1.6, 7.8, 1.85)),
        block("Methods", (0.8, 2.1, 2.5, 2.35), zone=Zone.heading, role="sectionHeading"),
        block("We measured everything twice.", (0.8, 2.5, 7.8, 2.75)),
        block("Fig 1: a chart of the results", (0.8, 4.6, 3.0, 4.8)),
        block("Page 1 of 9", (3.9, 10.5, 4.6, 10.7), zone=Zone.furniture, role="pageNumber"),
        block("ACME CORP CONFIDENTIAL", (0.8, 0.2, 3.4, 0.4), zone=Zone.furniture,
              role="pageHeader"),
    ]
    structure = ProviderStructure(
        root=SectionRef(0, (("paragraph", 0), ("section", 1), ("section", 2)), (
            SectionRef(1, (("paragraph", 1), ("paragraph", 2)), ()),
            SectionRef(2, (("paragraph", 3), ("paragraph", 4), ("figure", 0)), ()),
        )),
        figures=(
            FigureRef(0, 1, tuple(quad(0.8, 3.2, 4.0, 4.5)), "1.1", 5,
                      tuple(quad(0.8, 4.6, 3.0, 4.8))),
        ),
        dangling_dropped=0, double_claims=0, cycles_cut=0,
    )
    return LayoutView(
        doc_id="doc-provider-1",
        pages=[page()],
        blocks=blocks,
        raw={"structure": to_raw(structure)},
    )


def seq_view() -> LayoutView:
    """The flat rung: no geometry at all, only provider sequence (HTML/XLSX shape)."""
    from dpc.models import Cell, Table

    return LayoutView(
        blocks=[
            TextBlock(text="alpha paragraph", seq=0),
            TextBlock(text="beta paragraph", seq=2),
        ],
        tables=[Table(table_id="t1", page=1, row_count=1, col_count=1,
                      cells=[Cell(row=0, col=0, text="cell")], seq=1)],
    )


def footnote_view() -> LayoutView:
    """A footnote-role block BETWEEN two body paragraphs — R15's interposer demotion."""
    return LayoutView(pages=[page()], blocks=[
        block("Chapter One", (0.8, 0.7, 7.8, 1.0), zone=Zone.title),
        block("The first paragraph of the chapter.", (0.8, 1.5, 7.8, 1.72)),
        block("1. See the annex for details.", (0.8, 1.9, 7.8, 2.05), role="footnote"),
        block("The second paragraph continues.", (0.8, 2.3, 7.8, 2.52)),
    ])


def kv_mark_view() -> LayoutView:
    """Key-values (one placed, one floating) and a selection mark — claim exactness."""
    blocks = [
        block(text, (0.83, 1.5 + 0.3 * i, 7.7, 1.72 + 0.3 * i))
        for i, text in enumerate([
            "Please complete every field.", "Sign at the bottom of the page.",
            "Use black ink only through out.", "Return the form to the counter.",
        ])
    ]
    return LayoutView(
        pages=[page()],
        blocks=blocks,
        key_values=[
            KeyValue(key="Name", value="Jane", page=1,
                     key_bbox=quad(0.9, 1.55, 1.6, 1.7),
                     value_bbox=quad(2.0, 1.55, 3.2, 1.7)),
            KeyValue(key="Ref", value="X-1", page=1),  # floating: no geometry at all
        ],
        marks=[Mark(state="selected", page=1, bbox=quad(0.9, 2.9, 1.05, 3.02))],
    )


def order_tie_view() -> LayoutView:
    """Two blocks sharing a band — the coin-toss ordering the report must confess."""
    return LayoutView(pages=[page()], blocks=[
        block("Left cell text", (0.9, 1.5, 3.0, 1.72)),
        block("Right cell text", (3.2, 1.5, 5.0, 1.72)),
        block("A following full paragraph here.", (0.9, 2.1, 7.6, 2.32)),
    ])


def nested_panel_view() -> LayoutView:
    """A two-column page whose LEFT column holds a nested two-sub-column panel.

    The panel's corridor is blocked at page level by the four wide rows above it, so only
    the §3.2 step-4 recursive XY-cut can find it — the fixture the flat band-order builder
    interleaves row-by-row.
    """
    rows = [(1.5 + 0.4 * i, 1.72 + 0.4 * i) for i in range(8)]
    blocks = [
        block(f"Wide introductory row number {i} here.", (0.9, y0, 4.0, y1))
        for i, (y0, y1) in enumerate(rows[:4])
    ]
    blocks += [
        block(f"Sub left {i}", (0.9, y0, 2.2, y1))
        for i, (y0, y1) in enumerate(rows[4:])
    ]
    blocks += [
        block(f"Sub right {i}", (2.8, y0, 4.0, y1))
        for i, (y0, y1) in enumerate(rows[4:])
    ]
    blocks += [
        block(f"Right column paragraph {i} runs.", (4.6, y0, 7.8, y1))
        for i, (y0, y1) in enumerate(rows)
    ]
    return LayoutView(pages=[page()], blocks=blocks)


ALL_VIEWS = {
    "two_column": two_column_view,
    "provider": provider_view,
    "seq": seq_view,
    "footnote": footnote_view,
    "kv_mark": kv_mark_view,
    "order_tie": order_tie_view,
    "nested_panel": nested_panel_view,
}


def node_by_blocks(tree: DocTree, bix: int):
    return next(n for n in tree.nodes if bix in n.block_ixs)


# ---------------------------------------------------------------------------
# The honesty ladder
# ---------------------------------------------------------------------------
def test_geometry_rung_builds_flow_group_with_frames() -> None:
    tree = build_doctree(two_column_view())
    assert tree.passes.provider_sections == "absent"
    assert tree.passes.geometry == "ran"
    groups = [n for n in tree.nodes if n.kind is NodeKind.flow_group]
    assert len(groups) == 1
    frames = [tree.nodes[c] for c in groups[0].children]
    assert [f.kind for f in frames] == [NodeKind.frame, NodeKind.frame]
    # Left column's blocks (odd ixs) in frame 1, right column's (even, >0) in frame 2.
    left = [tree.nodes[c].block_ixs[0] for c in frames[0].children]
    right = [tree.nodes[c].block_ixs[0] for c in frames[1].children]
    assert left == [1, 3, 5, 7, 9]
    assert right == [2, 4, 6, 8, 10]


def test_geometry_rung_title_opens_a_section() -> None:
    tree = build_doctree(two_column_view())
    sections = [n for n in tree.nodes if n.kind is NodeKind.section]
    assert len(sections) == 1
    assert sections[0].level == 1
    heading = tree.nodes[sections[0].children[0]]
    assert heading.kind is NodeKind.heading
    assert heading.block_ixs == [0]


def test_provider_rung_seeds_sections_figures_and_captions() -> None:
    view = provider_view()
    tree = build_doctree(view)
    assert tree.passes.provider_sections == "used(3)"
    assert tree.passes.provider_figures == "used(1)"
    sections = [n for n in tree.nodes if n.kind is NodeKind.section]
    assert [s.prov.provider_ref for s in sections] == ["/sections/1", "/sections/2"]
    assert all(s.prov.source is ProvSource.azure_section for s in sections)
    figure = next(n for n in tree.nodes if n.kind is NodeKind.figure)
    assert figure.figure_id == "fig-1-1"
    caption = tree.nodes[figure.children[0]]
    assert caption.kind is NodeKind.caption
    assert caption.block_ixs == [5]  # the caption's strongest home is its figure (step 9)


def test_provider_rung_reading_order_is_preorder_of_body() -> None:
    tree = build_doctree(provider_view())
    body_blocks = [n.block_ixs[0] for n in walk_body(tree) if n.block_ixs]
    assert body_blocks == [0, 1, 2, 3, 4, 5]  # title, intro h+p, methods h+p, caption


def test_flat_rung_orders_by_provider_sequence() -> None:
    view = seq_view()
    tree = build_doctree(view)
    assert tree.passes.geometry == "absent"
    kinds = [n.kind for n in walk_body(tree) if n.kind is not NodeKind.body]
    assert kinds == [NodeKind.paragraph, NodeKind.table, NodeKind.paragraph]
    assert all(
        n.prov.source is ProvSource.seq_fallback
        for n in tree.nodes if n.block_ixs or n.table_ix is not None
    )


# ---------------------------------------------------------------------------
# Structural passes
# ---------------------------------------------------------------------------
def test_furniture_blocks_land_under_the_furniture_root_in_role_order() -> None:
    tree = build_doctree(provider_view())
    furn = tree.nodes[tree.furniture]
    assert furn.kind is NodeKind.furniture
    leaves = [tree.nodes[c] for c in furn.children]
    # pageHeader (rank 0) before pageNumber (rank 1), regardless of provider order.
    assert [leaf.block_ixs[0] for leaf in leaves] == [7, 6]
    assert [leaf.prov.provider_role for leaf in leaves] == ["pageHeader", "pageNumber"]
    body_ids = {n.id for n in walk_body(tree)}
    assert not body_ids & {leaf.id for leaf in leaves}  # excluded from flow


def test_footnote_is_demoted_to_its_section_tail() -> None:
    tree = build_doctree(footnote_view())
    assert tree.passes.interposer == "ran(footnotes=1)"
    section = next(n for n in tree.nodes if n.kind is NodeKind.section)
    kinds = [tree.nodes[c].kind for c in section.children]
    # heading, both paragraphs, THEN the footnote — deferred, still body content.
    assert kinds == [NodeKind.heading, NodeKind.paragraph, NodeKind.paragraph,
                     NodeKind.footnote]
    footnote = tree.nodes[section.children[-1]]
    assert footnote.block_ixs == [2]


def test_kv_pairs_cluster_into_a_group_and_floaters_fall_back() -> None:
    view = kv_mark_view()
    tree = build_doctree(view)
    group = next(n for n in tree.nodes if n.kind is NodeKind.kv_group)
    placed = [tree.nodes[c] for c in group.children]
    assert [p.kv_ix for p in placed] == [0]
    floater = next(
        n for n in tree.nodes if n.kind is NodeKind.kv_pair and n.kv_ix == 1
    )
    assert floater.prov.source is ProvSource.seq_fallback
    mark = next(n for n in tree.nodes if n.kind is NodeKind.mark)
    assert mark.mark_ix == 0


def test_order_ties_are_confessed_in_the_report() -> None:
    tree = build_doctree(order_tie_view())
    assert tree.report.order_ties, "two same-band siblings must be reported as a tie"
    a, b, margin = tree.report.order_ties[0]
    assert margin == 0
    assert tree.nodes[a].block_ixs == [0]
    assert tree.nodes[b].block_ixs == [1]


# ---------------------------------------------------------------------------
# I3 claim exactness + invariants, on every fixture
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(ALL_VIEWS))
def test_every_view_element_is_claimed_exactly_once(name: str) -> None:
    view = ALL_VIEWS[name]()
    tree = build_doctree(view)
    check = validate_tree(tree, view)
    assert check.ok, f"{name}: {check.violations}"
    assert tree.counters.blocks_claimed == len(view.blocks)
    assert tree.counters.tables_claimed == len(view.tables)
    assert tree.counters.kvs_claimed == len(view.key_values)
    assert tree.counters.marks_claimed == len(view.marks)
    assert tree.counters.nodes == len(tree.nodes)


@pytest.mark.parametrize("name", sorted(ALL_VIEWS))
def test_ids_are_preorder_ordinals(name: str) -> None:
    tree = build_doctree(ALL_VIEWS[name]())
    assert [n.id for n in tree.nodes] == list(range(len(tree.nodes)))
    body_ids = [n.id for n in walk_body(tree)]
    assert body_ids == sorted(body_ids), "body pre-order must be ascending id order"


# ---------------------------------------------------------------------------
# Determinism — the product contract
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(ALL_VIEWS))
def test_building_twice_yields_identical_bytes(name: str) -> None:
    first = dump_tree(build_doctree(ALL_VIEWS[name]()))
    second = dump_tree(build_doctree(ALL_VIEWS[name]()))
    assert first == second


@pytest.mark.parametrize("name", ["two_column", "provider", "seq", "nested_panel"])
def test_bytes_survive_hash_seed_variation_across_processes(
    name: str, tmp_path: Path
) -> None:
    """The three rungs, rebuilt in subprocesses with adversarial PYTHONHASHSEEDs.

    Set/dict iteration order is the classic nondeterminism vector; a builder that leans on
    it produces different pre-orders under different hash seeds, and the sha256-stamped
    artifact stops being content-addressed. Two seeds plus the in-process build must agree
    byte-for-byte.
    """
    view = ALL_VIEWS[name]()
    view_path = tmp_path / f"{name}.view.json"
    view_path.write_text(view.model_dump_json())
    script = tmp_path / "rebuild.py"
    script.write_text(
        "import sys\n"
        "from dpc.models import LayoutView\n"
        "from dpc.doctree.build import build_doctree\n"
        "from dpc.doctree.models import tree_sha256\n"
        "view = LayoutView.model_validate_json(open(sys.argv[1]).read())\n"
        "print(tree_sha256(build_doctree(view)))\n"
    )
    local = tree_sha256(build_doctree(view))
    for seed in ("0", "4242"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(ROOT)}
        out = subprocess.run(
            [sys.executable, str(script), str(view_path)],
            capture_output=True, text=True, env=env, check=True,
        )
        assert out.stdout.strip() == local, f"seed={seed} diverged on {name}"


# ---------------------------------------------------------------------------
# Never raises — degraded output beats no output
# ---------------------------------------------------------------------------
def test_internal_error_degrades_to_a_valid_flat_tree(monkeypatch) -> None:
    import dpc.doctree.build as build_mod

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(build_mod, "_assemble_body", boom)
    view = provider_view()
    tree = build_doctree(view)
    assert tree.passes.geometry == "error(RuntimeError)"
    check = validate_tree(tree, view)
    assert check.ok, check.violations
    assert all(
        n.prov.source is ProvSource.seq_fallback
        for n in walk_body(tree) if n.block_ixs
    )


def test_an_empty_view_yields_the_minimal_tree() -> None:
    view = LayoutView()
    tree = build_doctree(view)
    assert validate_tree(tree, view).ok
    assert [n.kind for n in tree.nodes] == [
        NodeKind.document, NodeKind.body, NodeKind.furniture
    ]
    assert tree.body == 1
    assert tree.furniture == 2


def test_view_sha_is_pinned_and_page_dims_are_mu_ints() -> None:
    view = two_column_view()
    tree = build_doctree(view)
    from dpc.doctree.models import view_sha256

    assert tree.view_sha256 == view_sha256(view)
    assert tree.pages[0].width_mu == 8500
    assert tree.pages[0].height_mu == 11000


# ---------------------------------------------------------------------------
# Never raises, never leaks — the hostile-role and last-resort paths
# ---------------------------------------------------------------------------
def test_hostile_provider_role_is_dropped_never_raised() -> None:
    """A sentence-shaped role fails the model's single-token grammar. The old builder fed
    it to pydantic verbatim: a ValidationError whose message embedded the payload text, and
    one that fired AGAIN inside the flat fallback — straight out of the entrypoint."""
    view = LayoutView(pages=[page()], blocks=[
        block("The first paragraph of the body text.", (0.8, 1.5, 7.8, 1.72),
              role="form field: Jane Q. Public"),
        block("STAMP 14 Rivermill Lane", (0.8, 0.2, 3.4, 0.4),
              zone=Zone.furniture, role="stamp: 14 Rivermill Lane"),
    ])
    tree = build_doctree(view)  # the old code raised ValidationError here
    assert validate_tree(tree, view).ok
    raw = dump_tree(tree).decode()
    assert "Jane" not in raw and "Rivermill" not in raw
    assert all(n.prov.provider_role is None for n in tree.nodes)


def test_a_failing_flat_rung_degrades_to_the_minimal_tree(monkeypatch) -> None:
    """When even the flat rung raises, build_doctree must still return — the three-node
    empty-body tree with the exception's TYPE NAME only, never its message."""
    import dpc.doctree.build as build_mod

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("message carrying Jane Q. Public payload text")

    monkeypatch.setattr(build_mod, "_finalize", boom)  # breaks _build AND _flat_tree
    view = provider_view()
    tree = build_doctree(view)  # the old code raised RuntimeError out of the entrypoint
    assert tree.passes.geometry == "error(RuntimeError)"
    assert [n.kind for n in tree.nodes] == [
        NodeKind.document, NodeKind.body, NodeKind.furniture
    ]
    assert tree.body == 1 and tree.furniture == 2
    raw = dump_tree(tree).decode()
    assert "Jane" not in raw and "payload" not in raw


# ---------------------------------------------------------------------------
# §3.2 step 3 — provider sibling order is verbatim, ACROSS pages too
# ---------------------------------------------------------------------------
def cross_page_provider_view() -> LayoutView:
    """Provider order [section(page 2), section(page 1)] — the legitimate logical-flow !=
    page-order case R19 protects (a summary bound after its appendix, etc.)."""
    blocks = [
        block("Summary of findings first.", (0.8, 1.5, 7.8, 1.72), page=2),
        block("Details of the findings follow.", (0.8, 1.9, 7.8, 2.12), page=2),
        block("Appendix material comes second.", (0.8, 1.5, 7.8, 1.72)),
        block("It stays after the summary.", (0.8, 1.9, 7.8, 2.12)),
    ]
    structure = ProviderStructure(
        root=SectionRef(0, (("section", 1), ("section", 2)), (
            SectionRef(1, (("paragraph", 0), ("paragraph", 1)), ()),
            SectionRef(2, (("paragraph", 2), ("paragraph", 3)), ()),
        )),
        figures=(),
        dangling_dropped=0, double_claims=0, cycles_cut=0,
    )
    return LayoutView(
        pages=[page(1), page(2)], blocks=blocks, raw={"structure": to_raw(structure)},
    )


def test_provider_sibling_order_is_verbatim_across_pages() -> None:
    view = cross_page_provider_view()
    tree = build_doctree(view)
    assert tree.passes.provider_sections == "used(3)"
    assert validate_tree(tree, view).ok
    body_blocks = [n.block_ixs[0] for n in walk_body(tree) if n.block_ixs]
    # Provider array order verbatim: the page-2 section STAYS first (the old builder
    # silently re-sorted the two provider siblings by first page).
    assert body_blocks == [0, 1, 2, 3]
    sections = [n for n in walk_body(tree) if n.kind is NodeKind.section]
    assert [s.prov.provider_ref for s in sections] == ["/sections/1", "/sections/2"]


# ---------------------------------------------------------------------------
# §3.2 step 5 + R19 — the coherence audit, both branches, and the column carve-out
# ---------------------------------------------------------------------------
def _sectioned_view(
    claims_a: tuple[int, ...],
    claims_b: tuple[int, ...],
    blocks: list[TextBlock],
    figures: tuple[FigureRef, ...] = (),
) -> LayoutView:
    structure = ProviderStructure(
        root=SectionRef(0, (("section", 1), ("section", 2)), (
            SectionRef(1, tuple(("paragraph", i) for i in claims_a), ()),
            SectionRef(2, tuple(("paragraph", i) for i in claims_b), ()),
        )),
        figures=figures,
        dangling_dropped=0, double_claims=0, cycles_cut=0,
    )
    return LayoutView(pages=[page()], blocks=blocks, raw={"structure": to_raw(structure)})


def parallel_sections_view() -> LayoutView:
    """Two sections in two side-by-side columns: full band overlap, disjoint columns —
    the shape sections exist for. Demoting it was the audit defect."""
    ys = [(1.5 + 0.4 * i, 1.72 + 0.4 * i) for i in range(5)]
    blocks = [block(f"Left column paragraph {i} text", (0.83, y0, 4.03, y1))
              for i, (y0, y1) in enumerate(ys)]
    blocks += [block(f"Right column paragraph {i} text", (4.58, y0, 7.78, y1))
               for i, (y0, y1) in enumerate(ys)]
    return _sectioned_view((0, 1, 2, 3, 4), (5, 6, 7, 8, 9), blocks)


def test_parallel_column_sections_survive_the_audit() -> None:
    view = parallel_sections_view()
    tree = build_doctree(view)
    assert validate_tree(tree, view).ok
    # The old rule (a) had no column carve-out: it demoted every legitimately parallel
    # column-section pair, silently degrading the provider rung to geometry.
    assert tree.passes.provider_sections == "used(3)"
    sections = [n for n in tree.nodes if n.kind is NodeKind.section]
    assert len(sections) == 2
    assert all(s.prov.source is ProvSource.azure_section for s in sections)


def interleaved_sections_view(
    figures: tuple[FigureRef, ...] = (),
) -> LayoutView:
    """Two sections claiming ALTERNATING rows of one column — provider hierarchy and page
    geometry materially disagree (the true SECT_IOU conflict)."""
    ys = [(1.5 + 0.4 * i, 1.72 + 0.4 * i) for i in range(6)]
    blocks = [block(f"Single column row {i} of the page", (0.83, y0, 5.0, y1))
              for i, (y0, y1) in enumerate(ys)]
    return _sectioned_view((0, 2, 4), (1, 3, 5), blocks, figures)


def test_same_column_interleaved_sections_demote_the_page() -> None:
    view = interleaved_sections_view()
    tree = build_doctree(view)
    assert validate_tree(tree, view).ok
    assert tree.passes.provider_sections == "conflict_demoted(pages=[1])"
    assert not [n for n in tree.nodes if n.kind is NodeKind.section]
    # The freed blocks are rebuilt by the geometry rung, claims stay exact.
    assert all(
        n.prov.source is ProvSource.geometry
        for n in walk_body(tree) if n.block_ixs
    )


def test_same_column_provider_order_inversion_demotes_r19() -> None:
    """R19 branch (b): no band overlap at all, but the provider orders the LOWER section
    first within one column. The old audit compared region ordinals — full-width rows are
    separator singletons, so same-column siblings never matched and the branch was dead."""
    ys = [(1.5 + 0.4 * i, 1.72 + 0.4 * i) for i in range(4)]
    blocks = [block(f"Full width row {i} of this single column page.", (0.83, y0, 7.78, y1))
              for i, (y0, y1) in enumerate(ys)]
    view = _sectioned_view((2, 3), (0, 1), blocks)  # section 1 claims the LOWER rows
    tree = build_doctree(view)
    assert validate_tree(tree, view).ok
    assert tree.passes.provider_sections == "conflict_demoted(pages=[1])"
    body_blocks = [n.block_ixs[0] for n in walk_body(tree) if n.block_ixs]
    assert body_blocks == [0, 1, 2, 3]  # geometry order, top to bottom


def test_demoted_figures_are_not_counted_as_used() -> None:
    """The manifest counts figure NODES that survived demotion, not minted ids — a demoted
    page's figure is removed and never rebuilt, and used(N) must not claim otherwise."""
    fig = FigureRef(0, 1, tuple(quad(5.5, 1.5, 7.0, 2.5)), None, None, None)
    view = interleaved_sections_view(figures=(fig,))
    tree = build_doctree(view)
    assert validate_tree(tree, view).ok
    assert tree.passes.provider_sections == "conflict_demoted(pages=[1])"
    assert not [n for n in tree.nodes if n.kind is NodeKind.figure]
    assert tree.passes.provider_figures == "used(0)"  # the old manifest said used(1)


# ---------------------------------------------------------------------------
# §3.2 step 4 — recursive XY-cut inside a frame
# ---------------------------------------------------------------------------
def test_nested_panel_is_recursively_cut_column_major() -> None:
    view = nested_panel_view()
    tree = build_doctree(view)
    assert validate_tree(tree, view).ok
    assert tree.passes.geometry == "ran"
    groups = [n for n in tree.nodes if n.kind is NodeKind.flow_group]
    assert len(groups) == 2, "the page group AND the nested panel group"
    outer = next(g for g in groups if tree.nodes[g.parent].kind is NodeKind.body)
    left_frame = tree.nodes[outer.children[0]]
    inner = next(
        tree.nodes[c] for c in left_frame.children
        if tree.nodes[c].kind is NodeKind.flow_group
    )
    assert tree.nodes[inner.parent].kind is NodeKind.frame
    sub_frames = [tree.nodes[c] for c in inner.children]
    assert [f.kind for f in sub_frames] == [NodeKind.frame, NodeKind.frame]
    # Column-major: ALL of sub-left (blocks 4-7) before ALL of sub-right (8-11). The flat
    # band-order builder interleaved them row by row (4, 8, 5, 9, ...).
    body_blocks = [n.block_ixs[0] for n in walk_body(tree) if n.block_ixs]
    assert body_blocks == list(range(20))
    assert [tree.nodes[c].block_ixs[0] for c in sub_frames[0].children] == [4, 5, 6, 7]
    assert [tree.nodes[c].block_ixs[0] for c in sub_frames[1].children] == [8, 9, 10, 11]


# ---------------------------------------------------------------------------
# §3.4 RTL_MAJ — the majority is over atoms that HAVE a direction
# ---------------------------------------------------------------------------
def rtl_marks_view() -> LayoutView:
    """A two-column Hebrew page dense with selection marks. Every line is RTL; the old
    denominator counted the direction-less mark atoms too, so the page missed its
    majority and read left-to-right."""
    from dpc.models import Mark

    ys = [(1.5 + 0.4 * i, 1.72 + 0.4 * i) for i in range(5)]
    blocks = [block(f"מסמך עברי שורה {i}", (0.83, y0, 4.03, y1))
              for i, (y0, y1) in enumerate(ys)]
    blocks += [block(f"עמודה שניה שורה {i}", (4.58, y0, 7.78, y1))
               for i, (y0, y1) in enumerate(ys)]
    marks = [
        Mark(state="selected", page=1,
             bbox=quad(0.9 + 0.35 * (i % 4), 3.6 + 0.3 * (i // 4),
                       1.02 + 0.35 * (i % 4), 3.72 + 0.3 * (i // 4)))
        for i in range(11)
    ]
    return LayoutView(pages=[page()], blocks=blocks, marks=marks)


def test_rtl_majority_is_over_line_atoms_only() -> None:
    view = rtl_marks_view()
    tree = build_doctree(view)
    assert validate_tree(tree, view).ok
    group = next(n for n in tree.nodes if n.kind is NodeKind.flow_group)
    frames = [tree.nodes[c] for c in group.children
              if tree.nodes[c].kind is NodeKind.frame]
    first_blocks = [
        tree.nodes[c].block_ixs[0] for c in frames[0].children
        if tree.nodes[c].block_ixs
    ]
    # Majority-RTL page: the RIGHT column (blocks 5-9) is read first.
    assert first_blocks == [5, 6, 7, 8, 9]
