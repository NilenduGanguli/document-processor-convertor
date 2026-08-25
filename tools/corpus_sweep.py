#!/usr/bin/env python3
"""Sweep the DCE reference corpus through the running service and grade every PMD produced.

Status codes prove nothing about a converter; this grades the OUTPUT. Per document:

  convert        the API accepted it and returned a PMD
  deterministic  converting the same bytes twice yields the same sha256 — the format's
                 core guarantee, checked at scale rather than asserted in one unit test
  anchors        every anchor parses against the grammar and its rect sits inside its
                 page's box (a tolerance for providers whose ink overhangs the page)
  pages          page markers strictly ascending, no duplicates
  fidelity       alphanumeric characters in the PMD vs an independent PyMuPDF read of the
                 same file (PDFs with text layers only, where the comparison is meaningful)

Usage: .venv/bin/python tools/corpus_sweep.py [--corpus DIR] [--url BASE] [--limit N]
Exit 0 only when every attempted document passes every applicable check.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

ANCHOR_RE = re.compile(r"^<!-- @(\d+) \[(-?\d+),(-?\d+),(-?\d+),(-?\d+)\] (.+?) -->$")
PAGE_RE = re.compile(r"^<!-- page (\d+)(?: size=(\d+)x(\d+) unit=(\w+))? -->$")
#: Provider ink genuinely overhangs the page edge on real scans; a rect slightly outside
#: the page is the provider's truth, not our bug. Beyond this it is somebody's bug.
BOUNDS_SLACK = 0.10

SUPPORTED = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".htm", ".html", ".xlsx"}


def call(url: str, body: dict, timeout: float = 180.0) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"content-type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"error": "non-json", "detail": raw[:200].decode("utf-8", "replace")}


def alnum(text: str) -> int:
    return sum(1 for c in text if c.isalnum())


def pdf_alnum(path: Path) -> int | None:
    """Independent ground truth for text-layer PDFs; None when fitz is unavailable."""
    try:
        import fitz
    except ImportError:
        return None
    total = 0
    with fitz.open(path) as doc:
        for page in doc:
            total += alnum(page.get_text("text") or "")
    return total


def grade(markdown: str) -> tuple[list[str], Counter]:
    """Grammar-and-geometry checks over one PMD. Returns (problems, anchor tag counts)."""
    problems: list[str] = []
    tags: Counter = Counter()
    page_sizes: dict[int, tuple[int, int]] = {}
    seen_pages: list[int] = []
    body = markdown.split("---", 2)
    if len(body) < 3 or not markdown.startswith("---"):
        return (["front matter missing"], tags)

    for line in body[2].splitlines():
        if m := PAGE_RE.match(line):
            page = int(m.group(1))
            seen_pages.append(page)
            if m.group(2):
                page_sizes[page] = (int(m.group(2)), int(m.group(3)))
        elif line.startswith("<!-- @"):
            m = ANCHOR_RE.match(line)
            if not m:
                problems.append(f"malformed anchor: {line[:70]}")
                continue
            page = int(m.group(1))
            x0, y0, x1, y1 = (int(m.group(i)) for i in range(2, 6))
            tags[m.group(6).split()[0]] += 1
            if x1 < x0 or y1 < y0:
                problems.append(f"inverted rect p{page} [{x0},{y0},{x1},{y1}]")
            if size := page_sizes.get(page):
                w, h = size
                if (x0 < -w * BOUNDS_SLACK or y0 < -h * BOUNDS_SLACK
                        or x1 > w * (1 + BOUNDS_SLACK) or y1 > h * (1 + BOUNDS_SLACK)):
                    problems.append(f"rect outside page p{page} [{x0},{y0},{x1},{y1}] vs {w}x{h}")
    if seen_pages != sorted(set(seen_pages)):
        problems.append(f"page markers not strictly ascending: {seen_pages[:12]}")
    return (problems, tags)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(Path.home() / "document-classification-extraction/corpus"))
    ap.add_argument("--url", default="http://localhost:8300")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    files = sorted(
        p for p in Path(args.corpus).rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED
    )
    if args.limit:
        files = files[: args.limit]

    tally: Counter = Counter()
    all_tags: Counter = Counter()
    failures: list[tuple[str, str]] = []
    slowest: list[tuple[int, str]] = []

    for path in files:
        rel = path.relative_to(args.corpus)
        body = {
            "doc_id": f"sweep/{rel}",
            "filename": path.name,
            "content_base64": base64.b64encode(path.read_bytes()).decode(),
            "echo": True,
        }
        t0 = time.perf_counter()
        status, first = call(f"{args.url}/api/v1/convert", body)
        ms = int((time.perf_counter() - t0) * 1000)
        slowest.append((ms, str(rel)))

        if status == 422 and first.get("error") == "needs_ocr":
            tally["needs_ocr"] += 1
            failures.append((str(rel), f"needs_ocr: {str(first.get('detail'))[:80]}"))
            print(f"OCR?  {rel}")
            continue
        if status != 200:
            tally["error"] += 1
            failures.append((str(rel), f"HTTP {status}: {str(first)[:110]}"))
            print(f"FAIL  {rel}  HTTP {status}")
            continue

        markdown = first.get("markdown") or ""
        problems, tags = grade(markdown)
        all_tags.update(tags)

        status2, second = call(f"{args.url}/api/v1/convert", body)
        if status2 != 200 or second.get("sha256_markdown") != first.get("sha256_markdown"):
            problems.append("NOT DETERMINISTIC across two conversions")

        if path.suffix.lower() == ".pdf" and first.get("provider") == "pymupdf":
            truth = pdf_alnum(path)
            got = alnum(markdown)
            if truth and truth > 200 and got < truth * 0.97:
                problems.append(f"fidelity {got}/{truth} alnum ({got / truth:.1%})")

        if problems:
            tally["degraded"] += 1
            failures.append((str(rel), "; ".join(problems[:3])))
            print(f"WARN  {rel}  {problems[0][:80]}")
        else:
            tally["ok"] += 1
            print(f"ok    {rel}  {first.get('provider'):>12s} p={first.get('pages'):<4} {ms}ms")

    print()
    print(f"swept {len(files)} file(s): " + "  ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    print("anchor tags: " + ", ".join(f"{k}:{v}" for k, v in all_tags.most_common(10)))
    print("slowest: " + ", ".join(f"{name} {ms}ms" for ms, name in sorted(slowest, reverse=True)[:5]))
    if failures:
        print(f"\n{len(failures)} problem file(s):")
        for name, why in failures:
            print(f"  {name}: {why}")
    return 0 if tally["error"] + tally["degraded"] + tally["needs_ocr"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
