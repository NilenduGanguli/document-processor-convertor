"""API tests: storage and db faked in-memory; no Postgres, MinIO, or dpc.pdfread required.

Workstream A's ``dpc.pdfread`` is imported lazily by the handler, so the ``document`` input
kind is exercised here through a fake module planted in ``sys.modules`` — including a
``NeedsRecognition``-equivalent for the 422 path and a ``None`` entry for the 503 path.
"""
from __future__ import annotations

import base64
import itertools
import sys
import types
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

import dpc
from dpc import db, storage
from dpc.api import app
from dpc.models import LayoutView, PageInfo, TextBlock

# ---------------------------------------------------------------------------
# Tiny fixture payloads, one per input kind
# ---------------------------------------------------------------------------
AZURE_LAYOUT = {
    "analyzeResult": {
        "apiVersion": "2024-11-30",
        "modelId": "prebuilt-layout",
        "content": "ACME BANK\nAccount opening form",
        "pages": [{"pageNumber": 1, "width": 8.5, "height": 11.0, "unit": "inch"}],
        "paragraphs": [
            {
                "role": "title",
                "content": "ACME BANK",
                "boundingRegions": [{"pageNumber": 1, "polygon": [1, 1, 4, 1, 4, 2, 1, 2]}],
            },
            {
                "content": "Account opening form",
                "boundingRegions": [{"pageNumber": 1, "polygon": [1, 3, 4, 3, 4, 4, 1, 4]}],
            },
        ],
    }
}

AZURE_READ = {
    "status": "succeeded",
    "analyzeResult": {
        "version": "3.2.0",
        "readResults": [
            {
                "page": 1,
                "width": 800,
                "height": 600,
                "unit": "pixel",
                "language": "en",
                "lines": [
                    {"text": "PASSPORT", "boundingBox": [10, 10, 200, 10, 200, 40, 10, 40]},
                    {"text": "Type P", "boundingBox": [10, 60, 120, 60, 120, 80, 10, 80]},
                ],
            }
        ],
    },
}

DES_OCR = {
    "page": {
        "page_number": 2,
        "width": 612,
        "height": 792,
        "unit": "point",
        "lines": [
            {"text": "Proof of address", "bbox": [10, 10, 300, 42]},
        ],
    },
}


# ---------------------------------------------------------------------------
# In-memory fakes for storage + db (monkeypatched onto our own modules)
# ---------------------------------------------------------------------------
@pytest.fixture()
def backends(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], dict[str, str]]:
    rows: dict[str, Any] = {}
    blobs: dict[str, str] = {}
    ticks = itertools.count()

    def fake_put(conversion_id: str, text: str, settings: Any = None) -> str:
        key = f"pmd/2026/08/{conversion_id}.md"
        blobs[key] = text
        return key

    def fake_get(key: str, settings: Any = None) -> str:
        return blobs[key]

    def fake_insert(row: dict[str, Any], settings: Any = None) -> None:
        stamp = datetime(2026, 8, 25, tzinfo=UTC) + timedelta(seconds=next(ticks))
        rows[row["id"]] = {**row, "created_at": stamp}

    def fake_list(limit: int = 50, offset: int = 0, settings: Any = None) -> list[dict]:
        ordered = sorted(rows.values(), key=lambda r: r["created_at"], reverse=True)
        return ordered[offset : offset + limit]

    def fake_get_row(conversion_id: str, settings: Any = None) -> dict | None:
        return rows.get(conversion_id)

    monkeypatch.setattr(storage, "put_markdown", fake_put)
    monkeypatch.setattr(storage, "get_markdown", fake_get)
    monkeypatch.setattr(storage, "check", lambda settings=None: True)
    monkeypatch.setattr(db, "insert_conversion", fake_insert)
    monkeypatch.setattr(db, "list_conversions", fake_list)
    monkeypatch.setattr(db, "get_conversion", fake_get_row)
    monkeypatch.setattr(db, "init_schema", lambda settings=None: None)
    monkeypatch.setattr(db, "check", lambda settings=None: True)
    return rows, blobs


