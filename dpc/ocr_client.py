"""Azure Document Intelligence client — the recognition path for scans and images.

This is the convertor's one outbound call. A raw document whose text layer is absent (a
scanned PDF page, or a plain image) cannot be converted without optical recognition, and this
deployment does recognition by calling Azure Document Intelligence ``prebuilt-layout`` —
deliberately, and only when :attr:`dpc.config.Settings.azure_di_endpoint` says where.

The protocol is Azure's asynchronous shape, the same one the sibling DCE service already
proved out (``dce/ingest/ocr_service.py``): ``POST
{endpoint}/documentintelligence/documentModels/prebuilt-layout:analyze?api-version=…`` with
the raw bytes as the body, a ``202`` answer carrying an ``Operation-Location`` header, then
``GET`` that URL until ``status`` reaches a terminal value.

**The whole document goes in one call.** Document Intelligence accepts PDFs and images
natively, so nothing is rasterised here — and the payload that comes back is byte-for-byte
the shape a caller would have posted as ``azure_analyze_result``, so the same adapter
(:func:`dpc.adapters.from_azure`) maps it. The service call is exactly the caller-supplied
path with the call made here instead of there.

Polling is bounded twice — a wall clock (``ocr_timeout_seconds``) *and* a poll count
(``ocr_max_polls``) — because a provider that answers instantly with a non-terminal status
would otherwise be hammered for the whole timeout.

**No log line here carries document text.** An OCR response *is* the document's text, and on
a KYC deployment that is customer PII going to wherever logs are shipped. Every log line
below carries counts, statuses and hosts only.
"""
from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from dpc.config import Settings

logger = logging.getLogger(__name__)

#: Terminal job statuses. Anything else means "keep polling".
_TERMINAL = {"succeeded", "failed"}


class OcrError(RuntimeError):
    """The OCR service refused, failed, or answered with something unusable."""


class OcrTimeout(OcrError):
    """The OCR job did not reach a terminal status within the configured bounds."""


def _shape_of(job: dict[str, Any]) -> str:
    """What came back, counted rather than quoted — no document text in any log line."""
    result = job.get("analyzeResult")
    if not isinstance(result, dict):
        return "shape=unreadable"
    pages = result.get("pages")
    paragraphs = result.get("paragraphs")
    tables = result.get("tables")
    return (
        f"pages={len(pages) if isinstance(pages, list) else 0} "
        f"paragraphs={len(paragraphs) if isinstance(paragraphs, list) else 0} "
        f"tables={len(tables) if isinstance(tables, list) else 0}"
    )


