"""The flattener (SPEC-DOCTREE-1 §5), tested against the claims that make PMD 3.0 trustworthy.

Four load-bearing properties, in the spec's own order:

1. **No text loss, exactly once** (§8.3): every block/cell/kv/mark text appears exactly once
   in the flattened file — canvas fence rows count as covering text for their atoms, with the
   same accounting as the coverage gate.
2. **Determinism**: same ``(tree, view, args)`` -> same bytes, including through a
   ``dump_tree`` -> ``model_validate_json`` round-trip of the tree.
3. **Reading-order fidelity** (§8.4): hand-labelled ``fixtures/order/*.order.json`` vs the
   emitted ``path=`` sequence, Kendall tau exactly 1.0.
4. **Flat ≡ 2.0** (§8.5): a flat/linear tree flattens element-for-element equal to the 2.0
   rendering, anchors differing only by the appended `` path=`` clause.

View builders are imported from ``test_doctree_build`` (the precedent set by ``test_geom``
importing ``test_emitter``), so the flattener is tested against exactly the trees the builder
suite already pins.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest
from test_doctree_build import (
    ALL_VIEWS,
    COLUMN_ROWS,
    block,
    node_by_blocks,
    page,
    quad,
)

from dpc import treemd
from dpc.doctree.build import build_doctree
from dpc.doctree.models import DocTree, NodeKind, dump_tree, tree_sha256
from dpc.emitter import to_pmd
from dpc.models import KeyValue, LayoutView, Mark, TextBlock, Zone
from dpc.treemd import FlattenReport, flatten

ORDER_DIR = Path(__file__).resolve().parent / "fixtures" / "order"


# ---------------------------------------------------------------------------
# Extra views: a fenced form panel and a two-page document
# ---------------------------------------------------------------------------
def fence_view() -> LayoutView:
    """A two-column panel with a MARK inside it (fence trigger, §5.3) plus two key-values:
    one whose key and value are both atom texts (covered => suppressed) and one whose text is
    on no canvas (additive => rendered)."""
    blocks = [block("CONFIDENTIAL BANK FORM", (0.83, 0.69, 7.78, 1.04), zone=Zone.title)]
    for left, right, y0, y1 in COLUMN_ROWS:
        blocks.append(block(left, (0.83, y0, 4.03, y1)))
        blocks.append(block(right, (4.58, y0, 7.78, y1)))
    return LayoutView(
        pages=[page()],
        blocks=blocks,
        marks=[Mark(state="selected", page=1, bbox=quad(0.9, 3.10, 1.05, 3.22))],
        key_values=[
            KeyValue(key="Statement Period", value="01 Jun 2026 - 30 Jun 2026", page=1,
                     key_bbox=quad(4.58, 1.53, 7.78, 1.78),
                     value_bbox=quad(4.58, 1.88, 7.78, 2.10)),
            KeyValue(key="Ref No", value="X-99", page=1,
                     key_bbox=quad(0.9, 2.48, 1.8, 2.7),
                     value_bbox=quad(2.0, 2.48, 3.0, 2.7)),
        ],
    )


def two_page_view() -> LayoutView:
    return LayoutView(pages=[page(1), page(2)], blocks=[
        block("First page prose paragraph here.", (0.8, 1.5, 7.8, 1.72)),
        block("Second page prose paragraph too.", (0.8, 1.5, 7.8, 1.72), page=2),
    ])


def heading_seq_view() -> LayoutView:
    """A geometry-less view with a heading — the flat rung's ``##`` depth floor (§5.2)."""
    return LayoutView(blocks=[
        TextBlock(text="Section head", zone=Zone.heading, seq=0),
        TextBlock(text="A body paragraph follows.", seq=1),
    ])


def empty_heading_view() -> LayoutView:
    """An EMPTY-text heading block: 2.0 emits a bare ``## `` (md truthy), and §8.5 demands
    3.0 does too — the gate is md truthiness, never the text."""
    return LayoutView(blocks=[
        TextBlock(text="", zone=Zone.heading, seq=0),
        TextBlock(text="Body paragraph.", seq=1),
    ])


