"""``apply_patch``: path-addressed ops, deterministic re-derivation, never partial.

The property under test is R12's auditability contract: either EVERY op applied and the
result is a fully re-derived tree (pre-order ids, fresh paths, remapped edges), or
``PatchInvalid`` was raised and the input tree is untouched byte-for-byte. A partially
applied patch with no error is the one outcome that must be unrepresentable.
"""
from __future__ import annotations

import pytest
from test_doctree_build import block, page, provider_view
from test_doctree_continuity import hyphen_columns_view

from dpc.doctree.build import build_doctree
from dpc.doctree.models import (
    DocTree,
    Node,
    NodeKind,
    dump_tree,
    validate_tree,
    walk_body,
)
from dpc.doctree.patch import PatchInvalid, apply_patch
from dpc.models import LayoutView, Zone


def small_view() -> LayoutView:
    return LayoutView(pages=[page()], blocks=[
        block("Annual Report", (0.8, 0.7, 7.8, 1.0), zone=Zone.title),
        block("First paragraph of the body text.", (0.8, 1.5, 7.8, 1.72)),
        block("Second paragraph follows on here.", (0.8, 1.9, 7.8, 2.12)),
        block("Third paragraph closes the page.", (0.8, 2.3, 7.8, 2.52)),
    ])


def small_tree() -> tuple[DocTree, LayoutView]:
    view = small_view()
    return build_doctree(view), view


def body_block_order(tree: DocTree) -> list[int]:
    return [n.block_ixs[0] for n in walk_body(tree) if n.block_ixs]


SECT = "//doc/body[1]/sect[1]"


# ---------------------------------------------------------------------------
# The four mutating ops
# ---------------------------------------------------------------------------
def test_move_after_repositions_the_node_and_rederives_ids() -> None:
    tree, view = small_tree()
    patched, joins = apply_patch(tree, [
        {"op": "move_after", "node": f"{SECT}/p[1]", "ref": f"{SECT}/p[3]"},
    ])
    assert joins == frozenset()
    assert body_block_order(tree) == [0, 1, 2, 3]      # the input is untouched
    assert body_block_order(patched) == [0, 2, 3, 1]
    assert [n.id for n in patched.nodes] == list(range(len(patched.nodes)))
    assert validate_tree(patched, view).ok


def test_move_before_and_paths_are_recomputed() -> None:
    tree, view = small_tree()
    patched, _ = apply_patch(tree, [
        {"op": "move_before", "node": f"{SECT}/p[3]", "ref": f"{SECT}/p[1]"},
    ])
    assert body_block_order(patched) == [0, 3, 1, 2]
    moved = next(n for n in patched.nodes if n.block_ixs == [3])
    assert moved.path == f"{SECT}/p[1]", "paths are positions, so the mover takes p[1]"
    assert validate_tree(patched, view).ok


def test_reparent_appends_as_last_child_of_the_ref() -> None:
    tree, view = small_tree()
    patched, _ = apply_patch(tree, [
        {"op": "reparent", "node": f"{SECT}/p[3]", "ref": f"{SECT}/h[1]"},
    ])
    heading = next(n for n in patched.nodes if n.kind is NodeKind.heading)
    child = patched.nodes[heading.children[-1]]
    assert child.block_ixs == [3]
    assert child.parent == heading.id
    assert validate_tree(patched, view).ok


def test_merge_flow_records_the_join_and_makes_ref_the_successor() -> None:
    tree, view = small_tree()
    patched, joins = apply_patch(tree, [
        {"op": "merge_flow", "node": f"{SECT}/p[1]", "ref": f"{SECT}/p[3]"},
    ])
    assert body_block_order(patched) == [0, 1, 3, 2]   # ref repositioned after node (R11)
    (src, dst), = joins
    assert patched.nodes[src].block_ixs == [1]
    assert patched.nodes[dst].block_ixs == [3]
    assert dst == src + 1, "the join IS rendering adjacency in the variant"
    assert validate_tree(patched, view).ok


def test_ops_apply_in_sequence_against_the_evolving_state() -> None:
    tree, view = small_tree()
    patched, _ = apply_patch(tree, [
        {"op": "move_after", "node": f"{SECT}/p[1]", "ref": f"{SECT}/p[3]"},
        {"op": "move_before", "node": f"{SECT}/p[2]", "ref": f"{SECT}/h[1]"},
    ])
    # Paths resolve against the ORIGINAL tree (canonical op addressing), application is
    # sequential: p[1]=block1 moves after p[3]=block3, then p[2]=block2 moves before the
    # heading.
    assert body_block_order(patched) == [2, 0, 3, 1]
    assert validate_tree(patched, view).ok


