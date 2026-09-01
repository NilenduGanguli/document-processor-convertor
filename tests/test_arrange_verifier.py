"""Verifier rules V1-V9 (SPEC-DOCTREE-1 §4.5), one by one, plus the structural guarantees:
the R6 cross-page carve-out actually works end to end, verdicts land in canonical order,
the simulated state evolves, replay is byte-identical, and — R10 — this module proves the
verifier owns NO scoring formula by reading its source.
"""
from __future__ import annotations

import json
from pathlib import Path

from test_arrange_features import (
    cross_page_view,
    furniture_view,
    prose_view,
    seq_only_view,
)

from dpc.arrange.ops import ParsedSample, parse_sample
from dpc.arrange.payload import make_windows
from dpc.arrange.verifier import RUNAWAY_MAX, verify_window
from dpc.doctree.build import build_doctree

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def sample(ops: list[dict]) -> ParsedSample:
    parsed = parse_sample(json.dumps({"schema": "dpc-arrange-ops/1", "ops": ops}))
    assert parsed.usable, "helper built an unparseable sample"
    return parsed


def votes2(ops: list[dict]) -> list[ParsedSample]:
    """Two identical samples — clears V8 so the rule under test is the one that decides."""
    return [sample(ops), sample(ops)]


def setup(view_factory, window_ix: int = 0):
    view = view_factory()
    tree = build_doctree(view)
    window = make_windows(tree, view)[window_ix]
    return tree, view, window


def one_verdict(out, op_name: str):
    matches = [v for v in out.verdicts if v["op"]["op"] == op_name]
    assert len(matches) == 1, f"expected exactly one {op_name} verdict"
    return matches[0]


# Window id cheat sheet for prose_view (asserted in test_arrange_features):
#   n0 section, n1 heading, n2 flow_group, n3 frame[1], n4-n7 left column paragraphs,
#   n8 frame[2], n9-n12 right column paragraphs.
# cross_page_view window 1: n0-n3 context = page 1's last four paragraphs (n3 = the
#   hyphen-ending final paragraph), n4-n6 own = page 2's paragraphs.


# ---------------------------------------------------------------------------
# V1 — unknown ids
# ---------------------------------------------------------------------------
def test_v1_unknown_id():
    tree, view, window = setup(prose_view)
    out = verify_window(tree, view, window, votes2(
        [{"op": "move_before", "node": "n99", "ref": "n4", "reason": "ORDER_INVERSION"}]
    ))
    verdict = one_verdict(out, "move_before")
    assert verdict["verdict"] == "REJECT_UNKNOWN_ID" and verdict["rule"] == "V1"
    assert verdict["op"]["node"] == "n99"  # unresolvable => the verbatim window id


# ---------------------------------------------------------------------------
# V2 — context nodes (and the R6 carve-out)
# ---------------------------------------------------------------------------
def test_v2_context_node_move_rejected():
    tree, view, window = setup(cross_page_view, window_ix=1)
    out = verify_window(tree, view, window, votes2(
        [{"op": "move_before", "node": "n3", "ref": "n4", "reason": "ORDER_INVERSION"}]
    ))
    verdict = one_verdict(out, "move_before")
    assert verdict["verdict"] == "REJECT_CONTEXT_TARGET" and verdict["rule"] == "V2"


def test_v2_context_ref_of_non_merge_rejected():
    tree, view, window = setup(cross_page_view, window_ix=1)
    out = verify_window(tree, view, window, votes2(
        [{"op": "move_after", "node": "n4", "ref": "n3", "reason": "ORDER_INVERSION"}]
    ))
    verdict = one_verdict(out, "move_after")
    assert verdict["verdict"] == "REJECT_CONTEXT_TARGET" and verdict["rule"] == "V2"


def test_r6_cross_page_merge_flow_accepted():
    """The previously-deadlocked case: the continuation SOURCE exists only as context in
    page 2's window, yet merge_flow from it is accepted end to end (V2 exception + V5 gate
    + V6 rubric)."""
    tree, view, window = setup(cross_page_view, window_ix=1)
    out = verify_window(tree, view, window, votes2(
        [{"op": "merge_flow", "node": "n3", "ref": "n4", "reason": "PAGE_CONTINUATION"}]
    ))
    verdict = one_verdict(out, "merge_flow")
    assert verdict["verdict"] == "ACCEPTED" and verdict["rule"] is None
    assert len(out.accepted) == 1
    accepted = out.accepted[0].op
    # Path-addressed (R3): the hyphen-ending page-1 tail into the page-2 head.
    assert accepted["node"] == "//doc/body[1]/p[5]"
    assert accepted["ref"] == "//doc/body[1]/p[6]"


