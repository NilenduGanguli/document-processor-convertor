"""S3/MinIO storage for emitted markdown and the doctree artifact family.

One bucket, prefix per artifact: ``pmd/{yyyy}/{mm}/{id}.md`` (the PMD 2.0 markdown),
``tree/…/{id}.tree.json`` (doctree), ``treemd/…/{id}[.{sha8}].md`` (PMD 3.0 and variants),
``arr/…/{id}.{n}.arr.json`` (arrangement artifacts) — SPEC-DOCTREE-1 §6.2. ``id`` is the
conversion uuid. The artifacts ARE the product — Postgres holds only the index rows that
point here. Nothing in this module logs document content; keys and sizes only.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

from dpc.config import Settings, get_settings


def client(settings: Settings | None = None) -> Any:
    """A boto3 S3 client bound to the configured endpoint (MinIO in every deployment so far)."""
    settings = settings or get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=BotoConfig(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
    )


def key_for(conversion_id: str, now: _dt.datetime | None = None) -> str:
    """The S3 key for a conversion: ``pmd/{yyyy}/{mm}/{id}.md``."""
    now = now or _dt.datetime.now(_dt.UTC)
    return f"pmd/{now:%Y}/{now:%m}/{conversion_id}.md"


def put_markdown(
    conversion_id: str, text: str, settings: Settings | None = None
) -> str:
    """Store one markdown document; returns the S3 key it now lives under."""
    settings = settings or get_settings()
    key = key_for(conversion_id)
    client(settings).put_object(
        Bucket=settings.s3_bucket,
        Key=key,
        Body=text.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )
    return key


def get_markdown(key: str, settings: Settings | None = None) -> str:
    """Fetch one stored markdown document by its S3 key."""
    settings = settings or get_settings()
    response = client(settings).get_object(Bucket=settings.s3_bucket, Key=key)
    return response["Body"].read().decode("utf-8")


# ---------------------------------------------------------------------------------------
# Doctree artifacts (SPEC-DOCTREE-1 §6.2). New prefixes — tree/, treemd/, arr/ — so any
# lifecycle rule an operator has pinned on pmd/ is untouched by the new artifact family.
# Same shape as put_markdown/get_markdown: the key scheme is the function, Postgres keeps
# only the pointer.
# ---------------------------------------------------------------------------------------
def tree_key_for(conversion_id: str, now: _dt.datetime | None = None) -> str:
    """The S3 key for a stored doctree: ``tree/{yyyy}/{mm}/{id}.tree.json``."""
    now = now or _dt.datetime.now(_dt.UTC)
    return f"tree/{now:%Y}/{now:%m}/{conversion_id}.tree.json"


def tree_md_key_for(
    conversion_id: str,
    variant_sha8: str | None = None,
    now: _dt.datetime | None = None,
) -> str:
    """The S3 key for PMD 3.0: ``treemd/{yyyy}/{mm}/{id}.md``; variants add ``.{sha8}``.

    A variant (an arrangement-patched flatten) gets its own key rather than overwriting the
    heuristic 3.0 file — two artifacts, two addresses, exactly the doctrine that keeps
    ``/markdown`` byte-stable.
    """
    now = now or _dt.datetime.now(_dt.UTC)
    suffix = f".{variant_sha8}" if variant_sha8 else ""
    return f"treemd/{now:%Y}/{now:%m}/{conversion_id}{suffix}.md"


def arrangement_key_for(
    conversion_id: str, n: int, now: _dt.datetime | None = None
) -> str:
    """The S3 key for an arrangement artifact: ``arr/{yyyy}/{mm}/{id}.{n}.arr.json``."""
    now = now or _dt.datetime.now(_dt.UTC)
    return f"arr/{now:%Y}/{now:%m}/{conversion_id}.{n}.arr.json"


def put_tree(conversion_id: str, data: bytes, settings: Settings | None = None) -> str:
    """Store one canonical ``doctree.json`` (dump_tree bytes, verbatim); returns its key.

    Takes bytes rather than a model deliberately: the caller hashes what it stores, and one
    re-serialization here could silently diverge from the sha it recorded.
    """
    settings = settings or get_settings()
    key = tree_key_for(conversion_id)
    client(settings).put_object(
        Bucket=settings.s3_bucket,
        Key=key,
        Body=data,
        ContentType="application/json",
    )
    return key


def get_tree(key: str, settings: Settings | None = None) -> bytes:
    """Fetch one stored doctree by its S3 key — bytes, so the sha can be re-verified."""
    settings = settings or get_settings()
    response = client(settings).get_object(Bucket=settings.s3_bucket, Key=key)
    return response["Body"].read()


def put_tree_markdown(
    conversion_id: str,
    text: str,
    variant_sha8: str | None = None,
    settings: Settings | None = None,
) -> str:
    """Store one PMD 3.0 document (or a ``.{sha8}`` variant); returns its key."""
    settings = settings or get_settings()
    key = tree_md_key_for(conversion_id, variant_sha8)
    client(settings).put_object(
        Bucket=settings.s3_bucket,
        Key=key,
        Body=text.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )
    return key


def get_tree_markdown(key: str, settings: Settings | None = None) -> str:
    """Fetch one stored PMD 3.0 document by its S3 key."""
    settings = settings or get_settings()
    response = client(settings).get_object(Bucket=settings.s3_bucket, Key=key)
    return response["Body"].read().decode("utf-8")


def put_arrangement(
    conversion_id: str, data: bytes, n: int, settings: Settings | None = None
) -> str:
    """Store one canonical ``arrangement.json`` (bytes, same reasoning as put_tree)."""
    settings = settings or get_settings()
    key = arrangement_key_for(conversion_id, n)
    client(settings).put_object(
        Bucket=settings.s3_bucket,
        Key=key,
        Body=data,
        ContentType="application/json",
    )
    return key


def get_arrangement(key: str, settings: Settings | None = None) -> bytes:
    """Fetch one stored arrangement artifact by its S3 key."""
    settings = settings or get_settings()
    response = client(settings).get_object(Bucket=settings.s3_bucket, Key=key)
    return response["Body"].read()


def check(settings: Settings | None = None) -> bool:
    """Readiness probe: can we see the bucket? Never raises."""
    settings = settings or get_settings()
    try:
        client(settings).head_bucket(Bucket=settings.s3_bucket)
        return True
    except Exception:  # noqa: BLE001 - a probe reports, it never raises
        return False


__all__ = [
    "arrangement_key_for",
    "check",
    "client",
    "get_arrangement",
    "get_markdown",
    "get_tree",
    "get_tree_markdown",
    "key_for",
    "put_arrangement",
    "put_markdown",
    "put_tree",
    "put_tree_markdown",
    "tree_key_for",
    "tree_md_key_for",
]
