#!/usr/bin/env python3
"""
Apply the SAFE, unambiguous corpus fixes surfaced by verify_corpus_articles.py.

Dry-run by default — prints exactly what would change. Pass --apply to write.
Only does fixes that are provably correct; everything else is reported, not touched.

Fixes:
  1. relabel-raw-code : chunks whose law_id is a raw adilet code that the registry
     maps to a short id (e.g. K1400000235 → 235-V) are re-labelled so they merge
     with the law's main corpus instead of being orphaned. A raw code that does NOT
     resolve (e.g. K950001000_ = Конституция, which has no short id) is left alone.
  2. drop-orphan-87iv : chunks with law_id '87-IV' — a law proven NOT to exist
     (the real Personal-Data law is 94-V) whose source_url is the bogus /docs/87-IV.
     These can never be valid grounding → deleted.
  3. fix-titles       : rewrite law_metadata.title_ru from adilet's real <h1>
     (ingest stored junk fragments). Requires network; skip with --no-titles.

Always back up zerde_cache.db before --apply (the script refuses without a backup).

Usage:
  PYTHONPATH=. python scripts/fix_corpus.py            # dry-run report
  PYTHONPATH=. python scripts/fix_corpus.py --apply     # write changes
  PYTHONPATH=. python scripts/fix_corpus.py --apply --no-titles
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import re
import sqlite3
import sys

from zerde.config import get_settings
from zerde.utils.law_registry import get_registry

_SHORT_ID = re.compile(r"^\d+-[IVXLCDM]+(?:-[A-Z]+)?$")
_RAW_CODE = re.compile(r"^[A-Z]\d{5,}_?$")


def _scan(db_path: str):
    """Return (relabel: list[(chunk_id, old_lid, new_lid)], drop: list[chunk_id])."""
    reg = get_registry()
    conn = sqlite3.connect(db_path)
    relabel, drop = [], []
    for (chunk_id, cj) in conn.execute("SELECT chunk_id, chunk_json FROM evidence_cache"):
        try:
            o = json.loads(cj)
        except Exception:
            continue
        lid = str(o.get("law_id") or "")
        if not lid:
            continue
        if lid == "87-IV":
            drop.append(chunk_id)
        elif _RAW_CODE.match(lid):
            resolved = reg.resolve(lid)
            if resolved != lid and _SHORT_ID.match(resolved):
                relabel.append((chunk_id, lid, resolved))
    conn.close()
    return relabel, drop


def _apply_chunk_fixes(db_path: str, relabel, drop) -> None:
    conn = sqlite3.connect(db_path)
    for chunk_id, _old, new_lid in relabel:
        row = conn.execute("SELECT chunk_json FROM evidence_cache WHERE chunk_id = ?", (chunk_id,)).fetchone()
        if not row:
            continue
        o = json.loads(row[0])
        o["law_id"] = new_lid
        conn.execute("UPDATE evidence_cache SET chunk_json = ? WHERE chunk_id = ?",
                     (json.dumps(o, ensure_ascii=False), chunk_id))
    if drop:
        conn.executemany("DELETE FROM evidence_cache WHERE chunk_id = ?", [(c,) for c in drop])
    conn.commit()
    conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--no-titles", action="store_true", help="skip the adilet title refresh")
    args = ap.parse_args()

    s = get_settings()
    db = s.cache_db_path

    relabel, drop = _scan(db)
    from collections import Counter
    relabel_by = Counter(f"{o}→{n}" for _, o, n in relabel)

    print("=== SAFE corpus fixes ===\n")
    print(f"1. relabel raw-code law_id → short id: {len(relabel)} chunks")
    for k, n in relabel_by.items():
        print(f"     {k}: {n} chunks")
    print(f"2. drop orphan 87-IV chunks (non-existent law): {len(drop)} chunks")

    if args.apply:
        if not glob.glob(db + ".bak*") and not glob.glob(db + "*.bak*"):
            print(f"\nREFUSING --apply: no backup of {db} found (expected {db}.bak*). Back it up first.")
            return 2
        _apply_chunk_fixes(db, relabel, drop)
        print(f"\n✅ applied: {len(relabel)} relabelled, {len(drop)} dropped.")
    else:
        print("\n(dry-run — no changes written; pass --apply to write)")

    if not args.no_titles:
        print("\n3. title refresh from adilet:")
        try:
            from scripts.verify_corpus_articles import _load_corpus_articles, _build
            by_law = _load_corpus_articles(db)
            reports = asyncio.run(_build(by_law, None))
            fixable = [r for r in reports if r.adilet_title and r.canonical_id]
            print(f"     {len(fixable)} law_metadata titles can be refreshed from adilet")
            if args.apply:
                from scripts.verify_corpus_articles import _fix_titles
                n = _fix_titles(db, reports)
                print(f"     ✅ rewrote {n} titles")
            else:
                print("     (dry-run — run verify_corpus_articles.py --fix-titles, or this with --apply)")
        except Exception as e:
            print(f"     skipped (title refresh error: {e})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
