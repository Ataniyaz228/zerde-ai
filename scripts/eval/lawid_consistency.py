"""Gather-path safety net: law_id -> adilet_code consistency across sources.

The dict-unification refactor (Phase 1.1) deletes/merges the hardcoded law_id
maps scattered across s3_gather / s6 / reference_data into one source of truth
(the `law_metadata` table, read by LawRegistry). The risk: a consolidated
mapping silently disagrees with what the old dicts produced, breaking Adilet
fetch / cache retrieval — and the label-free grounding eval does NOT exercise
that path.

This module compares law_id -> adilet_code across every source and reports:
  - drift_count            : law_ids where sources disagree on the adilet code
  - registry_adilet_match  : % of law_metadata laws whose registry.get_adilet_url
                             yields the ground-truth adilet code

Run BEFORE the refactor to record drift; AFTER, drift should collapse toward 0
(single source) and the match rate stay 100%. No LLM, no network.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zerde.config import get_settings
from zerde.utils.cache import CacheManager
from zerde.utils.law_registry import (
    get_registry,
    _SHORT_TO_ADILET_CODE,
)

_ADILET_CODE_RE = re.compile(r"^[A-Z]\d{6,}_?$")


def _norm_code(code: str | None) -> str | None:
    if not code:
        return None
    return code.strip().rstrip("_").upper() or None


def run_consistency() -> dict:
    settings = get_settings()
    cache = CacheManager(settings.cache_db_path)
    registry = get_registry()

    # Ground truth: law_metadata table.
    meta = {
        r["law_id"].upper(): _norm_code(r["adilet_code"])
        for r in cache.get_law_registry_rows()
        if r.get("law_id") and r.get("adilet_code")
    }

    # After Phase 1.1 there is a single owner of law identity — the registry:
    # law_metadata (DB, authoritative) + its two internal static maps. No stage
    # keeps its own law dict anymore, so this now checks registry-internal
    # consistency against the DB rather than cross-stage drift.
    sources: dict[str, dict[str, str | None]] = {
        "law_metadata": meta,
        "registry._SHORT_TO_ADILET_CODE": {k.upper(): _norm_code(v) for k, v in _SHORT_TO_ADILET_CODE.items()},
    }

    # Per law_id: {source: code}.
    per_law: dict[str, dict[str, str]] = defaultdict(dict)
    for src, mapping in sources.items():
        for lid, code in mapping.items():
            if code:
                per_law[lid][src] = code

    drifts = []
    for lid, by_src in sorted(per_law.items()):
        distinct = set(by_src.values())
        if len(distinct) > 1:
            drifts.append((lid, by_src))

    # registry.get_adilet_url should yield the ground-truth code for known laws.
    matched = mismatched = 0
    mismatches = []
    for lid, gt_code in sorted(meta.items()):
        url = registry.get_adilet_url(lid)
        got = _norm_code(url.rstrip("/").split("/")[-1]) if url else None
        if got == gt_code:
            matched += 1
        else:
            mismatched += 1
            mismatches.append((lid, gt_code, got))

    # COVERAGE GAP (the real refactor risk): law_ids the hardcoded dicts know but
    # which are ABSENT from law_metadata (the would-be single source of truth).
    # These must be migrated into law_metadata before their dict can be deleted,
    # else they lose their adilet mapping. For each, check whether the registry
    # can already reproduce the dict's code (then it's safe) or not (then it blocks).
    dict_codes: dict[str, str] = {}
    for src in ("registry._SHORT_TO_ADILET_CODE",):
        for lid, code in sources[src].items():
            if code:
                dict_codes.setdefault(lid, code)
    only_in_dicts = sorted(lid for lid in dict_codes if lid not in meta)
    blockers = []  # dict-only law_ids the registry CANNOT reproduce -> must migrate
    for lid in only_in_dicts:
        url = registry.get_adilet_url(lid)
        got = _norm_code(url.rstrip("/").split("/")[-1]) if url else None
        if got != dict_codes[lid]:
            blockers.append((lid, dict_codes[lid], got))

    return {
        "sources": {k: len(v) for k, v in sources.items()},
        "law_ids_compared": len(per_law),
        "drift_count": len(drifts),
        "drifts": drifts,
        "registry_adilet_matched": matched,
        "registry_adilet_mismatched": mismatched,
        "registry_adilet_match_rate": round(matched / (matched + mismatched), 3) if (matched + mismatched) else None,
        "mismatches": mismatches,
        "only_in_dicts": only_in_dicts,
        "migration_blockers": blockers,
    }


def print_report(rep: dict) -> None:
    print("\n" + "=" * 80)
    print("LAW_ID -> ADILET CODE CONSISTENCY (gather-path safety net)")
    print("-" * 80)
    print("sources (law_id->code entries):")
    for src, n in rep["sources"].items():
        print(f"    {src:<34} {n}")
    print(f"law_ids compared (≥1 source)   : {rep['law_ids_compared']}")
    print(f"drift_count (sources disagree) : {rep['drift_count']}   (→0 after single source)")
    print(f"registry get_adilet_url match  : {rep['registry_adilet_matched']}/"
          f"{rep['registry_adilet_matched'] + rep['registry_adilet_mismatched']} "
          f"({rep['registry_adilet_match_rate']})")
    if rep["drifts"]:
        print("\n  DRIFTS:")
        for lid, by_src in rep["drifts"]:
            print(f"    {lid}:")
            for src, code in by_src.items():
                print(f"        {code:<14} {src}")
    if rep["mismatches"]:
        print("\n  REGISTRY get_adilet_url MISMATCH (law_id: ground_truth -> got):")
        for lid, gt, got in rep["mismatches"]:
            print(f"    {lid}: {gt} -> {got}")
    print(f"\nCOVERAGE GAP (refactor checklist):")
    print(f"  law_ids known to dicts but NOT in law_metadata : {len(rep['only_in_dicts'])}")
    print(f"  of those, registry CANNOT reproduce (blockers)  : {len(rep['migration_blockers'])}")
    print(f"      → these must be ingested into law_metadata BEFORE deleting their dict")
    if rep["migration_blockers"]:
        print("      blockers (law_id: dict_code -> registry_got):")
        for lid, dc, got in rep["migration_blockers"]:
            print(f"        {lid}: {dc} -> {got}")
    print("=" * 80)


if __name__ == "__main__":
    print_report(run_consistency())
