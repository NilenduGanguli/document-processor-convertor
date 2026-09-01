"""Continuation linking: the one rubric, its gates, and the fixtures that matter.

The two fixtures the spec names (§8 / Phase 1): a hyphen-split word across a column
boundary MUST link, and a KYC label column facing a value column must NOT — the false merge
that glues two field values is strictly worse than a missed one. The discriminator on real
forms is the column measure: label and value columns are different widths and often
different type sizes, so the width/height evidence points are absent and the score stays
under ``CONT_EDGE_MIN``. The metrics unit tests pin the signals the rubric consumes.
"""
from __future__ import annotations

from test_doctree_build import block, page, quad

from dpc.doctree.build import build_doctree
from dpc.doctree.continuity import (
    CONT_CONFIRM_MIN,
    CONT_EDGE_MIN,
    ContinuityFeatures,
    continuation_score,
    score_with_evidence,
)
from dpc.doctree.metrics import block_metrics, height_class, page_case_profile
from dpc.doctree.models import Evidence, NodeKind, ScriptClass, validate_tree
from dpc.models import LayoutView, Mark, TextBlock, Zone


# ---------------------------------------------------------------------------
# Metrics — the signals the rubric consumes
# ---------------------------------------------------------------------------
def test_ends_hyphen_covers_ascii_and_unicode_hyphens() -> None:
    for tail in ("-", "‐", "‑"):
        blk = TextBlock(text=f"transfor{tail}")
        assert block_metrics(blk, 0).ends_hyphen, repr(tail)
    assert not block_metrics(TextBlock(text="an em dash —"), 0).ends_hyphen
    assert not block_metrics(TextBlock(text="plain end"), 0).ends_hyphen


def test_terminal_punctuation_sees_through_closing_quotes() -> None:
    assert block_metrics(TextBlock(text='He said "stop."'), 0).ends_terminal_punct
    assert block_metrics(TextBlock(text="A sentence."), 0).ends_terminal_punct
    assert not block_metrics(TextBlock(text="a KYC field value"), 0).ends_terminal_punct
    assert not block_metrics(TextBlock(text="Amount:"), 0).ends_terminal_punct


def test_starts_lowercase_and_the_page_case_gate() -> None:
    assert block_metrics(TextBlock(text="mation of records"), 0).starts_lowercase is True
    assert block_metrics(TextBlock(text="Mation of records"), 0).starts_lowercase is False
    # The all-caps page (passport/MRZ) voids the signal instead of faking False.
    voided = block_metrics(TextBlock(text="SURNAME GIVEN"), 0, case_voided=True)
    assert voided.starts_lowercase is None
    # No cased characters at all (digits, CJK) is honestly None too.
    assert block_metrics(TextBlock(text="12345"), 0).starts_lowercase is None


def test_page_case_profile_flags_allcaps_and_non_bicameral_pages() -> None:
    allcaps = [TextBlock(text="PASSPORT"), TextBlock(text="P<GBRPUBLIC<<JANE<<<<")]
    assert page_case_profile(allcaps) is True
    prose = [TextBlock(text="A quiet sentence in ordinary case.")]
    assert page_case_profile(prose) is False
    cjk = [TextBlock(text="申請書類")]
    assert page_case_profile(cjk) is True


def test_script_class_is_dominant_at_ninety_percent() -> None:
    assert block_metrics(TextBlock(text="ordinary latin text"), 0).script_class \
        is ScriptClass.latin
    assert block_metrics(TextBlock(text="договор"),
                         0).script_class is ScriptClass.cyrillic
    mixed = block_metrics(TextBlock(text="abc дог"), 0)
    assert mixed.script_class is ScriptClass.mixed
    assert block_metrics(TextBlock(text="12345 --"), 0).script_class is ScriptClass.none


def test_height_class_thresholds_are_integer_tests() -> None:
    em = 200
    assert height_class(169, em) == "small"    # 100*169 < 85*200
    assert height_class(200, em) == "body"
    assert height_class(250, em) == "large"    # >= 125*200/100
    assert height_class(360, em) == "display"  # >= 180*200/100
    assert height_class(50, 0) == "body"       # no geometry degrades to the no-signal class