def fence_furniture_view() -> LayoutView:
    """fence_view plus a MIS-ZONED furniture stamp INSIDE the fenced panel's band range:
    the canvas paints it as an atom, so the furniture section must NOT re-emit it (§8.3)."""
    view = fence_view()
    view.blocks.append(block("STAMP 77", (4.6, 2.18, 5.4, 2.40),
                             zone=Zone.furniture, role="pageNumber"))
    return view


MULTISET_VIEWS = dict(ALL_VIEWS, fence=fence_view, two_page=two_page_view,
                      heading_seq=heading_seq_view, empty_heading=empty_heading_view,
                      fence_furniture=fence_furniture_view)


def render(view: LayoutView, **kw: object) -> tuple[str, FlattenReport]:
    tree = build_doctree(view)
    return flatten(tree, view, generated="2026-01-01T00:00:00Z", **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Markdown parsing helpers (test-side, deliberately independent of the emitter)
# ---------------------------------------------------------------------------
def norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def body_lines(md: str) -> list[str]:
    lines = md.split("\n")
    assert lines[0] == "---"
    return lines[lines.index("---", 1) + 1:]


def parse_elements(md: str) -> tuple[list[tuple[str | None, str]], list[list[str]]]:
    """``(elements, fences)``: elements as ``(anchor|None, markdown)`` in emission order
    (tables kept whole, multi-line); fences as raw row lists. Page markers, the furniture
    marker, canvas anchors and legend lines are structure, not elements."""
    lines = body_lines(md)
    elements: list[tuple[str | None, str]] = []
    fences: list[list[str]] = []
    anchor: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line == "":
            i += 1
            continue
        opened = re.match(r"^(`{3,})text$", line)
        if opened:
            rows: list[str] = []
            i += 1
            while i < len(lines) and lines[i] != opened.group(1):
                rows.append(lines[i])
                i += 1
            fences.append(rows)
            anchor = None
            i += 1
            continue
        if line.startswith("<!--"):
            is_anchor = line.startswith("<!-- @")
            if is_anchor and " canvas " not in line and " cell=" not in line:
                anchor = line
            i += 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(lines[i])
                i += 1
            elements.append((anchor, "\n".join(rows)))
            anchor = None
            continue
        elements.append((anchor, line))
        anchor = None
        i += 1
    return elements, fences


def element_texts(elements: list[tuple[str | None, str]]) -> list[str]:
    """Every element's document text(s), normalised — the emitted half of §8.3's multiset."""
    out: list[str] = []
    for _, md in elements:
        if md.startswith("|"):
            for row in md.split("\n"):
                core = row.strip().strip("|")
                if set(core) <= set(" -|"):
                    continue  # the GFM separator row
                for cell in re.split(r"(?<!\\)\|", core):
                    text = norm(cell.replace("\\|", "|").replace("<br>", " "))
                    if text:
                        out.append(text)
            continue
        kv = re.match(r"^\*\*(.*?):\*\* ?(.*)$", md)
        if kv:
            out.append(norm(kv.group(1)))
            if norm(kv.group(2)):
                out.append(norm(kv.group(2)))
            continue
        if md in ("- [x]", "- [ ]"):
            out.append(md[2:])
            continue
        if md.startswith("!["):
            continue  # figure placeholder: a URI, not document text
        caption = re.match(r"^\*([^*].*)\*$", md)
        if caption:
            out.append(norm(caption.group(1)))
            continue
        heading = re.match(r"^#+ (.*)$", md)
        if heading:
            text = heading.group(1)
            if text.startswith("\\#"):
                text = text[1:]
            out.append(norm(text))
            continue
        out.append(norm(md))
    return [t for t in out if t]


def expected_texts(view: LayoutView) -> Counter[str]:
    """§8.3's expected multiset: non-table-zone blocks + cells + kv keys/values + marks."""
    out: list[str] = []
    for b in view.blocks:
        if b.zone is Zone.table:
            continue
        if norm(b.text):
            out.append(norm(b.text))
    for table in view.tables:
        for row in table.grid():
            out.extend(norm(cell) for cell in row if norm(cell))
    for kv in view.key_values:
        for text in (kv.key, kv.value):
            if norm(text):
                out.append(norm(text))
    for mark in view.marks:
        out.append("[x]" if mark.selected else "[ ]")
    return Counter(out)


def covered_texts(tree: DocTree, view: LayoutView) -> Counter[str]:
    """Texts whose home is a canvas fence: atoms of fenced flow_groups, suppressed kvs,
    and furniture leaves whose blocks the fence paints.

    Re-derives the fence plan through the module's own planner, then applies §5.2's
    containment rule for kv suppression and the atom-membership rule for furniture
    suppression — the accounting the multiset test needs to know which texts must live
    INSIDE a fence rather than as elements.
    """
    fenced, canvas_text, canvas_blocks, _ = treemd._fence_plan(tree, view)
    out: list[str] = []
    for gid in fenced:
        stack = list(tree.nodes[gid].children)
        while stack:
            node = tree.nodes[stack.pop()]
            stack.extend(node.children)
            for bix in node.block_ixs:
                if norm(view.blocks[bix].text):
                    out.append(norm(view.blocks[bix].text))
            if node.mark_ix is not None:
                out.append("[x]" if view.marks[node.mark_ix].selected else "[ ]")
    for node in tree.nodes:
        if node.kind is not NodeKind.kv_pair or node.kv_ix is None:
            continue
        if node.prov.region_ix is None:
            continue
        on_canvas = canvas_text.get((node.page, node.prov.region_ix))
        if on_canvas is None:
            continue
        kv = view.key_values[node.kv_ix]
        if any(norm(kv.key) in t for t in on_canvas) and any(
            norm(kv.value) in t for t in on_canvas
        ):
            out.extend(t for t in (norm(kv.key), norm(kv.value)) if t)
    for nid in tree.nodes[tree.furniture].children:
        node = tree.nodes[nid]
        if not node.block_ixs:
            continue
        painted = [b for (pg, _rix), b in canvas_blocks.items() if pg == node.page]
        if all(any(bix in b for b in painted) for bix in node.block_ixs):
            out.extend(
                norm(view.blocks[bix].text)
                for bix in node.block_ixs if norm(view.blocks[bix].text)
            )
    return Counter(out)


def emitted_paths(md: str) -> list[str]:
    return [m for m in re.findall(r" path=(\S+) -->", md) if m.startswith("//doc/body")]


def strip_path(anchor: str | None) -> str | None:
    if anchor is None:
        return None
    return re.sub(r" path=\S+ -->$", " -->", anchor)


def kendall_tau(expected: list[str], emitted: list[str]) -> float:
    assert sorted(expected) == sorted(emitted), "order fixtures must cover the same nodes"
    pos = {path: ix for ix, path in enumerate(emitted)}
    concordant = discordant = 0
    for a in range(len(expected)):
        for b in range(a + 1, len(expected)):
            if pos[expected[a]] < pos[expected[b]]:
                concordant += 1
            else:
                discordant += 1
    pairs = concordant + discordant
    return 1.0 if pairs == 0 else (concordant - discordant) / pairs


# ---------------------------------------------------------------------------
# 1. No text loss — exactly once (§8.3)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(MULTISET_VIEWS))
def test_no_text_loss_exactly_once(name: str) -> None:
    """Every element text appears exactly once: as one element, or covered by one fence —
    in BOTH directions. The forward equation catches drops; the reverse check catches a
    text that is emitted as an element AND painted inside a fence (the furniture
    double-emission class the old test structurally could not see)."""
    view = MULTISET_VIEWS[name]()
    tree = build_doctree(view)
    md, report = flatten(tree, view, generated="T")
    assert report.error is None
    elements, fences = parse_elements(md)
    covered = covered_texts(tree, view)
    emitted = Counter(element_texts(elements))
    assert emitted + covered == expected_texts(view)
    fence_rows = [norm(row) for rows in fences for row in rows]
    for text in covered:
        assert any(text in row for row in fence_rows), f"{name}: fence must cover {text!r}"
    for text in emitted:
        assert not any(text in row for row in fence_rows), (
            f"{name}: {text!r} emitted as an element AND painted in a fence"
        )


# ---------------------------------------------------------------------------
# 2. Determinism
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["provider", "two_column", "fence", "seq"])
def test_deterministic_bytes_and_model_round_trip(name: str) -> None:
    view = MULTISET_VIEWS[name]()
    tree = build_doctree(view)
    first, _ = flatten(tree, view, generated="T", extra={"sha256_input": "ab" * 32})
    second, _ = flatten(tree, view, generated="T", extra={"sha256_input": "ab" * 32})
    assert first == second
    revived = DocTree.model_validate_json(dump_tree(tree))
    third, report = flatten(revived, view, generated="T", extra={"sha256_input": "ab" * 32})
    assert report.error is None
    assert third == first


