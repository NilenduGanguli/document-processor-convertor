"""S3/MinIO storage for emitted markdown.

One bucket, one key layout: ``pmd/{yyyy}/{mm}/{id}.md`` where ``id`` is the conversion uuid.
The markdown IS the product — Postgres holds only the index row that points here. Nothing in
this module logs document content; keys and sizes only.
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


def check(settings: Settings | None = None) -> bool:
    """Readiness probe: can we see the bucket? Never raises."""
    settings = settings or get_settings()
    try:
        client(settings).head_bucket(Bucket=settings.s3_bucket)
        return True
    except Exception:  # noqa: BLE001 - a probe reports, it never raises
        return False


__all__ = ["check", "client", "get_markdown", "key_for", "put_markdown"]