class OcrClient:
    """Bounded 202 + ``Operation-Location`` + poll client for Azure DI ``prebuilt-layout``.

    Args:
        settings: Where the endpoint, key, API version and the polling bounds come from.
        transport: Optional ``httpx`` transport, injected by tests
            (:class:`httpx.MockTransport`) so the protocol is testable without a socket.

    Raises:
        ValueError: Constructed with no endpoint configured. The caller
            (:func:`dpc.pdfread.read_document`) must check first and raise
            :class:`~dpc.pdfread.NeedsRecognition` instead — reaching here without an
            endpoint is a programming error, not a deployment state.
    """

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None):
        if not settings.azure_di_endpoint.strip():
            raise ValueError(
                "OcrClient constructed with no azure_di_endpoint; the caller must raise "
                "NeedsRecognition before constructing a client"
            )
        self._settings = settings
        self._transport = transport

    # -- naming ---------------------------------------------------------------
    @property
    def host(self) -> str:
        """Endpoint host — what a log line names instead of a URL with query parts."""
        endpoint = self._settings.azure_di_endpoint
        parsed = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
        return parsed.hostname or ""

    @property
    def analyze_url(self) -> str:
        """The submit URL (query string excluded; ``api-version`` travels as a param)."""
        endpoint = self._settings.azure_di_endpoint.rstrip("/")
        return f"{endpoint}/documentintelligence/documentModels/prebuilt-layout:analyze"

    def _headers(self, *, content_type: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if content_type is not None:
            headers["Content-Type"] = content_type
        if self._settings.azure_di_key:
            headers["Ocp-Apim-Subscription-Key"] = self._settings.azure_di_key
        return headers

    # -- the one public operation --------------------------------------------
    def analyze(
        self, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> dict[str, Any]:
        """Submit the whole document, poll to a terminal status, and return the job JSON.

        Args:
            data: The entire document — a PDF or an image; DI parses both natively, so the
                caller never rasterises and never splits.
            content_type: The MIME type submitted with the bytes. DI decides its parser from
                this, so a wrong value is a failed analyse rather than a wrong answer.

        Returns:
            The terminal job document (``{"status": "succeeded", "analyzeResult": {…}}``),
            byte-compatible with what a caller would have posted as ``azure_analyze_result``.

        Raises:
            OcrError: Transport failure, a non-202 submit, a 202 with no
                ``Operation-Location``, a non-object job body, or a terminal ``failed``.
            OcrTimeout: Neither bound was reached with a terminal status — the wall clock
                (``ocr_timeout_seconds``) or the poll cap (``ocr_max_polls``).
        """
        budget = float(self._settings.ocr_timeout_seconds)
        started = time.monotonic()
        try:
            with httpx.Client(timeout=budget, transport=self._transport) as client:
                operation_url = self._submit(client, data, content_type)
                job = self._poll(client, operation_url, started, budget)
        except httpx.HTTPError as exc:
            # Names the host and the exception type only: a transport error can carry the
            # request body, and this one's body is a customer's document.
            raise OcrError(
                f"OCR call to {self.host!r} failed: {type(exc).__name__}"
            ) from exc

        status = str(job.get("status") or "").lower()
        if status != "succeeded":
            error = job.get("error")
            code = str(error.get("code")) if isinstance(error, dict) else ""
            raise OcrError(
                f"OCR job at {self.host!r} ended with status {status!r}"
                + (f" (code {code})" if code else "")
                + "; the document could not be analysed"
            )
        return job

    # -- protocol steps -------------------------------------------------------
    def _submit(self, client: httpx.Client, data: bytes, content_type: str) -> str:
        logger.info(
            "ocr.submit host=%s bytes=%d content_type=%s", self.host, len(data), content_type
        )
        response = client.post(
            self.analyze_url,
            content=data,
            params={"api-version": self._settings.azure_di_api_version},
            headers=self._headers(content_type=content_type),
        )
        if response.status_code != 202:
            logger.warning(
                "ocr.submit host=%s refused: HTTP %s", self.host, response.status_code
            )
            raise OcrError(
                f"OCR endpoint {self.host!r} answered the analyse request with HTTP "
                f"{response.status_code} rather than 202"
            )
        operation_url = response.headers.get("Operation-Location", "")
        if not operation_url:
            raise OcrError(
                f"OCR endpoint {self.host!r} returned 202 with no Operation-Location "
                "header; there is nothing to poll"
            )
        return operation_url

    def _poll(
        self, client: httpx.Client, operation_url: str, started: float, budget: float
    ) -> dict[str, Any]:
        """Poll to a terminal status under two independent bounds (wall clock, poll count)."""
        interval = float(self._settings.ocr_poll_interval_seconds)
        for attempt in range(max(1, self._settings.ocr_max_polls)):
            elapsed = time.monotonic() - started
            if elapsed > budget:
                break
            time.sleep(min(interval, max(0.0, budget - elapsed)))
            response = client.get(operation_url, headers=self._headers())
            response.raise_for_status()
            job = response.json()
            if not isinstance(job, dict):
                raise OcrError(f"OCR endpoint {self.host!r} returned a non-object job document")
            status = str(job.get("status") or "").lower()
            logger.debug(
                "ocr.poll host=%s attempt=%d status=%s", self.host, attempt + 1, status or "?"
            )
            if status in _TERMINAL:
                logger.info(
                    "ocr.done host=%s status=%s polls=%d ms=%d %s",
                    self.host,
                    status,
                    attempt + 1,
                    int((time.monotonic() - started) * 1000),
                    _shape_of(job),
                )
                return job
        raise OcrTimeout(
            f"OCR job at {self.host!r} did not finish within "
            f"{self._settings.ocr_timeout_seconds:g}s / {self._settings.ocr_max_polls} polls; "
            "the document was not converted rather than the request being held open"
        )


__all__ = ["OcrClient", "OcrError", "OcrTimeout"]
