"""``ArrangeLlmClient`` — the arrange pass's one outbound seam (SPEC-DOCTREE-1 §4.7, §10).

Provider-agnostic, mirroring :class:`dpc.ocr_client.OcrClient`'s posture: settings-driven,
injectable ``httpx`` transport, no hardcoded URL anywhere, and NO log line that carries
payload or response content — counts, hosts and statuses only. Three providers:

- ``stellar`` — the org's OpenAI-compatible gateway (`POST {endpoint}/chat/completions`)
  behind COIN OAuth2 client-credentials. The client id/secret/scope arrive BASE64-ENCODED in
  settings and are decoded at use; the effective scope is ``"coinscope" + decoded_scope``;
  the minted token is cached and re-minted after ``coin_token_ttl_seconds`` on a MONOTONIC
  clock (a wall-clock jump must never strand a stale token). Images travel as ``image_url``
  data: URIs.
- ``vertex`` — Gemini via ``google-genai`` with ``GOOGLE_APPLICATION_CREDENTIALS`` (the
  local-dev/laptop path). Imported LAZILY: the package's absence makes the provider
  unavailable, never an import error at module load. Images travel as inline-data parts.
- ``stub`` — fixture replay from ``{fixture_dir}/{payload_sha256}.{sample_ix}.json``; the
  network is NEVER touched. Setting ``arrange_fixture_dir`` forces this provider outright,
  the DI stub's exact posture, so the whole pass runs offline from recorded payloads.

Settings are read via ``getattr`` with the spec's defaults so this module works against a
``Settings`` object that predates workstream E's config additions.
"""
from __future__ import annotations

import base64
import binascii
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from dpc.arrange.payload import PROMPT_TEMPLATE

logger = logging.getLogger(__name__)

#: §4.3: k=3 samples per window at these temperatures — one greedy, two exploratory.
SAMPLE_TEMPS: tuple[float, float, float] = (0.0, 0.7, 0.7)

#: §10 default TTL for a COIN token: 840 s (14 min) against the gateway's 15-min tokens.
COIN_TOKEN_TTL_SECONDS = 840


class ArrangeUnavailable(RuntimeError):
    """The configured provider cannot run here (no endpoint / package / fixture dir)."""


class ArrangeCallError(RuntimeError):
    """One sample call failed. Message carries provider/host/status ONLY — a transport
    error can embed the request body, and this request body left the PII boundary once
    already; it does not get a second trip through the logs."""


def _now() -> float:
    """Monotonic seconds — the token TTL's clock, seamed so tests can drive it.

    Monotonic on purpose (§10): a wall-clock jump (NTP step, DST) must never make a cached
    token look fresh forever or expire instantly.
    """
    return time.monotonic()


def _get(settings: Any, name: str, default: Any) -> Any:
    """``getattr`` with the spec default — E's config module may not have landed yet."""
    value = getattr(settings, name, default)
    return default if value is None else value


def _b64(value: str) -> str:
    """Decode one base64-encoded credential from settings; refuse-don't-guess on garbage."""
    try:
        return base64.b64decode(value.strip(), validate=True).decode()
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ArrangeCallError(
            f"COIN credential is not valid base64 ({type(exc).__name__})"
        ) from exc