# ---------------------------------------------------------------------------
# 3. Reading-order fidelity (§8.4) — hand-labelled fixtures, tau == 1.0
# ---------------------------------------------------------------------------
#: Hand-labelled path -> text bindings for the order fixtures. Paths are minted FROM the
#: emitted order, so the ``order`` lists alone cannot catch a builder that visits content
#: in the wrong order and simply renames it (right column becoming ``frame[1]``): these
#: bindings pin what each labelled path must SAY, making the label constrain content.
ORDER_TEXTS: dict[str, dict[str, str]] = {
    "provider": {
        "//doc/body[1]/h[1]": "Annual Report",
        "//doc/body[1]/sect[1]/h[1]": "Introduction",
        "//doc/body[1]/sect[1]/p[1]": "This report presents the results.",
        "//doc/body[1]/sect[2]/h[1]": "Methods",
        "//doc/body[1]/sect[2]/p[1]": "We measured everything twice.",
        "//doc/body[1]/sect[2]/fig[1]/cap[1]": "Fig 1: a chart of the results",
    },
    "two_column": {
        "//doc/body[1]/sect[1]/h[1]": "QUARTERLY ACCOUNT STATEMENT",
        "//doc/body[1]/sect[1]/fg[1]/frame[1]/p[1]": "Account Holder",
        "//doc/body[1]/sect[1]/fg[1]/frame[1]/p[2]": "Jane Q. Public",
        "//doc/body[1]/sect[1]/fg[1]/frame[1]/p[3]": "14 Rivermill Lane",
        "//doc/body[1]/sect[1]/fg[1]/frame[1]/p[4]": "Portland, OR 97205",
        "//doc/body[1]/sect[1]/fg[1]/frame[1]/p[5]": "Account  ****-****-4417",
        "//doc/body[1]/sect[1]/fg[1]/frame[2]/p[1]": "Statement Period",
        "//doc/body[1]/sect[1]/fg[1]/frame[2]/p[2]": "01 Jun 2026 - 30 Jun 2026",
        "//doc/body[1]/sect[1]/fg[1]/frame[2]/p[3]": "Currency  USD",
        "//doc/body[1]/sect[1]/fg[1]/frame[2]/p[4]": "Closing Balance",
        "//doc/body[1]/sect[1]/fg[1]/frame[2]/p[5]": "USD 12,480.55",
    },
    "footnote": {
        "//doc/body[1]/sect[1]/h[1]": "Chapter One",
        "//doc/body[1]/sect[1]/p[1]": "The first paragraph of the chapter.",
        "//doc/body[1]/sect[1]/p[2]": "The second paragraph continues.",
        "//doc/body[1]/sect[1]/fn[1]": "1. See the annex for details.",
    },
}


