"""Raw document bytes -> (:class:`~dpc.models.LayoutView`, provider name).

The ``document`` input kind lands here: base64-decoded bytes plus a filename hint, and no
statement about what they are. **Every renderable input goes to Azure Document Intelligence
``prebuilt-layout``.** There is no local text reader on the product path any more.

Why the local PyMuPDF text path is gone
---------------------------------------
The PMD 2.0 canvas needs *line* polygons — one atom per visual row — and only DI produces
them. PyMuPDF's ``page.get_text("dict")`` returns block hulls, and a hull spans many visual
rows, so its geometry is precisely the geometry that cannot be placed on a canvas. Keeping
the local path would have produced a corpus that is silently half spatial and half not, with
nothing in either artifact to say which half it came from. So the per-page "is this a scan"
test (``settings.min_alnum_chars`` and the ``get_images()`` heuristic) is deleted along with
it: the question it answered has exactly one answer now.

PyMuPDF survives for two jobs, neither of which reads text on the product path:

* :func:`_precheck_pdf` — open the bytes, refuse a corrupt, encrypted, zero-page or
  over-``max_pages`` PDF *before* spending an Azure call. Azure bills per page; a 2100-page
  PDF is refused locally, in milliseconds, with a message that names the bound.
* ``tools/di_stub.py`` — the offline fixture synthesiser, test-time only, never imported by
  this module. It is what lets a developer with no Azure key run the whole suite: the product
  path is Azure-only, the test path never calls Azure.

An escape hatch exists and is off (``DPC_ALLOW_LOCAL_PDF_TEXT``). Turned on, the old block
reader returns for air-gapped development; it is not a supported production mode, and an
artifact produced that way is identifiable because the provider is ``pymupdf``.

Why Office and text formats are exceptions
------------------------------------------
Microsoft states officially that DOCX/XLSX/PPTX/HTML are **not rendered**, so DI returns no
``polygon``, no ``boundingRegions``, no ``pages[].lines[]`` and no page dimensions for them —
and, for XLSX specifically, no tables at all. Sending a spreadsheet to Azure would gain
nothing spatial under any configuration and would *lose* the row/column structure
``dpc/xlsxread.py`` produces today. DI v4.0 does not accept ``.txt``/``.csv``/``.md``/
``.eml``/``.msg`` at all. So both families get an explicit route
(``DPC_OFFICE_ROUTE``/``DPC_TEXT_ROUTE``) whose default is the honest one, and the canvas gate
downstream keys off measured geometry, never off a provider name — a DOCX sent to Azure lands
in ``layout: linear-only`` by measurement, and the file says so.

**No endpoint configured is a structured refusal, not a fallback.** A local fallback produces
a *perfectly valid* PMD file — pages, blocks, anchors, no canvases — that nothing warns about,
so "the columns feature doesn't work on our documents" arrives with no evidence attached. A
silent degradation indistinguishable from a correct answer is worse than a refusal a caller
can read. :class:`NeedsRecognition` carries a reason naming media type, counts and the
variable to set — never a character of document text, because this is a KYC service and log
lines and error bodies both travel.
"""
from __future__ import annotations

import io
import logging
import shlex
import subprocess
import tempfile
import zipfile
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pymupdf

from dpc.adapters import from_azure, from_plain_text
from dpc.config import Settings
from dpc.models import LayoutView, PageInfo, Quad, TextBlock, Zone
from dpc.ocr_client import OcrClient

logger = logging.getLogger(__name__)

#: Provider names as the API reports them (`to_pmd(provider=…)`, the ``conversions`` row).
PROVIDER_AZURE_LAYOUT = "azure_layout"
PROVIDER_OPENPYXL = "openpyxl"
PROVIDER_HTMLREAD = "htmlread"
PROVIDER_PLAIN_TEXT = "plain-text"
#: Retained as a name only: never returned unless ``settings.allow_local_pdf_text`` is on.
PROVIDER_PYMUPDF = "pymupdf"

#: What DI v4.0 (``2024-11-30``) accepts, exactly. A web search will confidently add
#: ``.txt``/``.eml``/``.md`` to this list; that is Azure AI *Content Understanding*, a
#: different product with a different limits page. It is not this API and must not get into
#: this codebase.
DI_MEDIA_TYPES: tuple[str, ...] = (
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/bmp",
    "image/heif",
    "text/html",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)

#: The image types DI accepts, named in refusals so a caller learns the remedy from the error.
_DI_IMAGE_TYPES = "image/jpeg, image/png, image/tiff, image/bmp, image/heif"

#: Magic prefixes -> MIME type. The bytes decide; the filename is only ever a tie-break.
#: ``image/gif`` and ``image/webp`` are here to be *recognised and refused* — they are not on
#: DI's accept list, and guessing a parser for them would put an unreadable body on the wire.
#:
#: BMP is deliberately **not** in this table: its signature is the two ASCII bytes ``BM``, and
#: two bytes of ASCII is not evidence. See :func:`_is_bmp`.
_MAGIC: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]

#: Every DIB header size Microsoft ever shipped, in the order the formats appeared:
#: BITMAPCOREHEADER, BITMAPINFOHEADER, and the V2/V3/V4/V5 extensions. The field is a
#: little-endian uint32 at offset 14, immediately behind the 14-byte BITMAPFILEHEADER, so a
#: file shorter than 26 bytes cannot carry even the smallest of them plus one pixel row.
_BMP_DIB_HEADER_SIZES = frozenset({12, 40, 52, 56, 64, 108, 124})

