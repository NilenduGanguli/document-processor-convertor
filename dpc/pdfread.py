"""Raw document bytes -> (:class:`~dpc.models.LayoutView`, provider name).

The ``document`` input kind lands here: base64-decoded bytes plus a filename hint, and no
statement about what they are. Two paths out, chosen by what the bytes actually contain:

* **PDF with a text layer** — read in-process with PyMuPDF. One :class:`TextBlock` per text
  block from ``page.get_text("dict")``, block rectangles carried as quads, page sizes from
  ``page.rect`` (points). Provider ``"pymupdf"``.
* **Anything that needs optical recognition** — a non-PDF image, or a PDF with *any* scanned
  page — goes to Azure Document Intelligence via :class:`~dpc.ocr_client.OcrClient`, **whole**,
  and comes back through :func:`dpc.adapters.from_azure`. Provider ``"azure_layout"``.

Whether a page is a scan is the same per-page rule the sibling DCE service measured its way
to: below ``settings.min_alnum_chars`` alphanumeric characters AND carrying at least one
image. A sparse page with no pixels is not a scan — OCR would recover nothing — so it is
absent. Watermarks and "Scanned by …" artefacts clear a lower bar; real prose clears this one.

**The whole document goes to DI when any page is a scan** — including the border case of a
PDF whose page 1 has text but whose page 2 is a picture. Converting half a document with
PyMuPDF and half with DI would splice two providers' geometry conventions and reading orders
into one output; DI reads PDFs natively and returns every page in one coherent payload, so
the mixed document is sent as one piece, exactly as DCE does.

**No OCR endpoint configured is a structured refusal, not a guess**: :class:`NeedsRecognition`
carries a reason a caller can read, and the API maps it to HTTP 422 ``needs_ocr``. The reason
names counts and floors — never a single character of document text, because this is a KYC
service and log lines and error bodies both travel.
"""
from __future__ import annotations

import logging

import pymupdf

from dpc.adapters import from_azure
from dpc.config import Settings
from dpc.models import LayoutView, PageInfo, Quad, TextBlock, Zone
from dpc.ocr_client import OcrClient

logger = logging.getLogger(__name__)

#: Provider names as the API reports them (`to_pmd(provider=…)`, the ``conversions`` row).
PROVIDER_PYMUPDF = "pymupdf"
PROVIDER_AZURE_LAYOUT = "azure_layout"

#: Magic prefixes -> MIME type, for the Content-Type DI is told. DI decides its parser from
#: this, so unmapped bytes go as ``application/octet-stream`` rather than a guessed image type.
_MAGIC: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
]

_EXTENSION_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "webp": "image/webp",
    "heic": "image/heic",
    "pdf": "application/pdf",
}