@pytest.mark.parametrize("name", ["provider", "two_column", "footnote"])
def test_reading_order_matches_hand_labelled_fixture(name: str) -> None:
    labelled = json.loads((ORDER_DIR / f"{name}.order.json").read_text())
    view = ALL_VIEWS[name]()
    md, report = render(view)
    assert report.error is None
    assert kendall_tau(labelled["order"], emitted_paths(md)) == 1.0
    # The label constrains CONTENT, not just shape: every non-figure path in the fixture
    # must render the hand-labelled text — a builder visiting the columns right-before-left
    # would keep tau == 1.0 (its paths rename to match) but fail here.
    bindings = ORDER_TEXTS[name]
    unbound = [p for p in labelled["order"] if p not in bindings and "/fig[" not in p]
    assert not unbound, f"order fixture path without a text binding: {unbound}"
    elements, _ = parse_elements(md)
    by_path: dict[str, str] = {}
    for anchor, element_md in elements:
        if anchor is None:
            continue
        match = re.search(r" path=(\S+) -->", anchor)
        if match:
            by_path[match.group(1)] = element_md
    for path, text in bindings.items():
        assert path in by_path, f"{name}: labelled path {path} not emitted with an anchor"
        assert element_texts([(None, by_path[path])]) == [norm(text)], (
            f"{name}: {path} does not render the hand-labelled text"
        )


