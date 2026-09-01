"""Feature projection + windowing (SPEC-DOCTREE-1 §4.2/§4.3) — the PII boundary's tests.

The view builders here are shared by the other ``test_arrange_*`` modules (the same pattern
``test_geom`` uses with ``test_emitter``): one two-column prose page (the flagship
cross-column continuation), one KYC form filled with two synthetic identities (§8.6's
byte-identity fixture, WITH the right-aligned filled field R7 demands), one cross-page
single-column letter (the R6 window-seam case), and degenerate views for the skip reasons.

The two adversarial tests here are the kill switches risk P3-d names: any future feature
addition that distinguishes two identities on one template, or that lets a document n-gram
into the serialized window, fails HERE — dead on arrival, not on review.
"""
from __future__ import annotations

import itertools
import json

from dpc.arrange.features import (
    CharCountClass,
    LineCountClass,
    WClass,
    _char_class,
    _grid_pm,
    _line_class,
    _w_class,
    build_features,
)
from dpc.arrange.payload import CONTEXT_CARRY, WindowPayload, make_windows
from dpc.doctree.build import build_doctree
from dpc.doctree.models import NodeKind
from dpc.models import LayoutView, PageInfo, TextBlock, TextLine, Zone

# ---------------------------------------------------------------------------
# Shared view builders (importable by the other test_arrange_* modules)
# ---------------------------------------------------------------------------


def quad(x0: float, y0: float, x1: float, y1: float) -> list[float]:
    return [x0, y0, x1, y0, x1, y1, x0, y1]


def block(text: str, box: tuple[float, float, float, float], **kw: object) -> TextBlock:
    """A block carrying its own single line — the Azure-layout shape the canvas needs."""
    q = quad(*box)
    return TextBlock(text=text, bbox=q, lines=[TextLine(text=text, bbox=q)], **kw)  # type: ignore[arg-type]


def page(n: int = 1) -> PageInfo:
    return PageInfo(page=n, width=8.5, height=11.0, unit="inch")


#: Two-column prose: the left column's last paragraph hyphen-splits a word that the right
#: column's first paragraph continues in lowercase — the flagship merge_flow case.
PROSE_LEFT = [
    ("The council met on Tuesday to review the harbor plan.", 1.5),
    ("Members debated the funding schedule at great length.", 2.4),
    ("Several amendments were proposed by visiting delegates.", 3.3),
    ("The final vote was postponed until the next assem-", 4.2),
]
PROSE_RIGHT = [
    ("bly, when the full budget will be published.", 1.5),
    ("Residents may submit comments in writing.", 2.4),
    ("Each submission receives a numbered receipt.", 3.3),
    ("A public hearing follows in October.", 4.2),
]


def prose_view() -> LayoutView:
    """Title + two 4-row columns; the canvas yields one flow_group with two frames."""
    blocks = [block("COMMUNITY UPDATE BULLETIN", (0.8, 0.7, 7.7, 0.95), zone=Zone.title)]
    for text, y0 in PROSE_LEFT:
        blocks.append(block(text, (0.8, y0, 3.5, y0 + 0.25)))
    for text, y0 in PROSE_RIGHT:
        blocks.append(block(text, (5.0, y0, 7.7, y0 + 0.25)))
    return LayoutView(doc_id="doc-arrange-prose", pages=[page()], blocks=blocks)


def furniture_view() -> LayoutView:
    """Prose plus two furniture blocks: a true header inside the 90 permille margin band
    and a MIS-ZONED one in the page middle — V4's furniture-reparent pair."""
    view = prose_view()
    view.blocks.append(block("CITY OF MILLBROOK RECORDS", (0.8, 0.2, 4.0, 0.4),
                             zone=Zone.furniture, role="pageHeader"))
    view.blocks.append(block("Approved for general distribution.", (0.8, 5.3, 4.2, 5.55),
                             zone=Zone.furniture, role="pageFooter"))
    return view


