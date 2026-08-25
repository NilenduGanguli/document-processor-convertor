"""Tests for :mod:`dpc.pdfread` and :mod:`dpc.ocr_client`.

Every fixture is built in-test with PyMuPDF — no corpus, no files on disk. The OCR client is
exercised against :class:`httpx.MockTransport`; no test opens a socket.
"""
from __future__ import annotations

from typing import Any

import httpx
import pymupdf
import pytest

from dpc import pdfread
from dpc.config import Settings
from dpc.models import Zone
from dpc.ocr_client import OcrClient, OcrError, OcrTimeout
from dpc.pdfread import NeedsRecognition, read_document

# ---------------------------------------------------------------------------
# Fixture builders — synthetic documents, no corpus dependency
# ---------------------------------------------------------------------------
TEXT = (
    "Know Your Customer onboarding form. Account holder name, account number and "
    "the branch identifier are captured on this page."
)


def make_settings(**overrides: Any) -> Settings:
    """Settings isolated from any .env file lying around the working directory."""
    return Settings(_env_file=None, **overrides)


def di_settings(**overrides: Any) -> Settings:
    """Settings with a (fake) DI endpoint and fast polling bounds for MockTransport tests."""
    values: dict[str, Any] = {
        "azure_di_endpoint": "https://di.example",
        "azure_di_key": "unit-test-key",
        "ocr_timeout_seconds": 5.0,
        "ocr_poll_interval_seconds": 0.0,
        "ocr_max_polls": 5,
    }
    values.update(overrides)
    return make_settings(**values)


def text_pdf(pages: int = 1) -> bytes:
    """A PDF whose every page carries a real text layer (well above the alnum floor)."""
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page(width=612, height=792)  # US Letter, in points
        page.insert_text((72, 100), f"Page {index + 1} heading line")
        page.insert_text((72, 140), TEXT)
    data = doc.tobytes()
    doc.close()
    return data


def png_bytes() -> bytes:
    """A small flat PNG — the 'scan' used both bare and embedded in PDF pages."""
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64), False)
    pixmap.clear_with(128)
    return pixmap.tobytes("png")


def scanned_pdf() -> bytes:
    """A PDF whose single page is one picture and no text — a scan."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(pymupdf.Rect(36, 36, 576, 756), stream=png_bytes())
    data = doc.tobytes()
    doc.close()
    return data


def mixed_pdf() -> bytes:
    """The border case: page 1 has a text layer, page 2 is a scan."""
    doc = pymupdf.open()
    page_one = doc.new_page()
    page_one.insert_text((72, 100), TEXT)
    page_two = doc.new_page()
    page_two.insert_image(pymupdf.Rect(36, 36, 576, 756), stream=png_bytes())
    data = doc.tobytes()
    doc.close()
    return data


# ---------------------------------------------------------------------------
# A stub DI endpoint behind httpx.MockTransport
# ---------------------------------------------------------------------------
LAYOUT_JOB: dict[str, Any] = {
    "status": "succeeded",
    "analyzeResult": {
        "apiVersion": "2024-11-30",
        "modelId": "prebuilt-layout",
        "content": "RECOGNISED TITLE\nRecognised body paragraph from the stub.",
        "pages": [
            {
                "pageNumber": 1,
                "width": 8.5,
                "height": 11.0,
                "unit": "inch",
                "angle": 0,
                "lines": [],
                "words": [],
            }
        ],
        "paragraphs": [
            {
                "content": "RECOGNISED TITLE",
                "role": "title",
                "boundingRegions": [
                    {"pageNumber": 1, "polygon": [1.0, 1.0, 4.0, 1.0, 4.0, 2.0, 1.0, 2.0]}
                ],
                "spans": [{"offset": 0, "length": 16}],
            },
            {
                "content": "Recognised body paragraph from the stub.",
                "boundingRegions": [
                    {"pageNumber": 1, "polygon": [1.0, 3.0, 4.0, 3.0, 4.0, 4.0, 1.0, 4.0]}
                ],
                "spans": [{"offset": 17, "length": 40}],
            },
        ],
    },
}


class DiStub:
    """A fake Azure DI endpoint: records submits and polls, serves a canned job."""

    def __init__(
        self,
        *,
        polls_before_done: int = 1,
        job: dict[str, Any] | None = None,
        submit_status: int = 202,
        operation_location: bool = True,
        running_forever: bool = False,
    ) -> None:
        self.polls_before_done = polls_before_done
        self.job = LAYOUT_JOB if job is None else job
        self.submit_status = submit_status
        self.operation_location = operation_location
        self.running_forever = running_forever
        self.submissions: list[tuple[str, bytes, dict[str, str]]] = []
        self.poll_headers: list[dict[str, str]] = []
        self.poll_count = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            self.submissions.append(
                (str(request.url), request.content, dict(request.headers))
            )
            headers = (
                {"Operation-Location": "https://di.example/op/1"}
                if self.operation_location
                else {}
            )
            return httpx.Response(self.submit_status, headers=headers)
        self.poll_count += 1
        self.poll_headers.append(dict(request.headers))
        if self.running_forever or self.poll_count < self.polls_before_done:
            return httpx.Response(200, json={"status": "running"})
        return httpx.Response(200, json=self.job)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def patch_ocr(monkeypatch: pytest.MonkeyPatch, stub: DiStub) -> None:
    """Route pdfread's internally-constructed OcrClient through the stub transport."""
    monkeypatch.setattr(
        pdfread, "OcrClient", lambda s: OcrClient(s, transport=stub.transport)
    )


