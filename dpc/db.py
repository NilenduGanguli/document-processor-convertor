"""Postgres index of conversions.

The markdown lives in S3; this table is the audit trail and the listing the console reads.
Schema is applied at startup with ``CREATE TABLE IF NOT EXISTS`` (see ``schema.sql``) — no
migration machinery for a single-table service. No function here ever touches document text
beyond the counts already reduced from it.
"""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from dpc.config import Settings, get_settings

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: Insert order — matches ``schema.sql`` minus the defaulted ``created_at``. The doctree
#: columns (SPEC-DOCTREE-1 §6.2) are all nullable, so a row from a ``tree_mode=off``
#: conversion simply inserts NULLs and old readers keep working.
_COLUMNS = (
    "id", "doc_id", "source", "provider", "filename", "media_type",
    "pages", "blocks", "tables_n", "marks", "key_values", "chars",
    "sha256_input", "sha256_markdown", "s3_bucket", "s3_key",
    "status", "error", "ms",
    "tree_s3_key", "sha256_tree", "tree_source", "tree_nodes", "tree_status",
    "tree_md_s3_key", "sha256_tree_markdown", "passes",
)

#: Insert order for ``arrangements`` — matches ``schema.sql`` minus the defaulted
#: ``created_at``. The variant columns are filled by the arrange runner when active mode
#: derives one (R17: ``variant_generated`` is the exact string the variant was flattened
#: with, making variant replay a pure function of recorded inputs).
_ARRANGEMENT_COLUMNS = (
    "suggestion_id", "conversion_id", "artifact_sha256", "s3_key", "status",
    "model_id", "prompt_template_version", "verifier_version",
    "n_accepted", "n_rejected",
    "variant_s3_key", "variant_sha256", "variant_generated",
)


def _valid_uuid(value: Any) -> bool:
    """Whether ``value`` can address a ``uuid`` column at all — by POSTGRES's grammar.

    Every lookup key here binds into ``WHERE <uuid_col> = %s``; Postgres raises
    ``InvalidTextRepresentation`` on a malformed string, turning "no such row" into an
    exception. A value that cannot be a uuid names no row by construction, so the getters
    below answer ``None`` for it without a round trip — total functions over caller data.

    Python's ``uuid.UUID`` accepts MORE than Postgres does — ``urn:uuid:<uuid>`` parses in
    Python and 500s on a real ``uuid`` column (braced, hyphenless and uppercase forms are
    fine on both sides). Measured live before this gate was tightened. Hence the extra
    refusal below rather than trusting the constructor's grammar.
    """
    text = str(value)
    if "urn:" in text.lower():
        return False
    try:
        uuid.UUID(text)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def connect(settings: Settings | None = None) -> psycopg.Connection[Any]:
    """One autocommitting-on-exit connection; rows come back as dicts."""
    settings = settings or get_settings()
    return psycopg.connect(settings.pg_dsn, row_factory=dict_row)


def init_schema(settings: Settings | None = None) -> None:
    """Apply ``schema.sql`` (idempotent — every statement is IF NOT EXISTS)."""
    with connect(settings) as conn:
        conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))


def init_schema_retrying(
    settings: Settings | None = None, *, attempts: int = 30, delay: float = 1.0
) -> bool:
    """Apply the schema, retrying while the database comes up. Returns success.

    At container start the app and Postgres race, and the first connection routinely loses.
    The first version of this startup logged "schema init deferred" on that failure and never
    retried — a deferral that never happens — so the service came up, reported ready, and
    500'd on the first insert with UndefinedTable. Bounded retries here, and the readiness
    check now verifies the TABLE exists rather than merely that the socket opens, so the two
    failures ("db down" and "db up but never initialised") are both visible instead of
    neither.
    """
    for attempt in range(1, attempts + 1):
        try:
            init_schema(settings)
            return True
        except Exception as exc:  # noqa: BLE001 - retried; the last failure is reported
            if attempt == attempts:
                logger.error(
                    "schema init failed after %d attempts (%s)", attempts, type(exc).__name__
                )
                return False
            time.sleep(delay)
    return False


def insert_conversion(row: dict[str, Any], settings: Settings | None = None) -> None:
    """Insert one conversion row; unknown keys in ``row`` are ignored."""
    columns = ", ".join(_COLUMNS)
    placeholders = ", ".join(f"%({name})s" for name in _COLUMNS)
    with connect(settings) as conn:
        conn.execute(
            f"INSERT INTO conversions ({columns}) VALUES ({placeholders})",
            {name: row.get(name) for name in _COLUMNS},
        )