class NeedsRecognition(Exception):
    """The document needs optical recognition and this deployment configured no endpoint.

    Raised instead of guessing: an image carries no text, converting one requires
    recognition, and with ``DPC_AZURE_DI_ENDPOINT`` empty there is nowhere to do it. The API
    maps this to HTTP 422 ``{"error": "needs_ocr", "detail": reason}``.

    Attributes:
        reason: A sentence naming counts and configuration — never document text.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _is_pdf(data: bytes) -> bool:
    """Whether the bytes are a PDF. The spec allows junk before the header; 1 KiB is scanned."""
    return b"%PDF-" in data[:1024]


def _content_type(data: bytes, filename: str | None) -> str:
    """MIME type for the DI submit: magic bytes first, filename extension as the fallback."""
    if _is_pdf(data):
        return "application/pdf"
    for prefix, mime in _MAGIC:
        if data.startswith(prefix):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if filename and "." in filename:
        return _EXTENSION_TYPES.get(filename.rsplit(".", 1)[-1].lower(), "application/octet-stream")
    return "application/octet-stream"


def _count_alnum(text: str) -> int:
    return sum(1 for char in text if char.isalnum())


def _page_blocks(page: pymupdf.Page, number: int) -> list[TextBlock]:
    """One :class:`TextBlock` per PyMuPDF text block, in document order.

    Image blocks (``type`` 1) are skipped — they carry no text, and their presence alone does
    not make the page a scan (the alnum floor decides that). Lines within a block are joined
    with newlines; spans within a line are concatenated, which is how PyMuPDF splits styled
    runs of one visual line.
    """
    blocks: list[TextBlock] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        lines: list[str] = []
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if text.strip():
                lines.append(text.strip())
        text = "\n".join(lines).strip()
        if not text:
            continue
        x0, y0, x1, y1 = (float(v) for v in block["bbox"])
        bbox: Quad = [x0, y0, x1, y0, x1, y1, x0, y1]
        blocks.append(TextBlock(text=text, zone=Zone.body, page=number, bbox=bbox))
    return blocks


def _recognize(
    data: bytes, *, content_type: str, settings: Settings, reason: str
) -> tuple[LayoutView, str]:
    """The OCR path: whole document to DI, or a structured refusal when there is no DI."""
    if not settings.azure_di_endpoint.strip():
        raise NeedsRecognition(
            f"{reason}; no OCR endpoint is configured (DPC_AZURE_DI_ENDPOINT)"
        )
    client = OcrClient(settings)
    job = client.analyze(data, content_type=content_type)
    view = from_azure(job)
    logger.info(
        "read.recognized provider=%s pages=%d blocks=%d tables=%d",
        PROVIDER_AZURE_LAYOUT,
        len(view.pages),
        len(view.blocks),
        len(view.tables),
    )
    return view, PROVIDER_AZURE_LAYOUT


def _read_pdf(
    data: bytes, *, filename: str | None, settings: Settings
) -> tuple[LayoutView, str]:
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # pymupdf raises its own hierarchy; the type is the diagnosis
        raise ValueError(f"cannot open PDF: {type(exc).__name__}") from exc
    with doc:
        if doc.needs_pass:
            raise ValueError("PDF is encrypted; its text layer cannot be read")
        page_total = doc.page_count
        if page_total == 0:
            raise ValueError("PDF contains no pages")
        read_n = min(page_total, settings.max_pages)

        pages: list[PageInfo] = []
        blocks: list[TextBlock] = []
        scanned = 0
        for index in range(read_n):
            page = doc[index]
            number = index + 1
            rect = page.rect
            pages.append(
                PageInfo(
                    page=number,
                    width=float(rect.width),
                    height=float(rect.height),
                    unit="point",
                )
            )
            page_blocks = _page_blocks(page, number)
            sparse = sum(_count_alnum(b.text) for b in page_blocks) < settings.min_alnum_chars
            # Below the floor AND carrying pixels = a scan. Below the floor with NO images is
            # merely a sparse page — a blank separator, an EDGAR "TABLE OF CONTENTS" link
            # anchor — and OCR would recover nothing from it. The sibling DCE service shipped
            # the naive version of this rule and it sent a 119-page born-digital proxy
            # statement to OCR because its blank pages tripped the floor; the corpus sweep
            # reproduced exactly that here (ReadTimeout on a filing with no scan in it).
            if sparse and page.get_images():
                scanned += 1
            else:
                blocks.extend(page_blocks)

    if scanned:
        # Any scanned page sends the WHOLE document to DI — including a PDF whose page 1 has
        # text but whose page 2 is a picture. Half-converting with two providers would splice
        # two geometry conventions into one file; DI reads the full PDF in one coherent pass.
        logger.info(
            "read.scan_detected pages_read=%d scanned_pages=%d floor=%d",
            read_n,
            scanned,
            settings.min_alnum_chars,
        )
        return _recognize(
            data,
            content_type="application/pdf",
            settings=settings,
            reason=(
                f"PDF has no usable text layer on {scanned} of {read_n} page(s) "
                f"(below the {settings.min_alnum_chars} alphanumeric-character floor); "
                "the document requires optical recognition"
            ),
        )

    logger.info(
        "read.pymupdf pages_total=%d pages_read=%d blocks=%d chars=%d",
        page_total,
        read_n,
        len(blocks),
        sum(len(b.text) for b in blocks),
    )
    view = LayoutView(
        pages=pages,
        blocks=blocks,
        raw={
            "provider": PROVIDER_PYMUPDF,
            "pages_total": page_total,
            "pages_read": read_n,
        },
    )
    return view, PROVIDER_PYMUPDF


def _is_xlsx(data: bytes) -> bool:
    """A zip whose members say xlsx. Content, never the filename: a caller's name is a hint
    and the corpus sweep sent real .xlsx bytes to an OCR mock because nothing looked."""
    if not data.startswith(b"PK\x03\x04"):
        return False
    import io
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist()[:50])
    except Exception:  # noqa: BLE001 - not a readable zip, so not an xlsx
        return False
    return "xl/workbook.xml" in names or "[Content_Types].xml" in names and any(
        n.startswith("xl/") for n in names
    )


def _looks_html(data: bytes) -> bool:
    """Markup sniff over the first KiB, tolerant of BOMs and leading whitespace."""
    head = data[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<body")) or (
        head.startswith(b"<") and b"<html" in data[:4096].lower()
    )


def read_document(
    data: bytes, *, filename: str | None, settings: Settings
) -> tuple[LayoutView, str]:
    """Read raw document bytes into a provider-neutral view.

    Args:
        data: The document, decoded (the API decodes base64 before calling).
        filename: Caller's filename hint; used only to pick a Content-Type for the DI submit
            when the bytes' magic is unrecognised. Never trusted over the bytes themselves.
        settings: Size/page bounds, the scan floor, and the (optional) DI endpoint.

    Returns:
        ``(view, provider)`` where provider is ``"pymupdf"`` for a text-layer PDF and
        ``"azure_layout"`` for anything that went through Document Intelligence.

    Raises:
        NeedsRecognition: Recognition is required and no endpoint is configured (API: 422).
        ValueError: The input exceeds ``max_bytes``, or is a PDF that cannot be read at all
            (corrupt, encrypted, zero pages) — states OCR would not fix.
        dpc.ocr_client.OcrError: The configured DI endpoint refused or failed the analyse.
        dpc.ocr_client.OcrTimeout: The DI job outlived the configured polling bounds.
    """
    if len(data) > settings.max_bytes:
        raise ValueError(
            f"document is {len(data)} bytes, over the {settings.max_bytes}-byte limit"
        )
    if _is_pdf(data):
        return _read_pdf(data, filename=filename, settings=settings)
    if _is_xlsx(data):
        from dpc.xlsxread import read_xlsx

        return read_xlsx(data), "openpyxl"
    if _looks_html(data):
        from dpc.htmlread import read_html

        return read_html(data), "htmlread"
    content_type = _content_type(data, filename)
    return _recognize(
        data,
        content_type=content_type,
        settings=settings,
        reason=f"input is not a PDF ({content_type}); optical recognition is required",
    )


__all__ = [
    "PROVIDER_AZURE_LAYOUT",
    "PROVIDER_PYMUPDF",
    "NeedsRecognition",
    "read_document",
]
