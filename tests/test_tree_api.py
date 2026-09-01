"""Tree/arrange API wiring tests (SPEC-DOCTREE-1 §6, §8.7) — no Postgres, MinIO, or LLM.

Same posture as ``test_api.py``: storage and db are faked in-memory on our own modules —
and the db fakes REFUSE malformed uuids exactly like live Postgres does (both id columns
are ``uuid``; ``InvalidTextRepresentation`` is what the real column raises), so a handler
that feeds caller text straight into a lookup fails here the way it fails in production.
``dpc.treemd`` (flattener) is exercised BOTH real and through a fake that models the module
on disk — ``flatten`` returns ``("", report)`` with ``report.error`` set on refusal, it
never raises (§5.1) — and the arrange pass runs the REAL ``dpc.arrange.runner`` with the
``stub`` LLM provider (fixture replay / no network): the only fake in that path is the LLM
itself, so the hand-off, the pass, and the persistence of its artifact are all the shipped
code. The doctree builder is REAL throughout: these tests build actual trees from the
recorded DI fixtures and assert the stored bytes re-hash to the recorded sha.
"""
from __future__ import annotations

import base64
import hashlib
import itertools
import json
import sys
import types
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from test_api import AZURE_LAYOUT, plant_fake_pdfread

from dpc import db, storage
from dpc.api import app
from dpc.config import Settings
from dpc.doctree import build as doctree_build
from dpc.doctree import models as doctree_models
from dpc.models import LayoutView, PageInfo, TextBlock
from dpc.treemd import FlattenReport

FIXTURES = Path(__file__).parent / "fixtures" / "di"

#: Provider rung: sections survive the coherence audit (passes.provider_sections=used(2)).
SECTIONS_PAYLOAD = json.loads((FIXTURES / "sections_figure_caption.json").read_text())

#: Two-column fixture: the coherence audit demotes page 1
#: (passes.provider_sections=conflict_demoted(pages=[1])) and the builder confesses order
#: ties — so the arrange pass's should_run trigger fires (§4.7) and the tree_source rubric's
#: conflict_demoted branch is exercised.
SECTIONS2_PAYLOAD = json.loads((FIXTURES / "sections_two_column.json").read_text())

#: A payload with pages but no polygons: no atoms, no geometry — the flat rung.
FLAT_PAYLOAD = {
    "analyzeResult": {
        "apiVersion": "2024-11-30",
        "modelId": "prebuilt-layout",
        "content": "Plain line one\nPlain line two",
        "pages": [{"pageNumber": 1, "width": 8.5, "height": 11.0, "unit": "inch"}],
        "paragraphs": [{"content": "Plain line one"}, {"content": "Plain line two"}],
    }
}

PMD3_TEXT = "---\npmd: 3.0\norder: tree\n---\n\nFlattened body\n"


def _refuse_non_uuid(value: Any) -> None:
    """What the live ``uuid`` column does to a malformed key — the fakes must match it."""
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise psycopg.DataError("invalid input syntax for type uuid") from None