@pytest.fixture()
def client(backends: Any) -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def plant_fake_pdfread(
    monkeypatch: pytest.MonkeyPatch,
    view: LayoutView | None = None,
    provider: str = "pymupdf",
    refuse: str | None = None,
) -> None:
    """Install a stand-in ``dpc.pdfread`` so tests never depend on workstream A's files."""
    module = types.ModuleType("dpc.pdfread")

    class NeedsRecognition(Exception):
        def __init__(self, reason: str) -> None:
            super().__init__(reason)
            self.reason = reason

    def read_document(data: bytes, *, filename: str | None, settings: Any) -> tuple:
        if refuse is not None:
            raise NeedsRecognition(refuse)
        return view, provider

    module.NeedsRecognition = NeedsRecognition
    module.read_document = read_document
    monkeypatch.setitem(sys.modules, "dpc.pdfread", module)
    monkeypatch.setattr(dpc, "pdfread", module, raising=False)


# ---------------------------------------------------------------------------
# Input kind: azure_analyze_result
# ---------------------------------------------------------------------------
def test_convert_azure_layout(client: TestClient, backends: Any) -> None:
    rows, blobs = backends
    response = client.post("/api/v1/convert", json={"doc_id": "d1", "azure_analyze_result": AZURE_LAYOUT})
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "azure_layout"
    assert body["provider"] == "azure-prebuilt-layout"
    assert body["doc_id"] == "d1"
    assert body["pages"] == 1
    assert body["blocks"] == 2
    assert body["chars"] == len("ACME BANK") + len("Account opening form")
    assert body["s3_key"].startswith("pmd/") and body["s3_key"].endswith(f"{body['id']}.md")
    assert "markdown" not in body  # echo defaults off
    assert body["id"] in rows
    assert rows[body["id"]]["tables_n"] == 0
    stored = blobs[body["s3_key"]]
    assert "# ACME BANK" in stored
    assert "<!-- @1 [" in stored  # anchors made it into the stored PMD


def test_convert_azure_layout_read_shaped_payload_is_detected(client: TestClient) -> None:
    # from_azure auto-detects a Read-shaped payload sent through azure_analyze_result.
    response = client.post("/api/v1/convert", json={"azure_analyze_result": AZURE_READ})
    assert response.status_code == 200
    assert response.json()["provider"] == "azure-read-v3.2"


# ---------------------------------------------------------------------------
# Input kind: azure_read_result
# ---------------------------------------------------------------------------
def test_convert_azure_read(client: TestClient) -> None:
    response = client.post("/api/v1/convert", json={"azure_read_result": AZURE_READ, "echo": True})
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "azure_read"
    assert body["provider"] == "azure-read-v3.2"
    assert body["pages"] == 1
    assert body["blocks"] == 2
    assert "PASSPORT" in body["markdown"]


# ---------------------------------------------------------------------------
# Input kind: des_ocr
# ---------------------------------------------------------------------------
def test_convert_des_ocr(client: TestClient) -> None:
    response = client.post("/api/v1/convert", json={"des_ocr": DES_OCR, "echo": True})
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "des_ocr"
    assert body["provider"] == "des-ocr"
    assert body["blocks"] == 1
    assert "<!-- page 2" in body["markdown"]  # DES page_number wins


