"""Settings. ``DPC_`` prefix, one flat object, refuse-don't-guess on nonsense.

Every enumerated setting is validated at construction rather than at the point of use. A
typo in ``DPC_OFFICE_ROUTE`` that silently fell back to the default would produce a corpus
converted two different ways with nothing in the artifacts to say which — the same class of
silent degradation that ``SPEC-PMD-2`` §2.3 refuses for a missing DI endpoint. Failing at
startup names the variable and the accepted values while a human is still watching.
"""
from __future__ import annotations

import logging
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

#: Accepted values for the enumerated string settings, kept next to each other so a refusal
#: message and the validator can never drift apart.
PMD_LAYOUTS = ("band", "linear")
RECT_SCALES = ("auto", "legacy")
OFFICE_ROUTES = ("local", "render", "azure")
TEXT_ROUTES = ("plain", "render", "refuse")
CANVAS_KV_MODES = ("additive", "always", "never")
TREE_MODES = ("off", "build", "emit")
ARRANGE_MODES = ("off", "shadow", "active")
ARRANGE_PAYLOADS = ("multimodal", "structure")
#: Empty string means "no LLM provider configured" — the arrange pass records
#: ``skipped(no_llm_configured)`` rather than guessing a gateway (SPEC-DOCTREE-1 §4.7).
ARRANGE_PROVIDERS = ("", "stellar", "vertex", "stub")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="dpc_", env_file=".env", env_file_encoding="utf-8",
        case_sensitive=False, extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8300
    #: Optional X-API-Key; empty disables the gate (fine behind a mesh, not on a desk).
    api_key: str = ""

    # ---- Postgres -----------------------------------------------------------
    pg_dsn: str = "postgresql://dpc:dpc@localhost:5438/dpc"

    # ---- S3 / MinIO ---------------------------------------------------------
    s3_endpoint: str = "http://localhost:9004"
    s3_access_key: str = "dpc"
    s3_secret_key: str = "dpc-secret"
    s3_bucket: str = "docmd"
    s3_region: str = "us-east-1"

    # ---- Reading a raw document --------------------------------------------
    #: Max upload size.
    max_bytes: int = 32 * 1024 * 1024
    #: Pages accepted in one PDF. Now a refusal bound rather than a truncation bound: the
    #: whole document goes to Azure in one call and Azure bills per page, so a 2100-page PDF
    #: is refused locally by the pre-check instead of being half-read and fully charged.
    max_pages: int = 500
    #: DEPRECATED, no-op, removed after one release. It answered "is this page a scan, i.e.
    #: must it go to DI?" — a question with one answer now that every renderable input goes
    #: to DI (SPEC-PMD-2 §2.1). Retained so an existing ``.env`` still validates; setting it
    #: logs a WARNING at startup and changes nothing.
    min_alnum_chars: int = 40
    #: Azure Document Intelligence endpoint. Every PDF and every image goes here — there is
    #: no local text reader on the product path. Empty = a structured refusal (422
    #: ``needs_ocr``), never a local guess: a PyMuPDF fallback produces a valid-LOOKING
    #: artifact with no signal that it is positionally worthless (SPEC-PMD-2 §2.3).
    azure_di_endpoint: str = ""
    azure_di_key: str = ""
    azure_di_api_version: str = "2024-11-30"
    #: Wall clock for one analyse, submit and polling together. Sized for a LARGE scan on
    #: real Document Intelligence (~1s/page means a 100-page scan alone needs ~100s): the
    #: corpus sweep showed 60s failing every 100+ page scanned filing while everything else
    #: finished in single-digit seconds. Local mocks are slower still — the compose file
    #: raises this for them.
    ocr_timeout_seconds: float = 180.0
    ocr_poll_interval_seconds: float = 0.5
    ocr_max_polls: int = 120

    # ---- PMD 2.0: layout and the canvas emitter (SPEC-PMD-2 §8) -------------
    #: ``band`` runs the PMD 2.0 canvas path, ``linear`` is the PMD 1.0 escape hatch. Band is
    #: the default because the feature is the point: a default that never runs the new path
    #: is a feature nobody sees.
    pmd_layout: str = "band"
    #: ``auto`` anchors inch-unit pages in milli-inches with ``scale=1000`` on the page
    #: marker, fixing a live data-loss bug (an inch page currently anchors on an 8x11 integer
    #: grid — total loss, not rounding loss). ``legacy`` reproduces a stored hash exactly.
    pmd_rect_scale: str = "auto"
    #: Snap canvas columns to detected tab stops. On because it fixes the +/-1-cell jitter on
    #: right-aligned numeric columns, the most eyeball-salient defect of any padded renderer;
    #: it is also the first thing to disable if a fidelity regression appears.
    canvas_tab_snap: bool = True
    #: Rows per canvas segment. ~350 tokens together with ``canvas_seg_chars``, which fits the
    #: smallest chunk window in common use. Deployments with a larger chunker may raise it.
    canvas_seg_rows: int = 20
    #: Characters per canvas segment — the same arithmetic from the other side. Whichever of
    #: the two bounds binds first cuts the segment.
    canvas_seg_chars: int = 1400
    #: Emit a per-row ``ys=`` clause making y exactly invertible. Off: it costs ~7% of segment
    #: bytes and a much uglier anchor, and segment-granularity y is enough for citation and
    #: highlighting.
    canvas_row_y: bool = False
    #: ``additive`` suppresses key/value pairs whose key and value text is already on the
    #: canvas, ``always`` is PMD 1.0 behaviour, ``never`` drops the section. ``additive``
    #: because a second copy under the picture is near-duplicate retrieval text — and because
    #: a pair that adds text must never be dropped.
    canvas_emit_kv: str = "additive"

    # ---- PMD 2.0: routing (SPEC-PMD-2 §2.2, §2.4) ---------------------------
    #: DOCX/PPTX/XLSX/HTML routing. ``local`` (xlsxread/htmlread) is the default because DI
    #: does not render Office formats — it returns no polygon, no ``pages[].lines[]``, no page
    #: dimensions for them, and no tables at all for XLSX. Routing a spreadsheet to Azure
    #: would obey the letter of "everything to Azure" while destroying the output. ``render``
    #: (LibreOffice -> PDF -> DI) is the honest way to get spatial fidelity on a DOCX;
    #: ``azure`` exists for a deployment that wants the letter, and says so in a WARNING.
    office_route: str = "local"
    #: TXT/CSV/MD/EML routing. DI v4.0 does not accept any of them (the search result that
    #: says it does is Azure AI Content Understanding, a different product). ``plain`` keeps
    #: them convertible and honest — ``layout: linear-only``, no invented geometry — rather
    #: than 415-ing a format the service handled yesterday. ``render`` wraps to a fixed-pitch
    #: PDF, which is often exactly right for a fixed-column report; ``refuse`` is 415.
    text_route: str = "plain"
    #: argv template for ``office_route=render``; must contain the ``{input}`` and ``{outdir}``
    #: placeholders, e.g. ``soffice --headless --convert-to pdf --outdir {outdir} {input}``.
    #: Empty because the base image ships no LibreOffice: enabling the route is an explicit
    #: deployment choice with an explicit determinism cost (the renderer version joins the
    #: envelope, SPEC-PMD-2 §7.3).
    render_cmd: str = ""
    #: Stamped at container build; becomes the front matter's ``renderer``. Empty means no
    #: render route is available, which is why an empty value with ``route=render`` refuses.
    render_version: str = ""
    #: The PyMuPDF text path, off. False is the requirement — no internal OCR, no internal
    #: text extraction. True is for air-gapped development only, and an artifact produced
    #: that way is identifiable because the front matter says ``provider: pymupdf``.
    allow_local_pdf_text: bool = False
    #: Replay directory for ``tools/di_stub.py``. Empty in production; set by the corpus
    #: sweep and by compose, where the "DI endpoint" is the offline stub.
    di_fixture_dir: str = ""

    # ---- Doctree (SPEC-DOCTREE-1 §6.3) --------------------------------------
    #: ``build`` constructs and stores ``doctree.json`` beside the PMD; ``emit`` additionally
    #: flattens it to PMD 3.0 (stored BESIDE, never replacing, the 2.0 markdown); ``off``
    #: skips the tree entirely. Default ``build`` because the tree becomes real data on day
    #: one while ``emit`` waits for the §8 measurement gates — and a tree failure NEVER
    #: fails a conversion (200 with ``tree_status=error:*``), so building is free of risk
    #: to the serving path.
    tree_mode: str = "build"

    # ---- Arrange: the LLM reading-order pass (SPEC-DOCTREE-1 §4.7, §10) -----
    #: ``off`` ships dark; ``shadow`` runs the full pass and writes the arrangement artifact
    #: but derives no variant (the measurement mode); ``active`` derives a PMD 3.0 variant
    #: from accepted ops. Advisory by construction: whatever this pass does, the heuristic
    #: artifacts are already stored before it runs.
    arrange_mode: str = "off"
    #: ``multimodal`` (default per §10, explicit owner authorization) sends page-image crops
    #: with the structural features; ``structure`` sends the §4.2 zero-string feature payload
    #: only — the posture for any deployment whose gateway approval excludes document pixels.
    arrange_payload: str = "multimodal"
    #: Which LLM seam to use: ``stellar`` (COIN OAuth2 gateway, VDI), ``vertex`` (google-genai,
    #: the local-dev path), ``stub`` (fixture replay, never any network). Empty = unavailable;
    #: the pass records ``skipped(no_llm_configured)`` instead of guessing.
    arrange_provider: str = ""
    #: OpenAI-compatible base URL for ``stellar``; unused by ``vertex``/``stub``.
    arrange_endpoint: str = ""
    arrange_api_key: str = ""
    #: DES's model for the same corpus; the arrangement artifact records whatever ran.
    arrange_model: str = "gemini-2.5-flash"
    #: Vertex project/location for arrange_provider=vertex. Empty project = read
    #: ``project_id`` from the GOOGLE_APPLICATION_CREDENTIALS file, so local dev with a
    #: service-account key needs nothing else. "global" is the location the same key
    #: answered on in the live smoke test.
    arrange_vertex_project: str = ""
    arrange_vertex_location: str = "global"
    #: Fixture replay directory for ``arrange_provider=stub`` — responses come from
    #: ``{dir}/{payload_sha256}.{sample_ix}.json`` and the network is never touched, the DI
    #: stub's exact posture (§4.7).
    arrange_fixture_dir: str = ""
    #: Self-consistency samples per window (§4.5 V8: accept at >=2 of k=3).
    arrange_samples: int = 3
    #: Nodes per window (§4.3): LayoutGPT-class degradation past ~15 objects argues small;
    #: 48 is about 1.5K tokens of features.
    arrange_max_window: int = 48
    #: Rasterization DPI for multimodal page crops — DES's value (§10).
    arrange_raster_dpi: int = 144
    #: Wall-clock budgets (§4.7): per window call, and per document. Exhaustion records the
    #: completed windows plus ``skipped(budget_exhausted)`` for the rest — never an error.
    arrange_window_timeout_seconds: float = 20.0
    arrange_doc_timeout_seconds: float = 120.0

    # ---- COIN OAuth2 (Stellar gateway; SPEC-DOCTREE-1 §10) ------------------
    #: Token endpoint plus client-credentials grant material. Client id/secret/scope arrive
    #: base64-encoded in env per the org pattern; scope is prefixed ``coinscope``. Empty means
    #: the stellar provider cannot authenticate and the pass skips. NEVER logged.
    coin_url: str = ""
    coin_client_id: str = ""
    coin_client_secret: str = ""
    coin_scope: str = ""
    #: Refresh cadence for the COIN bearer token — 840 s (14 min) against the gateway's
    #: 15-minute tokens, the margin the org's other clients use.
    coin_token_ttl_seconds: int = 840
    #: CA bundle for Stellar API calls (the token POST is intentionally unverified per the
    #: gateway's own contract; API calls verify against this file). Empty = system store.
    ssl_cert_file: str = ""

    #: DEBUG-by-default tracing, same posture and same reasoning as DCE: a trace nobody can
    #: find is not a trace, and no log line here carries document text.
    log_level: str = "DEBUG"

    # -- validation -----------------------------------------------------------
    @field_validator("pmd_layout")
    @classmethod
    def _check_layout(cls, value: str) -> str:
        return _one_of(value, PMD_LAYOUTS, "DPC_PMD_LAYOUT")

    @field_validator("pmd_rect_scale")
    @classmethod
    def _check_rect_scale(cls, value: str) -> str:
        return _one_of(value, RECT_SCALES, "DPC_PMD_RECT_SCALE")

    @field_validator("office_route")
    @classmethod
    def _check_office_route(cls, value: str) -> str:
        return _one_of(value, OFFICE_ROUTES, "DPC_OFFICE_ROUTE")

    @field_validator("text_route")
    @classmethod
    def _check_text_route(cls, value: str) -> str:
        return _one_of(value, TEXT_ROUTES, "DPC_TEXT_ROUTE")

    @field_validator("canvas_emit_kv")
    @classmethod
    def _check_emit_kv(cls, value: str) -> str:
        return _one_of(value, CANVAS_KV_MODES, "DPC_CANVAS_EMIT_KV")

    @field_validator("tree_mode")
    @classmethod
    def _check_tree_mode(cls, value: str) -> str:
        return _one_of(value, TREE_MODES, "DPC_TREE_MODE")

    @field_validator("arrange_mode")
    @classmethod
    def _check_arrange_mode(cls, value: str) -> str:
        return _one_of(value, ARRANGE_MODES, "DPC_ARRANGE_MODE")

    @field_validator("arrange_payload")
    @classmethod
    def _check_arrange_payload(cls, value: str) -> str:
        return _one_of(value, ARRANGE_PAYLOADS, "DPC_ARRANGE_PAYLOAD")

    @field_validator("arrange_provider")
    @classmethod
    def _check_arrange_provider(cls, value: str) -> str:
        return _one_of(value, ARRANGE_PROVIDERS, "DPC_ARRANGE_PROVIDER")

    @field_validator(
        "canvas_seg_rows", "canvas_seg_chars", "max_pages", "max_bytes",
        "arrange_samples", "arrange_max_window", "arrange_raster_dpi",
        "coin_token_ttl_seconds",
    )
    @classmethod
    def _check_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be a positive integer")
        return value

    @field_validator("arrange_window_timeout_seconds", "arrange_doc_timeout_seconds")
    @classmethod
    def _check_positive_seconds(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be a positive number of seconds")
        return value


def _one_of(value: str, accepted: tuple[str, ...], env_var: str) -> str:
    """Normalise an enumerated setting, or refuse naming the variable and the accepted set.

    Args:
        value: The configured value; surrounding whitespace and case are not significant.
        accepted: The accepted values, in the order the spec lists them.
        env_var: The environment variable's name, so the message points at the fix.

    Returns:
        The normalised (stripped, lower-cased) value.

    Raises:
        ValueError: The value is not one of ``accepted``.
    """
    normalised = value.strip().lower()
    if normalised not in accepted:
        raise ValueError(f"{env_var} must be one of {', '.join(accepted)}; got {value!r}")
    return normalised


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if "min_alnum_chars" in settings.model_fields_set:
        # Deprecated no-op. Warn rather than fail: an operator whose .env still carries the
        # old floor should learn that it no longer decides anything, without a deployment
        # refusing to start over a setting that is now inert.
        logger.warning(
            "config.deprecated setting=DPC_MIN_ALNUM_CHARS effect=none "
            "reason=every renderable input now goes to Azure DI"
        )
    return settings


__all__ = [
    "ARRANGE_MODES",
    "ARRANGE_PAYLOADS",
    "ARRANGE_PROVIDERS",
    "CANVAS_KV_MODES",
    "OFFICE_ROUTES",
    "PMD_LAYOUTS",
    "RECT_SCALES",
    "TEXT_ROUTES",
    "TREE_MODES",
    "Settings",
    "get_settings",
]