class ArrangeLlmClient:
    """One client per pass run; holds the COIN token cache across window calls.

    Args:
        settings: Where endpoint/model/credentials come from (``arrange_*``/``coin_*``).
        transport: Optional injectable ``httpx`` transport (tests use
            :class:`httpx.MockTransport`); used for BOTH the token mint and the API call.
    """

    def __init__(self, settings: Any, *, transport: httpx.BaseTransport | None = None):
        self._settings = settings
        self._transport = transport
        self._token: str | None = None
        self._token_minted: float = 0.0

    # -- provider selection ----------------------------------------------------
    @property
    def provider(self) -> str:
        """The effective provider. A configured fixture dir FORCES ``stub`` — replay mode
        must never race a live endpoint config to the network."""
        if str(_get(self._settings, "arrange_fixture_dir", "")).strip():
            return "stub"
        return str(_get(self._settings, "arrange_provider", "stellar")).strip() or "stellar"

    @property
    def host(self) -> str:
        """Endpoint host — what log lines name instead of a URL."""
        endpoint = str(_get(self._settings, "arrange_endpoint", ""))
        parsed = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
        return parsed.hostname or ""

    def available(self) -> bool:
        """Whether a call can be attempted at all. Unavailable => ``skipped(no_llm_configured)``."""
        provider = self.provider
        if provider == "stub":
            return True
        if provider == "stellar":
            return bool(str(_get(self._settings, "arrange_endpoint", "")).strip())
        if provider == "vertex":
            return self._genai() is not None
        return False

    # -- the one public operation ----------------------------------------------
    def complete(
        self,
        *,
        payload_text: str,
        payload_sha256: str,
        sample_ix: int,
        temperature: float,
        image_png: bytes | None = None,
        timeout: float = 20.0,
    ) -> str:
        """One sample: send (template + payload [+ page image]) and return the raw text.

        Args:
            payload_text: The window's canonical payload JSON, decoded (the user message).
            payload_sha256: The payload's content address — the stub's fixture key.
            sample_ix: 0-based sample index (the stub's second fixture key).
            temperature: Sampling temperature for this sample (:data:`SAMPLE_TEMPS`).
            image_png: §10 multimodal — the window page's PNG, or None in structure mode.
            timeout: Per-call wall bound (``arrange_window_timeout_seconds``).

        Returns:
            The provider's raw response text (parsed later by :mod:`dpc.arrange.ops`).

        Raises:
            ArrangeUnavailable: The provider cannot run (checked before any I/O).
            ArrangeCallError: The call failed; message carries no payload content.
        """
        provider = self.provider
        if provider == "stub":
            return self._stub(payload_sha256, sample_ix)
        if not self.available():
            raise ArrangeUnavailable(f"provider {provider!r} is not configured")
        if provider == "stellar":
            return self._stellar(payload_text, temperature, image_png, timeout)
        if provider == "vertex":
            return self._vertex(payload_text, temperature, image_png, timeout)
        raise ArrangeUnavailable(f"unknown provider {provider!r}")

    # -- stub ------------------------------------------------------------------
    def _stub(self, payload_sha256: str, sample_ix: int) -> str:
        fixture_dir = str(_get(self._settings, "arrange_fixture_dir", ""))
        path = Path(fixture_dir) / f"{payload_sha256}.{sample_ix}.json"
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            # Names the KEY (a hash), never any content.
            raise ArrangeCallError(
                f"fixture_missing {payload_sha256[:12]}.{sample_ix}"
            ) from exc

    # -- stellar ---------------------------------------------------------------
    def _coin_token(self) -> str:
        """Mint-or-reuse the COIN token; re-mint after the TTL on the monotonic clock."""
        ttl = float(_get(self._settings, "coin_token_ttl_seconds", COIN_TOKEN_TTL_SECONDS))
        if self._token is not None and _now() - self._token_minted < ttl:
            return self._token
        coin_url = str(_get(self._settings, "coin_url", "")).strip()
        if not coin_url:
            raise ArrangeUnavailable("stellar provider configured with no coin_url")
        # The id/secret/scope arrive base64-encoded in the environment (the org's secret
        # distribution wraps them); decode at use, never at startup, so a rotated secret is
        # picked up without a restart. The effective scope is "coinscope" + decoded scope.
        client_id = _b64(str(_get(self._settings, "coin_client_id", "")))
        client_secret = _b64(str(_get(self._settings, "coin_client_secret", "")))
        scope = "coinscope" + _b64(str(_get(self._settings, "coin_scope", "")))
        try:
            # verify=False on the TOKEN POST is INTENTIONAL, not an oversight: per the org
            # handoff doc ("Stellar gateway onboarding", COIN section), the COIN token
            # endpoint sits behind the VDI's TLS-intercepting proxy whose cert chain is not
            # in any bundle we ship; the handoff doc mandates skipping verification for the
            # mint while the API calls verify against the corporate bundle (ssl_cert_file).
            with httpx.Client(
                timeout=20.0, verify=False, transport=self._transport
            ) as client:
                response = client.post(coin_url, data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": scope,
                })
                response.raise_for_status()
                token = response.json().get("access_token")
        except httpx.HTTPError as exc:
            raise ArrangeCallError(f"coin mint failed: {type(exc).__name__}") from exc
        if not isinstance(token, str) or not token:
            raise ArrangeCallError("coin mint returned no access_token")
        self._token = token
        self._token_minted = _now()
        return token

    def _stellar(
        self, payload_text: str, temperature: float, image_png: bytes | None, timeout: float
    ) -> str:
        endpoint = str(_get(self._settings, "arrange_endpoint", "")).rstrip("/")
        model = str(_get(self._settings, "arrange_model", "gemini-2.5-flash"))
        token = self._coin_token()
        content: Any = payload_text
        if image_png is not None:
            data_uri = "data:image/png;base64," + base64.b64encode(image_png).decode()
            content = [
                {"type": "text", "text": payload_text},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]
        # API calls DO verify: against the corporate bundle when configured, else default.
        cert_file = str(_get(self._settings, "ssl_cert_file", "")).strip()
        verify: Any = cert_file if cert_file else True
        try:
            with httpx.Client(
                timeout=timeout, verify=verify, transport=self._transport
            ) as client:
                response = client.post(
                    f"{endpoint}/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "model": model,
                        "temperature": temperature,
                        "messages": [
                            {"role": "system", "content": PROMPT_TEMPLATE},
                            {"role": "user", "content": content},
                        ],
                    },
                )
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            raise ArrangeCallError(
                f"stellar call to {self.host!r} failed: {type(exc).__name__}"
            ) from exc
        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ArrangeCallError("stellar response missing choices content") from exc
        return str(text)

    # -- vertex ----------------------------------------------------------------
    @staticmethod
    def _genai() -> Any | None:
        """Lazy ``google-genai`` import — absence is an unavailable provider, never an
        import error at module load (§10: the extra is optional on purpose)."""
        try:
            from google import genai  # lazy on purpose (optional extra)
        except ImportError:
            return None
        return genai

    def _vertex(
        self, payload_text: str, temperature: float, image_png: bytes | None, timeout: float
    ) -> str:
        genai = self._genai()
        if genai is None:  # pragma: no cover - available() gates this path.
            raise ArrangeUnavailable("google-genai is not installed")
        from google.genai import types  # lazy with the sdk itself

        model = str(_get(self._settings, "arrange_model", "gemini-2.5-flash"))
        contents: list[Any] = [payload_text]
        if image_png is not None:
            contents.append(types.Part.from_bytes(data=image_png, mime_type="image/png"))
        # Vertex explicitly, or the SDK hunts for a GOOGLE_API_KEY and every call dies
        # ApiKeyMissing regardless of a perfectly good service account — measured live: the
        # bare Client() form returned three unreachable samples while the same credentials
        # answered a direct vertexai=True smoke call. Project falls back to the service
        # account file's own project_id so local dev needs zero extra configuration.
        project = str(_get(self._settings, "arrange_vertex_project", "") or "")
        if not project:
            import json as json_module
            import os

            sa_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
            if sa_path:
                try:
                    with open(sa_path, encoding="utf-8") as handle:
                        project = str(json_module.load(handle).get("project_id", ""))
                except (OSError, ValueError):
                    project = ""
        if not project:
            raise ArrangeUnavailable(
                "vertex provider needs arrange_vertex_project or GOOGLE_APPLICATION_CREDENTIALS"
            )
        location = str(_get(self._settings, "arrange_vertex_location", "global") or "global")
        try:
            client = genai.Client(
                vertexai=True, project=project, location=location,
                http_options=types.HttpOptions(timeout=int(timeout * 1000)),
            )
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=temperature,
                    system_instruction=PROMPT_TEMPLATE,
                ),
            )
        except Exception as exc:  # sdk raises its own hierarchy; name only, then re-raise.
            raise ArrangeCallError(f"vertex call failed: {type(exc).__name__}") from exc
        return str(response.text or "")


__all__ = [
    "COIN_TOKEN_TTL_SECONDS",
    "SAMPLE_TEMPS",
    "ArrangeCallError",
    "ArrangeLlmClient",
    "ArrangeUnavailable",
]