# ---------------------------------------------------------------------------
# 4. Flat/linear tree ≡ PMD 2.0 rendering (§8.5)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["seq", "order_tie", "heading_seq", "empty_heading"])
def test_linear_tree_flattens_to_the_2_0_element_sequence(name: str) -> None:
    """Element-for-element text AND anchor equality with 2.0; anchors differ only by path=."""
    view = MULTISET_VIEWS[name]()
    two = to_pmd(view, source="azure_layout", provider="test", generated="T")
    three, report = render(view, source="azure_layout", provider="test")
    assert report.error is None
    two_elements, _ = parse_elements(two)
    three_elements, _ = parse_elements(three)
    assert [(strip_path(a), m) for a, m in three_elements] == two_elements


# ---------------------------------------------------------------------------
# Refusals — typed statuses, never exceptions
# ---------------------------------------------------------------------------
def test_view_sha_mismatch_is_refused() -> None:
    view = ALL_VIEWS["provider"]()
    tree = build_doctree(view)
    other = view.model_copy(deep=True)
    other.blocks.append(TextBlock(text="an extra block the tree never saw"))
    md, report = flatten(tree, other, generated="T")
    assert md == ""
    assert report.error == "TreeInvalid:view_sha_mismatch"


def test_invalid_tree_is_refused_with_the_invariant_name() -> None:
    view = ALL_VIEWS["provider"]()
    tree = build_doctree(view).model_copy(deep=True)
    victim = next(n for n in tree.nodes if n.block_ixs)
    victim.block_ixs = []  # an unclaimed block: the census (I3) must fail closed.
    md, report = flatten(tree, view, generated="T")
    assert md == ""
    assert report.error == "TreeInvalid:I3:blocks"


def test_flatten_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    view = ALL_VIEWS["provider"]()
    tree = build_doctree(view)

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("no document text in this message")

    monkeypatch.setattr(treemd, "_fence_plan", boom)
    md, report = flatten(tree, view, generated="T")
    assert md == ""
    assert report.error == "error:RuntimeError"