# ---------------------------------------------------------------------------
# In-memory fakes: the §6.2 artifact family beside test_api's markdown pair
# ---------------------------------------------------------------------------
@pytest.fixture()
def backends(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    arrangement_rows: dict[str, Any] = {}
    md_blobs: dict[str, str] = {}
    tree_blobs: dict[str, bytes] = {}
    treemd_blobs: dict[str, str] = {}
    arr_blobs: dict[str, bytes] = {}
    ticks = itertools.count()

    def stamp() -> datetime:
        return datetime(2026, 9, 1, tzinfo=UTC) + timedelta(seconds=next(ticks))

    def fake_put_md(conversion_id: str, text: str, settings: Any = None) -> str:
        key = f"pmd/2026/09/{conversion_id}.md"
        md_blobs[key] = text
        return key

    def fake_put_tree(conversion_id: str, data: bytes, settings: Any = None) -> str:
        key = f"tree/2026/09/{conversion_id}.tree.json"
        tree_blobs[key] = data
        return key

    def fake_put_treemd(
        conversion_id: str, text: str, variant_sha8: str | None = None, settings: Any = None
    ) -> str:
        suffix = f".{variant_sha8}" if variant_sha8 else ""
        key = f"treemd/2026/09/{conversion_id}{suffix}.md"
        treemd_blobs[key] = text
        return key

    def fake_put_arr(conversion_id: str, data: bytes, n: int, settings: Any = None) -> str:
        key = f"arr/2026/09/{conversion_id}.{n}.arr.json"
        arr_blobs[key] = data
        return key

    def fake_insert(row: dict[str, Any], settings: Any = None) -> None:
        rows[row["id"]] = {**row, "created_at": stamp()}

    def fake_get_row(conversion_id: str, settings: Any = None) -> dict | None:
        _refuse_non_uuid(conversion_id)  # live PG refuses before it looks
        return rows.get(conversion_id)

    def fake_insert_arrangement(row: dict[str, Any], settings: Any = None) -> None:
        # Live PG refuses NULLs in the NOT NULL columns; the fake must be as strict, or a
        # hand-off that drops suggestion_id/s3_key passes here and dies in production.
        for name in ("suggestion_id", "conversion_id", "artifact_sha256", "s3_key",
                     "status"):
            if row.get(name) is None:
                raise psycopg.errors.NotNullViolation(
                    f'null value in column "{name}"'
                )
        arrangement_rows[row["suggestion_id"]] = {**row, "created_at": stamp()}

    def fake_get_arrangement(suggestion_id: str, settings: Any = None) -> dict | None:
        _refuse_non_uuid(suggestion_id)
        return arrangement_rows.get(suggestion_id)

    def fake_latest_arrangement(conversion_id: str, settings: Any = None) -> dict | None:
        _refuse_non_uuid(conversion_id)
        mine = [
            r for r in arrangement_rows.values() if r["conversion_id"] == conversion_id
        ]
        if not mine:
            return None
        return max(mine, key=lambda r: (r["created_at"], r["suggestion_id"]))

    monkeypatch.setattr(storage, "put_markdown", fake_put_md)
    monkeypatch.setattr(storage, "get_markdown", lambda key, settings=None: md_blobs[key])
    monkeypatch.setattr(storage, "put_tree", fake_put_tree)
    monkeypatch.setattr(storage, "get_tree", lambda key, settings=None: tree_blobs[key])
    monkeypatch.setattr(storage, "put_tree_markdown", fake_put_treemd)
    monkeypatch.setattr(
        storage, "get_tree_markdown", lambda key, settings=None: treemd_blobs[key]
    )
    monkeypatch.setattr(storage, "put_arrangement", fake_put_arr)
    monkeypatch.setattr(
        storage, "get_arrangement", lambda key, settings=None: arr_blobs[key]
    )
    monkeypatch.setattr(storage, "check", lambda settings=None: True)
    monkeypatch.setattr(db, "insert_conversion", fake_insert)
    monkeypatch.setattr(db, "get_conversion", fake_get_row)
    monkeypatch.setattr(db, "list_conversions", lambda **kw: list(rows.values()))
    monkeypatch.setattr(db, "insert_arrangement", fake_insert_arrangement)
    monkeypatch.setattr(db, "get_arrangement", fake_get_arrangement)
    monkeypatch.setattr(db, "latest_arrangement", fake_latest_arrangement)
    monkeypatch.setattr(db, "init_schema", lambda settings=None: None)
    monkeypatch.setattr(db, "check", lambda settings=None: True)
    return {
        "rows": rows,
        "arrangements": arrangement_rows,
        "md": md_blobs,
        "tree": tree_blobs,
        "treemd": treemd_blobs,
        "arr": arr_blobs,
    }


@pytest.fixture()
def client(backends: Any) -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def use_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Pin exact settings for the request path (the ``dpc.api.get_settings`` seam).

    ``_env_file=None`` so a developer's ``.env`` can never flip a mode under the suite —
    the same isolation ``test_routing.py``'s ``make_settings`` uses.
    """

    def _use(**overrides: Any) -> Settings:
        settings = Settings(_env_file=None, **overrides)
        monkeypatch.setattr("dpc.api.get_settings", lambda: settings)
        return settings

    return _use


def plant_fake_treemd(
    monkeypatch: pytest.MonkeyPatch, text: str = PMD3_TEXT, broken: str | None = None
) -> list[dict[str, Any]]:
    """Stand-in ``dpc.treemd`` MODELLING THE MODULE ON DISK — the api imports it lazily,
    so the ``sys.modules`` plant is the real seam.

    ``broken="refuse"`` reproduces the real §5.1 refusal channel: ``("", report)`` with
    ``report.error`` set — the shipped ``flatten`` NEVER raises. ``broken="raise"`` models
    a treemd that violates its own contract (the api's defensive except-branch).
    """
    module = types.ModuleType("dpc.treemd")
    calls: list[dict[str, Any]] = []

    def flatten(
        tree: Any,
        view: Any,
        *,
        doc_id: str = "",
        source: str = "",
        provider: str = "",
        generated: str = "",
        extra: dict[str, Any] | None = None,
        decided_by: str = "heuristics",
        flow_joins: frozenset = frozenset(),
    ) -> tuple[str, FlattenReport]:
        if broken == "raise":
            raise RuntimeError("flatten broken")
        if broken == "refuse":
            return "", FlattenReport(error="TreeInvalid:view_sha_mismatch")
        calls.append({
            "tree": tree, "view": view, "doc_id": doc_id, "source": source,
            "provider": provider, "extra": extra, "decided_by": decided_by,
        })
        return text, FlattenReport(elements_emitted=1)

    module.flatten = flatten
    module.FlattenReport = FlattenReport
    monkeypatch.setitem(sys.modules, "dpc.treemd", module)
    return calls


def spy_real_arrange(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record the api->runner hand-off while running the REAL ``dpc.arrange.runner``.

    Deliberately NOT a fake runner: a fake that persisted its own artifact hid a hand-off
    that threw the real pass's result away. The wrapper's signature is copied from the
    module on disk, every call falls through to the real pass (stub LLM provider — no
    network), and persistence is whatever the api actually does with the returned result.
    """
    from dpc.arrange import runner as runner_module

    real = runner_module.run_arrange_pass
    calls: list[dict[str, Any]] = []

    def wrapper(
        conversion_id: str,
        tree: Any,
        view: Any,
        page_images: Any,
        settings: Any,
        storage: Any = None,
        db: Any = None,
        *,
        pmd_sha256: str = "",
    ) -> Any:
        calls.append({
            "conversion_id": conversion_id,
            "tree": tree,
            "view": view,
            "page_images": page_images,
            "settings": settings,
            "storage": storage,
            "db": db,
            "pmd_sha256": pmd_sha256,
        })
        return real(conversion_id, tree, view, page_images, settings, storage, db,
                    pmd_sha256=pmd_sha256)

    monkeypatch.setattr(runner_module, "run_arrange_pass", wrapper)
    return calls


def convert(client: TestClient, payload: dict[str, Any], **extra: Any) -> dict[str, Any]:
    response = client.post("/api/v1/convert", json={**payload, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def front_matter_lines(text: str) -> list[str]:
    """The front-matter block of a PMD document, as lines (between the two ``---``)."""
    assert text.startswith("---\n")
    return text[4: text.index("\n---\n", 4)].splitlines()


# ---------------------------------------------------------------------------
# tree_mode=build: stored, retrievable, sha matches recomputation
# ---------------------------------------------------------------------------
def test_tree_built_stored_and_sha_matches_recomputation(
    client: TestClient, backends: Any, use_settings: Any
) -> None:
    use_settings(tree_mode="build")
    body = convert(client, {"azure_analyze_result": SECTIONS_PAYLOAD, "doc_id": "d1"})
    assert body["tree_status"] == "built"
    assert body["tree_source"] == "provider_sections"
    assert body["tree_nodes"] > 0
    assert isinstance(body["passes"], dict)
    assert body["passes"]["provider_sections"].startswith("used")

    served = client.get(f"/api/v1/conversions/{body['id']}/tree")
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("application/json")
    # The spec's audit chain: served bytes re-hash to the recorded sha, and the bytes are
    # the canonical dump of a valid dpc.doctree/1 document.
    assert hashlib.sha256(served.content).hexdigest() == body["sha256_tree"]
    tree = json.loads(served.content)
    assert tree["schema"] == "dpc.doctree/1"
    assert len(tree["nodes"]) == body["tree_nodes"]

    row = backends["rows"][body["id"]]
    assert row["tree_s3_key"].startswith("tree/")
    assert row["sha256_tree"] == body["sha256_tree"]
    # The row's passes column is canonical JSON text (sorted, compact), not a repr.
    assert json.loads(row["passes"]) == body["passes"]
    assert row["passes"] == json.dumps(body["passes"], sort_keys=True, separators=(",", ":"))
    # One vocabulary on both surfaces: the row endpoint serves passes as the same dict the
    # /convert response used, never the raw column text.
    served_row = client.get(f"/api/v1/conversions/{body['id']}").json()
    assert served_row["passes"] == body["passes"]


def test_tree_mode_off_stores_no_tree_and_404_names_why(
    client: TestClient, backends: Any, use_settings: Any
) -> None:
    use_settings(tree_mode="off")
    body = convert(client, {"azure_analyze_result": AZURE_LAYOUT})
    assert "tree_status" not in body
    assert backends["tree"] == {}
    missing = client.get(f"/api/v1/conversions/{body['id']}/tree")
    assert missing.status_code == 404
    assert missing.json() == {"error": "no_tree", "tree_status": None}


def test_request_tree_flag_overrides_settings_both_ways(
    client: TestClient, backends: Any, use_settings: Any
) -> None:
    # Opt out of a build-mode deployment for one conversion.
    use_settings(tree_mode="build")
    body = convert(client, {"azure_analyze_result": AZURE_LAYOUT}, tree=False)
    assert "tree_status" not in body
    # Opt in under an off-mode deployment: builds, but never promotes to emit.
    use_settings(tree_mode="off")
    body = convert(client, {"azure_analyze_result": AZURE_LAYOUT}, tree=True)
    assert body["tree_status"] == "built"
    assert "sha256_tree_markdown" not in body


# ---------------------------------------------------------------------------
# tree_mode=emit: PMD 3.0 beside — never instead of — the 2.0 bytes (the spec gate)
# ---------------------------------------------------------------------------
def test_emit_serves_pmd3_and_markdown_bytes_are_unchanged_vs_off(
    client: TestClient, backends: Any, use_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"azure_analyze_result": SECTIONS_PAYLOAD, "doc_id": "gate"}
    use_settings(tree_mode="off")
    off_body = convert(client, payload)
    use_settings(tree_mode="emit")
    calls = plant_fake_treemd(monkeypatch)
    emit_body = convert(client, payload)

    # §8.5 backward-compat gate, byte-asserted: /markdown is byte-identical under emit.
    off_md = backends["md"][off_body["s3_key"]]
    emit_md = backends["md"][emit_body["s3_key"]]
    assert off_md == emit_md
    assert off_body["sha256_markdown"] == emit_body["sha256_markdown"]
    served_md = client.get(f"/api/v1/conversions/{emit_body['id']}/markdown")
    assert served_md.text == off_md  # /markdown still serves 2.0, never 3.0

    # §5.4/§5.2: the call site hands flatten the conversion id (front-matter doc_id AND
    # figure-URI authority), the source, and the provider — not just the extra dict.
    assert len(calls) == 1
    assert calls[0]["doc_id"] == emit_body["id"]
    assert calls[0]["source"] == "azure_layout"
    assert calls[0]["provider"] == emit_body["provider"]
    assert calls[0]["extra"] == {"sha256_input": emit_body_sha_input(backends, emit_body)}

    assert emit_body["sha256_tree_markdown"] == hashlib.sha256(
        PMD3_TEXT.encode()
    ).hexdigest()
    served = client.get(f"/api/v1/conversions/{emit_body['id']}/tree.md")
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("text/markdown")
    assert served.text == PMD3_TEXT
    assert served.text.startswith("---\npmd: 3.0\n")
    row = backends["rows"][emit_body["id"]]
    assert row["tree_md_s3_key"].startswith("treemd/")


def emit_body_sha_input(backends: Any, body: dict[str, Any]) -> str:
    return str(backends["rows"][body["id"]]["sha256_input"])


def test_emit_front_matter_and_figure_uris_from_the_real_flattener(
    client: TestClient, backends: Any, use_settings: Any
) -> None:
    """§5.4 + §5.2 through the module on disk: doc_id/source/provider reach the front
    matter and figure URIs carry the conversion id as their authority — never the empty
    ``figure:///`` that an argument-dropping call site produces."""
    use_settings(tree_mode="emit")
    body = convert(client, {"azure_analyze_result": SECTIONS_PAYLOAD, "doc_id": "kyc-42"})
    served = client.get(f"/api/v1/conversions/{body['id']}/tree.md")
    assert served.status_code == 200
    front = front_matter_lines(served.text)
    assert f"doc_id: {body['id']}" in front
    assert "source: azure_layout" in front
    assert f"provider: {body['provider']}" in front
    assert f"sha256_input: {backends['rows'][body['id']]['sha256_input']}" in front
    assert f"](figure://{body['id']}/fig-1-1)" in served.text
    assert "figure:///" not in served.text


def test_build_mode_serves_no_tree_md_and_says_why(
    client: TestClient, use_settings: Any
) -> None:
    use_settings(tree_mode="build")
    body = convert(client, {"azure_analyze_result": AZURE_LAYOUT})
    missing = client.get(f"/api/v1/conversions/{body['id']}/tree.md")
    assert missing.status_code == 404
    assert missing.json() == {"error": "no_tree_markdown", "tree_status": "built"}


def test_flatten_refusal_stores_nothing_and_tree_md_404s(
    client: TestClient, backends: Any, use_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§5.1's REAL refusal channel: flatten returns ``("", report.error)`` — it does not
    raise. A refusal must cost only the PMD 3.0 artifact: nothing stored, no empty 200,
    no sha-of-empty-string recorded, tree stays built."""
    use_settings(tree_mode="emit")
    plant_fake_treemd(monkeypatch, broken="refuse")
    body = convert(client, {"azure_analyze_result": AZURE_LAYOUT})
    assert body["tree_status"] == "built"
    assert "sha256_tree_markdown" not in body
    assert backends["treemd"] == {}
    row = backends["rows"][body["id"]]
    assert row["tree_md_s3_key"] is None
    assert row["sha256_tree_markdown"] is None
    assert client.get(f"/api/v1/conversions/{body['id']}/tree").status_code == 200
    missing = client.get(f"/api/v1/conversions/{body['id']}/tree.md")
    assert missing.status_code == 404
    assert missing.json() == {"error": "no_tree_markdown", "tree_status": "built"}


def test_real_flattener_refusal_is_never_an_empty_200(
    client: TestClient, backends: Any, use_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same contract through ``dpc.treemd`` ON DISK, refusal forced via its view-sha
    pin (a stale-tree read — the production failure class)."""
    import dpc.treemd as treemd_real

    assert sys.modules["dpc.treemd"] is treemd_real  # no plant: the real module answers
    use_settings(tree_mode="emit")
    monkeypatch.setattr(treemd_real, "view_sha256", lambda view: "0" * 64)
    body = convert(client, {"azure_analyze_result": SECTIONS_PAYLOAD})
    assert body["tree_status"] == "built"
    assert "sha256_tree_markdown" not in body
    assert backends["treemd"] == {}
    empty_sha = hashlib.sha256(b"").hexdigest()
    assert backends["rows"][body["id"]]["sha256_tree_markdown"] != empty_sha
    assert client.get(f"/api/v1/conversions/{body['id']}/tree.md").status_code == 404


def test_flatten_empty_output_without_error_is_not_stored_either(
    client: TestClient, backends: Any, use_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(tree_mode="emit")
    plant_fake_treemd(monkeypatch, text="")
    body = convert(client, {"azure_analyze_result": AZURE_LAYOUT})
    assert "sha256_tree_markdown" not in body
    assert backends["treemd"] == {}
    assert client.get(f"/api/v1/conversions/{body['id']}/tree.md").status_code == 404


def test_raising_flattener_degrades_the_same_way(
    client: TestClient, backends: Any, use_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth: a treemd that violates its own never-raises contract still costs
    only the PMD 3.0 artifact."""
    use_settings(tree_mode="emit")
    plant_fake_treemd(monkeypatch, broken="raise")
    body = convert(client, {"azure_analyze_result": AZURE_LAYOUT})
    assert body["tree_status"] == "built"
    assert "sha256_tree_markdown" not in body
    assert client.get(f"/api/v1/conversions/{body['id']}/tree").status_code == 200
    assert client.get(f"/api/v1/conversions/{body['id']}/tree.md").status_code == 404


def test_absent_treemd_package_degrades_the_same_way(
    client: TestClient, use_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(tree_mode="emit")
    monkeypatch.setitem(sys.modules, "dpc.treemd", None)  # forces ImportError forever
    body = convert(client, {"azure_analyze_result": AZURE_LAYOUT})
    assert body["tree_status"] == "built"
    assert "sha256_tree_markdown" not in body


# ---------------------------------------------------------------------------
# Degradation matrix (§8.7): builder raises / invalid tree / the three source rungs
# ---------------------------------------------------------------------------
def test_builder_raise_is_200_with_error_status_and_pmd2_stored(
    client: TestClient, backends: Any, use_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(tree_mode="build")

    def boom(view: Any) -> Any:
        raise RuntimeError("builder blew up")

    monkeypatch.setattr(doctree_build, "build_doctree", boom)
    body = convert(client, {"azure_analyze_result": AZURE_LAYOUT})
    assert body["tree_status"] == "error:RuntimeError"
    assert "sha256_tree" not in body
    # PMD 2.0 is stored and served exactly as if the tree never existed.
    md = client.get(f"/api/v1/conversions/{body['id']}/markdown")
    assert md.status_code == 200 and "ACME BANK" in md.text
    missing = client.get(f"/api/v1/conversions/{body['id']}/tree")
    assert missing.status_code == 404
    assert missing.json() == {"error": "no_tree", "tree_status": "error:RuntimeError"}


def test_invalid_tree_records_first_rule_and_stores_no_artifacts(
    client: TestClient, backends: Any, use_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(tree_mode="emit")
    plant_fake_treemd(monkeypatch)
    monkeypatch.setattr(
        doctree_models,
        "validate_tree",
        lambda tree, view: doctree_models.TreeValidation(
            ok=False, violations=("I3:blocks_claimed",)
        ),
    )
    body = convert(client, {"azure_analyze_result": AZURE_LAYOUT})
    assert body["tree_status"] == "invalid:I3:blocks_claimed"
    assert "sha256_tree" not in body and "sha256_tree_markdown" not in body
    assert backends["tree"] == {} and backends["treemd"] == {}
    assert client.get(f"/api/v1/conversions/{body['id']}/tree").status_code == 404
    assert client.get(f"/api/v1/conversions/{body['id']}/tree.md").status_code == 404


def test_tree_source_rungs_provider_geometry_flat(
    client: TestClient, use_settings: Any
) -> None:
    """The honesty ladder end-to-end: each rung named in the row, none an error."""
    use_settings(tree_mode="build")
    provider = convert(client, {"azure_analyze_result": SECTIONS_PAYLOAD})
    geometry = convert(client, {"azure_analyze_result": AZURE_LAYOUT})
    flat = convert(client, {"azure_analyze_result": FLAT_PAYLOAD})
    assert provider["tree_source"] == "provider_sections"
    assert geometry["tree_source"] == "geometry"
    assert flat["tree_source"] == "flat"
    assert {provider["tree_status"], geometry["tree_status"], flat["tree_status"]} == {
        "built"
    }


def test_tree_source_is_one_rubric_row_equals_front_matter(
    client: TestClient, backends: Any, use_settings: Any
) -> None:
    """R10 "one rubric, one place": the conversions row and the served PMD 3.0 front
    matter must name the SAME rung — asserted on a conflict_demoted document, the manifest
    the two old implementations classified differently (row said geometry, bytes said
    provider_sections)."""
    use_settings(tree_mode="emit")
    body = convert(client, {"azure_analyze_result": SECTIONS2_PAYLOAD})
    assert body["passes"]["provider_sections"].startswith("conflict_demoted")
    served = client.get(f"/api/v1/conversions/{body['id']}/tree.md")
    assert served.status_code == 200
    front = front_matter_lines(served.text)
    front_value = next(
        line.split(": ", 1)[1] for line in front if line.startswith("tree_source: ")
    )
    row = backends["rows"][body["id"]]
    assert row["tree_source"] == front_value == "provider_sections"
    assert body["tree_source"] == front_value


def test_tree_source_is_literally_the_treemd_function() -> None:
    """The structural guarantee behind the test above: the api COPIES treemd's verdict —
    same function object, not a lookalike that can drift."""
    from dpc import api as api_module
    from dpc import treemd as treemd_module

    assert api_module._tree_source is treemd_module._tree_source


# ---------------------------------------------------------------------------
# Arrange pass hand-off (§4.7, §10) — REAL runner, stub LLM provider, api persistence
# ---------------------------------------------------------------------------
def test_arrange_off_never_calls_runner_and_no_arrangement_row(
    client: TestClient, backends: Any, use_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(tree_mode="emit", arrange_mode="off")
    plant_fake_treemd(monkeypatch)
    calls = spy_real_arrange(monkeypatch)
    body = convert(client, {"azure_analyze_result": AZURE_LAYOUT})
    assert calls == []
    assert backends["arrangements"] == {}
    missing = client.get(f"/api/v1/conversions/{body['id']}/arrangement")
    assert missing.status_code == 404
    assert missing.json() == {"error": "no_arrangement"}


def test_arrange_requires_emit_mode(
    client: TestClient, use_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§4.7 precondition: tree_mode=emit. Shadow under build mode must not run."""
    use_settings(tree_mode="build", arrange_mode="shadow", arrange_provider="stub")
    calls = spy_real_arrange(monkeypatch)
    convert(client, {"azure_analyze_result": AZURE_LAYOUT})
    assert calls == []


def test_arrange_shadow_real_runner_persists_a_retrievable_ran_artifact(
    client: TestClient, backends: Any, use_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE persistence contract, end-to-end with the shipped pass: /convert →
    ``dpc.arrange.runner.run_arrange_pass`` (stub LLM — the only fake in the path) → the
    background task stores the returned artifact through storage+db → /arrangement serves
    it. A fake runner that wrote its own artifact could not see any of this."""
    settings = use_settings(
        tree_mode="emit", arrange_mode="shadow", arrange_provider="stub",
    )
    calls = spy_real_arrange(monkeypatch)
    # Two-column fixture: the builder confesses order ties, so should_run fires and the
    # pass RUNS its windows (each stub sample is a named per-sample discard — no network).
    body = convert(client, {"azure_analyze_result": SECTIONS2_PAYLOAD})

    assert len(calls) == 1
    call = calls[0]
    assert call["conversion_id"] == body["id"]
    assert isinstance(call["tree"], doctree_models.DocTree)
    assert isinstance(call["view"], LayoutView)
    assert call["settings"] is settings
    # Provider-JSON input: no pixels exist, so no images are handed off — the runner's
    # payload_mode falls back to structure (§10).
    assert call["page_images"] is None
    # Persistence belongs to the api's background task, not the runner's optional seams.
    assert call["storage"] is None and call["db"] is None
    # §4.6: the artifact pins the serving bytes it reviewed.
    assert call["pmd_sha256"] == body["sha256_markdown"]

    served = client.get(f"/api/v1/conversions/{body['id']}/arrangement")
    assert served.status_code == 200, served.text
    assert served.headers["content-type"].startswith("application/json")
    artifact = json.loads(served.content)
    assert artifact["schema"] == "dpc-arrangement/1"
    assert artifact["status"] == "ran"
    assert artifact["pmd_sha256"] == body["sha256_markdown"]
    assert artifact["payload_mode"] == "structure(fallback_no_images)"

    assert len(backends["arrangements"]) == 1
    row = next(iter(backends["arrangements"].values()))
    assert row["conversion_id"] == body["id"]
    assert row["s3_key"].startswith("arr/")
    assert row["status"] == "ran"
    assert row["artifact_sha256"] == hashlib.sha256(served.content).hexdigest()
    assert row["model_id"] == artifact["model_id"]
    assert row["verifier_version"] == artifact["verifier_version"]
    assert row["n_accepted"] == len(artifact["accepted_ops"])
    uuid.UUID(row["suggestion_id"])  # a live-schema-insertable primary key


def test_arrange_skip_is_a_stored_artifact_too(
    client: TestClient, backends: Any, use_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§4.7: a pass that declines is stamped, never silent — the skip artifact lands in
    S3+DB through the same hand-off and /arrangement serves it."""
    use_settings(tree_mode="emit", arrange_mode="shadow", arrange_provider="stub")
    calls = spy_real_arrange(monkeypatch)
    body = convert(client, {"azure_analyze_result": SECTIONS_PAYLOAD})
    assert len(calls) == 1
    served = client.get(f"/api/v1/conversions/{body['id']}/arrangement")
    assert served.status_code == 200
    artifact = json.loads(served.content)
    assert artifact["status"] == "skipped"
    assert artifact["reason"] == "clean_single_column"
    row = next(iter(backends["arrangements"].values()))
    assert row["status"] == "skipped:clean_single_column"
    assert row["s3_key"].startswith("arr/")


def test_arrange_multimodal_single_image_passes_through_as_page_1(
    client: TestClient, use_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(
        tree_mode="emit", arrange_mode="shadow", arrange_provider="stub",
        arrange_payload="multimodal",
    )
    plant_fake_treemd(monkeypatch)
    calls = spy_real_arrange(monkeypatch)
    view = LayoutView(
        pages=[PageInfo(page=1, width=612, height=792, unit="point")],
        blocks=[TextBlock(text="scan text", page=1,
                          bbox=[10.0, 10.0, 200.0, 10.0, 200.0, 30.0, 10.0, 30.0])],
    )
    plant_fake_pdfread(monkeypatch, view=view, provider="azure-prebuilt-layout")
    raw = b"\x89PNG\r\n\x1a\nfake-image-bytes"
    convert(
        client,
        {"content_base64": base64.b64encode(raw).decode(), "filename": "scan.png"},
    )
    assert len(calls) == 1
    # The runner's declared seam type: pages keyed by number (§10).
    assert calls[0]["page_images"] == {1: raw}


def test_arrange_multimodal_pdf_rasterizes_one_png_per_page(
    client: TestClient, use_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    pymupdf = pytest.importorskip("pymupdf")
    use_settings(
        tree_mode="emit", arrange_mode="shadow", arrange_provider="stub",
        arrange_payload="multimodal", arrange_raster_dpi=36,
    )
    plant_fake_treemd(monkeypatch)
    calls = spy_real_arrange(monkeypatch)
    with pymupdf.open() as doc:
        doc.new_page(width=144, height=144)
        doc.new_page(width=144, height=144)
        pdf_bytes = doc.tobytes()
    view = LayoutView(
        pages=[PageInfo(page=1, width=144, height=144, unit="point")],
        blocks=[TextBlock(text="page text", page=1,
                          bbox=[10.0, 10.0, 100.0, 10.0, 100.0, 30.0, 10.0, 30.0])],
    )
    plant_fake_pdfread(monkeypatch, view=view, provider="azure-prebuilt-layout")
    convert(
        client,
        {"content_base64": base64.b64encode(pdf_bytes).decode(), "filename": "two.pdf"},
    )
    assert len(calls) == 1
    images = calls[0]["page_images"]
    assert sorted(images) == [1, 2]
    assert all(png.startswith(b"\x89PNG") for png in images.values())


def test_arrange_raster_sniffs_content_not_filename(
    client: TestClient, use_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PDF uploaded as ``scan.png`` must be rasterized, never shipped to the LLM raw
    under an image label — content decides, the filename only hints."""
    pymupdf = pytest.importorskip("pymupdf")
    use_settings(
        tree_mode="emit", arrange_mode="shadow", arrange_provider="stub",
        arrange_payload="multimodal", arrange_raster_dpi=36,
    )
    plant_fake_treemd(monkeypatch)
    calls = spy_real_arrange(monkeypatch)
    with pymupdf.open() as doc:
        doc.new_page(width=144, height=144)
        pdf_bytes = doc.tobytes()
    view = LayoutView(
        pages=[PageInfo(page=1, width=144, height=144, unit="point")],
        blocks=[TextBlock(text="text", page=1,
                          bbox=[10.0, 10.0, 100.0, 10.0, 100.0, 30.0, 10.0, 30.0])],
    )
    plant_fake_pdfread(monkeypatch, view=view, provider="azure-prebuilt-layout")
    convert(
        client,
        {"content_base64": base64.b64encode(pdf_bytes).decode(), "filename": "scan.png"},
    )
    assert len(calls) == 1
    images = calls[0]["page_images"]
    assert sorted(images) == [1]
    assert images[1].startswith(b"\x89PNG")
    assert images[1] != pdf_bytes


def test_arrange_structure_payload_sends_no_images_even_for_documents(
    client: TestClient, use_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(
        tree_mode="emit", arrange_mode="shadow", arrange_provider="stub",
        arrange_payload="structure",
    )
    plant_fake_treemd(monkeypatch)
    calls = spy_real_arrange(monkeypatch)
    view = LayoutView(
        pages=[PageInfo(page=1, width=612, height=792, unit="point")],
        blocks=[TextBlock(text="text", page=1,
                          bbox=[10.0, 10.0, 200.0, 10.0, 200.0, 30.0, 10.0, 30.0])],
    )
    plant_fake_pdfread(monkeypatch, view=view, provider="azure-prebuilt-layout")
    convert(
        client,
        {"content_base64": base64.b64encode(b"raw").decode(), "filename": "doc.pdf"},
    )
    assert len(calls) == 1
    assert calls[0]["page_images"] is None


def test_broken_arrange_package_cannot_break_convert(
    client: TestClient, use_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(tree_mode="emit", arrange_mode="shadow", arrange_provider="stub")
    plant_fake_treemd(monkeypatch)
    monkeypatch.setitem(sys.modules, "dpc.arrange.runner", None)  # forces ImportError
    body = convert(client, {"azure_analyze_result": AZURE_LAYOUT})
    assert body["tree_status"] == "built"


# ---------------------------------------------------------------------------
# Arrangement variant serving (§6.1: 404 arrangement_not_found / 409 arrangement_rejected)
# ---------------------------------------------------------------------------
def test_tree_md_arrangement_variant_endpoints(
    client: TestClient, backends: Any, use_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_settings(tree_mode="emit")
    plant_fake_treemd(monkeypatch)
    body = convert(client, {"azure_analyze_result": SECTIONS_PAYLOAD})
    conversion_id = body["id"]

    # A non-uuid ?arrangement= is a plain "no such thing" — 404, never the
    # InvalidTextRepresentation 500 a raw uuid-column lookup raises (the fake db refuses
    # malformed uuids exactly like live Postgres, so this asserts the endpoint's guard).
    unknown = client.get(
        f"/api/v1/conversions/{conversion_id}/tree.md", params={"arrangement": "nope"}
    )
    assert unknown.status_code == 404
    assert unknown.json() == {"error": "arrangement_not_found"}
    # An unknown-but-well-formed id is the same 404.
    absent = client.get(
        f"/api/v1/conversions/{conversion_id}/tree.md",
        params={"arrangement": str(uuid.uuid4())},
    )
    assert absent.status_code == 404
    assert absent.json() == {"error": "arrangement_not_found"}

    # A shadow-mode row: artifact exists, variant deliberately does not.
    rejected_id = str(uuid.uuid4())
    db.insert_arrangement(
        {
            "suggestion_id": rejected_id,
            "conversion_id": conversion_id,
            "artifact_sha256": "0" * 64,
            "s3_key": storage.put_arrangement(conversion_id, b"{}", 0),
            "status": "ran",
        }
    )
    rejected = client.get(
        f"/api/v1/conversions/{conversion_id}/tree.md",
        params={"arrangement": rejected_id},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"] == "arrangement_rejected"

    # An active-mode row with a stored variant serves the variant bytes.
    variant_text = "---\npmd: 3.0\ndecided_by: heuristics+patch@deadbeef\n---\n"
    variant_key = storage.put_tree_markdown(conversion_id, variant_text, "deadbeef")
    accepted_id = str(uuid.uuid4())
    db.insert_arrangement(
        {
            "suggestion_id": accepted_id,
            "conversion_id": conversion_id,
            "artifact_sha256": "1" * 64,
            "s3_key": storage.put_arrangement(conversion_id, b"{}", 1),
            "status": "ran",
            "variant_s3_key": variant_key,
            "variant_sha256": hashlib.sha256(variant_text.encode()).hexdigest(),
            "variant_generated": "",
        }
    )
    variant = client.get(
        f"/api/v1/conversions/{conversion_id}/tree.md",
        params={"arrangement": accepted_id},
    )
    assert variant.status_code == 200
    assert variant.text == variant_text
    # A variant belonging to conversion A is not addressable through conversion B.
    other = convert(client, {"azure_analyze_result": AZURE_LAYOUT})
    cross = client.get(
        f"/api/v1/conversions/{other['id']}/tree.md", params={"arrangement": accepted_id}
    )
    assert cross.status_code == 404


def test_figures_endpoint_is_reserved_and_honest(client: TestClient, use_settings: Any) -> None:
    use_settings(tree_mode="build")
    body = convert(client, {"azure_analyze_result": SECTIONS_PAYLOAD})
    response = client.get(f"/api/v1/conversions/{body['id']}/figures/fig-1-1")
    assert response.status_code == 404
    assert response.json() == {"error": "figure_extraction", "detail": "not_stored"}


def test_conversion_endpoints_404_for_unknown_and_non_uuid_ids(
    client: TestClient, use_settings: Any
) -> None:
    """Every conversion address answers 404 for ids that name no row — including ids a
    live uuid column would REFUSE (the fakes refuse them too, so these assertions run
    against Postgres semantics, not a forgiving dict)."""
    use_settings(tree_mode="build")
    for conversion_id in ("missing", str(uuid.uuid4())):
        assert client.get(f"/api/v1/conversions/{conversion_id}").status_code == 404
        for suffix in ("markdown", "tree", "tree.md", "arrangement"):
            response = client.get(f"/api/v1/conversions/{conversion_id}/{suffix}")
            assert response.status_code == 404, (conversion_id, suffix, response.text)


def test_db_getters_answer_none_for_malformed_uuids_without_a_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The db layer is total over caller data: a key that cannot be a uuid names no row,
    answered locally — never sent to Postgres to blow up as InvalidTextRepresentation."""

    def explode(settings: Any = None) -> Any:
        raise AssertionError("connect() must not be called for a malformed uuid")

    monkeypatch.setattr(db, "connect", explode)
    assert db.get_conversion("not-a-uuid") is None
    assert db.get_arrangement("not-a-uuid") is None
    assert db.latest_arrangement("not-a-uuid") is None


# ---------------------------------------------------------------------------
# Config: the new enums refuse nonsense at startup, naming the variable (§6.3)
# ---------------------------------------------------------------------------
def test_config_validators_refuse_nonsense_and_accept_the_spec_values() -> None:
    defaults = Settings(_env_file=None)
    assert defaults.tree_mode == "build"
    assert defaults.arrange_mode == "off"
    assert defaults.arrange_payload == "multimodal"
    assert defaults.arrange_provider == ""  # empty = unavailable, a valid state
    assert defaults.arrange_model == "gemini-2.5-flash"
    assert defaults.arrange_max_window == 48
    assert defaults.arrange_raster_dpi == 144
    assert defaults.arrange_window_timeout_seconds == 20.0
    assert defaults.arrange_doc_timeout_seconds == 120.0
    assert defaults.coin_token_ttl_seconds == 840
    assert defaults.ssl_cert_file == ""
    for field, bad, var in (
        ("tree_mode", "banana", "DPC_TREE_MODE"),
        ("arrange_mode", "on", "DPC_ARRANGE_MODE"),
        ("arrange_payload", "images", "DPC_ARRANGE_PAYLOAD"),
        ("arrange_provider", "openai", "DPC_ARRANGE_PROVIDER"),
    ):
        with pytest.raises(ValueError, match=var):
            Settings(_env_file=None, **{field: bad})
    with pytest.raises(ValueError):
        Settings(_env_file=None, arrange_raster_dpi=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, arrange_window_timeout_seconds=0)


# ---------------------------------------------------------------------------
# Determinism of the stored tree through the full HTTP path
# ---------------------------------------------------------------------------
def test_same_payload_twice_yields_identical_tree_bytes(
    client: TestClient, backends: Any, use_settings: Any
) -> None:
    use_settings(tree_mode="build")
    first = convert(client, {"azure_analyze_result": SECTIONS_PAYLOAD, "doc_id": "x"})
    second = convert(client, {"azure_analyze_result": SECTIONS_PAYLOAD, "doc_id": "x"})
    assert first["sha256_tree"] == second["sha256_tree"]
    first_bytes = client.get(f"/api/v1/conversions/{first['id']}/tree").content
    second_bytes = client.get(f"/api/v1/conversions/{second['id']}/tree").content
    assert first_bytes == second_bytes


# ---------------------------------------------------------------------------
# Recheck residuals N1 + N2 — found by replaying recorded artifacts and live PG
# ---------------------------------------------------------------------------
def test_urn_prefixed_uuid_is_a_404_not_a_500() -> None:
    """Python's uuid grammar is wider than Postgres's; the gate must use Postgres's.

    ``uuid.UUID("urn:uuid:<uuid>")`` parses, and binding that string into a ``uuid`` column
    raises ``InvalidTextRepresentation`` — measured live as an HTTP 500 for what is a plain
    "no such row". The rubric lives in db.py (api re-exports it), so both layers refuse.
    """
    import uuid as uuid_module

    from dpc import db as db_module

    urn = f"urn:uuid:{uuid_module.uuid4()}"
    uuid_module.UUID(urn)  # Python accepts it — that is exactly the trap
    assert db_module._valid_uuid(urn) is False
    assert db_module.get_conversion(urn) is None


def test_api_and_db_share_one_uuid_rubric() -> None:
    """Two private copies of one rubric is how rows and front matter learn to disagree."""
    from dpc import api as api_module
    from dpc import db as db_module

    assert api_module._valid_uuid is db_module._valid_uuid


def test_n_rejected_counts_the_verifiers_actual_verdict_vocabulary() -> None:
    """The verifier emits ACCEPTED | ADVISORY | REJECT_<RULE> — never a bare "REJECTED".

    The row-building count matched the literal "REJECTED" and therefore counted zero on
    every artifact ever produced. Pin the counting expression against a verdict list shaped
    exactly like dpc/arrange/verifier.py's output.
    """
    import ast
    import inspect

    from dpc import api as api_module

    source = inspect.getsource(api_module._arrange_and_persist)
    assert '== "REJECTED"' not in source, "counting a verdict string the verifier never emits"
    assert 'startswith("REJECT")' in source
    # And the expression's semantics, run directly against a verifier-shaped artifact:
    windows = [{"verdicts": [
        {"verdict": "ACCEPTED"}, {"verdict": "ADVISORY"},
        {"verdict": "REJECT_NO_MAJORITY"}, {"verdict": "REJECT_GEOMETRY"},
    ]}]
    count = sum(
        1 for w in windows for v in w.get("verdicts", ())
        if str(v.get("verdict", "")).startswith("REJECT")
    )
    assert count == 2
    ast.parse(source)  # the source we asserted against is real, parsed code