def list_conversions(
    limit: int = 50, offset: int = 0, settings: Settings | None = None
) -> list[dict[str, Any]]:
    """Newest conversions first."""
    with connect(settings) as conn:
        cursor = conn.execute(
            "SELECT * FROM conversions ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (limit, offset),
        )
        return list(cursor.fetchall())


def get_conversion(
    conversion_id: str, settings: Settings | None = None
) -> dict[str, Any] | None:
    """One conversion row by id, or ``None`` (a non-uuid id names no row)."""
    if not _valid_uuid(conversion_id):
        return None
    with connect(settings) as conn:
        cursor = conn.execute(
            "SELECT * FROM conversions WHERE id = %s", (conversion_id,)
        )
        return cursor.fetchone()


def insert_arrangement(row: dict[str, Any], settings: Settings | None = None) -> None:
    """Insert one arrangement row; unknown keys in ``row`` are ignored."""
    columns = ", ".join(_ARRANGEMENT_COLUMNS)
    placeholders = ", ".join(f"%({name})s" for name in _ARRANGEMENT_COLUMNS)
    with connect(settings) as conn:
        conn.execute(
            f"INSERT INTO arrangements ({columns}) VALUES ({placeholders})",
            {name: row.get(name) for name in _ARRANGEMENT_COLUMNS},
        )


def get_arrangement(
    suggestion_id: str, settings: Settings | None = None
) -> dict[str, Any] | None:
    """One arrangement row by suggestion id, or ``None`` (a non-uuid id names no row)."""
    if not _valid_uuid(suggestion_id):
        return None
    with connect(settings) as conn:
        cursor = conn.execute(
            "SELECT * FROM arrangements WHERE suggestion_id = %s", (suggestion_id,)
        )
        return cursor.fetchone()


def latest_arrangement(
    conversion_id: str, settings: Settings | None = None
) -> dict[str, Any] | None:
    """The newest arrangement row for a conversion, or ``None``.

    ``suggestion_id`` is the tiebreaker on equal timestamps so the answer is total — two
    passes landing in the same clock tick must not make "the latest" flap between reads.
    A non-uuid conversion id names no row.
    """
    if not _valid_uuid(conversion_id):
        return None
    with connect(settings) as conn:
        cursor = conn.execute(
            "SELECT * FROM arrangements WHERE conversion_id = %s"
            " ORDER BY created_at DESC, suggestion_id DESC LIMIT 1",
            (conversion_id,),
        )
        return cursor.fetchone()


def update_arrangement(
    suggestion_id: str, fields: dict[str, Any], settings: Settings | None = None
) -> None:
    """Update named columns on one arrangement row (the runner's variant-derivation write).

    Only columns from ``_ARRANGEMENT_COLUMNS`` are accepted — the column list is code, never
    caller data — and they are applied in that tuple's fixed order so the statement text is
    deterministic for a given key set.
    """
    names = [name for name in _ARRANGEMENT_COLUMNS if name in fields]
    if not names:
        return
    assignments = ", ".join(f"{name} = %({name})s" for name in names)
    params: dict[str, Any] = {name: fields[name] for name in names}
    params["suggestion_id"] = suggestion_id
    with connect(settings) as conn:
        conn.execute(
            f"UPDATE arrangements SET {assignments} WHERE suggestion_id = %(suggestion_id)s",
            params,
        )


def check(settings: Settings | None = None) -> bool:
    """Readiness probe: is the CONVERSIONS TABLE there? Never raises.

    Deliberately stricter than "can I open a socket": a database that is up but was never
    initialised answered the old SELECT 1 probe with ready=true and then failed the first
    insert. to_regclass returns NULL rather than raising, so this is still one cheap query.
    """
    try:
        with connect(settings) as conn:
            row = conn.execute("SELECT to_regclass('conversions') AS t").fetchone()
        return bool(row and row["t"])
    except Exception:  # noqa: BLE001 - a probe reports, it never raises
        return False


__all__ = [
    "check",
    "connect",
    "get_arrangement",
    "get_conversion",
    "init_schema",
    "insert_arrangement",
    "insert_conversion",
    "latest_arrangement",
    "list_conversions",
    "update_arrangement",
]
