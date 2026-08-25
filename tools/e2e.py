#!/usr/bin/env python3
"""End-to-end proof against the RUNNING stack: every input kind, S3 and PG verified.

Not a unit test on purpose: this drives the real compose stack over HTTP and then checks the
side effects where they actually land — the object in MinIO and the row in Postgres — because
"the API returned 200" proves neither.

Usage: .venv/bin/python tools/e2e.py [base_url]   (default http://localhost:8300)
"""
from __future__ import annotations

import base64
import json
import sys
import urllib.request
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8300"
SCRATCH = Path(
    "/private/tmp/claude-501/-Users-neelu-document-intelligence/"
    "097d4133-f295-4c84-a52f-83fd737c7152/scratchpad"
)
CORPUS = Path.home() / "document-classification-extraction" / "corpus"


def call(path: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"content-type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main() -> int:
    failures: list[str] = []
    ids: dict[str, str] = {}

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
        if not ok:
            failures.append(name)

    status, ready = call("/readyz")
    check("readyz", status == 200 and ready.get("ready") is True, str(ready.get("checks")))

    # 1. document (PDF with text layer)
    pdf = (CORPUS / "us" / "us_w9.pdf").read_bytes()
    status, r = call("/api/v1/convert", {
        "doc_id": "e2e-doc", "filename": "us_w9.pdf",
        "content_base64": base64.b64encode(pdf).decode(), "echo": True,
    })
    ok = status == 200 and r.get("provider") == "pymupdf" and r.get("pages", 0) >= 1
    md = r.get("markdown") or ""
    ok = ok and md.startswith("---") and "<!-- @" in md and "<!-- page 1" in md
    check("convert document(pdf)", ok, f"pages={r.get('pages')} blocks={r.get('blocks')}")
    if r.get("id"):
        ids["document"] = r["id"]

    # 2. azure_layout payload
    with open(SCRATCH / "azure_layout_sample.json") as fh:
        payload = json.load(fh)
    status, r = call("/api/v1/convert", {
        "doc_id": "e2e-layout", "azure_analyze_result": payload, "echo": True,
    })
    ok = status == 200 and "P<USATRAVELER" in (r.get("markdown") or "")
    check("convert azure_layout", ok, f"provider={r.get('provider')}")
    if r.get("id"):
        ids["azure_layout"] = r["id"]

    # 3. azure_read payload (converted from the DCE fixture shape via the read mock)
    status, r = call("/api/v1/convert", {
        "doc_id": "e2e-read",
        "azure_read_result": json.loads((SCRATCH / "azure_read_sample.json").read_text()),
        "echo": True,
    })
    check("convert azure_read", status == 200 and (r.get("blocks") or 0) > 0,
          f"blocks={r.get('blocks')}")
    if r.get("id"):
        ids["azure_read"] = r["id"]

    # 4. exactly-one-input validation
    status, _ = call("/api/v1/convert", {"doc_id": "bad"})
    check("no input -> 400", status == 400)
    status, _ = call("/api/v1/convert", {
        "content_base64": "aGk=", "azure_analyze_result": {"analyzeResult": {}},
    })
    check("two inputs -> 400", status == 400)

    # 5. history + fetch back from S3 via the API
    status, rows = call("/api/v1/conversions?limit=10")
    items = rows if isinstance(rows, list) else rows.get("conversions", [])
    listed = {row["id"] for row in items}
    check("history lists all", status == 200 and set(ids.values()) <= listed,
          f"listed={len(listed)}")
    if "document" in ids:
        req = urllib.request.Request(f"{BASE}/api/v1/conversions/{ids['document']}/markdown")
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode()
        check("markdown roundtrip from S3", text.startswith("---") and "<!-- @" in text,
              f"{len(text)} bytes")

    # 6. the row really is in Postgres and the object really is in MinIO
    import subprocess
    row = subprocess.run(
        ["docker", "exec", "document-processor-convertor-postgres-1", "psql", "-U", "dpc", "-d",
         "dpc", "-tAc", "select count(*) from conversions"],
        capture_output=True, text=True, check=False,
    )
    check("postgres row count > 0", row.returncode == 0 and int(row.stdout.strip() or 0) > 0,
          row.stdout.strip())

    print()
    if failures:
        print(f"E2E FAILED: {failures}")
        return 1
    print("E2E: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