def test_r6_requires_the_cross_page_gate():
    """A context node that is NOT at its frame bottom stays untouchable even for merge."""
    tree, view, window = setup(cross_page_view, window_ix=1)
    # n0 is a mid-page paragraph from page 1 (context, not at_frame_bottom).
    out = verify_window(tree, view, window, votes2(
        [{"op": "merge_flow", "node": "n0", "ref": "n4", "reason": "PAGE_CONTINUATION"}]
    ))
    verdict = one_verdict(out, "merge_flow")
    assert verdict["verdict"] == "REJECT_CONTEXT_TARGET" and verdict["rule"] == "V2"


# ---------------------------------------------------------------------------
# V3 — structural simulation
# ---------------------------------------------------------------------------
def test_v3_reparent_target_must_be_heading_or_section():
    tree, view, window = setup(prose_view)
    out = verify_window(tree, view, window, votes2(
        [{"op": "reparent", "node": "n4", "ref": "n2", "reason": "HEADING_SCOPE"}]
    ))
    verdict = one_verdict(out, "reparent")
    assert verdict["verdict"] == "REJECT_ORPHAN" and verdict["rule"] == "V3"


def test_v3_self_ancestry_rejected():
    tree, view, window = setup(prose_view)
    out = verify_window(tree, view, window, votes2(
        [{"op": "move_before", "node": "n2", "ref": "n4", "reason": "ORDER_INVERSION"}]
    ))
    verdict = one_verdict(out, "move_before")
    assert verdict["verdict"] == "REJECT_ORPHAN" and verdict["rule"] == "V3"


def test_v3_checks_the_evolving_simulated_state():
    """An op that is legal against the STORED tree but illegal after an earlier accepted
    op must be judged against the simulated state (§4.5's application-order re-check)."""
    tree, view, window = setup(prose_view)
    ops = [
        # Applied first (op_rank reparent=0): the flow_group becomes the heading's child.
        {"op": "reparent", "node": "n2", "ref": "n1", "reason": "HEADING_SCOPE"},
        # Now n4 is a DESCENDANT of n1 — moving n1 relative to it is self-ancestry.
        {"op": "move_before", "node": "n1", "ref": "n4", "reason": "ORDER_INVERSION"},
    ]
    out = verify_window(tree, view, window, votes2(ops))
    assert one_verdict(out, "reparent")["verdict"] == "ACCEPTED"
    move = one_verdict(out, "move_before")
    assert move["verdict"] == "REJECT_ORPHAN" and move["rule"] == "V3"


# ---------------------------------------------------------------------------
# V4 — type rules
# ---------------------------------------------------------------------------
def test_v4_merge_flow_requires_matching_flow_kinds():
    tree, view, window = setup(prose_view)
    out = verify_window(tree, view, window, votes2(
        [{"op": "merge_flow", "node": "n1", "ref": "n4", "reason": "COLUMN_CONTINUATION"}]
    ))
    verdict = one_verdict(out, "merge_flow")
    assert verdict["verdict"] == "REJECT_TYPE" and verdict["rule"] == "V4"


def test_v4_caption_never_leaves_its_figure():
    from test_doctree_build import provider_view  # the recorded provider-rung fixture

    view = provider_view()
    tree = build_doctree(view)
    window = make_windows(tree, view)[0]
    by_path = {tree.nodes[tid].path: nid for nid, tid in window.id_map.items()}
    caption = next(nid for path, nid in by_path.items() if "/cap[" in path)
    target = next(nid for path, nid in by_path.items() if path.endswith("sect[1]/p[1]"))
    out = verify_window(tree, view, window, votes2(
        [{"op": "move_after", "node": caption, "ref": target,
          "reason": "CAPTION_DETACHED"}]
    ))
    verdict = one_verdict(out, "move_after")
    assert verdict["verdict"] == "REJECT_TYPE" and verdict["rule"] == "V4"


