"""Label-free eval orchestrator (Phase 0).

Runs S1 ingest + S2.5 deterministic extraction + S6 grounding over a
stratified sample of the corpus, plus mutation tests, and reports metrics.
NO LLM is called — everything here is deterministic and free.

Usage:
    python scripts/eval/run_eval.py --sample 30
    python scripts/eval/run_eval.py --sample 50 --max-scan 250
    python scripts/eval/run_eval.py --corpus data/corpus --out data/eval
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, UTC
from pathlib import Path

# Make `zerde.*` and sibling eval modules importable when run directly.
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# OCR off — label-free eval must stay fast; we skip PDFs anyway.
os.environ.setdefault("ZERDE_PDF_OCR_ENABLED", "0")

from zerde.config import get_settings  # noqa: E402
from zerde.stages.s1_ingest import ingest_document  # noqa: E402
from zerde.stages.s2_5_claim_extractor import _detect_target_laws, _regex_extract  # noqa: E402
from zerde.utils.cache import CacheManager  # noqa: E402

from grounding import build_article_index, check_grounding  # noqa: E402
from mutate import mutate_article, mutate_law  # noqa: E402


def _claims_with_article(claims) -> list:
    """Claims that have both a target law and an extractable article."""
    from zerde.stages.s6_auditor import _extract_article_from_claim
    out = []
    for c in claims:
        if getattr(c, "target_law_ids", None) and _extract_article_from_claim(c):
            out.append(c)
    return out


async def _load_qualifying_bills(corpus_dir: Path, max_scan: int) -> list[dict]:
    """Scan .docx files, ingest (no OCR), keep bills with target law + claims.

    Returns list of {file, target_laws, claims} sorted deterministically.
    """
    files = sorted(p for p in corpus_dir.rglob("*.docx") if p.is_file())
    qualifying: list[dict] = []
    scanned = 0
    for path in files:
        if scanned >= max_scan:
            break
        scanned += 1
        try:
            ds = await ingest_document(str(path))
        except Exception:
            continue
        text = ds.normalized_text
        targets = _detect_target_laws(text)
        if not targets:
            continue
        claims = _regex_extract(text)
        # Propagate detected target laws onto claims (mirrors S2.5 behaviour).
        for c in claims:
            if not getattr(c, "target_law_ids", None):
                c.target_law_ids = targets
        usable = _claims_with_article(claims)
        if usable:
            qualifying.append({"file": str(path), "target_laws": targets, "claims": usable})
    return qualifying


def _stratified_sample(bills: list[dict], n: int) -> list[dict]:
    """Round-robin across primary target_law so the sample spans laws."""
    by_law: dict[str, list[dict]] = defaultdict(list)
    for b in bills:
        by_law[b["target_laws"][0]].append(b)
    groups = [by_law[k] for k in sorted(by_law)]
    picked: list[dict] = []
    i = 0
    while len(picked) < n and any(groups):
        g = groups[i % len(groups)]
        if g:
            picked.append(g.pop(0))
        i += 1
        groups = [grp for grp in groups if grp] or []
        if not groups:
            break
    return picked[:n]


def _eval_bill(bill: dict, article_index) -> dict:
    claims = bill["claims"]
    total = len(claims)

    # Clean grounding (run twice for stability).
    clean_a = [check_grounding(c, article_index) for c in claims]
    clean_b = [check_grounding(c, article_index) for c in claims]
    grounded = sum(1 for r in clean_a if r["grounded"])
    stable = [r["grounded"] for r in clean_a] == [r["grounded"] for r in clean_b]

    # Mutation: law corruption (right article, wrong law).
    law_total = law_false = 0
    for c in claims:
        mut = mutate_law(c)
        if not mut:
            continue
        law_total += 1
        if check_grounding(mut[0], article_index)["grounded"]:
            law_false += 1

    # Mutation: article corruption (right law, impossible article).
    art_total = art_false = 0
    for c in claims:
        mut = mutate_article(c)
        if not mut:
            continue
        art_total += 1
        if check_grounding(mut[0], article_index)["grounded"]:
            art_false += 1

    return {
        "file": bill["file"],
        "target_laws": bill["target_laws"],
        "claims": total,
        "grounded": grounded,
        "grounding_rate": round(grounded / total, 3) if total else None,
        "stable": stable,
        "law_mut_total": law_total,
        "law_false_grounding": law_false,
        "article_mut_total": art_total,
        "article_false_grounding": art_false,
    }


def _aggregate(results: list[dict]) -> dict:
    n = len(results)
    tot_claims = sum(r["claims"] for r in results)
    tot_grounded = sum(r["grounded"] for r in results)
    law_total = sum(r["law_mut_total"] for r in results)
    law_false = sum(r["law_false_grounding"] for r in results)
    art_total = sum(r["article_mut_total"] for r in results)
    art_false = sum(r["article_false_grounding"] for r in results)
    return {
        "bills": n,
        "claims_total": tot_claims,
        "grounding_rate": round(tot_grounded / tot_claims, 3) if tot_claims else None,
        "law_false_grounding_rate": round(law_false / law_total, 3) if law_total else None,
        "article_false_grounding_rate": round(art_false / art_total, 3) if art_total else None,
        "stability_all_pass": all(r["stable"] for r in results),
    }


def _print_table(results: list[dict], agg: dict) -> None:
    print("\n" + "=" * 92)
    print(f"{'target':<12}{'claims':>7}{'grnd':>6}{'rate':>7}{'lawFG':>7}{'artFG':>7}  file")
    print("-" * 92)
    for r in results:
        print(
            f"{r['target_laws'][0]:<12}{r['claims']:>7}{r['grounded']:>6}"
            f"{(r['grounding_rate'] or 0):>7.2f}"
            f"{r['law_false_grounding']:>4}/{r['law_mut_total']:<2}"
            f"{r['article_false_grounding']:>4}/{r['article_mut_total']:<2}"
            f"  {Path(r['file']).name[:34]}"
        )
    print("=" * 92)
    print("AGGREGATE")
    print(f"  bills                        : {agg['bills']}")
    print(f"  claims (with law+article)    : {agg['claims_total']}")
    print(f"  grounding_rate (clean)       : {agg['grounding_rate']}  (coverage potential, higher=better)")
    print(f"  law_false_grounding_rate     : {agg['law_false_grounding_rate']}  (→0; corrupted law still grounded = BUG)")
    print(f"  article_false_grounding_rate : {agg['article_false_grounding_rate']}  (→0; impossible article grounded = BUG)")
    print(f"  stability_all_pass           : {agg['stability_all_pass']}  (deterministic path)")
    print("=" * 92)


async def main_async(args) -> int:
    corpus = Path(args.corpus)
    if not corpus.exists():
        print(f"Corpus not found: {corpus}", file=sys.stderr)
        return 1

    settings = get_settings()
    cache = CacheManager(settings.cache_db_path)
    print(f"cache: {settings.cache_db_path}")
    print("Building article index from cache (once)...")
    t0 = time.time()
    article_index = build_article_index(cache)
    print(f"  indexed {sum(len(v) for v in article_index.values())} chunks "
          f"across {len(article_index)} (law,article) keys in {time.time()-t0:.1f}s")

    print(f"Scanning corpus for qualifying bills (max-scan={args.max_scan})...")
    bills = await _load_qualifying_bills(corpus, args.max_scan)
    print(f"  {len(bills)} qualifying bills (target law + claims with article)")

    sample = _stratified_sample(bills, args.sample)
    print(f"  sampled {len(sample)} (stratified by target law)")

    results = [_eval_bill(b, article_index) for b in sample]
    agg = _aggregate(results)
    _print_table(results, agg)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"results_{ts}.json"
    out_path.write_text(
        json.dumps(
            {"generated_at": ts, "aggregate": agg, "bills": results},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nResults: {out_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="data/corpus")
    ap.add_argument("--out", default="data/eval")
    ap.add_argument("--sample", type=int, default=30, help="Bills to evaluate (stratified)")
    ap.add_argument("--max-scan", type=int, default=200, help="Max files to ingest while collecting candidates")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