# ---------------------------------------------------------------------------
# PDF with a text layer -> PyMuPDF
# ---------------------------------------------------------------------------
def test_text_pdf_reads_with_pymupdf() -> None:
    view, provider = read_document(text_pdf(), filename="form.pdf", settings=make_settings())
    assert provider == "pymupdf"
    assert view.raw["provider"] == "pymupdf"
    assert [p.page for p in view.pages] == [1]
    assert view.pages[0].unit == "point"
    assert round(view.pages[0].width) == 612
    assert round(view.pages[0].height) == 792
    assert view.blocks
    assert "Know Your Customer" in view.text()
    assert all(b.zone is Zone.body for b in view.blocks)


def test_text_pdf_blocks_carry_rect_quads() -> None:
    view, _ = read_document(text_pdf(), filename=None, settings=make_settings())
    for block in view.blocks:
        assert block.bbox is not None and len(block.bbox) == 8
        # Quad is the rectangle convention [x0,y0, x1,y0, x1,y1, x0,y1].
        assert block.bbox[0] == block.bbox[6]  # x0 repeats bottom-left
        assert block.bbox[2] == block.bbox[4]  # x1 repeats bottom-right
        assert block.bbox[1] == block.bbox[3]  # y0 across the top edge
        assert block.bbox[5] == block.bbox[7]  # y1 across the bottom edge
        assert block.bbox[0] < block.bbox[2] and block.bbox[1] < block.bbox[5]


def test_multipage_text_pdf_numbers_pages() -> None:
    view, provider = read_document(text_pdf(3), filename=None, settings=make_settings())
    assert provider == "pymupdf"
    assert [p.page for p in view.pages] == [1, 2, 3]
    assert sorted({b.page for b in view.blocks}) == [1, 2, 3]


def test_max_pages_bounds_the_read() -> None:
    view, _ = read_document(text_pdf(3), filename=None, settings=make_settings(max_pages=2))
    assert [p.page for p in view.pages] == [1, 2]
    assert view.raw["pages_total"] == 3
    assert view.raw["pages_read"] == 2


def test_oversize_document_refused() -> None:
    with pytest.raises(ValueError, match="byte"):
        read_document(text_pdf(), filename=None, settings=make_settings(max_bytes=10))


# ---------------------------------------------------------------------------
# Recognition required, no endpoint -> NeedsRecognition (API: 422)
# ---------------------------------------------------------------------------
def test_scanned_pdf_without_endpoint_raises_needs_recognition() -> None:
    with pytest.raises(NeedsRecognition) as excinfo:
        read_document(scanned_pdf(), filename="scan.pdf", settings=make_settings())
    assert "no OCR endpoint is configured" in excinfo.value.reason
    assert "text layer" in excinfo.value.reason


def test_mixed_pdf_without_endpoint_raises_needs_recognition() -> None:
    with pytest.raises(NeedsRecognition) as excinfo:
        read_document(mixed_pdf(), filename=None, settings=make_settings())
    assert "1 of 2 page(s)" in excinfo.value.reason


