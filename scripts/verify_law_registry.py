#!/usr/bin/env python3
"""
Verify the law registry against adilet.zan.kz — the ground truth for НПА РК.

Why this exists
---------------
The system has several places that claim "law_id X ↔ adilet_code Y ↔ title Z":
the cache DB (`law_metadata`) and the hand-typed static fallbacks in
`law_registry._STATIC_FALLBACK` / `_ADILET_CODE_TO_SHORT`. None of those are
authoritative — they are copies that can drift or be wrong (a repealed code, a
typo, a renumbered law). Neither the LLM nor a human editing a dict is allowed
to be the arbiter of "which law exists": the only arbiter is adilet.

This harness turns trust into a test. For every (law_id → adilet_code) mapping
it fetches the adilet page and records what adilet actually says: the title and
whether the act is in force ("действует") or repealed ("Утратил силу").

Classification per entry:
  verified  — page exists, in force, title looks like a real act title
  stale     — page exists but adilet says "Утратил силу" (repealed)
  broken    — 404 / no title / unreachable  → the mapping is unprovable

Exit code is non-zero if any entry is `broken` (so CI can gate on it). `stale`
entries are reported but do not fail the run by default (use --strict to also
fail on stale): a repealed code may still be intentionally referenced by an
amending bill.

This is the registry-level analogue of scripts/eval/run_eval.py: a $0-ish
deterministic check (only cost is HTTP to adilet) that proves the law data
instead of asserting it.

Usage:
  python scripts/verify_law_registry.py                 # check everything
  python scripts/verify_law_registry.py --only-db       # only law_metadata rows
  python scripts/verify_law_registry.py --only-static   # only static fallbacks
  python scripts/verify_law_registry.py --strict        # fail on stale too
  python scripts/verify_law_registry.py --json out.json # also write JSON report
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

import httpx

from zerde.config import get_settings
from zerde.utils.law_registry import (
    _ADILET_CODE_TO_SHORT,
    get_registry,
)

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
# adilet renders the in-force banner as plain text in the page body.
_REPEALED_RE = re.compile(r"утратил[ау]?\s+силу", re.IGNORECASE)
_IN_FORCE_RE = re.compile(r"\bдейству(?:ет|ющ)", re.IGNORECASE)


@dataclass
class Entry:
    law_id: str
    adilet_code: str
    expected_title: str  # what our registry thinks the title is ("" if unknown)
    source: str          # "db" | "static"


@dataclass
class Result:
    law_id: str
    adilet_code: str
    source: str
    status: str          # verified | stale | broken
    http_status: int | None = None
    adilet_title: str = ""
    in_force: bool | None = None
    note: str = ""
    checked_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


def _collect_entries(only_db: bool, only_static: bool) -> list[Entry]:
    """Gather every (law_id → adilet_code) mapping the system relies on."""
    reg = get_registry()
    seen: dict[tuple[str, str], Entry] = {}

    if not only_static:
        for law_id, meta in reg._by_law_id.items():
            code = (meta.get("adilet_code") or "").strip()
            if not code:
                continue
            seen[(law_id, code)] = Entry(
                law_id=law_id,
                adilet_code=code,
                expected_title=meta.get("title_ru") or "",
                source="db",
            )

    if not only_db:
        for code, law_id in _ADILET_CODE_TO_SHORT.items():
            key = (law_id, code)
            if key not in seen:
                seen[key] = Entry(
                    law_id=law_id,
                    adilet_code=code,
                    expected_title="",
                    source="static",
                )

    return sorted(seen.values(), key=lambda e: (e.source, e.law_id))


def _clean_title(raw_title: str) -> str:
    # adilet titles look like: 'Бюджетный кодекс РК - ИПС "Әділет"'
    t = re.sub(r"\s*-\s*ИПС.*$", "", raw_title, flags=re.IGNORECASE).strip()
    return re.sub(r"\s+", " ", t)


async def _check_one(client: httpx.AsyncClient, base: str, sem: asyncio.Semaphore, e: Entry) -> Result:
    url = f"{base}/rus/docs/{e.adilet_code}"
    async with sem:
        try:
            resp = await client.get(url)
        except Exception as exc:  # network / TLS / timeout
            return Result(e.law_id, e.adilet_code, e.source, "broken", note=f"fetch error: {exc}")

    if resp.status_code != 200:
        return Result(e.law_id, e.adilet_code, e.source, "broken",
                      http_status=resp.status_code, note="non-200")

    html = resp.text
    m = _TITLE_RE.search(html)
    title = _clean_title(m.group(1)) if m else ""
    # adilet returns a 200 "not found" shell for unknown codes; treat empty/error titles as broken.
    if not title or "не найден" in title.lower() or "ошибка" in title.lower():
        return Result(e.law_id, e.adilet_code, e.source, "broken",
                      http_status=200, adilet_title=title, note="no real title (likely 404 shell)")

    repealed = bool(_REPEALED_RE.search(html))
    in_force = (not repealed) and bool(_IN_FORCE_RE.search(html))
    if repealed:
        return Result(e.law_id, e.adilet_code, e.source, "stale",
                      http_status=200, adilet_title=title, in_force=False,
                      note="adilet: Утратил силу (repealed)")

    note = ""
    if e.expected_title and e.source == "db":
        # soft title sanity check: first 12 normalized chars overlap
        a = re.sub(r"\W+", "", e.expected_title.lower())[:12]
        b = re.sub(r"\W+", "", title.lower())[:12]
        if a and b and a != b:
            note = f"title differs from registry ('{e.expected_title}')"
    return Result(e.law_id, e.adilet_code, e.source, "verified",
                  http_status=200, adilet_title=title, in_force=in_force, note=note)


async def _run(entries: list[Entry]) -> list[Result]:
    s = get_settings()
    base = str(s.adilet_base_url).rstrip("/")
    sem = asyncio.Semaphore(3)  # be polite to adilet (same as pipeline)
    # verify=s.adilet_tls_verify (default False): adilet's chain often fails default verification.
    async with httpx.AsyncClient(
        timeout=s.adilet_timeout_seconds,
        follow_redirects=True,
        verify=s.adilet_tls_verify,
        headers={"User-Agent": "zerde-law-registry-verifier/1.0"},
    ) as client:
        return await asyncio.gather(*[_check_one(client, base, sem, e) for e in entries])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only-db", action="store_true", help="check only law_metadata DB rows")
    ap.add_argument("--only-static", action="store_true", help="check only static fallback codes")
    ap.add_argument("--strict", action="store_true", help="exit non-zero on stale (repealed) entries too")
    ap.add_argument("--json", metavar="PATH", help="write full JSON report to PATH")
    args = ap.parse_args()

    entries = _collect_entries(args.only_db, args.only_static)
    if not entries:
        print("No (law_id → adilet_code) mappings found to verify.")
        return 0

    print(f"Verifying {len(entries)} law_id→adilet_code mappings against adilet.zan.kz ...\n")
    results = asyncio.run(_run(entries))

    order = {"broken": 0, "stale": 1, "verified": 2}
    results.sort(key=lambda r: (order.get(r.status, 9), r.source, r.law_id))

    icon = {"verified": "✅", "stale": "⚠️ ", "broken": "❌"}
    width = max(len(r.law_id) for r in results)
    for r in results:
        line = f"{icon[r.status]} {r.law_id:<{width}}  {r.adilet_code:<14} [{r.source}]  {r.status}"
        if r.adilet_title:
            line += f"  — {r.adilet_title[:60]}"
        if r.note:
            line += f"   ({r.note})"
        print(line)

    n = len(results)
    n_ver = sum(1 for r in results if r.status == "verified")
    n_stale = sum(1 for r in results if r.status == "stale")
    n_broken = sum(1 for r in results if r.status == "broken")
    print(f"\nSummary: {n} mappings → ✅ {n_ver} verified · ⚠️  {n_stale} stale · ❌ {n_broken} broken")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in results], f, ensure_ascii=False, indent=2)
        print(f"JSON report → {args.json}")

    if n_broken:
        print("\nFAIL: broken mappings exist (code unprovable against adilet).")
        return 1
    if args.strict and n_stale:
        print("\nFAIL (--strict): stale (repealed) mappings exist.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