# ---------------------------------------------------------------------------
# Input kind: document (fake pdfread)
# ---------------------------------------------------------------------------
def test_convert_document(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    view = LayoutView(
        pages=[PageInfo(page=1, width=612, height=792, unit="point")],
        blocks=[TextBlock(text="From the PDF text layer", page=1,
                          bbox=[10.0, 10.0, 200.0, 10.0, 200.0, 30.0, 10.0, 30.0])],
    )
    plant_fake_pdfread(monkeypatch, view=view, provider="pymupdf")
    payload = base64.b64encode(b"%PDF-1.4 tiny fixture").decode()
    response = client.post(
        "/api/v1/convert",
        json={"content_base64": payload, "filename": "tiny.pdf", "echo": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "document"
    assert body["provider"] == "pymupdf"
    assert body["pages"] == 1
    assert "From the PDF text layer" in body["markdown"]


def test_convert_document_needs_ocr_is_422(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    plant_fake_pdfread(monkeypatch, refuse="scanned page and no azure_di_endpoint configured")
    payload = base64.b64encode(b"%PDF-1.4 scanned").decode()
    response = client.post("/api/v1/convert", json={"content_base64": payload})
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "needs_ocr"
    assert "azure_di_endpoint" in body["detail"]


def test_convert_document_without_pdfread_is_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "dpc.pdfread", None)  # forces ImportError
    monkeypatch.delattr(dpc, "pdfread", raising=False)
    payload = base64.b64encode(b"%PDF-1.4").decode()
    response = client.post("/api/v1/convert", json={"content_base64": payload})
    assert response.status_code == 503
    assert "not yet available" in response.json()["detail"]


def test_convert_document_bad_base64_is_400(client: TestClient) -> None:
    response = client.post("/api/v1/convert", json={"content_base64": "not-base64!!!"})
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Validation: exactly one input
# ---------------------------------------------------------------------------
def test_convert_no_input_is_400(client: TestClient) -> None:
    response = client.post("/api/v1/convert", json={"doc_id": "d1"})
    assert response.status_code == 400
    assert "exactly one" in response.json()["detail"]


def test_convert_two_inputs_is_400(client: TestClient) -> None:
    response = client.post(
        "/api/v1/convert",
        json={"azure_read_result": AZURE_READ, "des_ocr": DES_OCR},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Echo
# ---------------------------------------------------------------------------
def test_echo_returns_markdown_and_matches_stored(client: TestClient, backends: Any) -> None:
    _, blobs = backends
    response = client.post("/api/v1/convert", json={"azure_analyze_result": AZURE_LAYOUT, "echo": True})
    assert response.status_code == 200
    body = response.json()
    assert body["markdown"] == blobs[body["s3_key"]]
    assert body["markdown"].startswith("---\npmd: 1.0\n")


# ---------------------------------------------------------------------------
# Conversions index + markdown retrieval
# ---------------------------------------------------------------------------
def test_list_get_and_markdown_roundtrip(client: TestClient) -> None:
    first = client.post("/api/v1/convert", json={"azure_read_result": AZURE_READ}).json()
    second = client.post("/api/v1/convert", json={"des_ocr": DES_OCR}).json()

    listing = client.get("/api/v1/conversions?limit=50&offset=0")
    assert listing.status_code == 200
    ids = [row["id"] for row in listing.json()]
    assert ids == [second["id"], first["id"]]  # newest first

    one = client.get(f"/api/v1/conversions/{first['id']}")
    assert one.status_code == 200
    assert one.json()["source"] == "azure_read"
    assert one.json()["tables_n"] == 0

    markdown = client.get(f"/api/v1/conversions/{first['id']}/markdown")
    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert "PASSPORT" in markdown.text

    assert client.get("/api/v1/conversions/does-not-exist").status_code == 404
    assert client.get("/api/v1/conversions/does-not-exist/markdown").status_code == 404


# ---------------------------------------------------------------------------
# Health, readiness, request id, SPA boundary
# ---------------------------------------------------------------------------
def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "document-processor-convertor"
    assert body["version"]


def test_readyz_ok_and_degraded(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    ok = client.get("/readyz")
    assert ok.status_code == 200
    assert ok.json() == {"ready": True, "checks": {"postgres": True, "s3": True}}

    monkeypatch.setattr(db, "check", lambda settings=None: False)
    degraded = client.get("/readyz")
    assert degraded.status_code == 503
    assert degraded.json()["checks"]["postgres"] is False


def test_request_id_echoed(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-Id": "req-42"})
    assert response.headers["X-Request-Id"] == "req-42"
    generated = client.get("/health")
    assert generated.headers["X-Request-Id"]  # one is minted when absent


def test_api_paths_never_fall_through_to_spa(client: TestClient) -> None:
    response = client.get("/api/v1/nope")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