#: ISO-BMFF brands that mean HEIF/HEIC. They sit at offset 8, behind the box length.
_HEIF_BRANDS = (b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1")

#: OOXML media types, by the zip member prefix that identifies the part.
_OOXML: list[tuple[str, str, str]] = [
    ("xl/", "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("word/", "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("ppt/", "pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
]

#: Filename extension -> (kind, media type). Consulted only when the magic says nothing.
_EXTENSION_TYPES: dict[str, tuple[str, str]] = {
    "pdf": ("pdf", "application/pdf"),
    "png": ("image", "image/png"),
    "jpg": ("image", "image/jpeg"),
    "jpeg": ("image", "image/jpeg"),
    "tif": ("image", "image/tiff"),
    "tiff": ("image", "image/tiff"),
    "bmp": ("image", "image/bmp"),
    "heic": ("image", "image/heif"),
    "heif": ("image", "image/heif"),
    "gif": ("other", "image/gif"),
    "webp": ("other", "image/webp"),
    "xlsx": ("xlsx", _OOXML[0][2]),
    "docx": ("docx", _OOXML[1][2]),
    "pptx": ("pptx", _OOXML[2][2]),
    "html": ("html", "text/html"),
    "htm": ("html", "text/html"),
    "txt": ("text", "text/plain"),
    "csv": ("text", "text/csv"),
    "md": ("text", "text/markdown"),
    "log": ("text", "text/plain"),
    "eml": ("email", "message/rfc822"),
    "msg": ("other", "application/vnd.ms-outlook"),
}

#: Control characters a text file legitimately contains. Anything else in category Cc means
#: the bytes are binary that happens to decode, and binary must not reach a text route.
_TEXT_CONTROLS = frozenset("\t\n\r\f\v")

#: Header names that identify RFC 5322 bytes when the extension does not.
_EMAIL_HEADERS = ("from:", "to:", "subject:", "date:", "message-id:", "received:",
                  "mime-version:")

# -- the fixed-pitch wrapper used by DPC_TEXT_ROUTE=render --------------------
#: US Letter at 72 dpi, 1-inch margins, 10 pt Courier. Courier's advance is exactly 0.6 em, so
#: 468 pt of text width is exactly 78 columns and 648 pt of text height exactly 54 lines — the
#: wrap is integer arithmetic with no rounding to argue about. (The spec names DejaVu Sans
#: Mono; no DejaVu font file ships in this image and §8 defines no setting that points at one,
#: so the base-14 monospace face — always present, metrically fixed, never substituted — is
#: what is pinned here. Both properties the spec asks of the font hold.) What the base-14 face
#: does NOT have is coverage beyond Latin-1, and a missing glyph is drawn silently, so
#: :func:`_uncoverable_characters` refuses rather than shipping a page of notdefs to DI. A
#: deployment that needs Cyrillic, Devanagari or CJK on this route needs a wide font file in
#: the image and a setting pointing at it; §8 defines neither, so refusing is the honest state.
_TEXT_PAGE_WIDTH = 612
_TEXT_PAGE_HEIGHT = 792
_TEXT_MARGIN = 72
_TEXT_FONT = "cour"
_TEXT_FONT_SIZE = 10
_TEXT_LEADING = 12
_TEXT_COLUMNS = 78
_TEXT_ROWS = 54
_TEXT_TAB = 8
#: The pinned face's coverage, expressed as a codec used for the membership test. PyMuPDF
#: draws base-14 text with ``TEXT_ENCODING_LATIN`` — Latin-1, not WinAnsi — so the
#: cp1252-only characters (em dash, euro sign, curly quotes) are notdefs here too. Measured,
#: not assumed: every printable Latin-1 character round-trips through ``insert_text`` +
#: ``get_text`` unchanged except the soft hyphen, which draws as a hyphen; every character
#: outside Latin-1 that was tried draws as a notdef dot.
_RENDER_ENCODING = "latin-1"

#: Wall clock for one external render. Not a config setting: the spec's config surface names
#: none, and a render that has not finished in two minutes is a broken deployment, not a slow
#: one — the caller is holding an HTTP request open the whole time.
_RENDER_TIMEOUT_SECONDS = 120.0


class NeedsRecognition(Exception):
    """The document must go to Azure DI and this deployment configured no endpoint.

    Raised instead of falling back: with ``DPC_AZURE_DI_ENDPOINT`` empty there is nowhere to
    do the one thing that produces line geometry, and a local guess would be indistinguishable
    from a correct answer in the artifact (§2.3). The API maps this to HTTP 422
    ``{"error": "needs_ocr", "detail": reason}``.

    Attributes:
        reason: A sentence naming media type, counts and configuration — never document text.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnsupportedFormat(ValueError):
    """DI does not accept this format and no route is configured for it (API: 415).

    Subclasses :class:`ValueError` so a deployment whose API layer has not yet mapped the
    name still answers with a readable ``4xx`` body rather than an opaque 500.

    Attributes:
        reason: A sentence naming the detected media type and the remedies — never text.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Classification — the bytes decide, the filename is a tie-break
# ---------------------------------------------------------------------------
def _is_pdf(data: bytes) -> bool:
    """Whether the bytes are a PDF. The spec allows junk before the header; 1 KiB is scanned."""
    return b"%PDF-" in data[:1024]


def _is_bmp(data: bytes) -> bool:
    """Whether the bytes are a BMP, judged on the DIB header and not on ``BM`` alone.

    ``BM`` is two printable ASCII characters, so the bare prefix classifies "BMW Finance
    Ltd…", "BMO Harris…" and "BM Group…" — ordinary KYC correspondence — as a bitmap, and
    because the bytes outrank the filename a ``.txt`` name cannot rescue them: they are
    submitted to DI as ``image/bmp`` and come back as ``InvalidContent`` (a 502) or, against
    a lenient endpoint, as a valid-looking artifact with zero blocks. Requiring the DIB
    header size to be one of the six real values costs four bytes of checking and rejects
    every such string, while every genuine BMP carries one by construction.
    """
    if len(data) < 26 or not data.startswith(b"BM"):
        return False
    return int.from_bytes(data[14:18], "little") in _BMP_DIB_HEADER_SIZES


def _looks_html(data: bytes) -> bool:
    """Markup sniff over the first KiB, tolerant of BOMs and leading whitespace."""
    head = data[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<body")) or (
        head.startswith(b"<") and b"<html" in data[:4096].lower()
    )


def _ooxml_kind(data: bytes) -> tuple[str, str] | None:
    """``(kind, media type)`` for OOXML bytes, or ``None`` when this is not an OOXML zip.

    Content, never the filename: a caller's name is a hint, and the corpus sweep sent real
    ``.xlsx`` bytes to an OCR mock because nothing looked inside the zip.

    ``[Content_Types].xml`` is asked first, because it is the package's own authoritative
    statement of what it is. A DOCX with an embedded workbook has both a ``word/`` and an
    ``xl/`` part, and the older "first prefix in the list wins" scan read it as a spreadsheet
    and handed a deed to ``read_xlsx``. The prefix scan survives as a fallback for packages
    with no usable content-types part, and it now requires **exactly one** of the three
    prefixes: an ambiguous package returns ``None`` rather than a guess, which lands it on the
    filename hint and then on a readable 415 — never on the wrong reader.

    The whole member list is scanned. It was capped at 200 entries, which is an arbitrary
    truncation of a writer-dependent ordering: a media-heavy package whose identifying part
    sits past member 200 classified as ``other``. The cap bought nothing — ``any()`` over the
    full list already stops at the first match.
    """
    if not data.startswith(b"PK\x03\x04"):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            content_types = b""
            try:
                info = archive.getinfo("[Content_Types].xml")
                # Bounded read: this part is a few KiB in every real package, and an
                # unbounded read of an attacker-supplied zip member is a decompression bomb.
                if info.file_size <= 1 << 20:
                    content_types = archive.read(info)
            except Exception:  # noqa: BLE001, S110 - unreadable part: use the prefix scan
                # Not just KeyError. A corrupt deflate stream raises BadZipFile and a
                # password-protected member raises RuntimeError, and catching only the
                # missing-part case let either of those discard `names` too — so a DOCX whose
                # content-types part is damaged but whose `word/` members are intact stopped
                # being a DOCX and became a 415. The docstring promises the prefix scan as the
                # fallback "for packages with no usable content-types part"; unreadable is
                # exactly that, so it must reach the fallback rather than fail the package.
                pass
    except Exception:  # noqa: BLE001 - not a readable zip, so not an OOXML package
        return None
    #: The main part's content type is the package media type plus ``.main+xml`` for all
    #: three formats, so the two tables cannot drift apart.
    declared = [
        (kind, media_type)
        for _prefix, kind, media_type in _OOXML
        if f"{media_type}.main+xml".encode("ascii") in content_types
    ]
    if len(declared) == 1:
        return declared[0]
    present = [
        (kind, media_type)
        for prefix, kind, media_type in _OOXML
        if any(name.startswith(prefix) for name in names)
    ]
    if len(present) == 1:
        return present[0]
    return None


def _is_clean_text(text: str) -> bool:
    """Whether a decoded string is text rather than binary that happened to decode.

    Control characters outside :data:`_TEXT_CONTROLS` mean the bytes are binary, and binary
    must never reach a text route: it would be "converted" into a page of mojibake that looks
    like a successful conversion. Split out from :func:`_decode_text` because the same
    question has to be asked of a *decoded message body*, which never passes through the
    UTF-8 gate (a message declares its own charset).
    """
    for char in text:
        if char < " " and char not in _TEXT_CONTROLS:
            return False
        if char == "\x7f":
            return False
    return True


def _decode_text(data: bytes) -> str | None:
    """Decode text-like bytes, or ``None`` when the bytes are not text.

    UTF-8 (with or without a BOM) and BOM-marked UTF-16 only.
    """
    encoding = "utf-8"
    if data.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    try:
        text = data.decode(encoding)
    except (UnicodeDecodeError, UnicodeError):
        return None
    return text if _is_clean_text(text) else None


def _is_header_line(line: str) -> bool:
    """Whether one line is a well-formed RFC 5322 header field or a folded continuation.

    A field name is one or more printable US-ASCII characters other than the colon, and a
    continuation begins with a space or a tab. This is the part the old sniff left out, and
    leaving it out is what let ``Date:,From:,To:,Amount`` — a ledger CSV's header row — open
    a "message" whose second line is ``2024-01-01,ACME,BETA,10``.
    """
    if line[:1] in (" ", "\t"):
        return True
    name, separator, _ = line.partition(":")
    if not separator or not name:
        return False
    return all(" " < char < "\x7f" for char in name)


def _looks_email(text: str) -> bool:
    """RFC 5322 sniff: a header block before the first blank line, with real header names.

    *Every* line of the block must be a header field or a continuation, not merely the first,
    and two recognised field names must appear. A sniff is still only a sniff: :func:`_classify`
    consults an explicit ``.csv``/``.txt``/``.md`` extension ahead of this function, because
    the cost of a false positive here is that :func:`_message_body` deletes the first line of
    the document as an envelope and reports it as a one-word warning.
    """
    head = text[:8192].replace("\r\n", "\n").replace("\r", "\n")
    block = head.split("\n\n", 1)[0]
    lines = [line for line in block.split("\n") if line]
    if not lines or not all(_is_header_line(line) for line in lines):
        return False
    lowered = "\n".join(lines).lower()
    return sum(1 for name in _EMAIL_HEADERS if name in lowered) >= 2


def _classify(data: bytes, filename: str | None) -> tuple[str, str]:
    """``(kind, media type)`` for the bytes, magic first and the extension as a tie-break.

    Kinds are the routing table's rows: ``pdf``, ``image``, ``xlsx``/``docx``/``pptx``,
    ``html``, ``text``, ``email``, and ``other`` for everything that has no route at all.

    Args:
        data: The document bytes.
        filename: The caller's filename hint. Consulted only where the magic is silent, and
            never allowed to override it — a ``.pdf`` name on PNG bytes is a lie the bytes
            win. Among the text-ish kinds the magic is *always* silent (a ``.csv``, an
            ``.md``, an ``.eml`` and an HTML fragment are the same bytes to any sniff), so
            there the extension outranks the sniffs — see the note in the body.

    Returns:
        The routing kind and the media type used in DI submits and in refusal messages.
    """
    if _is_pdf(data):
        return "pdf", "application/pdf"
    for prefix, media_type in _MAGIC:
        if data.startswith(prefix):
            return ("other" if media_type == "image/gif" else "image"), media_type
    if _is_bmp(data):
        return "image", "image/bmp"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "other", "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS:
        return "image", "image/heif"
    ooxml = _ooxml_kind(data)
    if ooxml is not None:
        return ooxml
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        # Legacy OLE compound file: .doc/.xls/.ppt/.msg. No reader here, none at DI.
        return "other", "application/x-ole-storage"
    if _looks_html(data):
        return "html", "text/html"
    text = _decode_text(data)
    hinted = _EXTENSION_TYPES.get(_extension(filename))
    if text is not None:
        # Decodable text: the bytes have said everything they are going to say — a `.csv`, a
        # `.md`, an `.eml` and an `<h1>`-only HTML fragment are the same bytes to a sniff — so
        # the caller's extension is the only evidence left, and among the text-ish kinds it is
        # decisive. This does not weaken "bytes beat the filename": the bytes are silent here.
        #
        # It is decisive over `_looks_email` in particular. A ledger whose header row reads
        # `Date:,From:,To:,Amount` used to classify as mail, and `_message_body` then deleted
        # that row as an envelope with nothing but a warning to show for it — document text
        # dropped on a file that is not a message, which is exactly the silent degradation
        # §2.3 exists to forbid. It is also decisive for `.html`: `_looks_html` misses a
        # fragment with no `<html>` tag (common in mail- and CMS-exported files), and routing
        # that to plain text injects raw markup into the corpus as if it were the document.
        if hinted is not None and hinted[0] in ("text", "email", "html"):
            return hinted
        if _looks_email(text):
            return "email", "message/rfc822"
        return "text", "text/plain"
    if hinted is not None:
        return hinted
    return "other", "application/octet-stream"


def _extension(filename: str | None) -> str:
    return filename.rsplit(".", 1)[-1].lower() if filename and "." in filename else ""


# ---------------------------------------------------------------------------
# The DI seam
# ---------------------------------------------------------------------------
def _ocr_client(settings: Settings) -> OcrClient:
    """The single construction site for the DI client — the seam tests replace.

    ``tests/test_pdfread.py`` and ``tests/test_routing.py`` monkeypatch ``pdfread.OcrClient``
    to inject an :class:`httpx.MockTransport`; naming the seam makes that an intended contract
    rather than a fortunate accident, and lets a stub be injected without touching this
    module's public surface. No unit test opens a socket and no unit test needs a key.
    """
    return OcrClient(settings)


def _require_endpoint(
    settings: Settings, *, content_type: str, page_count: int | None = None
) -> None:
    """Refuse now if no DI endpoint is configured, before anything expensive happens.

    Called by :func:`_recognize` and again at the *top* of both render routes. A deployment
    with ``DPC_OFFICE_ROUTE=render`` and no endpoint used to spend up to 120 s of LibreOffice
    per request to produce a 422 it could have produced immediately; the text render route
    wrapped the whole document to a PDF for the same nothing.

    Raises:
        NeedsRecognition: ``DPC_AZURE_DI_ENDPOINT`` is empty.
    """
    if settings.azure_di_endpoint.strip():
        return
    pages = f", {page_count} page(s)" if page_count is not None else ""
    raise NeedsRecognition(
        f"the document source requires Azure Document Intelligence ({content_type}"
        f"{pages}); no endpoint is configured (DPC_AZURE_DI_ENDPOINT)"
    )


def _recognize(
    data: bytes,
    *,
    content_type: str,
    settings: Settings,
    page_count: int | None = None,
    renderer: str | None = None,
    warning: str | None = None,
) -> tuple[LayoutView, str]:
    """Send the whole document to DI ``prebuilt-layout``, or refuse for want of an endpoint.

    Args:
        data: The bytes to analyse, whole — DI reads PDFs and images natively, so nothing is
            rasterised, split or pre-processed here.
        content_type: The media type DI is told; it picks its parser from this.
        settings: Endpoint, key, api-version and the polling bounds.
        page_count: Pages, when a local pre-check counted them. Named in the refusal so a
            caller can see what the call would have cost.
        renderer: Renderer version stamp, when the bytes came from a render route. It joins
            the determinism envelope and is published in the front matter.
        warning: A caller-visible caveat to carry on the view (envelope discarded, and so on).

    Returns:
        ``(view, "azure_layout")``.

    Raises:
        NeedsRecognition: ``DPC_AZURE_DI_ENDPOINT`` is empty.
    """
    _require_endpoint(settings, content_type=content_type, page_count=page_count)
    client = _ocr_client(settings)
    job = client.analyze(data, content_type=content_type)
    view = from_azure(job)
    if renderer:
        view.raw["renderer"] = renderer
    if warning:
        view.raw["warning"] = warning
    logger.info(
        "read.recognized provider=%s content_type=%s pages=%d blocks=%d tables=%d",
        PROVIDER_AZURE_LAYOUT,
        content_type,
        len(view.pages),
        len(view.blocks),
        len(view.tables),
    )
    return view, PROVIDER_AZURE_LAYOUT


# ---------------------------------------------------------------------------
# PDF: a local pre-check, then Azure
# ---------------------------------------------------------------------------
def _precheck_pdf(data: bytes, *, settings: Settings) -> int:
    """Validity and size pre-flight on PDF bytes. Reads no text.

    Every refusal here is a state Azure would also fail on, or a bill nobody wants: an
    encrypted or zero-page PDF cannot be recognised either, and a document over
    ``max_pages`` is refused locally in milliseconds rather than page-by-page at the meter.

    Args:
        data: The PDF bytes.
        settings: Supplies ``max_pages``.

    Returns:
        The page count, for the log line and for the refusal message.

    Raises:
        ValueError: Corrupt, encrypted, zero pages, or over ``max_pages``.
    """
    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # pymupdf raises its own hierarchy; the type is the diagnosis
        raise ValueError(f"cannot open PDF: {type(exc).__name__}") from exc
    with document:
        if document.needs_pass:
            raise ValueError("PDF is encrypted; it cannot be read or recognised")
        page_count = int(document.page_count)
    if page_count == 0:
        raise ValueError("PDF contains no pages")
    if page_count > settings.max_pages:
        raise ValueError(
            f"PDF has {page_count} pages, over the {settings.max_pages}-page limit "
            "(DPC_MAX_PAGES)"
        )
    return page_count


def _local_pdf_text(data: bytes, *, page_count: int) -> tuple[LayoutView, str]:
    """The air-gapped escape hatch: PyMuPDF block text, off unless explicitly enabled.

    One :class:`TextBlock` per PyMuPDF text block. Image blocks (``type`` 1) carry no text and
    are skipped. **These are block hulls, not lines**: a hull spans many visual rows, so the
    canvas gate downstream will send every page of this view to linear output. That is the
    honest outcome — the provider name in the artifact is ``pymupdf`` and the file will show
    no canvases — and it is exactly why this is not a production mode.
    """
    pages: list[PageInfo] = []
    blocks: list[TextBlock] = []
    with pymupdf.open(stream=data, filetype="pdf") as document:
        for index in range(page_count):
            page = document[index]
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
            for block in page.get_text("dict").get("blocks", []):
                if block.get("type") != 0:
                    continue
                lines = [
                    "".join(span.get("text", "") for span in line.get("spans", [])).strip()
                    for line in block.get("lines", [])
                ]
                text = "\n".join(line for line in lines if line).strip()
                if not text:
                    continue
                x0, y0, x1, y1 = (float(value) for value in block["bbox"])
                bbox: Quad = [x0, y0, x1, y0, x1, y1, x0, y1]
                blocks.append(TextBlock(text=text, zone=Zone.body, page=number, bbox=bbox))
    logger.warning(
        "read.local_pdf_text pages=%d blocks=%d reason=DPC_ALLOW_LOCAL_PDF_TEXT",
        page_count,
        len(blocks),
    )
    view = LayoutView(
        pages=pages,
        blocks=blocks,
        raw={"provider": PROVIDER_PYMUPDF, "pages_total": page_count,
             "pages_read": page_count},
    )
    return view, PROVIDER_PYMUPDF


def _read_pdf(data: bytes, *, settings: Settings) -> tuple[LayoutView, str]:
    """PDF -> DI, after the local pre-check. No text is read locally on this path."""
    page_count = _precheck_pdf(data, settings=settings)
    if settings.allow_local_pdf_text:
        return _local_pdf_text(data, page_count=page_count)
    return _recognize(
        data, content_type="application/pdf", settings=settings, page_count=page_count
    )


# ---------------------------------------------------------------------------
# Office and HTML: DPC_OFFICE_ROUTE
# ---------------------------------------------------------------------------
def _render_argv(settings: Settings, source: Path, outdir: Path) -> list[str]:
    """The external render command, with its placeholders substituted.

    Raises:
        UnsupportedFormat: The route is selected but not configured — no command, or no
            version stamp to put in the determinism envelope.
        ValueError: The command is configured but names no ``{input}``/``{outdir}``.
    """
    template = shlex.split(settings.render_cmd)
    if not template:
        raise UnsupportedFormat(
            "DPC_OFFICE_ROUTE=render but DPC_RENDER_CMD is empty; set a renderer command, "
            "or use DPC_OFFICE_ROUTE=local or =azure"
        )
    if not settings.render_version.strip():
        raise UnsupportedFormat(
            "DPC_OFFICE_ROUTE=render but DPC_RENDER_VERSION is empty; the renderer version "
            "joins the determinism envelope and must be stamped at container build"
        )
    joined = " ".join(template)
    if "{input}" not in joined or "{outdir}" not in joined:
        raise ValueError("DPC_RENDER_CMD must contain the {input} and {outdir} placeholders")
    return [
        token.replace("{input}", str(source)).replace("{outdir}", str(outdir))
        for token in template
    ]


def _render_to_pdf(data: bytes, *, kind: str, settings: Settings) -> bytes:
    """Render Office/HTML bytes to PDF with ``DPC_RENDER_CMD``, and return the PDF bytes.

    The command runs as an argv list with no shell, on a temporary directory that holds the
    input and receives the output, so neither a filename nor a caller string is ever
    interpreted. Failure messages name the exit status and file counts and never the
    renderer's stderr: LibreOffice quotes document content in some diagnostics, and this is a
    KYC service.

    Raises:
        ValueError: The renderer is missing, timed out, failed, or produced other than
            exactly one PDF.
    """
    with tempfile.TemporaryDirectory(prefix="dpc-render-") as tmp:
        root = Path(tmp)
        source = root / f"input.{kind}"
        outdir = root / "out"
        outdir.mkdir()
        source.write_bytes(data)
        argv = _render_argv(settings, source, outdir)
        try:
            # argv list, no shell: nothing a caller supplies is ever interpreted.
            completed = subprocess.run(
                argv, capture_output=True, timeout=_RENDER_TIMEOUT_SECONDS, check=False
            )
        except FileNotFoundError as exc:
            raise ValueError(
                f"DPC_RENDER_CMD names an executable that is not present: {argv[0]!r}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ValueError(
                f"render timed out after {_RENDER_TIMEOUT_SECONDS:.0f}s (DPC_RENDER_CMD)"
            ) from exc
        if completed.returncode != 0:
            raise ValueError(f"render command exited {completed.returncode} (DPC_RENDER_CMD)")
        produced = sorted(p for p in outdir.iterdir() if p.suffix.lower() == ".pdf")
        if len(produced) != 1:
            raise ValueError(
                f"render produced {len(produced)} PDF(s) from a {kind} input; expected 1"
            )
        rendered = produced[0].read_bytes()
    logger.info(
        "read.rendered format=%s bytes_in=%d bytes_out=%d renderer=%s",
        kind,
        len(data),
        len(rendered),
        settings.render_version,
    )
    return rendered


def _read_office(
    data: bytes, *, kind: str, media_type: str, settings: Settings
) -> tuple[LayoutView, str]:
    """Route DOCX/PPTX/XLSX/HTML by ``DPC_OFFICE_ROUTE`` (§2.2).

    ``local`` keeps the readers that measure real structure out of these formats — and keeps
    XLSX tables, which DI would drop entirely. ``render`` is the only way to get true spatial
    fidelity out of a DOCX, at the cost of putting the renderer version in the determinism
    envelope. ``azure`` sends the original bytes, gaining nothing spatial and saying so once
    per request at WARNING.
    """
    route = settings.office_route
    if route == "local":
        if kind == "xlsx":
            from dpc.xlsxread import read_xlsx

            return read_xlsx(data), PROVIDER_OPENPYXL
        if kind == "html":
            from dpc.htmlread import read_html

            return read_html(data), PROVIDER_HTMLREAD
        raise UnsupportedFormat(
            f"{media_type} has no local reader (DPC_OFFICE_ROUTE=local); set "
            "DPC_OFFICE_ROUTE=render to convert it through a PDF, or =azure to send it to "
            "Document Intelligence without geometry"
        )
    if route == "render":
        # Before the subprocess, not after it: the render is worthless without an endpoint.
        _require_endpoint(settings, content_type=media_type)
        rendered = _render_to_pdf(data, kind=kind, settings=settings)
        page_count = _precheck_pdf(rendered, settings=settings)
        return _recognize(
            rendered,
            content_type="application/pdf",
            settings=settings,
            page_count=page_count,
            renderer=settings.render_version,
        )
    # route == "azure": honest but lossy, and the artifact will show it as linear-only.
    logger.warning(
        "route.azure_office_no_geometry format=%s reason=DI does not render this format",
        kind,
    )
    return _recognize(data, content_type=media_type, settings=settings)


# ---------------------------------------------------------------------------
# Text and mail: DPC_TEXT_ROUTE
# ---------------------------------------------------------------------------
def _message_body(data: bytes) -> str:
    """The decoded ``text/plain`` body of an RFC 5322 message, envelope discarded.

    Headers, attachments and alternative parts are dropped, which is a real loss and is why
    the caller is told about it. Nothing is invented in their place.

    Raises:
        UnsupportedFormat: The message carries no ``text/plain`` part to convert.
        ValueError: The bytes do not parse as a message at all.
    """
    try:
        message = BytesParser(policy=policy.default).parsebytes(data)
        body = message.get_body(preferencelist=("plain",))
        text = body.get_content() if body is not None else None
    # The stdlib parser raises broadly on malformed mail; the type is the whole diagnosis,
    # and the message must not carry a byte of the mail itself.
    except Exception as exc:
        raise ValueError(f"cannot parse message: {type(exc).__name__}") from exc
    if not text:
        raise UnsupportedFormat(
            "message carries no text/plain part; only the decoded text body is converted "
            "(DPC_TEXT_ROUTE=plain)"
        )
    return text


def _uncoverable_characters(text: str) -> int:
    """How many characters the pinned render face cannot draw. Counts only; never the text.

    ``insert_text`` reports nothing when it is handed a character outside the pinned face's
    coverage — it draws a notdef and returns a line count as if all were well. So a Cyrillic,
    Devanagari or CJK document, or a rupee sign, or an em dash (cp1252 has one; the Latin-1
    the renderer uses does not), reaches Azure as a page of dots, DI faithfully OCRs the
    dots, and a plausible
    PMD file is produced with the content gone and nothing anywhere saying so. That is the
    one outcome this codebase refuses on principle (§2.3), so the route refuses instead.
    """
    try:
        text.encode(_RENDER_ENCODING)
    except UnicodeEncodeError:
        pass
    else:
        return 0
    missing = set()
    for char in set(text):
        try:
            char.encode(_RENDER_ENCODING)
        except UnicodeEncodeError:
            missing.add(char)
    # sorted() only so the count is assembled in a fixed order, never a set's.
    return sum(text.count(char) for char in sorted(missing))


def _wrap_text_to_pdf(text: str, *, settings: Settings) -> bytes:
    """Lay text out on fixed-pitch pages and return PDF bytes, for ``DPC_TEXT_ROUTE=render``.

    Wrapping is a hard character wrap at :data:`_TEXT_COLUMNS`, never a word wrap: the input
    this route exists for is a fixed-column report, and a word wrap would move column 40 to a
    different place on every line, which is the exact property the canvas is meant to preserve.
    Tabs expand to :data:`_TEXT_TAB` for the same reason.

    The form feed is honoured as a page break, because the fixed-column report this route
    exists for is exactly the format whose page boundaries are written with ``\\f``. Treating
    it as an ordinary line break (which is what ``str.splitlines`` does) discarded the
    document's own pagination and re-cut pages every 54 rows.

    Raises:
        UnsupportedFormat: The text needs glyphs the pinned face does not have.
        ValueError: The wrap would exceed ``max_pages``.
    """
    uncoverable = _uncoverable_characters(text)
    if uncoverable:
        raise UnsupportedFormat(
            f"{uncoverable} character(s) fall outside the pinned render font's Latin-1 "
            "coverage and DPC_TEXT_ROUTE=render would draw them as blanks; set "
            "DPC_TEXT_ROUTE=plain to convert the text without geometry"
        )
    pages: list[list[str]] = []
    for chunk in text.split("\f"):
        rows: list[str] = []
        for source_line in chunk.splitlines():
            line = source_line.expandtabs(_TEXT_TAB).rstrip()
            if not line:
                rows.append("")
                continue
            for start in range(0, len(line), _TEXT_COLUMNS):
                rows.append(line[start:start + _TEXT_COLUMNS])
        # A chunk that produced no rows is the empty span a leading or trailing form feed
        # leaves behind, not a blank page the document asked for.
        for start in range(0, len(rows), _TEXT_ROWS):
            pages.append(rows[start:start + _TEXT_ROWS])
    if not pages:
        pages = [[]]
    page_total = len(pages)
    if page_total > settings.max_pages:
        raise ValueError(
            f"text wraps to {page_total} pages, over the {settings.max_pages}-page limit "
            "(DPC_MAX_PAGES)"
        )
    document = pymupdf.open()
    try:
        for page_rows in pages:
            page = document.new_page(width=_TEXT_PAGE_WIDTH, height=_TEXT_PAGE_HEIGHT)
            for row, line in enumerate(page_rows):
                if not line:
                    continue
                page.insert_text(
                    (_TEXT_MARGIN, _TEXT_MARGIN + _TEXT_LEADING * (row + 1)),
                    line,
                    fontname=_TEXT_FONT,
                    fontsize=_TEXT_FONT_SIZE,
                )
        # The intermediate PDF must be a pure function of the text: the DI stub replays a
        # recorded payload by sha256 of the submitted body (§2.5(iii)), so an unstable byte
        # anywhere in it would make this route unreplayable. An empty metadata block drops
        # the creation/modification timestamps, and ``no_new_id`` stops MuPDF minting a fresh
        # random trailer /ID on every save — the one remaining source of variance.
        document.set_metadata({})
        rendered: bytes = document.tobytes(no_new_id=True)
    finally:
        document.close()
    logger.info(
        "read.text_wrapped rows=%d pages=%d columns=%d",
        sum(len(page_rows) for page_rows in pages),
        page_total,
        _TEXT_COLUMNS,
    )
    return rendered


def _read_text(
    data: bytes, *, kind: str, media_type: str, settings: Settings
) -> tuple[LayoutView, str]:
    """Route TXT/CSV/MD/EML by ``DPC_TEXT_ROUTE`` (§2.4).

    DI accepts none of these formats, so there is no "send it to Azure" option to argue about:
    ``plain`` converts them with no geometry claimed and the ordering stated, ``render`` wraps
    them onto fixed-pitch pages and takes the PDF route, ``refuse`` is 415.
    """
    route = settings.text_route
    if route == "refuse":
        raise UnsupportedFormat(
            f"{media_type} is not accepted by Document Intelligence and DPC_TEXT_ROUTE=refuse; "
            "set DPC_TEXT_ROUTE=plain to convert the text without geometry, or =render to "
            "lay it out on fixed-pitch pages"
        )
    if kind == "email":
        # The gate belongs on the decoded BODY, not on the envelope bytes. A message states
        # its own charset and the stdlib parser honours it, so running the UTF-8 gate over the
        # raw bytes refused every legitimate 8-bit `.eml` — any French, German or Spanish KYC
        # correspondence saved as mail — from a route the configuration says handles it.
        warning = "message envelope discarded"
        text = _message_body(data)
        if not _is_clean_text(text):
            raise UnsupportedFormat(
                f"{media_type} decoded to a body containing binary control characters, not "
                "text; supply a format Document Intelligence accepts"
            )
    else:
        warning = None
        decoded = _decode_text(data)
        if decoded is None:
            raise UnsupportedFormat(
                f"{media_type} did not decode as UTF-8 or BOM-marked UTF-16 text; re-encode "
                "it, or supply a format Document Intelligence accepts"
            )
        text = decoded

    if route == "render":
        # Before the wrap, not after it: the wrapped PDF is worthless without an endpoint.
        _require_endpoint(settings, content_type=media_type)
        rendered = _wrap_text_to_pdf(text, settings=settings)
        page_count = _precheck_pdf(rendered, settings=settings)
        return _recognize(
            rendered,
            content_type="application/pdf",
            settings=settings,
            page_count=page_count,
            warning=warning,
        )

    view = from_plain_text(text)
    view.raw["media_type"] = media_type
    if warning:
        view.raw["warning"] = warning
    logger.info(
        "read.plain_text media_type=%s blocks=%d chars=%d", media_type, len(view.blocks),
        len(text),
    )
    return view, PROVIDER_PLAIN_TEXT


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------
def read_document(
    data: bytes, *, filename: str | None, settings: Settings
) -> tuple[LayoutView, str]:
    """Raw bytes -> ``(view, provider)``. Azure DI is the only text reader for renderable input.

    Routing, in order, decided by the BYTES (magic first, filename only as a tie-break):

    ==============================  ====================================================
    PDF, JPEG, PNG, BMP, TIFF, HEIF  Azure DI ``prebuilt-layout``, whole document, always.
    DOCX / PPTX / XLSX / HTML        ``settings.office_route`` (§2.2).
    TXT / CSV / MD / EML             ``settings.text_route`` (§2.4).
    anything else                    HTTP 415 ``unsupported_media_type``.
    ==============================  ====================================================

    There is no per-page scan test any more. ``settings.min_alnum_chars`` is retired: the
    question it answered ("is this page a scan, i.e. must it go to DI?") has one answer now.

    Args:
        data: The document, decoded (the API decodes base64 before calling).
        filename: Caller's filename hint. Used only where the magic is silent — to separate a
            ``.csv`` from a ``.txt``, or to name an extension-only format. Never trusted over
            the bytes.
        settings: Size and page bounds, the routes, and the (optional) DI endpoint.

    Returns:
        ``(view, provider)`` where provider is ``"azure_layout"`` for anything that went
        through Document Intelligence, ``"openpyxl"``/``"htmlread"`` for the local Office
        routes, ``"plain-text"`` for the text route, and ``"pymupdf"`` only under
        ``DPC_ALLOW_LOCAL_PDF_TEXT``.

    Raises:
        NeedsRecognition: No ``azure_di_endpoint`` is configured (API: 422). See §2.3.
        UnsupportedFormat: DI does not accept this format and no route is configured (415).
        ValueError: Over ``max_bytes``/``max_pages``, or a PDF that cannot be opened at all.
        dpc.ocr_client.OcrError: The configured DI endpoint refused or failed the analyse.
        dpc.ocr_client.OcrTimeout: The DI job outlived the configured polling bounds.
    """
    if len(data) > settings.max_bytes:
        raise ValueError(
            f"document is {len(data)} bytes, over the {settings.max_bytes}-byte limit"
        )
    kind, media_type = _classify(data, filename)
    logger.debug("read.classified kind=%s media_type=%s bytes=%d", kind, media_type, len(data))

    if kind == "pdf":
        return _read_pdf(data, settings=settings)
    if kind == "image":
        return _recognize(data, content_type=media_type, settings=settings)
    if kind in ("xlsx", "docx", "pptx", "html"):
        return _read_office(data, kind=kind, media_type=media_type, settings=settings)
    if kind in ("text", "email"):
        return _read_text(data, kind=kind, media_type=media_type, settings=settings)
    raise UnsupportedFormat(
        f"{media_type} is not a format this service converts; Document Intelligence accepts "
        f"application/pdf, {_DI_IMAGE_TYPES} and text/html, and the Office and plain-text "
        "routes cover the OOXML and UTF-8 text formats"
    )


__all__ = [
    "DI_MEDIA_TYPES",
    "PROVIDER_AZURE_LAYOUT",
    "PROVIDER_HTMLREAD",
    "PROVIDER_OPENPYXL",
    "PROVIDER_PLAIN_TEXT",
    "PROVIDER_PYMUPDF",
    "NeedsRecognition",
    "UnsupportedFormat",
    "read_document",
]