# ---------------------------------------------------------------------------
# Refusals — PatchInvalid, never partial application
# ---------------------------------------------------------------------------
def test_unknown_path_refuses() -> None:
    tree, _ = small_tree()
    with pytest.raises(PatchInvalid) as err:
        apply_patch(tree, [
            {"op": "move_after", "node": f"{SECT}/p[9]", "ref": f"{SECT}/p[1]"},
        ])
    assert err.value.rule == "unknown_path"


def test_unknown_op_refuses() -> None:
    tree, _ = small_tree()
    with pytest.raises(PatchInvalid) as err:
        apply_patch(tree, [{"op": "split", "node": f"{SECT}/p[1]"}])
    assert err.value.rule == "unknown_op"  # advisory ops never reach apply_patch


def test_reparent_to_a_paragraph_refuses() -> None:
    tree, _ = small_tree()
    with pytest.raises(PatchInvalid) as err:
        apply_patch(tree, [
            {"op": "reparent", "node": f"{SECT}/p[1]", "ref": f"{SECT}/p[2]"},
        ])
    assert err.value.rule == "reparent_target_kind"


def test_moving_a_node_into_its_own_subtree_refuses() -> None:
    tree, _ = small_tree()
    with pytest.raises(PatchInvalid) as err:
        apply_patch(tree, [
            {"op": "move_after", "node": "//doc/body[1]", "ref": f"{SECT}/p[1]"},
        ])
    assert err.value.rule in ("anchored_node", "self_or_descendant_ref")


def test_the_skeleton_roots_cannot_be_moved() -> None:
    tree, _ = small_tree()
    for path in ("//doc/body[1]", "//doc/furn[1]"):
        with pytest.raises(PatchInvalid) as err:
            apply_patch(tree, [
                {"op": "reparent", "node": path, "ref": f"{SECT}/h[1]"},
            ])
        assert err.value.rule == "anchored_node"


def test_merge_flow_requires_paragraph_kinds() -> None:
    tree, _ = small_tree()
    with pytest.raises(PatchInvalid) as err:
        apply_patch(tree, [
            {"op": "merge_flow", "node": f"{SECT}/h[1]", "ref": f"{SECT}/p[1]"},
        ])
    assert err.value.rule == "merge_flow_kind"


def nested_flow_tree() -> DocTree:
    """Schema-valid nested list_items — a shape the v1 builder never emits, but apply_patch
    is the ONE shared R12 library and must refuse it typed, not crash."""
    return DocTree(body=1, furniture=4, nodes=[
        Node(id=0, kind=NodeKind.document, path="//doc", parent=None, children=[1, 4]),
        Node(id=1, kind=NodeKind.body, path="//doc/body[1]", parent=0, children=[2]),
        Node(id=2, kind=NodeKind.list_item, path="//doc/body[1]/li[1]", parent=1,
             children=[3], block_ixs=[0]),
        Node(id=3, kind=NodeKind.list_item, path="//doc/body[1]/li[1]/li[1]", parent=2,
             block_ixs=[1]),
        Node(id=4, kind=NodeKind.furniture, path="//doc/furn[1]", parent=0),
    ])


def test_merge_flow_with_an_ancestor_ref_refuses_typed() -> None:
    """merge_flow moves REF, so the dangerous ancestry is ref-over-node: detaching the
    ancestor and reinserting it inside its own detached subtree makes an unreachable cycle.
    The old code checked only the node-over-ref direction and crashed with a raw KeyError —
    an API caller mapping PatchInvalid to 409 would have 500'd instead (R12)."""
    tree = nested_flow_tree()
    before = dump_tree(tree)
    with pytest.raises(PatchInvalid) as err:  # the old code raised KeyError here
        apply_patch(tree, [
            {"op": "merge_flow", "node": "//doc/body[1]/li[1]/li[1]",
             "ref": "//doc/body[1]/li[1]"},
        ])
    assert err.value.rule == "self_or_descendant_ref"
    assert dump_tree(tree) == before