def kyc_view(name: str, name_w: float, idnum: str, id_w: float,
             amount: str, amt_w: float) -> LayoutView:
    """One KYC template, filled: left label column, RIGHT-ALIGNED value column (R7)."""
    rows = [
        ("FULL LEGAL NAME", name, name_w, 1.6),
        ("IDENTITY NUMBER", idnum, id_w, 2.5),
        ("DECLARED AMOUNT", amount, amt_w, 3.4),
        ("BRANCH OF RECORD", "HARBOR NORTH", 1.6, 4.3),
    ]
    blocks = [block("CLIENT VERIFICATION FORM", (0.8, 0.7, 7.7, 0.95), zone=Zone.title)]
    for label, value, width, y in rows:
        blocks.append(block(label, (0.8, y, 3.2, y + 0.25)))
        blocks.append(block(value, (7.6 - width, y, 7.6, y + 0.25)))
    return LayoutView(doc_id="doc-arrange-kyc", pages=[page()], blocks=blocks)


def identity_a() -> LayoutView:
    return kyc_view("JANE MAY QUARLES", 2.0, "83915027", 1.2, "USD 4,120.50", 1.5)


def identity_b() -> LayoutView:
    return kyc_view("OMAR K DIALLO", 1.7, "17460293", 1.4, "USD 918.25", 1.3)


def cross_page_view() -> LayoutView:
    """A single-column letter whose last page-1 paragraph hyphen-splits into page 2 — the
    R6 case: the continuation source is only ever a CONTEXT node in page 2's window."""
    page1 = [
        ("Dear residents of the harbor district and beyond,", 1.5),
        ("the annual meeting concluded without a quorum being reached.", 2.4),
        ("Attendees voted to reconvene at the earliest opportunity.", 3.3),
        ("Volunteers catalogued every motion in the official ledger.", 4.2),
        ("The delegates then adjourned and walked to the assem-", 5.1),
    ]
    page2 = [
        ("bly hall where the session resumed after a short recess.", 1.5),
        ("Minutes will be circulated to every registered household.", 2.4),
        ("Questions may be directed to the clerk of the district.", 3.3),
    ]
    blocks = [block(t, (0.8, y, 7.7, y + 0.25)) for t, y in page1]
    blocks += [block(t, (0.8, y, 7.7, y + 0.25), page=2) for t, y in page2]
    return LayoutView(doc_id="doc-arrange-xpage", pages=[page(1), page(2)], blocks=blocks)


def single_col_small_view() -> LayoutView:
    """A clean little letter: geometry runs, nothing to review => clean_single_column."""
    blocks = [
        block("A short note about nothing in particular.", (0.8, 1.5, 7.7, 1.75)),
        block("It ends exactly where it began.", (0.8, 2.4, 7.7, 2.65)),
    ]
    return LayoutView(doc_id="doc-arrange-small", pages=[page()], blocks=blocks)


def seq_only_view() -> LayoutView:
    """No geometry at all (the HTML/XLSX shape) => no_geometry."""
    return LayoutView(
        doc_id="doc-arrange-seq",
        blocks=[
            TextBlock(text="alpha paragraph", seq=0),
            TextBlock(text="beta paragraph", seq=1),
        ],
    )


def all_block_texts(view: LayoutView) -> list[str]:
    texts = [b.text for b in view.blocks]
    texts += [c.text for t in view.tables for c in t.cells]
    texts += [kv.key for kv in view.key_values] + [kv.value for kv in view.key_values]
    return [t for t in texts if t]


def word_4grams(text: str) -> set[str]:
    """Case-folded, whitespace-normalized word 4-grams — §8.6's tripwire unit."""
    words = text.casefold().split()
    return {" ".join(words[i:i + 4]) for i in range(len(words) - 3)}


def assert_no_ngrams(view: LayoutView, haystack: bytes) -> None:
    normalized = " ".join(haystack.decode("utf-8", "replace").casefold().split())
    for text in all_block_texts(view):
        for gram in word_4grams(text):
            assert gram not in normalized, f"document 4-gram leaked: {len(gram)} chars"