def test_v4_furniture_reparent_r9():
    """R9's margin rule: a mid-page 'furniture' block may be rescued into the body with
    reason FURNITURE_MISPLACED; a block inside the 90 permille margin bands may not, and
    neither may a rescue under any other reason."""
    tree, view, window = setup(furniture_view)
    # n13 = the real header (y 200..400, inside the 990 mu margin); n14 = mid-page (y 5300).
    accepted = verify_window(tree, view, window, votes2(
        [{"op": "reparent", "node": "n14", "ref": "n0", "reason": "FURNITURE_MISPLACED"}]
    ))
    assert one_verdict(accepted, "reparent")["verdict"] == "ACCEPTED"

    in_margin = verify_window(tree, view, window, votes2(
        [{"op": "reparent", "node": "n13", "ref": "n0", "reason": "FURNITURE_MISPLACED"}]
    ))
    verdict = one_verdict(in_margin, "reparent")
    assert verdict["verdict"] == "REJECT_TYPE" and verdict["rule"] == "V4"

    wrong_reason = verify_window(tree, view, window, votes2(
        [{"op": "reparent", "node": "n14", "ref": "n0", "reason": "HEADING_SCOPE"}]
    ))
    verdict = one_verdict(wrong_reason, "reparent")
    assert verdict["verdict"] == "REJECT_TYPE" and verdict["rule"] == "V4"


def test_v4_fence_rendered_flow_group_is_opaque_to_moves():
    """§5.3: 'fence-rendered regions are opaque to ops (verifier V4 boundary)'. A move
    INTO the fenced panel would silently delete the moved paragraph's text from the
    derived variant (the fence paints only the region's own atoms); a move OUT would
    duplicate it. Both directions reject at V4 — while a move WHOLLY INSIDE the panel
    stays legal, exactly like the table/figure boundary."""
    from test_doctree_build import block as build_block
    from test_treemd import fence_view

    from dpc.doctree.models import NodeKind

    view = fence_view()
    view.blocks.append(build_block(
        "This closing remark sits under the panel.", (0.83, 4.2, 7.78, 4.45)
    ))
    tree = build_doctree(view)
    window = make_windows(tree, view)[0]
    by_tree_id = {tid: nid for nid, tid in window.id_map.items()}
    outside = next(
        n for n in tree.nodes
        if n.block_ixs and "closing remark" in view.blocks[n.block_ixs[0]].text
    )
    fg = next(n for n in tree.nodes if n.kind is NodeKind.flow_group)
    frame = tree.nodes[fg.children[0]]
    para_ids = [c for c in frame.children
                if tree.nodes[c].kind is NodeKind.paragraph]
    inside, inside2 = para_ids[0], para_ids[1]

    move_in = verify_window(tree, view, window, votes2(
        [{"op": "move_before", "node": by_tree_id[outside.id],
          "ref": by_tree_id[inside], "reason": "ORDER_INVERSION"}]
    ))
    verdict = one_verdict(move_in, "move_before")
    assert verdict["verdict"] == "REJECT_TYPE" and verdict["rule"] == "V4"
    assert move_in.accepted == []

    move_out = verify_window(tree, view, window, votes2(
        [{"op": "move_before", "node": by_tree_id[inside],
          "ref": by_tree_id[outside.id], "reason": "ORDER_INVERSION"}]
    ))
    verdict = one_verdict(move_out, "move_before")
    assert verdict["verdict"] == "REJECT_TYPE" and verdict["rule"] == "V4"

    within = verify_window(tree, view, window, votes2(
        [{"op": "move_after", "node": by_tree_id[inside],
          "ref": by_tree_id[inside2], "reason": "ORDER_INVERSION"}]
    ))
    assert one_verdict(within, "move_after")["verdict"] == "ACCEPTED"


# ---------------------------------------------------------------------------
# V5 — cross-page relations
# ---------------------------------------------------------------------------
def test_v5_backwards_cross_page_merge_rejected():
    """merge_flow INTO the previous page inverts the gate (ref.page must be node.page+1)."""
    tree, view, window = setup(cross_page_view, window_ix=1)
    out = verify_window(tree, view, window, votes2(
        [{"op": "merge_flow", "node": "n4", "ref": "n3", "reason": "PAGE_CONTINUATION"}]
    ))
    verdict = one_verdict(out, "merge_flow")
    assert verdict["verdict"] == "REJECT_PAGE_CROSS" and verdict["rule"] == "V5"


