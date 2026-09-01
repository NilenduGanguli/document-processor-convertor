"""``run_arrange_pass`` end to end, OFFLINE, from the recorded stub fixtures
(SPEC-DOCTREE-1 §4.6/§4.7/§10). No test here can touch a network: the client runs in stub
mode (fixture replay) or is never constructed, and the §4.7 skip matrix, the budget
machinery, the multimodal disclosure record, the R8 no-wall-clock property, replay
byte-identity from the stored artifact, and the active-mode derivation path are all
asserted through the one public entrypoint.

The fixture files live in ``tests/fixtures/arrange/{payload_sha256}.{sample_ix}.json``.
They are authored against the prose fixture's payload sha, so any change to the feature
projection or windowing shows up HERE first, as a loud fixture-missing discard.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from test_arrange_features import (
    assert_no_ngrams,
    prose_view,
    seq_only_view,
    single_col_small_view,
)

from dpc.arrange import runner as runner_mod
from dpc.arrange.artifact import SKIP_REASONS, artifact_sha256, canonical_bytes
from dpc.arrange.ops import ParsedSample, RawOp
from dpc.arrange.payload import make_windows
from dpc.arrange.runner import run_arrange_pass, should_run
from dpc.arrange.verifier import verify_window
from dpc.doctree.build import build_doctree
from dpc.doctree.patch import apply_patch

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "arrange"

PNG = b"\x89PNG\r\n\x1a\nnot-really-pixels"


def settings(**overrides: object) -> SimpleNamespace:
    """A plain namespace instead of ``Settings``: the runner reads every ``arrange_*`` key
    via getattr-with-default (workstream E may land config later), and pydantic-settings
    refuses setattr of fields it does not define yet."""
    values: dict[str, object] = {
        "arrange_mode": "shadow",
        "arrange_payload": "structure",
        "arrange_fixture_dir": str(FIXTURE_DIR),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def prose_setup():
    view = prose_view()
    tree = build_doctree(view)
    return tree, view


def run(tree, view, *, page_images=None, storage=None, db=None, **overrides):
    return run_arrange_pass(
        "11111111-2222-3333-4444-555555555555", tree, view, page_images,
        settings(**overrides), storage, db, pmd_sha256="ab" * 32,
    )


def test_fixture_files_match_current_payload_sha():
    """The committed fixtures are keyed by the payload's content address; if features or
    windowing change, this test names the drift before anything else fails obscurely."""
    tree, view = prose_setup()
    sha = make_windows(tree, view)[0].payload_sha256
    for sample_ix in range(3):
        path = FIXTURE_DIR / f"{sha}.{sample_ix}.json"
        assert path.is_file(), (
            f"stub fixture missing for current payload sha: {path.name} - regenerate the "
            "fixtures if the feature projection deliberately changed"
        )


# ---------------------------------------------------------------------------
# The offline end-to-end shadow pass
# ---------------------------------------------------------------------------
def test_shadow_pass_end_to_end_offline():
    tree, view = prose_setup()
    result = run(tree, view)
    assert result.status == "ran"
    artifact = result.artifact
    assert artifact["schema"] == "dpc-arrangement/1"
    assert artifact["status"] == "ran"
    assert artifact["payload_mode"] == "structure"
    assert artifact["prompt_template_version"] == "ap1"
    assert artifact["verifier_version"] == "av1"
    assert artifact["pmd_sha256"] == "ab" * 32

    [window] = artifact["windows"]
    raw = window["raw"]
    assert [r["sample_ix"] for r in raw] == [0, 1, 2]
    assert raw[0]["discarded"] is None and raw[1]["discarded"] is None
    assert raw[2]["discarded"] == "runaway_ops"       # V9, ops still recorded verbatim
    assert raw[2]["ops"] is not None and len(raw[2]["ops"]) == 20

    verdicts = {v["op"]["op"]: v for v in window["verdicts"]}
    assert verdicts["merge_flow"]["verdict"] == "ACCEPTED"
    assert verdicts["merge_flow"]["votes"] == 2
    assert verdicts["move_before"]["verdict"] == "REJECT_GEOMETRY"
    assert verdicts["move_before"]["rule"] == "V6"
    assert verdicts["move_after"]["verdict"] == "REJECT_NO_MAJORITY"
    assert verdicts["move_after"]["rule"] == "V8"

    assert result.accepted_ops == [{
        "op": "merge_flow",
        "node": "//doc/body[1]/sect[1]/fg[1]/frame[1]/p[4]",
        "ref": "//doc/body[1]/sect[1]/fg[1]/frame[2]/p[1]",
        "reason": "COLUMN_CONTINUATION",
    }]
    assert artifact["accepted_ops"] == result.accepted_ops
    # Shadow mode: artifact written, NO variant derived.
    assert result.variant_markdown is None and result.variant_status is None
    # Content address recomputes.
    assert result.artifact_sha256 == artifact_sha256(artifact)
    assert result.artifact_bytes == canonical_bytes(artifact)


def test_artifact_has_no_wall_clock_and_no_document_text():
    """R8 + the PII rule, over the actual stored bytes."""
    tree, view = prose_setup()
    result = run(tree, view)
    assert_no_ngrams(view, result.artifact_bytes)
    lowered = {key.lower() for key in _all_keys(result.artifact)}
    for banned in ("latency", "time", "timestamp", "created", "generated", "elapsed",
                   "duration", "ms"):
        assert not any(banned in key for key in lowered), f"wall-clock-ish key: {banned}"


def _all_keys(document: object) -> list[str]:
    keys: list[str] = []
    stack = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            keys.extend(str(k) for k in node)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return keys


def test_verifier_replay_from_recorded_artifact_is_byte_identical():
    """§8.9: rebuild the windows, re-run the verifier over the artifact's recorded raw
    samples, and reproduce the stored verdict list byte for byte."""
    tree, view = prose_setup()
    stored = run(tree, view).artifact
    windows = make_windows(tree, view)
    for record in stored["windows"]:
        window = windows[record["window_ix"]]
        assert window.payload_sha256 == record["payload_sha256"]
        samples = []
        for raw in record["raw"]:
            if raw["ops"] is None:
                samples.append(ParsedSample(ops=None, discarded=raw["discarded"]))
            else:
                samples.append(ParsedSample(
                    ops=tuple(RawOp.model_validate(op) for op in raw["ops"]),
                    discarded=None,
                ))
        replayed = verify_window(tree, view, window, samples)
        assert (
            json.dumps(replayed.verdicts, sort_keys=True)
            == json.dumps(record["verdicts"], sort_keys=True)
        )


def test_run_is_deterministic():
    tree, view = prose_setup()
    assert run(tree, view).artifact_bytes == run(tree, view).artifact_bytes


# ---------------------------------------------------------------------------
# §10 multimodal
# ---------------------------------------------------------------------------
def test_multimodal_records_image_sha_never_bytes():
    tree, view = prose_setup()
    result = run(tree, view, page_images={1: PNG}, arrange_payload="multimodal")
    assert result.status == "ran"
    assert result.artifact["payload_mode"] == "multimodal"
    [window] = result.artifact["windows"]
    assert window["image_sha256"] == hashlib.sha256(PNG).hexdigest()
    assert base64.b64encode(PNG) not in result.artifact_bytes
    assert PNG not in result.artifact_bytes
    # Same structural payload => the same recorded fixtures drive the same acceptance.
    assert len(result.accepted_ops) == 1


def test_multimodal_without_pixels_falls_back_to_structure():
    """Provider-JSON inputs have no pixels: the pass falls back and SAYS so."""
    tree, view = prose_setup()
    result = run(tree, view, page_images=None, arrange_payload="multimodal")
    assert result.status == "ran"
    assert result.artifact["payload_mode"] == "structure(fallback_no_images)"
    assert "image_sha256" not in result.artifact["windows"][0]


# ---------------------------------------------------------------------------
# §4.7 skip matrix — a stamped artifact for every reason
# ---------------------------------------------------------------------------
def test_skip_mode_off_runs_nothing_and_stores_nothing():
    tree, view = prose_setup()
    result = run(tree, view, arrange_mode="off")
    assert result.status == "skipped:off"
    assert result.artifact == {} and not result.stored


def test_skip_no_llm_configured():
    tree, view = prose_setup()
    result = run(tree, view, arrange_fixture_dir="", arrange_provider="stellar",
                 arrange_endpoint="")
    assert result.status == "skipped:no_llm_configured"
    assert result.artifact["status"] == "skipped"
    assert result.artifact["reason"] == "no_llm_configured"


def test_skip_clean_single_column():
    view = single_col_small_view()
    tree = build_doctree(view)
    assert should_run(tree) == (False, "clean_single_column")
    result = run(tree, view)
    assert result.status == "skipped:clean_single_column"
    assert result.artifact["reason"] in SKIP_REASONS


def test_skip_no_geometry():
    view = seq_only_view()
    tree = build_doctree(view)
    assert should_run(tree) == (False, "no_geometry")
    result = run(tree, view)
    assert result.status == "skipped:no_geometry"
    assert result.artifact["reason"] in SKIP_REASONS


def test_skip_timeout_when_budget_gone_before_first_window():
    tree, view = prose_setup()
    result = run(tree, view, arrange_doc_timeout_seconds=0.0)
    assert result.status == "skipped:timeout"
    assert result.artifact["reason"] == "timeout"


def test_budget_exhausted_records_completed_windows_and_skips_the_rest(monkeypatch):
    """One window finishes, the doc budget dies, the remaining windows are stamped
    ``budget_exhausted`` — completed work is never thrown away (§4.7)."""
    clock = {"t": 0.0}

    def tick() -> float:
        clock["t"] += 1.0
        return clock["t"]

    monkeypatch.setattr(runner_mod, "_now", tick)
    tree, view = prose_setup()
    result = run(tree, view, arrange_max_window=6, arrange_doc_timeout_seconds=3.0)
    assert result.status == "ran"
    assert result.windows_run == 1
    assert result.windows_skipped >= 1
    skipped = [w for w in result.artifact["windows"] if "skipped" in w]
    assert skipped and all(w["skipped"] == "budget_exhausted" for w in skipped)
    for window in skipped:  # a skip record still names its payload's content address
        assert len(window["payload_sha256"]) == 64


def test_should_run_triggers():
    tree, _ = prose_setup()
    ran, reason = should_run(tree)
    assert ran and reason == "multi_frame"


# ---------------------------------------------------------------------------
# Active mode — variant derivation via the ONE apply_patch (R12)
# ---------------------------------------------------------------------------
def test_active_mode_derives_via_apply_patch():
    tree, view = prose_setup()
    result = run(tree, view, arrange_mode="active")
    assert result.status == "ran" and len(result.accepted_ops) == 1

    # The accepted ops really drive the shared patch implementation: the continuation
    # target becomes the immediate successor of its source in the derived variant (R11).
    patched, flow_joins = apply_patch(tree, result.accepted_ops)
    assert len(flow_joins) == 1
    [(src, dst)] = flow_joins
    assert dst == src + 1
    src_node = patched.nodes[src]
    assert tree.nodes[tree.nodes[src].id] is not None  # original tree untouched
    assert src_node.metrics is not None and src_node.metrics.ends_hyphen

    if importlib.util.find_spec("dpc.treemd") is None:  # pragma: no cover - treemd landed
        # Phase 2's flattener absent: the runner must record that honestly, never raise.
        assert result.variant_status == "unavailable(treemd_missing)"
        assert result.variant_markdown is None
    else:
        # The full active path: accepted ops -> apply_patch -> treemd.flatten, with the
        # §5.4 audit stamp naming this artifact's sha.
        assert result.variant_status == "derived"
        assert result.variant_markdown is not None
        stamp = f"decided_by: heuristics+patch@{result.artifact_sha256[:8]}"
        assert stamp in result.variant_markdown


def test_active_mode_reports_a_flatten_refusal_never_derived(monkeypatch):
    """``treemd.flatten`` NEVER raises — a refusal is ``("", report)`` with a typed
    ``report.error``. The runner must surface that as ``flatten_refused(<code>)``, never
    store an EMPTY string under ``variant_status == "derived"``."""
    from dpc import treemd as treemd_mod
    from dpc.treemd import FlattenReport

    monkeypatch.setattr(
        treemd_mod, "flatten",
        lambda *a, **k: ("", FlattenReport(error="TreeInvalid:I3:blocks")),
    )
    tree, view = prose_setup()
    result = run(tree, view, arrange_mode="active")
    assert result.status == "ran" and len(result.accepted_ops) == 1
    assert result.variant_status == "flatten_refused(TreeInvalid:I3:blocks)"
    assert result.variant_markdown is None


# ---------------------------------------------------------------------------
# The advisory boundary + storage seams
# ---------------------------------------------------------------------------
def test_boundary_catches_everything(monkeypatch):
    def explode(*args: object, **kwargs: object):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(runner_mod, "make_windows", explode)
    tree, view = prose_setup()
    result = run(tree, view)
    assert result.status == "error:RuntimeError"
    assert result.artifact["status"] == "error:RuntimeError"
    assert result.artifact_sha256 == artifact_sha256(result.artifact)


def test_storage_and_db_seams_are_used_when_present():
    calls: dict[str, object] = {}

    class FakeStorage:
        def put_arrangement(self, conversion_id: str, data: bytes) -> str:
            calls["put"] = (conversion_id, data)
            return "arr/2026/09/key.arr.json"

    class FakeDb:
        def insert_arrangement(self, row: dict) -> None:
            calls["row"] = row

    tree, view = prose_setup()
    result = run(tree, view, storage=FakeStorage(), db=FakeDb())
    assert result.stored
    conversion_id, data = calls["put"]
    assert conversion_id == "11111111-2222-3333-4444-555555555555"
    assert data == result.artifact_bytes
    assert calls["row"]["artifact_sha256"] == result.artifact_sha256
    assert calls["row"]["status"] == "ran"


def test_no_storage_objects_is_fine():
    tree, view = prose_setup()
    result = run(tree, view, storage=None, db=None)
    assert result.status == "ran" and not result.stored


def test_runs_against_a_bare_settings_object():
    """The runner must work against a ``Settings`` that predates workstream E's
    ``arrange_*`` fields: every read is getattr-with-spec-default, and the spec default
    for ``arrange_mode`` is ``off`` — the pass ships dark."""
    from dpc.config import Settings

    tree, view = prose_setup()
    result = run_arrange_pass(
        "11111111-2222-3333-4444-555555555555", tree, view, None,
        Settings(_env_file=None),
    )
    assert result.status.startswith("skipped:")


def test_artifact_bytes_stable_across_hash_seeds():
    """The product contract: the stored artifact is sha256-stamped, so its bytes must not
    depend on interpreter hash seeds (dict/set iteration anywhere in the pass)."""
    import os
    import subprocess
    import sys

    repo_root = str(FIXTURE_DIR.parent.parent.parent)
    tests_dir = str(FIXTURE_DIR.parent.parent)
    script = (
        f"import sys; sys.path.insert(0, {repo_root!r}); sys.path.insert(0, {tests_dir!r})\n"
        "from test_arrange_runner import prose_setup, run\n"
        "tree, view = prose_setup()\n"
        "print(run(tree, view).artifact_sha256)\n"
    )
    shas = set()
    for seed in ("0", "42"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        out = subprocess.run(
            [sys.executable, "-c", script], env=env, capture_output=True, text=True,
            check=True,
        )
        shas.add(out.stdout.strip())
    assert len(shas) == 1 and len(next(iter(shas))) == 64


def test_gate_fires_on_geometry_frames_even_when_the_provider_rung_built_the_tree() -> None:
    """The flagship case: provider reading order on a multi-column page.

    Frame nodes exist in the tree only when the GEOMETRY rung built it. On a provider-seeded
    tree the canvas's 2-frame region is recorded in prov but no frame node exists, so the
    first shipped tree-only gate answered clean_single_column on exactly the page a reading-
    order validator exists for — found live against a two-column terms PDF. The gate now
    consults the view's own geometry when given one.
    """
    import textwrap

    from dpc.doctree.build import build_doctree
    from dpc.models import LayoutView, PageInfo, TextBlock, TextLine

    def quad(x0: float, y0: float, x1: float, y1: float) -> list[float]:
        return [x0, y0, x1, y0, x1, y1, x0, y1]

    blocks = []
    for col_x0, col_x1 in ((0.8, 4.0), (4.6, 7.8)):
        lines, y = [], 1.4
        for row in textwrap.wrap("agreement obligations notice liability " * 12, 40)[:14]:
            lines.append(TextLine(text=row, bbox=quad(col_x0, y, col_x1, y + 0.18)))
            y += 0.2
        blocks.append(TextBlock(
            text=" ".join(line.text for line in lines),
            page=1, bbox=quad(col_x0, 1.4, col_x1, y), lines=lines,
        ))
    view = LayoutView(pages=[PageInfo(page=1, width=8.5, height=11.0, unit="inch")],
                      blocks=blocks)
    # Simulate the provider rung owning the tree: a section wrapping both paragraphs in
    # provider order, which is how the live mock payload seeded it.
    view.raw["structure"] = {
        "schema": "dpc.provider-structure/1",
        "root": {"section_ix": 0,
                 "elements": [["paragraph", 0], ["paragraph", 1]], "children": []},
        "figures": [],
        "counters": {"dangling_dropped": 0, "double_claims": 0, "cycles_cut": 0},
    }
    tree = build_doctree(view)
    assert "frame" not in {n.kind.value for n in tree.nodes}, "precondition: provider rung"

    # This fixture happens to also record order ties (side-by-side hulls share a band), so
    # silence every tree-side signal to isolate the branch under test — the live failure was
    # a provider-seeded tree whose report was empty while the canvas saw two frames.
    from dpc.doctree.models import Report

    quiet = tree.model_copy(update={"report": Report()})
    assert should_run(quiet)[0] is False, "tree-only gate cannot see the columns"
    ran, reason = should_run(quiet, view)
    assert ran is True and reason == "multi_frame_geometry"