# ---------------------------------------------------------------------------
# §8.6 test_payload_model_closed — the type-level PII boundary
# ---------------------------------------------------------------------------
def _string_schemas(schema: object) -> list[dict]:
    """Every subschema of type string, wherever it hides ($defs, anyOf, items...)."""
    found: list[dict] = []
    stack = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("type") == "string":
                found.append(node)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def test_payload_model_closed():
    """EVERY string-typed field in the payload's JSON schema carries enum/pattern/const.

    This is the test the spec says an unconstrained ``str`` field must fail — adding one to
    any payload model breaks the suite, not code review.
    """
    schema = WindowPayload.model_json_schema()
    strings = _string_schemas(schema)
    assert strings, "schema walk found no string fields - the walk itself is broken"
    for sub in strings:
        assert (
            "enum" in sub or "pattern" in sub or "const" in sub
        ), f"unconstrained string field in payload schema: {sub}"


# ---------------------------------------------------------------------------
# Bucket units — integer thresholds, exact boundaries
# ---------------------------------------------------------------------------
def test_char_count_buckets():
    assert _char_class(0) is CharCountClass.xs
    assert _char_class(7) is CharCountClass.xs
    assert _char_class(8) is CharCountClass.s
    assert _char_class(31) is CharCountClass.s
    assert _char_class(32) is CharCountClass.m
    assert _char_class(127) is CharCountClass.m
    assert _char_class(128) is CharCountClass.l
    assert _char_class(511) is CharCountClass.l
    assert _char_class(512) is CharCountClass.xl


def test_line_count_buckets():
    assert _line_class(0) is LineCountClass.one
    assert _line_class(1) is LineCountClass.one
    assert _line_class(2) is LineCountClass.two_three
    assert _line_class(3) is LineCountClass.two_three
    assert _line_class(4) is LineCountClass.four_plus


def test_w_class_buckets():
    width = 8000
    assert _w_class(499, width) is WClass.lt_1_16
    assert _w_class(500, width) is WClass.lt_1_8    # 16*500 == width: not < => next bucket
    assert _w_class(999, width) is WClass.lt_1_8
    assert _w_class(1999, width) is WClass.lt_1_4
    assert _w_class(3999, width) is WClass.lt_1_2
    assert _w_class(5999, width) is WClass.lt_3_4
    assert _w_class(6000, width) is WClass.ge_3_4
    assert _w_class(0, width) is WClass.lt_1_16
    assert _w_class(100, 0) is WClass.lt_1_16


def test_grid_pm_is_one_percent():
    assert _grid_pm(0, 8500) == 0
    assert _grid_pm(8500, 8500) == 1000
    assert _grid_pm(4250, 8500) == 500
    # Sub-percent positions collapse onto the grid: 1% of 8500 is 85.
    assert _grid_pm(84, 8500) == 10       # rounds to 1%
    assert _grid_pm(42, 8500) == 0        # rounds down to 0%
    assert _grid_pm(43, 8500) == 10       # half rounds up
    assert _grid_pm(1, 0) is None
    # Every produced value is a multiple of 10 permille.
    for x in range(0, 8501, 137):
        value = _grid_pm(x, 8500)
        assert value is not None and value % 10 == 0


# ---------------------------------------------------------------------------
# R7 — the anchored edge, and only the anchored edge
# ---------------------------------------------------------------------------
def test_right_aligned_field_anchors_right_edge():
    """Two different fill widths on one right-aligned field => identical anchor."""
    feats_a = build_features(build_doctree(identity_a()), identity_a())
    feats_b = build_features(build_doctree(identity_b()), identity_b())
    by_path_a = {f.path: f for f in feats_a.values()}
    by_path_b = {f.path: f for f in feats_b.values()}
    # The name value: frame[2]/p[1] in both trees.
    key = next(p for p in by_path_a if "frame[2]/p[1]" in p)
    fa, fb = by_path_a[key], by_path_b[key]
    assert fa.anchor_edge.value == "right" and fb.anchor_edge.value == "right"
    assert fa.anchor_pm == fb.anchor_pm  # the template-fixed edge, not the filled length
    assert fa.w_class is fb.w_class     # widths differ, bucket does not