# ---------------------------------------------------------------------------
# The rubric — scores and thresholds (§3.3)
# ---------------------------------------------------------------------------
def _features(**kw: object) -> ContinuityFeatures:
    base: dict[str, object] = {
        "ends_terminal_punct": False,
        "ends_hyphen": False,
        "starts_lowercase": False,
        "height_class": "body",
        "width_mu": 3000,
        "em": 200,
        "script_class": "latin",
    }
    base.update(kw)
    return ContinuityFeatures(**base)  # type: ignore[arg-type]


def test_hyphen_alone_passes_the_edge_threshold() -> None:
    """Hyphen (3) + adjacency (2) = 5 == CONT_EDGE_MIN — the split-word signature suffices,
    even with every other signal absent."""
    src = _features(ends_hyphen=True, ends_terminal_punct=True, height_class="display",
                    width_mu=1000)
    dst = _features(starts_lowercase=False, width_mu=9000)
    score, evidence = score_with_evidence(src, dst, adjacency=True)
    assert score == CONT_EDGE_MIN
    assert evidence == [Evidence.ends_hyphen, Evidence.adjacency]


def test_no_terminal_plus_lowercase_passes() -> None:
    """no_terminal (1) + starts_lower (2) + adjacency (2) = 5 — the spec's second exemplar."""
    src = _features(height_class="display", width_mu=1000)
    dst = _features(starts_lowercase=True, width_mu=9000)
    assert continuation_score(src, dst, adjacency=True) == CONT_EDGE_MIN


def test_none_starts_lowercase_contributes_zero() -> None:
    src = _features()
    dst_none = _features(starts_lowercase=None)
    dst_false = _features(starts_lowercase=False)
    assert continuation_score(src, dst_none, True) == \
        continuation_score(src, dst_false, True)


def test_width_mismatch_and_height_mismatch_each_cost_a_point() -> None:
    src = _features()
    same = _features(starts_lowercase=False)
    assert continuation_score(src, same, True) == 5  # no_term+height+width+adjacency
    narrow = _features(starts_lowercase=False, width_mu=3441)  # |diff| > 2*em = 400
    assert continuation_score(src, narrow, True) == 4
    tall = _features(starts_lowercase=False, width_mu=3441, height_class="display")
    assert continuation_score(src, tall, True) == 3


def test_confirm_threshold_is_laxer_than_edge_threshold() -> None:
    """The verifier confirms at 4 what the builder declines below 5 — the LLM may confirm
    plausible links, never create impossible ones (R10's asymmetry)."""
    assert CONT_CONFIRM_MIN == CONT_EDGE_MIN - 1
    src = _features()
    narrow = _features(starts_lowercase=False, width_mu=9000)
    score = continuation_score(src, narrow, True)
    assert CONT_CONFIRM_MIN <= score < CONT_EDGE_MIN


def test_unknown_widths_void_the_width_point() -> None:
    src = _features(width_mu=0)
    dst = _features(width_mu=0, starts_lowercase=False)
    _, evidence = score_with_evidence(src, dst, True)
    assert Evidence.width_match not in evidence


# ---------------------------------------------------------------------------
# Build-level fixtures
# ---------------------------------------------------------------------------
YS = [(1.53, 1.75), (1.85, 2.07), (2.17, 2.39), (2.49, 2.71), (2.81, 3.03)]


def hyphen_columns_view() -> LayoutView:
    """Symmetric prose columns; the left column's last word is split with a hyphen."""
    left = ["The applicant declares that all", "statements are true and complete.",
            "Any omission may cause refusal.", "Officers verified the supporting",
            "documents and found the transfor-"]
    right = ["mation of the records consistent", "with the register entries held.",
             "No discrepancy was found in the", "audit trail for this account.",
             "The file is approved for review."]
    blocks = [block("DECLARATION OF THE APPLICANT", (0.83, 0.69, 7.78, 1.04),
                    zone=Zone.title)]
    for text, (y0, y1) in zip(left, YS):
        blocks.append(block(text, (0.83, y0, 4.03, y1)))
    for text, (y0, y1) in zip(right, YS):
        blocks.append(block(text, (4.58, y0, 7.78, y1)))
    return LayoutView(pages=[page()], blocks=blocks)