# ---------------------------------------------------------------------------
# Front matter (§5.4)
# ---------------------------------------------------------------------------
def test_front_matter_fixed_order_and_audit_spine() -> None:
    view = ALL_VIEWS["provider"]()
    tree = build_doctree(view)
    md, _ = flatten(
        tree, view, source="azure_layout", provider="azure_layout_v4", generated="T",
        decided_by="heuristics+patch@deadbeef", extra={"sha256_input": "cd" * 32},
    )
    lines = md.split("\n")
    front = lines[1:lines.index("---", 1)]
    keys = [line.split(":", 1)[0] for line in front]
    assert keys == [
        "pmd", "generator", "order", "tree_source", "decided_by", "sha256_tree",
        "doc_id", "source", "provider", "pages", "blocks", "tables", "marks",
        "key_values", "chars", "figures", "furniture_nodes", "passes", "generated",
        "sha256_input",
    ]
    assert "pmd: 3.0" in front
    assert "order: tree" in front
    assert "tree_source: provider_sections" in front
    assert "decided_by: heuristics+patch@deadbeef" in front
    assert f"sha256_tree: {tree_sha256(tree)}" in front
    assert "passes: sections,geometry" in front


def test_unicode_line_only_when_a_fence_exists() -> None:
    """2.0's rule carried over: only canvas bytes depend on the East-Asian-Width tables."""
    no_fence, _ = render(ALL_VIEWS["two_column"]())
    with_fence, _ = render(fence_view())
    assert "\nunicode: " not in no_fence
    assert "\nunicode: " in with_fence


def test_tree_source_reports_the_rung_that_built_the_tree() -> None:
    cases = {"provider": "provider_sections", "two_column": "geometry", "seq": "flat"}
    for name, source in cases.items():
        md, _ = render(ALL_VIEWS[name]())
        assert f"tree_source: {source}" in md.split("\n"), name


# ---------------------------------------------------------------------------
# Rendering semantics (§5.2 / §5.3)
# ---------------------------------------------------------------------------
def test_fence_covers_the_form_panel_and_suppresses_covered_kvs() -> None:
    view = fence_view()
    tree = build_doctree(view)
    md, report = flatten(tree, view, generated="T")
    assert "```text" in md
    _, fences = parse_elements(md)
    blob = [norm(row) for rows in fences for row in rows]
    for text in ("Account Holder", "USD 12,480.55", "[x]"):
        assert any(norm(text) in row for row in blob)
    # The covered pair is suppressed (its text IS the canvas), the additive one survives.
    assert "**Statement Period:**" not in md
    assert "**Ref No:** X-99" in md
    assert report.kv_in_canvas == 1
    # The segment anchor carries the flow_group's path — the audit link for the whole panel.
    assert re.search(r"canvas .* path=//doc/body\[1\]/sect\[1\]/fg\[1\] -->", md)


def test_fenced_region_is_opaque_no_text_loss_through_a_patched_tree() -> None:
    """§5.3 defense-in-depth: a paragraph moved INTO a fence-rendered flow_group (a patch
    the verifier's V4 boundary should never accept, applied here directly to bypass it)
    must not lose its text. The fence plan refuses to fence a subtree holding an unpainted
    leaf and linearizes instead — before/after multisets stay equal, nothing vanishes."""
    from dpc.doctree.patch import apply_patch

    view = fence_view()
    view.blocks.append(
        block("This closing remark sits under the panel.", (0.83, 4.2, 7.78, 4.45))
    )
    tree = build_doctree(view)
    outside = next(
        n for n in tree.nodes
        if n.block_ixs and "closing remark" in view.blocks[n.block_ixs[0]].text
    )
    fg = next(n for n in tree.nodes if n.kind is NodeKind.flow_group)
    frame = tree.nodes[fg.children[0]]
    inside = next(
        tree.nodes[c] for c in frame.children
        if tree.nodes[c].kind is NodeKind.paragraph
    )
    patched, joins = apply_patch(tree, [{
        "op": "move_before", "node": outside.path, "ref": inside.path,
        "reason": "ORDER_INVERSION",
    }])
    before, before_report = flatten(tree, view, generated="T")
    after, after_report = flatten(patched, view, generated="T", flow_joins=joins)
    assert before_report.error is None and after_report.error is None
    assert before.count("closing remark") == 1
    assert after.count("closing remark") == 1  # the old fence silently DELETED it (0)
    # The refused fence linearizes: every text is now an element, none is lost.
    elements, fences = parse_elements(after)
    assert fences == []
    assert Counter(element_texts(elements)) == expected_texts(view)