def test_payload_carries_no_free_edge_or_exact_extent():
    """The serialized node dicts hold no raw coordinate or exact count anywhere."""
    view = prose_view()
    window = make_windows(build_doctree(view), view)[0]
    forbidden = {"x0", "x1", "y0", "y1", "bbox", "width", "width_mu", "height_mu",
                 "char_count", "line_count"}
    for node in json.loads(window.payload_bytes)["nodes"]:
        assert not (set(node) & forbidden), f"raw geometry key in payload: {set(node)}"
        for grid_key in ("anchor_pm", "y0_pm"):
            if grid_key in node:
                assert node[grid_key] % 10 == 0  # nothing finer than the 1% grid


def test_containers_carry_no_geometry_buckets():
    """A container's bbox is a union of its children — content-dependent, so containers
    send structure only (the frame-of-right-aligned-values leak found by the two-identities
    fixture)."""
    view = prose_view()
    tree = build_doctree(view)
    feats = build_features(tree, view)
    containers = [f for i, f in feats.items() if tree.nodes[i].children]
    assert containers
    for feat in containers:
        assert feat.anchor_pm is None and feat.y0_pm is None
        assert feat.w_class is WClass.lt_1_16


# ---------------------------------------------------------------------------
# §8.6 the two-identities byte-identity kill switch (R7 fixture included)
# ---------------------------------------------------------------------------
def test_two_identities_one_payload():
    view_a, view_b = identity_a(), identity_b()
    windows_a = make_windows(build_doctree(view_a), view_a)
    windows_b = make_windows(build_doctree(view_b), view_b)
    assert len(windows_a) == len(windows_b) == 1
    assert windows_a[0].payload_bytes == windows_b[0].payload_bytes
    assert windows_a[0].payload_sha256 == windows_b[0].payload_sha256


def test_allcaps_page_voids_starts_lowercase_in_payload():
    """The KYC page is all-caps: the metrics gate voids the signal, and the honest null
    travels as ABSENCE (exclude_none), never as a fabricated False."""
    view = identity_a()
    window = make_windows(build_doctree(view), view)[0]
    assert b"starts_lowercase" not in window.payload_bytes
    # The prose page is mixed-case: the signal is present there.
    prose = prose_view()
    prose_window = make_windows(build_doctree(prose), prose)[0]
    assert b"starts_lowercase" in prose_window.payload_bytes


# ---------------------------------------------------------------------------
# §8.6 n-gram tripwire — payload half (the wire half lives in test_arrange_client)
# ---------------------------------------------------------------------------
def test_no_document_ngrams_in_payload():
    for view in (prose_view(), identity_a(), cross_page_view()):
        tree = build_doctree(view)
        for window in make_windows(tree, view):
            assert_no_ngrams(view, window.payload_bytes)


# ---------------------------------------------------------------------------
# Windowing — §4.3
# ---------------------------------------------------------------------------
def test_single_window_covers_body_preorder():
    view = prose_view()
    tree = build_doctree(view)
    window = make_windows(tree, view)[0]
    body_ids = [n.id for n in tree.nodes
                if n.kind not in (NodeKind.document, NodeKind.body, NodeKind.furniture)]
    assert sorted(window.id_map.values()) == sorted(body_ids)
    assert window.payload.order == [n.id for n in window.payload.nodes]
    assert not window.context_ids
    assert window.node_span == (min(body_ids), max(body_ids))


