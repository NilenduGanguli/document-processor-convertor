"""``ArrangeLlmClient`` (SPEC-DOCTREE-1 §4.7, §10) against a mock transport.

COIN OAuth2: base64-decoded credentials, the ``coinscope`` prefix, TTL re-mint on the
monotonic clock. Stellar: bearer-authed ``/chat/completions`` with the frozen template,
data-URI images in multimodal mode. Stub: pure fixture replay, provably no network. Plus
the WIRE half of the §8.6 n-gram tripwire: the actual outgoing request body (template +
payload) carries no document 4-gram, in either payload mode.
"""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from test_arrange_features import assert_no_ngrams, prose_view

from dpc.arrange import client as client_mod
from dpc.arrange.client import ArrangeCallError, ArrangeLlmClient
from dpc.arrange.payload import PROMPT_TEMPLATE, make_windows
from dpc.doctree.build import build_doctree

OPS_EMPTY = '{"schema": "dpc-arrange-ops/1", "ops": []}'


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def stellar_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "arrange_provider": "stellar",
        "arrange_endpoint": "https://stellar.example/v1",
        "arrange_model": "gemini-2.5-flash",
        "arrange_fixture_dir": "",
        "coin_url": "https://coin.example/oauth/token",
        "coin_client_id": b64("cid-123"),
        "coin_client_secret": b64("sec-456"),
        "coin_scope": b64("/apps/dpc"),
        "coin_token_ttl_seconds": 840,
        "ssl_cert_file": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class Gateway:
    """A mock COIN + Stellar pair that records every request it sees."""

    def __init__(self, chat_content: str = OPS_EMPTY, chat_status: int = 200):
        self.requests: list[httpx.Request] = []
        self.minted = 0
        self.chat_content = chat_content
        self.chat_status = chat_status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "coin.example":
            self.minted += 1
            return httpx.Response(200, json={"access_token": f"tok-{self.minted}"})
        if self.chat_status != 200:
            return httpx.Response(self.chat_status, json={"error": "nope"})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": self.chat_content}}],
        })

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    @property
    def token_request(self) -> httpx.Request:
        return next(r for r in self.requests if r.url.host == "coin.example")

    @property
    def chat_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host != "coin.example"]


def complete(client: ArrangeLlmClient, *, payload_text: str = '{"page": 1}',
             image_png: bytes | None = None) -> str:
    return client.complete(
        payload_text=payload_text, payload_sha256="0" * 64, sample_ix=0,
        temperature=0.0, image_png=image_png, timeout=5.0,
    )


# ---------------------------------------------------------------------------
# COIN token
# ---------------------------------------------------------------------------
def test_coin_mint_decodes_base64_and_prefixes_coinscope():
    gateway = Gateway()
    client = ArrangeLlmClient(stellar_settings(), transport=gateway.transport)
    assert complete(client) == OPS_EMPTY

    form = parse_qs(gateway.token_request.content.decode())
    assert form["grant_type"] == ["client_credentials"]
    assert form["client_id"] == ["cid-123"]          # decoded, not the base64 blob
    assert form["client_secret"] == ["sec-456"]
    assert form["scope"] == ["coinscope/apps/dpc"]   # "coinscope" + decoded scope

    chat = gateway.chat_requests[0]
    assert str(chat.url) == "https://stellar.example/v1/chat/completions"
    assert chat.headers["Authorization"] == "Bearer tok-1"
    body = json.loads(chat.content)
    assert body["model"] == "gemini-2.5-flash"
    assert body["messages"][0] == {"role": "system", "content": PROMPT_TEMPLATE}
    assert body["messages"][1]["content"] == '{"page": 1}'