def test_furniture_painted_in_a_fence_is_suppressed_and_counted() -> None:
    """§8.3 exactly-once: a mis-zoned furniture block that the canvas paints as a region
    atom renders ONCE (in the fence), with the suppression counted — 2.0's behaviour."""
    view = fence_furniture_view()
    tree = build_doctree(view)
    md, report = flatten(tree, view, generated="T")
    assert report.error is None
    assert md.count("STAMP 77") == 1  # the old furniture section re-emitted it (2)
    assert report.furniture_in_canvas == 1
    assert report.furniture_nodes == 0
    assert "<!-- furniture -->" not in md  # nothing left to demote


def test_furniture_outside_every_fence_still_renders() -> None:
    """The suppression is atom-membership, not page-wide: furniture on a fence page whose
    blocks the region does NOT paint keeps its furniture-section rendering. (The prose
    paragraph below the panel keeps the footer out of the spatial region's atoms.)"""
    view = fence_view()
    view.blocks.append(
        block("This closing remark sits under the panel.", (0.83, 4.2, 7.78, 4.45))
    )
    view.blocks.append(block("Page 1 of 2", (3.9, 10.5, 4.6, 10.7),
                             zone=Zone.furniture, role="pageNumber"))
    tree = build_doctree(view)
    _fenced, _text, canvas_blocks, _layouts = treemd._fence_plan(tree, view)
    footer_ix = len(view.blocks) - 1
    assert all(footer_ix not in blocks for blocks in canvas_blocks.values())
    md, report = flatten(tree, view, generated="T")
    assert report.error is None
    assert md.count("Page 1 of 2") == 1
    assert report.furniture_in_canvas == 0
    assert report.furniture_nodes == 1
    assert "<!-- furniture -->" in md


def skew_quad(x0: float, y0: float, x1: float, y1: float, dy: float) -> list[float]:
    return [x0, y0, x1, y0 + dy, x1, y1 + dy, x0, y1]


def test_tree_source_flat_when_geometry_only_declined() -> None:
    """§5.4: an all-declined document places every node by seq_fallback — the audit spine
    must say ``flat``, not claim the geometry rung that placed nothing."""
    view = LayoutView(
        pages=[page()],
        blocks=[TextBlock(text=f"skewed line {i}",
                          bbox=skew_quad(0.8, 1.0 + i, 7.7, 1.25 + i, 0.9), lines=[])
                for i in range(4)],
    )
    tree = build_doctree(view)
    assert tree.passes.geometry.startswith("declined")
    md, report = flatten(tree, view, generated="T")
    assert report.error is None
    assert "tree_source: flat" in md.split("\n")


def test_tree_source_geometry_when_declined_only_on_some_pages() -> None:
    """The mixed case: page 1 declines (skew) but page 2's nodes are geometry-placed —
    the rung claim stays ``geometry``."""
    view = LayoutView(
        pages=[page(1), page(2)],
        blocks=(
            [TextBlock(text=f"skewed line {i}",
                       bbox=skew_quad(0.8, 1.0 + i, 7.7, 1.25 + i, 0.9), lines=[])
             for i in range(4)]
            + [block("Left column paragraph on page two.", (0.83, 1.53, 4.03, 1.78),
                     page=2),
               block("Right column paragraph on page two.", (4.58, 1.53, 7.78, 1.78),
                     page=2)]
        ),
    )
    tree = build_doctree(view)
    assert tree.passes.geometry.startswith("declined")
    md, report = flatten(tree, view, generated="T")
    assert report.error is None
    assert "tree_source: geometry" in md.split("\n")


