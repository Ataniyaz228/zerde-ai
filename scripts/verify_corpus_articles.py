#!/usr/bin/env python3
"""
Verify the grounding corpus (cache DB chunks) against adilet — at the ARTICLE level.

Why this exists
---------------
`verify_law_registry.py` proves that a *law* exists on adilet. But the system
grounds verdicts on individual *article* chunks, and that is where confident-but-
false confirmations are born: a claim about article "889-1" (a NEW article a bill
introduces) gets "confirmed" by a chunk of the existing article 889. The corpus
itself is the thing we must be able to trust — every chunk should correspond to a
real adilet article, and we should know which articles we actually cover.

For each law present in the cache, this:
  1. collects the distinct article numbers we have chunks for,
  2. fetches the law from adilet and extracts the REAL article set
     (reusing s3_gather._extract_adilet_articles — the production parser),
  3. reports:
       - articles in our corpus that are NOT on adilet  → false-grounding risk
         (ingest error, wrong attribution, or a hallucinated/renumbered article),
       - coverage = how much of adilet's article set we actually ingested,
       - laws whose law_id is a raw adilet code (e.g. K950001000_) instead of a
         short id — a normalization bug that splits the corpus.

`--fix-titles` rewrites law_metadata.title_ru/title_kz from adilet's <h1> (the
registry verifier showed many stored titles are junk fragments from ingest).

Ground truth is adilet, not the DB. This is the article-level analogue of
verify_law_registry.py and the same CITE-OR-ABSTAIN discipline applied to the
corpus: prove the chunks, don't trust them.

Usage:
  PYTHONPATH=. python scripts/verify_corpus_articles.py
  PYTHONPATH=. python scripts/verify_corpus_articles.py --law 235-V
  PYTHONPATH=. python scripts/verify_corpus_articles.py --fix-titles
  PYTHONPATH=. python scripts/verify_corpus_articles.py --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

import httpx

from zerde.config import get_settings
from zerde.utils.law_registry import get_registry
from zerde.stages.s3_gather import _extract_adilet_articles


@dataclass
class LawReport:
    law_id: str                       # as stored in the corpus
    canonical_id: str                 # registry-resolved short id
    adilet_code: str
    corpus_articles: int
    adilet_articles: int
    missing_from_adilet: list[str]    # in corpus, NOT on adilet  → risk
    coverage_pct: int                 # adilet articles we have a chunk for
    status: str                       # ok | risk | unfetchable | no_code | raw_code_law_id
    adilet_title: str = ""
    note: str = ""
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


_RAW_CODE = __import__("re").compile(r"^[A-Z]\d{5,}_?$")


def _load_corpus_articles(db_path: str) -> dict[str, set[str]]:
    """law_id (as stored) → set of distinct article numbers in the corpus."""
    by_law: dict[str, set[str]] = defaultdict(set)
    conn = sqlite3.connect(db_path)
    for (cj,) in conn.execute("SELECT chunk_json FROM evidence_cache"):
        try:
            o = json.loads(cj)
        except Exception:
            continue
        lid = o.get("law_id")
        art = o.get("article")
        if lid and art:
            by_law[str(lid)].add(str(art).strip())
    conn.close()
    return by_law


async def _fetch_adilet_articles(client: httpx.AsyncClient, base: str, code: str) -> tuple[set[str], str]:
    """Returns (set of adilet article numbers, adilet <h1> title). Empty set if unfetchable."""
    from selectolax.parser import HTMLParser

    url = f"{base}/rus/docs/{code}"
    resp = await client.get(url)
    if resp.status_code != 200:
        return set(), ""
    tree = HTMLParser(resp.text)
    h1 = tree.css_first("h1")
    title = h1.text(strip=True) if h1 else ""
    arts = {a["article_num"].strip() for a in _extract_adilet_articles(tree) if a.get("article_num")}
    return arts, title


async def _build(by_law: dict[str, set[str]], only: str | None) -> list[LawReport]:
    s = get_settings()
    reg = get_registry()
    base = str(s.adilet_base_url).rstrip("/")
    sem = asyncio.Semaphore(3)
    reports: list[LawReport] = []

    async with httpx.AsyncClient(
        timeout=s.adilet_timeout_seconds, follow_redirects=True,
        verify=s.adilet_tls_verify, headers={"User-Agent": "zerde-corpus-verifier/1.0"},
    ) as client:

        async def one(law_id: str, arts: set[str]) -> LawReport:
            canonical = reg.resolve(law_id)
            code = reg.get_adilet_code(law_id) or ""
            is_raw = bool(_RAW_CODE.match(law_id))
            if not code:
                return LawReport(law_id, canonical, "", len(arts), 0, [], 0,
                                 "raw_code_law_id" if is_raw else "no_code",
                                 note="no adilet_code resolvable — corpus law_id may be malformed"
                                 if is_raw else "law not in registry")
            async with sem:
                try:
                    adilet_arts, title = await _fetch_adilet_articles(client, base, code)
                except Exception as exc:
                    return LawReport(law_id, canonical, code, len(arts), 0, [], 0,
                                     "unfetchable", note=f"fetch error: {exc}")
            if not adilet_arts:
                return LawReport(law_id, canonical, code, len(arts), 0, [], 0,
                                 "unfetchable", adilet_title=title, note="no articles parsed from adilet")
            missing = sorted(arts - adilet_arts, key=lambda x: (len(x), x))
            covered = len(arts & adilet_arts)
            coverage = int(covered / len(adilet_arts) * 100) if adilet_arts else 0
            status = "risk" if missing else "ok"
            note = ""
            if is_raw:
                status = "raw_code_law_id"
                note = "corpus law_id is a raw adilet code, not a short id (normalization bug)"
            return LawReport(law_id, canonical, code, len(arts), len(adilet_arts),
                             missing, coverage, status, adilet_title=title, note=note)

        items = sorted(by_law.items(), key=lambda kv: -len(kv[1]))
        if only:
            items = [(lid, a) for lid, a in items if lid == only or reg.resolve(lid) == only]
        reports = await asyncio.gather(*[one(lid, a) for lid, a in items])
    return list(reports)


def _fix_titles(db_path: str, reports: list[LawReport]) -> int:
    """Rewrite law_metadata.title_ru from adilet h1 where we fetched a real title."""
    conn = sqlite3.connect(db_path)
    fixed = 0
    for r in reports:
        if r.adilet_title and r.canonical_id:
            cur = conn.execute(
                "UPDATE law_metadata SET title_ru = ?, updated_at = ? WHERE law_id = ?",
                (r.adilet_title, datetime.now(timezone.utc).isoformat(), r.canonical_id),
            )
            fixed += cur.rowcount
    conn.commit()
    conn.close()
    return fixed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--law", help="check only this law_id (corpus or canonical)")
    ap.add_argument("--fix-titles", action="store_true", help="rewrite law_metadata.title_ru from adilet h1")
    ap.add_argument("--json", metavar="PATH", help="write full JSON report")
    ap.add_argument("--max-missing", type=int, default=15, help="how many missing articles to print per law")
    args = ap.parse_args()

    s = get_settings()
    by_law = _load_corpus_articles(s.cache_db_path)
    if not by_law:
        print("Corpus is empty (no chunks with law_id+article).")
        return 0

    print(f"Verifying {len(by_law)} laws from the corpus against adilet.zan.kz ...\n")
    reports = asyncio.run(_build(by_law, args.law))

    order = {"risk": 0, "raw_code_law_id": 1, "unfetchable": 2, "no_code": 3, "ok": 4}
    reports.sort(key=lambda r: (order.get(r.status, 9), -r.corpus_articles))

    icon = {"ok": "✅", "risk": "⚠️ ", "unfetchable": "❌", "no_code": "❓", "raw_code_law_id": "🔀"}
    for r in reports:
        line = (f"{icon.get(r.status,'?')} {r.law_id:<14} corpus={r.corpus_articles:<4} "
                f"adilet={r.adilet_articles:<4} coverage={r.coverage_pct:>3}%  [{r.status}]")
        if r.missing_from_adilet:
            shown = r.missing_from_adilet[:args.max_missing]
            more = f" …+{len(r.missing_from_adilet)-len(shown)}" if len(r.missing_from_adilet) > len(shown) else ""
            line += f"  not-on-adilet: {', '.join(shown)}{more}"
        if r.note:
            line += f"   ({r.note})"
        print(line)

    n_risk = sum(1 for r in reports if r.status == "risk")
    n_raw = sum(1 for r in reports if r.status == "raw_code_law_id")
    n_unf = sum(1 for r in reports if r.status == "unfetchable")
    total_missing = sum(len(r.missing_from_adilet) for r in reports)
    print(f"\nSummary: {len(reports)} laws → ⚠️  {n_risk} with articles not on adilet "
          f"({total_missing} article-chunks total) · 🔀 {n_raw} raw-code law_id · ❌ {n_unf} unfetchable")

    if args.fix_titles:
        fixed = _fix_titles(s.cache_db_path, reports)
        print(f"\n--fix-titles: rewrote title_ru for {fixed} law_metadata rows from adilet.")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in reports], f, ensure_ascii=False, indent=2)
        print(f"JSON report → {args.json}")

    return 1 if (n_risk or n_unf) else 0


if __name__ == "__main__":
    sys.exit(main())