# ---------------------------------------------------------------------------
# V6 — geometry (score imported, inversions, geometry-null)
# ---------------------------------------------------------------------------
def test_v6_move_inverting_clear_stacking_rejected():
    tree, view, window = setup(prose_view)
    out = verify_window(tree, view, window, votes2(
        [{"op": "move_before", "node": "n5", "ref": "n4", "reason": "ORDER_INVERSION"}]
    ))
    verdict = one_verdict(out, "move_before")
    assert verdict["verdict"] == "REJECT_GEOMETRY" and verdict["rule"] == "V6"


def test_v6_consistent_move_passes():
    """The same pair, the non-inverting direction: geometry does not object."""
    tree, view, window = setup(prose_view)
    out = verify_window(tree, view, window, votes2(
        [{"op": "move_after", "node": "n5", "ref": "n4", "reason": "ORDER_INVERSION"}]
    ))
    assert one_verdict(out, "move_after")["verdict"] == "ACCEPTED"


def test_v6_merge_below_confirm_threshold_rejected():
    """Two mid-column paragraphs from different columns: no adjacency, terminal period,
    uppercase start — the imported rubric scores below CONT_CONFIRM_MIN."""
    tree, view, window = setup(prose_view)
    out = verify_window(tree, view, window, votes2(
        [{"op": "merge_flow", "node": "n5", "ref": "n10", "reason": "COLUMN_CONTINUATION"}]
    ))
    verdict = one_verdict(out, "merge_flow")
    assert verdict["verdict"] == "REJECT_GEOMETRY" and verdict["rule"] == "V6"


def test_v6_column_continuation_merge_accepted():
    """The flagship: hyphen-ending column tail into lowercase column head."""
    tree, view, window = setup(prose_view)
    out = verify_window(tree, view, window, votes2(
        [{"op": "merge_flow", "node": "n7", "ref": "n9", "reason": "COLUMN_CONTINUATION"}]
    ))
    assert one_verdict(out, "merge_flow")["verdict"] == "ACCEPTED"
    assert out.accepted[0].op["node"].endswith("frame[1]/p[4]")
    assert out.accepted[0].op["ref"].endswith("frame[2]/p[1]")


def test_v6_geometry_null_fails_all_mutating_ops():
    tree, view, window = setup(seq_only_view)
    nids = sorted(window.id_map, key=lambda s: int(s[1:]))
    out = verify_window(tree, view, window, votes2(
        [{"op": "move_before", "node": nids[1], "ref": nids[0],
          "reason": "ORDER_INVERSION"}]
    ))
    verdict = one_verdict(out, "move_before")
    assert verdict["verdict"] == "REJECT_GEOMETRY" and verdict["rule"] == "V6"


# ---------------------------------------------------------------------------
# V7 / V8 / V9
# ---------------------------------------------------------------------------
def test_v7_flag_break_confidence_gate():
    tree, view, window = setup(prose_view)
    queued = verify_window(tree, view, window, votes2(
        [{"op": "flag_break", "node": "n6", "confidence_pm": 800,
          "reason": "INTERRUPTED_FLOW"}]
    ))
    verdict = one_verdict(queued, "flag_break")
    assert verdict["verdict"] == "ADVISORY" and verdict["rule"] is None
    assert queued.review == [{
        "after": tree.nodes[window.id_map["n6"]].path,
        "confidence_pm": 800,
        "reason": "INTERRUPTED_FLOW",
    }]

    dropped = verify_window(tree, view, window, votes2(
        [{"op": "flag_break", "node": "n6", "confidence_pm": 500,
          "reason": "INTERRUPTED_FLOW"}]
    ))
    verdict = one_verdict(dropped, "flag_break")
    assert verdict["verdict"] == "REJECT_LOW_CONFIDENCE" and verdict["rule"] == "V7"
    assert dropped.review == []  # recorded in verdicts, dropped from the queue


def test_v8_no_majority():
    tree, view, window = setup(prose_view)
    lone = sample([{"op": "move_after", "node": "n5", "ref": "n4",
                    "reason": "ORDER_INVERSION"}])
    out = verify_window(tree, view, window, [lone, sample([]), sample([])])
    verdict = one_verdict(out, "move_after")
    assert verdict["verdict"] == "REJECT_NO_MAJORITY" and verdict["rule"] == "V8"
    assert verdict["votes"] == 1 and out.accepted == []