def test_continues_edge_renders_without_intervening_blank_line() -> None:
    """The builder linked the two columns (frame tail -> next frame head); the pair renders
    adjacently — a rendering adjacency only, text untouched (R11, dehyphenation off)."""
    view = ALL_VIEWS["two_column"]()
    tree = build_doctree(view)
    assert tree.flow, "fixture must produce a continues edge"
    md, _ = flatten(tree, view, generated="T")
    src = tree.nodes[tree.flow[0].src]
    src_text = view.blocks[src.block_ixs[0]].text
    lines = md.split("\n")
    after = lines[lines.index(src_text) + 1]
    assert after.startswith("<!-- @"), "no blank line may separate a continues pair"


def test_flow_joins_argument_glues_like_a_continues_edge() -> None:
    view = ALL_VIEWS["footnote"]()
    tree = build_doctree(view)
    p1 = node_by_blocks(tree, 1).id
    p2 = node_by_blocks(tree, 3).id
    plain, _ = flatten(tree, view, generated="T")
    joined, _ = flatten(tree, view, generated="T", flow_joins=frozenset({(p1, p2)}))
    src_text = view.blocks[1].text
    plain_lines = plain.split("\n")
    joined_lines = joined.split("\n")
    assert plain_lines[plain_lines.index(src_text) + 1] == ""
    assert joined_lines[joined_lines.index(src_text) + 1].startswith("<!-- @")


def test_furniture_renders_once_after_the_body_marked_and_counted() -> None:
    view = ALL_VIEWS["provider"]()
    md, report = render(view)
    lines = md.split("\n")
    marker = lines.index("<!-- furniture -->")
    assert "Page 1 of 9" in lines[marker:]
    assert "ACME CORP CONFIDENTIAL" in lines[marker:]
    header_anchor = next(line for line in lines[marker:] if "furniture:pageHeader" in line)
    assert " path=//doc/furn[1]/p[" in header_anchor  # true page/rect kept, demoted not lost
    assert report.furniture_nodes == 2
    assert "furniture_nodes: 2" in lines[:marker]


def test_figure_placeholder_uri_and_caption() -> None:
    view = ALL_VIEWS["provider"]()
    md, report = render(view)
    assert "![figure fig-1-1](figure://doc-provider-1/fig-1-1)" in md.split("\n")
    assert "*Fig 1: a chart of the results*" in md.split("\n")
    assert report.figures == 1
    assert "figures: 1" in md.split("\n")


def test_page_markers_appear_at_first_body_visit() -> None:
    md, report = render(two_page_view())
    lines = body_lines(md)
    first = next(ix for ix, line in enumerate(lines) if line.startswith("<!-- page 1 "))
    second = next(ix for ix, line in enumerate(lines) if line.startswith("<!-- page 2 "))
    assert first < lines.index("First page prose paragraph here.") < second
    assert second < lines.index("Second page prose paragraph too.")
    assert report.pages_visited == 2


def test_footnote_renders_at_section_tail_with_footnote_tag() -> None:
    view = ALL_VIEWS["footnote"]()
    md, _ = render(view)
    elements, _ = parse_elements(md)
    assert elements[-1][1] == "1. See the annex for details."
    anchor = elements[-1][0]
    assert anchor is not None and "] footnote path=" in anchor


def test_heading_depth_title_reserved_and_flat_floor() -> None:
    """`#` only for title; a section-less sectionHeading keeps 2.0's `##` (the depth floor);
    provider nesting deepens by section depth."""
    provider_md, _ = render(ALL_VIEWS["provider"]())
    assert "# Annual Report" in provider_md.split("\n")
    assert "## Introduction" in provider_md.split("\n")
    flat_md, _ = render(heading_seq_view())
    assert "## Section head" in flat_md.split("\n")


def test_report_counts_are_the_emission_truth() -> None:
    view = fence_view()
    md, report = render(view)
    elements, fences = parse_elements(md)
    assert report.elements_emitted == len(elements) + len(fences)
    assert report.pages_visited == 1
    assert report.furniture_nodes == 0
    assert report.error is None