def label_value_view() -> LayoutView:
    """A KYC form: wide label column, narrow value column — different measures."""
    labels = ["Full Name", "Date of Birth", "Nationality", "Passport Number",
              "Place of Issue"]
    values = ["Jane Q. Public", "14 Feb 1988", "Portuguese", "PA1234567", "Lisbon"]
    blocks = [block("APPLICANT DETAILS FORM SECTION", (0.83, 0.69, 7.78, 1.04),
                    zone=Zone.title)]
    for text, (y0, y1) in zip(labels, YS):
        blocks.append(block(text, (0.83, y0, 4.90, y1)))
    for text, (y0, y1) in zip(values, YS):
        blocks.append(block(text, (5.60, y0, 7.78, y1)))
    return LayoutView(pages=[page()], blocks=blocks)


def cross_page_view() -> LayoutView:
    """A paragraph split by a page break, hyphen at the seam."""
    p1 = [
        block(t, (0.9, 1.0 + 0.3 * i, 7.6, 1.22 + 0.3 * i))
        for i, t in enumerate(["Background of the case.", "Facts were established early.",
                               "Records were collected on site.",
                               "Testimony was heard in June."])
    ]
    p1.append(block("The subject account was opened in", (0.9, 9.4, 7.6, 9.62)))
    p1.append(block("March and remained active in trans-", (0.9, 9.7, 7.6, 9.92)))
    p2 = [
        block(t, (0.9, 1.0 + 0.3 * i, 7.6, 1.22 + 0.3 * i), page=2)
        for i, t in enumerate(["actions with several counterparties",
                               "until the closure of the account.",
                               "Final findings follow below here.",
                               "The panel concurred with all of it."])
    ]
    return LayoutView(pages=[page(1), page(2)], blocks=p1 + p2)


def allcaps_columns_view() -> LayoutView:
    """A passport-style all-caps page, hyphen-split across the columns."""
    left = ["SURNAME OF THE HOLDER STATED", "GIVEN NAMES AS PRINTED HERE",
            "NATIONALITY OF THE HOLDER IS", "AUTHORITY THAT ISSUED IT WAS",
            "REMARKS CONTINUE IN THE SECO-"]
    right = ["ND COLUMN OF THIS DOCUMENT AS", "REQUIRED BY THE ISSUING STATE",
             "AND ITS APPLICABLE REGULATION", "WITHOUT ANY FURTHER COMMENTS",
             "END OF THE MACHINE ZONE TEXT"]
    blocks = [block("TRAVEL DOCUMENT SPECIMEN PAGE", (0.83, 0.69, 7.78, 1.04),
                    zone=Zone.title)]
    for text, (y0, y1) in zip(left, YS):
        blocks.append(block(text, (0.83, y0, 4.03, y1)))
    for text, (y0, y1) in zip(right, YS):
        blocks.append(block(text, (4.58, y0, 7.78, y1)))
    return LayoutView(pages=[page()], blocks=blocks)


def test_hyphen_link_is_created_across_the_column_boundary() -> None:
    view = hyphen_columns_view()
    tree = build_doctree(view)
    assert validate_tree(tree, view).ok
    assert len(tree.flow) == 1
    edge = tree.flow[0]
    assert tree.nodes[edge.src].block_ixs == [5]   # "...transfor-"
    assert tree.nodes[edge.dst].block_ixs == [6]   # "mation of the records..."
    assert edge.score >= CONT_EDGE_MIN
    assert Evidence.ends_hyphen in edge.evidence
    assert Evidence.adjacency in edge.evidence