def test_coin_token_cached_then_reminted_after_ttl(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(client_mod, "_now", lambda: clock["t"])
    gateway = Gateway()
    client = ArrangeLlmClient(
        stellar_settings(coin_token_ttl_seconds=840), transport=gateway.transport
    )
    complete(client)
    clock["t"] = 500.0    # inside the TTL: the cached token is reused
    complete(client)
    assert gateway.minted == 1
    assert all(r.headers["Authorization"] == "Bearer tok-1"
               for r in gateway.chat_requests)

    clock["t"] = 900.0    # past 840 s: re-mint
    complete(client)
    assert gateway.minted == 2
    assert gateway.chat_requests[-1].headers["Authorization"] == "Bearer tok-2"


def test_bad_base64_credential_is_refused():
    gateway = Gateway()
    client = ArrangeLlmClient(
        stellar_settings(coin_client_id="not-base64!!!"), transport=gateway.transport
    )
    with pytest.raises(ArrangeCallError):
        complete(client)
    assert gateway.requests == []  # refused before anything left the process


# ---------------------------------------------------------------------------
# Provider selection / availability
# ---------------------------------------------------------------------------
def test_stellar_unavailable_without_endpoint():
    client = ArrangeLlmClient(stellar_settings(arrange_endpoint=""))
    assert not client.available()


def test_fixture_dir_forces_stub_even_with_endpoint(tmp_path):
    client = ArrangeLlmClient(stellar_settings(arrange_fixture_dir=str(tmp_path)))
    assert client.provider == "stub"
    assert client.available()


def test_vertex_available_iff_sdk_importable(monkeypatch):
    settings = SimpleNamespace(arrange_provider="vertex", arrange_fixture_dir="")
    assert ArrangeLlmClient(settings).available()  # google-genai is an installed extra
    monkeypatch.setattr(ArrangeLlmClient, "_genai", staticmethod(lambda: None))
    assert not ArrangeLlmClient(settings).available()


# ---------------------------------------------------------------------------
# Stub replay — never any network
# ---------------------------------------------------------------------------
def test_stub_replays_fixture_and_never_touches_network(tmp_path):
    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError("stub mode touched the network")

    sha = "ab" * 32
    (tmp_path / f"{sha}.1.json").write_text(OPS_EMPTY, encoding="utf-8")
    client = ArrangeLlmClient(
        stellar_settings(arrange_fixture_dir=str(tmp_path)),
        transport=httpx.MockTransport(explode),
    )
    text = client.complete(payload_text="{}", payload_sha256=sha, sample_ix=1,
                           temperature=0.7, image_png=None, timeout=5.0)
    assert text == OPS_EMPTY
    with pytest.raises(ArrangeCallError, match="fixture_missing"):
        client.complete(payload_text="{}", payload_sha256=sha, sample_ix=2,
                        temperature=0.7, image_png=None, timeout=5.0)


# ---------------------------------------------------------------------------
# Multimodal request shape (§10)
# ---------------------------------------------------------------------------
def test_image_travels_as_data_uri_exactly_when_provided():
    gateway = Gateway()
    client = ArrangeLlmClient(stellar_settings(), transport=gateway.transport)
    png = b"\x89PNG\r\n\x1a\nfakepixels"

    complete(client, image_png=png)
    body = json.loads(gateway.chat_requests[-1].content)
    content = body["messages"][1]["content"]
    assert isinstance(content, list) and len(content) == 2
    assert content[0] == {"type": "text", "text": '{"page": 1}'}
    expected = "data:image/png;base64," + base64.b64encode(png).decode()
    assert content[1] == {"type": "image_url", "image_url": {"url": expected}}

    complete(client, image_png=None)
    body = json.loads(gateway.chat_requests[-1].content)
    assert body["messages"][1]["content"] == '{"page": 1}'  # structure mode: text only


# ---------------------------------------------------------------------------
# §8.6 n-gram tripwire — the WIRE half, against the captured request body
# ---------------------------------------------------------------------------
def test_no_document_ngrams_in_request():
    view = prose_view()
    tree = build_doctree(view)
    window = make_windows(tree, view)[0]
    gateway = Gateway()
    client = ArrangeLlmClient(stellar_settings(), transport=gateway.transport)
    client.complete(
        payload_text=window.payload_bytes.decode(),
        payload_sha256=window.payload_sha256, sample_ix=0, temperature=0.0,
        image_png=None, timeout=5.0,
    )
    outgoing = gateway.chat_requests[0].content
    assert_no_ngrams(view, outgoing)                      # template + payload, whole body
    body = json.loads(outgoing)
    assert body["messages"][1]["content"] == window.payload_bytes.decode()  # it went out


def test_multimodal_structured_part_still_carries_no_ngrams():
    """§10: in multimodal mode the disclosure is the IMAGE; the structured part stays
    n-gram-clean, and the image is present exactly when provided."""
    view = prose_view()
    tree = build_doctree(view)
    png = b"\x89PNG\r\n\x1a\npixels-of-the-page"
    window = make_windows(tree, view, page_images={1: png})[0]
    gateway = Gateway()
    client = ArrangeLlmClient(stellar_settings(), transport=gateway.transport)
    client.complete(
        payload_text=window.payload_bytes.decode(),
        payload_sha256=window.payload_sha256, sample_ix=0, temperature=0.0,
        image_png=window.image_png, timeout=5.0,
    )
    body = json.loads(gateway.chat_requests[0].content)
    text_part, image_part = body["messages"][1]["content"]
    assert_no_ngrams(view, text_part["text"].encode())
    assert_no_ngrams(view, body["messages"][0]["content"].encode())
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


# ---------------------------------------------------------------------------
# Failure hygiene
# ---------------------------------------------------------------------------
def test_call_error_names_host_never_payload():
    gateway = Gateway(chat_status=500)
    client = ArrangeLlmClient(stellar_settings(), transport=gateway.transport)
    secret_payload = '{"page": 1, "marker": "distinctive-payload-marker"}'
    with pytest.raises(ArrangeCallError) as info:
        complete(client, payload_text=secret_payload)
    assert "distinctive-payload-marker" not in str(info.value)
    assert "stellar.example" in str(info.value)