def test_window_split_carries_context():
    view = prose_view()
    tree = build_doctree(view)
    windows = make_windows(tree, view, max_window=6)
    assert len(windows) >= 2
    for window in windows:
        own = [nid for nid in window.id_map if nid not in window.context_ids]
        assert len(own) <= 6
    later = windows[1]
    assert len(later.context_ids) == CONTEXT_CARRY
    # Context nodes are the previous window's last own nodes, flagged context:true.
    prev_own = [windows[0].id_map[n.id] for n in windows[0].payload.nodes
                if n.id not in windows[0].context_ids]
    ctx_tree_ids = [later.id_map[nid] for nid in sorted(later.context_ids,
                                                        key=lambda s: int(s[1:]))]
    assert ctx_tree_ids == prev_own[-CONTEXT_CARRY:]
    by_id = {n.id: n for n in later.payload.nodes}
    assert all(by_id[nid].context for nid in later.context_ids)
    assert all(not by_id[nid].context for nid in by_id if nid not in later.context_ids)


def test_window_split_is_band_atomic():
    """When a split point exists at a band change, the seam falls on one — a band never
    straddles a window boundary that had an alternative."""
    view = prose_view()
    tree = build_doctree(view)
    windows = make_windows(tree, view, max_window=6)
    for first, second in itertools.pairwise(windows):
        last_own = [first.id_map[n.id] for n in first.payload.nodes
                    if n.id not in first.context_ids][-1]
        first_own = next(second.id_map[n.id] for n in second.payload.nodes
                         if n.id not in second.context_ids)
        band_a = tree.nodes[last_own].prov.band_ix
        band_b = tree.nodes[first_own].prov.band_ix
        assert band_a != band_b


def test_cross_page_windows_are_per_page():
    view = cross_page_view()
    tree = build_doctree(view)
    windows = make_windows(tree, view)
    assert [w.page for w in windows] == [1, 2]
    # Page 2's window carries page 1's tail as context — the R6 seam.
    assert windows[1].context_ids
    ctx_pages = {tree.nodes[windows[1].id_map[n]].page for n in windows[1].context_ids}
    assert ctx_pages == {1}


def test_multimodal_image_recorded_never_alters_payload():
    """§10: the image rides the request; the STRUCTURAL bytes are identical either way."""
    view = prose_view()
    tree = build_doctree(view)
    png = b"\x89PNG\r\n\x1a\nfakepixels"
    plain = make_windows(tree, view)[0]
    modal = make_windows(tree, view, page_images={1: png})[0]
    assert modal.image_png == png
    assert modal.image_sha256 is not None and len(modal.image_sha256) == 64
    assert plain.image_png is None and plain.image_sha256 is None
    assert modal.payload_bytes == plain.payload_bytes
    assert modal.payload_sha256 == plain.payload_sha256


def test_make_windows_clamps_to_the_nid_id_space():
    """The ``NId`` grammar allows ``n0``..``n99`` and context carry-overs share the space:
    a configured ``arrange_max_window`` beyond 96 own nodes must clamp, not mint ``n100``
    and kill the whole pass with a payload ValidationError."""
    import re

    blocks = [
        block(f"line {i} of a very long single column page.",
              (0.8, 0.9 + 0.09 * i, 7.7, 0.96 + 0.09 * i))
        for i in range(105)
    ]
    view = LayoutView(doc_id="doc-arrange-tall", pages=[page()], blocks=blocks)
    tree = build_doctree(view)
    windows = make_windows(tree, view, max_window=120)  # raised ValidationError unclamped
    assert len(windows) >= 2
    covered: set[int] = set()
    for window in windows:
        own = [nid for nid in window.id_map if nid not in window.context_ids]
        assert len(own) <= 100 - CONTEXT_CARRY
        assert all(re.fullmatch(r"n[0-9]{1,2}", nid) for nid in window.id_map)
        covered.update(window.id_map[nid] for nid in own)
    body_ids = {n.id for n in tree.nodes
                if n.kind not in (NodeKind.document, NodeKind.body, NodeKind.furniture)}
    assert covered == body_ids  # the clamp splits, it never drops nodes


def test_windows_deterministic():
    view = prose_view()
    tree = build_doctree(view)
    first = make_windows(tree, view)
    second = make_windows(tree, view)
    assert [w.payload_bytes for w in first] == [w.payload_bytes for w in second]
    assert [w.payload_sha256 for w in first] == [w.payload_sha256 for w in second]