def test_image_without_endpoint_raises_needs_recognition() -> None:
    with pytest.raises(NeedsRecognition) as excinfo:
        read_document(png_bytes(), filename="photo.png", settings=make_settings())
    assert "not a PDF" in excinfo.value.reason
    assert "image/png" in excinfo.value.reason


# ---------------------------------------------------------------------------
# Recognition with an endpoint -> whole document to DI, provider azure_layout
# ---------------------------------------------------------------------------
def test_mixed_pdf_goes_to_di_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    """The border case: page 1 text + page 2 scan sends the WHOLE PDF to DI in one call."""
    data = mixed_pdf()
    stub = DiStub(polls_before_done=2)
    patch_ocr(monkeypatch, stub)

    view, provider = read_document(data, filename="mixed.pdf", settings=di_settings())

    assert provider == "azure_layout"
    assert len(stub.submissions) == 1
    url, body, headers = stub.submissions[0]
    assert body == data  # the whole document, byte-for-byte — not just the scanned page
    assert "documentModels/prebuilt-layout:analyze" in url
    assert "api-version=2024-11-30" in url
    assert headers["content-type"] == "application/pdf"
    # The result is the adapter's view of the stub payload, not PyMuPDF's view of page 1.
    assert view.raw["provider"] == "azure-prebuilt-layout"
    assert "RECOGNISED TITLE" in view.text()
    assert any(b.zone is Zone.title for b in view.blocks)


def test_scanned_pdf_goes_to_di(monkeypatch: pytest.MonkeyPatch) -> None:
    data = scanned_pdf()
    stub = DiStub()
    patch_ocr(monkeypatch, stub)
    view, provider = read_document(data, filename=None, settings=di_settings())
    assert provider == "azure_layout"
    assert stub.submissions[0][1] == data
    assert view.pages and view.pages[0].unit == "inch"


def test_image_goes_to_di_with_sniffed_content_type(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = DiStub()
    patch_ocr(monkeypatch, stub)
    view, provider = read_document(png_bytes(), filename="scan.png", settings=di_settings())
    assert provider == "azure_layout"
    assert stub.submissions[0][2]["content-type"] == "image/png"
    assert "RECOGNISED TITLE" in view.text()


# ---------------------------------------------------------------------------
# OcrClient protocol, against MockTransport only
# ---------------------------------------------------------------------------
def test_ocr_client_submits_and_polls_to_terminal() -> None:
    stub = DiStub(polls_before_done=3)
    client = OcrClient(di_settings(), transport=stub.transport)
    job = client.analyze(b"pdf-bytes", content_type="application/pdf")
    assert job["status"] == "succeeded"
    assert stub.poll_count == 3
    _, body, headers = stub.submissions[0]
    assert body == b"pdf-bytes"
    assert headers["ocp-apim-subscription-key"] == "unit-test-key"
    assert all(h["ocp-apim-subscription-key"] == "unit-test-key" for h in stub.poll_headers)


def test_ocr_client_non_202_submit_is_an_error() -> None:
    stub = DiStub(submit_status=403)
    client = OcrClient(di_settings(), transport=stub.transport)
    with pytest.raises(OcrError, match="403"):
        client.analyze(b"x")
    assert stub.poll_count == 0


def test_ocr_client_missing_operation_location_is_an_error() -> None:
    stub = DiStub(operation_location=False)
    client = OcrClient(di_settings(), transport=stub.transport)
    with pytest.raises(OcrError, match="Operation-Location"):
        client.analyze(b"x")


def test_ocr_client_poll_cap_times_out() -> None:
    stub = DiStub(running_forever=True)
    client = OcrClient(di_settings(ocr_max_polls=3), transport=stub.transport)
    with pytest.raises(OcrTimeout):
        client.analyze(b"x")
    assert stub.poll_count == 3


def test_ocr_client_failed_job_is_an_error() -> None:
    stub = DiStub(job={"status": "failed", "error": {"code": "InvalidContent"}})
    client = OcrClient(di_settings(), transport=stub.transport)
    with pytest.raises(OcrError, match="failed"):
        client.analyze(b"x")


def test_ocr_client_requires_an_endpoint() -> None:
    with pytest.raises(ValueError):
        OcrClient(make_settings())