def test_flow_edges_annotate_and_never_reorder() -> None:
    """R11: the linked paragraphs keep their tree positions; only the edge exists."""
    tree = build_doctree(hyphen_columns_view())
    edge = tree.flow[0]
    src, dst = tree.nodes[edge.src], tree.nodes[edge.dst]
    assert src.parent != dst.parent, "src and dst stay in their own frames"
    assert edge.src < edge.dst
    assert src.kind is NodeKind.paragraph and dst.kind is NodeKind.paragraph


def test_label_column_does_not_link_to_value_column() -> None:
    """The KYC false-merge case: the candidate exists (frame-edge pair) but the differing
    column measures deny the width point and the score stays below CONT_EDGE_MIN."""
    tree = build_doctree(label_value_view())
    assert tree.flow == []
    assert tree.passes.continuity == "ran(edges=0, candidates=1)"


def test_cross_page_hyphen_link_is_created() -> None:
    view = cross_page_view()
    tree = build_doctree(view)
    assert len(tree.flow) == 1
    edge = tree.flow[0]
    assert tree.nodes[edge.src].page == 1
    assert tree.nodes[edge.dst].page == 2
    assert tree.nodes[edge.src].block_ixs == [5]  # "...in trans-"
    assert tree.nodes[edge.dst].block_ixs == [6]  # "actions with..."
    assert Evidence.ends_hyphen in edge.evidence


def test_allcaps_page_voids_lowercase_but_hyphen_still_links() -> None:
    """§8.8: on the passport page ``starts_lowercase`` is None throughout, and the link is
    carried by hyphen evidence alone."""
    view = allcaps_columns_view()
    tree = build_doctree(view)
    for node in tree.nodes:
        if node.metrics is not None:
            assert node.metrics.starts_lowercase is None
    assert len(tree.flow) == 1
    edge = tree.flow[0]
    assert Evidence.ends_hyphen in edge.evidence
    assert Evidence.starts_lower not in edge.evidence


def mark_interposed_view() -> LayoutView:
    """R15's interposed pair (§3.3 gate b): a hyphen-split paragraph with a selection mark
    between its halves, in ONE column. The full-measure paragraphs are separator bands, so
    the canvas splits the column into three REGIONS at exactly the interposer — the region
    ordinal is not the column identity, the x-extent is."""
    return LayoutView(pages=[page()], blocks=[
        block("Officers verified the supporting documents and found the transfor-",
              (0.83, 1.5, 7.78, 1.72)),
        block("mation of the records consistent with the register entries held.",
              (0.83, 2.3, 7.78, 2.52)),
    ], marks=[Mark(state="selected", page=1, bbox=quad(0.9, 1.95, 1.05, 2.1))])


def test_mark_interposed_pair_links_across_the_region_split() -> None:
    """The R15 branch the flat region-identity gate silently killed: mark/figure/kv
    interposition counts as adjacency even though the interposer band split the region."""
    view = mark_interposed_view()
    tree = build_doctree(view)
    assert validate_tree(tree, view).ok
    # The mark leaf sits BETWEEN the paragraphs in pre-order (the interposed shape).
    kinds = [n.kind for n in tree.nodes if n.kind in (NodeKind.paragraph, NodeKind.mark)]
    assert kinds == [NodeKind.paragraph, NodeKind.mark, NodeKind.paragraph]
    assert tree.passes.continuity == "ran(edges=1, candidates=1)"
    edge = tree.flow[0]
    assert tree.nodes[edge.src].block_ixs == [0]
    assert tree.nodes[edge.dst].block_ixs == [1]
    assert Evidence.ends_hyphen in edge.evidence
    assert Evidence.adjacency in edge.evidence


def test_emitted_edges_satisfy_i4_by_construction() -> None:
    for view in (hyphen_columns_view(), cross_page_view(), allcaps_columns_view()):
        tree = build_doctree(view)
        seen_dst: set[int] = set()
        for edge in tree.flow:
            assert edge.src < edge.dst
            assert edge.dst not in seen_dst
            seen_dst.add(edge.dst)
            assert tree.nodes[edge.src].kind is NodeKind.paragraph
            assert tree.nodes[edge.dst].kind is NodeKind.paragraph