def test_unknown_op_message_never_carries_the_op_string() -> None:
    """The op name of an UNKNOWN op is unvalidated caller input; the message (and the
    stored attribute) may only name ops from the closed vocabulary."""
    tree, _ = small_tree()
    with pytest.raises(PatchInvalid) as err:
        apply_patch(tree, [
            {"op": "reorder: Jane Q. Public asked for it",
             "node": f"{SECT}/p[1]", "ref": f"{SECT}/p[2]"},
        ])
    assert err.value.rule == "unknown_op"
    assert err.value.op == "?"
    assert "Jane" not in str(err.value)


def test_a_failing_op_leaves_no_partial_application() -> None:
    """Op 0 is valid, op 1 is not: the WHOLE patch must refuse and the input stay intact."""
    tree, _ = small_tree()
    before = dump_tree(tree)
    with pytest.raises(PatchInvalid) as err:
        apply_patch(tree, [
            {"op": "move_after", "node": f"{SECT}/p[1]", "ref": f"{SECT}/p[3]"},
            {"op": "reparent", "node": f"{SECT}/p[2]", "ref": f"{SECT}/p[3]"},
        ])
    assert err.value.op_ix == 1
    assert dump_tree(tree) == before, "never partial application (R12)"


def test_the_input_tree_is_never_mutated_on_success_either() -> None:
    tree, _ = small_tree()
    before = dump_tree(tree)
    apply_patch(tree, [
        {"op": "move_after", "node": f"{SECT}/p[1]", "ref": f"{SECT}/p[3]"},
    ])
    assert dump_tree(tree) == before


# ---------------------------------------------------------------------------
# Deterministic re-derivation
# ---------------------------------------------------------------------------
def test_applying_the_same_patch_twice_yields_identical_bytes() -> None:
    ops = [
        {"op": "move_after", "node": f"{SECT}/p[1]", "ref": f"{SECT}/p[3]"},
        {"op": "merge_flow", "node": f"{SECT}/p[2]", "ref": f"{SECT}/p[3]"},
    ]
    first_tree, _ = small_tree()
    second_tree, _ = small_tree()
    first, joins_a = apply_patch(first_tree, ops)
    second, joins_b = apply_patch(second_tree, ops)
    assert dump_tree(first) == dump_tree(second)
    assert joins_a == joins_b


def test_flow_edges_are_remapped_to_the_new_ids() -> None:
    """A benign move upstream of a continues-edge shifts ids; the edge must follow."""
    view = hyphen_columns_view()
    tree = build_doctree(view)
    assert tree.flow, "fixture must carry a continues edge"
    src_blocks = tree.nodes[tree.flow[0].src].block_ixs
    dst_blocks = tree.nodes[tree.flow[0].dst].block_ixs
    # Move the first left-column paragraph to the end of its frame: ids shift, edge endures.
    frame = "//doc/body[1]/sect[1]/fg[1]/frame[1]"
    patched, _ = apply_patch(tree, [
        {"op": "move_after", "node": f"{frame}/p[1]", "ref": f"{frame}/p[5]"},
    ])
    assert len(patched.flow) == 1
    assert patched.nodes[patched.flow[0].src].block_ixs == src_blocks
    assert patched.nodes[patched.flow[0].dst].block_ixs == dst_blocks


def test_an_inverted_flow_edge_is_dropped_not_kept_stale() -> None:
    """Moving the edge's dst BEFORE its src breaks I4 in the variant; the stale annotation
    is dropped rather than stored wrong."""
    tree = build_doctree(hyphen_columns_view())
    edge = tree.flow[0]
    dst_path = tree.nodes[edge.dst].path
    src_path = tree.nodes[edge.src].path
    parent_path = src_path.rsplit("/", 1)[0]
    first_sibling = f"{parent_path}/p[1]"
    patched, _ = apply_patch(tree, [
        {"op": "move_before", "node": dst_path, "ref": first_sibling},
    ])
    assert patched.flow == []
    assert validate_tree(patched, hyphen_columns_view()).ok


def test_patch_on_a_provider_tree_keeps_claims_exact() -> None:
    view = provider_view()
    tree = build_doctree(view)
    patched, _ = apply_patch(tree, [
        {"op": "move_before", "node": "//doc/body[1]/sect[2]",
         "ref": "//doc/body[1]/sect[1]"},
    ])
    check = validate_tree(patched, view)
    assert check.ok, check.violations
    assert patched.counters.nodes == tree.counters.nodes