def test_v8_one_vote_per_sample_per_identity():
    """The same op twice in ONE sample is one ballot, not two."""
    tree, view, window = setup(prose_view)
    op = {"op": "move_after", "node": "n5", "ref": "n4", "reason": "ORDER_INVERSION"}
    out = verify_window(tree, view, window, [sample([op, op]), sample([]), sample([])])
    verdict = one_verdict(out, "move_after")
    assert verdict["votes"] == 1
    assert verdict["verdict"] == "REJECT_NO_MAJORITY"


def test_v9_runaway_sample_discarded_before_voting():
    tree, view, window = setup(prose_view)
    runaway = sample([
        {"op": "move_after", "node": f"n{k}", "ref": f"n{k + 1}",
         "reason": "OTHER_STRUCTURAL"}
        for k in range(RUNAWAY_MAX + 1)
    ])
    ok_sample = sample([{"op": "move_after", "node": "n5", "ref": "n4",
                         "reason": "ORDER_INVERSION"}])
    out = verify_window(tree, view, window, [runaway, ok_sample, ok_sample])
    assert out.sample_discards == ["runaway_ops", None, None]
    # None of the runaway sample's ops reached the ballot box.
    assert all(v["op"]["reason"] != "OTHER_STRUCTURAL" for v in out.verdicts)
    assert one_verdict(out, "move_after")["verdict"] == "ACCEPTED"


def test_v9_cap_is_mutating_ops_only():
    """Exactly RUNAWAY_MAX mutating ops plus advisories is NOT a runaway sample."""
    tree, view, window = setup(prose_view)
    at_cap = sample(
        [{"op": "move_after", "node": f"n{k}", "ref": f"n{k + 1}",
          "reason": "OTHER_STRUCTURAL"} for k in range(RUNAWAY_MAX)]
        + [{"op": "flag_break", "node": "n4", "confidence_pm": 900,
            "reason": "INTERRUPTED_FLOW"}]
    )
    out = verify_window(tree, view, window, [at_cap, sample([]), sample([])])
    assert out.sample_discards == [None, None, None]


# ---------------------------------------------------------------------------
# split is advisory-only
# ---------------------------------------------------------------------------
def test_split_is_advisory_only():
    tree, view, window = setup(prose_view)
    out = verify_window(tree, view, window, votes2(
        [{"op": "split", "node": "n6", "reason": "TABLE_FRAGMENT"}]
    ))
    verdict = one_verdict(out, "split")
    assert verdict["verdict"] == "ADVISORY"
    assert out.accepted == [] and out.review == []


# ---------------------------------------------------------------------------
# Canonical order + replay + R10
# ---------------------------------------------------------------------------
def test_verdicts_in_canonical_application_order():
    """Ops arrive scrambled; verdicts land in (page, op_rank, node, ref) order."""
    tree, view, window = setup(prose_view)
    scrambled = [
        {"op": "merge_flow", "node": "n7", "ref": "n9", "reason": "COLUMN_CONTINUATION"},
        {"op": "move_before", "node": "n5", "ref": "n4", "reason": "ORDER_INVERSION"},
        {"op": "reparent", "node": "n2", "ref": "n1", "reason": "HEADING_SCOPE"},
    ]
    out = verify_window(tree, view, window, votes2(scrambled))
    assert [v["op"]["op"] for v in out.verdicts] == [
        "reparent", "move_before", "merge_flow",
    ]


def test_verifier_replay_is_byte_identical():
    tree, view, window = setup(prose_view)
    samples = votes2([
        {"op": "merge_flow", "node": "n7", "ref": "n9", "reason": "COLUMN_CONTINUATION"},
        {"op": "move_before", "node": "n5", "ref": "n4", "reason": "ORDER_INVERSION"},
    ]) + [sample([])]
    first = verify_window(tree, view, window, samples)
    second = verify_window(tree, view, window, samples)
    def dump(out) -> bytes:
        return json.dumps(out.verdicts, sort_keys=True).encode()

    assert dump(first) == dump(second)
    assert [a.op for a in first.accepted] == [a.op for a in second.accepted]


def test_verifier_owns_no_score_formula():
    """R10, structurally: verifier.py IMPORTS continuation_score and never restates the
    rubric — none of the rubric's signal names appear in its source."""
    source = (ROOT / "dpc" / "arrange" / "verifier.py").read_text()
    assert "from dpc.doctree.continuity import" in source
    assert "continuation_score" in source
    assert "def continuation_score" not in source
    for signal in ("ends_hyphen", "starts_lower", "height_match", "width_match",
                   "no_terminal"):
        assert signal not in source, f"verifier restates rubric signal {signal!r}"
