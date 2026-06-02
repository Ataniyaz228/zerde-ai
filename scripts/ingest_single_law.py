"""Phase 4: ingest ONE law from adilet into the cache, retiring its fallback.

Single safe increment of the "replace hardcoded fallbacks with real adilet data"
plan. Reuses the production fetch+parse path (s3_gather._fetch_adilet_with_fallback)
so the corpus stays consistent with how the pipeline gathers live.

Two gotchas this handles that ad-hoc ingest gets wrong:
  1. _parse_adilet_html stamps chunk.law_id with the ADILET CODE from the URL
     (e.g. "Z1300000550"), but the corpus convention is the canonical SHORT id
     ("550-IV"). We remap every chunk to the short id before writing, else exact
     law_id+article retrieval can't find them.
  2. The adilet article parser is known-flaky (misses pages). A PARTIAL ingest
     looks authoritative but is incomplete — worse than the honest fallback. So
     --apply refuses below --min-articles, and DRY mode (default) writes nothing.

Usage:
  # DRY (default): fetch + parse + report, NO db write, NO embed
  PYTHONPATH=. .venv/bin/python scripts/ingest_single_law.py 550-IV

  # APPLY: backs up the db, writes chunks + embeddings + law_metadata, reloads
  PYTHONPATH=. .venv/bin/python scripts/ingest_single_law.py 550-IV --apply
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import time
from pathlib import Path

from zerde.models import AdiletFallbackStrategy, AdiletQuery
from zerde.stages.s3_gather import _fetch_adilet_with_fallback
from zerde.utils.cache import CacheManager
from zerde.utils.law_registry import get_registry, reload_registry


def _title_from_chunks(chunks) -> str:
    """Law title = the chunk source_title with the trailing "| Ст. N" stripped."""
    for c in chunks:
        t = (c.source_title or "").split("| Ст.")[0].strip()
        if t:
            return t
    return ""


async def _fetch(law_id: str) -> list:
    cache = CacheManager()
    query = AdiletQuery(query_text=law_id, law_ids=[law_id])
    chunks = await _fetch_adilet_with_fallback(query, cache)
    # Keep only chunks really parsed off adilet (drop any local-cache fallback).
    return [c for c in chunks if c.adilet_fallback_used == AdiletFallbackStrategy.CSS_SELECTOR]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("law_id", help="canonical short id, e.g. 550-IV")
    ap.add_argument("--adilet-code", default="", help="override; else resolved via registry")
    ap.add_argument("--apply", action="store_true", help="actually write to the db (default: dry-run)")
    ap.add_argument("--min-articles", type=int, default=15, help="refuse --apply below this (anti partial-ingest)")
    args = ap.parse_args()

    registry = get_registry()
    law_id = registry.resolve(args.law_id)
    adilet_code = args.adilet_code or registry.get_adilet_code(law_id)
    if not adilet_code:
        print(f"❌ no adilet code for {law_id!r} (not in registry/fallbacks) — pass --adilet-code")
        return 1

    print(f"law_id={law_id}  adilet_code={adilet_code}  mode={'APPLY' if args.apply else 'DRY-RUN'}")
    chunks = asyncio.run(_fetch(law_id))
    if not chunks:
        print("❌ adilet returned 0 parsed articles — parser failed or page unreachable. Nothing written.")
        return 1

    # Remap code-form law_id (from URL) → canonical short id (corpus convention).
    for c in chunks:
        c.law_id = law_id

    arts = sorted({c.article for c in chunks if c.article}, key=lambda a: [int(p) for p in a.split("-") if p.isdigit()] or [0])
    title = _title_from_chunks(chunks)
    print(f"parsed: {len(chunks)} chunks | {len(arts)} unique articles")
    print(f"title : {title[:80]}")
    print(f"articles: {arts[:25]}{' …' if len(arts) > 25 else ''}")

    if not args.apply:
        print(f"\nDRY-RUN — nothing written. {len(chunks)} chunks would go under law_id={law_id}.")
        print("Inspect the article list for completeness vs adilet, then re-run with --apply.")
        return 0

    if len(arts) < args.min_articles:
        print(f"\n❌ only {len(arts)} articles (< --min-articles {args.min_articles}). "
              f"Refusing: a partial ingest is worse than the fallback. Nothing written.")
        return 1

    cache = CacheManager()
    bak = Path(str(cache.db_path) + f".bak-phase4-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(cache.db_path, bak)
    print(f"\nbackup: {bak}")

    stored = asyncio.run(cache.put_many(chunks))
    cache.embed_chunks(chunks)
    cache.upsert_law_metadata(law_id=law_id, adilet_code=adilet_code, title_ru=title, chunk_count=len(chunks))
    reload_registry()
    print(f"✓ wrote {stored} new chunks + embeddings; law_metadata[{law_id}] upserted; registry reloaded.")
    print("\nVERIFY ($0):")
    print(f"  PYTHONPATH=. .venv/bin/python scripts/verify_law_registry.py | grep {law_id}")
    print("  PYTHONPATH=. .venv/bin/python scripts/eval/lawid_consistency.py")
    print(f"{law_id} is now in law_metadata — adilet is its single source. No dict to edit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
