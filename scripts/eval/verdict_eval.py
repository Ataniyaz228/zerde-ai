#!/usr/bin/env python3
"""
Verdict-level eval — measures the system's WORST failure: false confirmation.

scripts/eval/run_eval.py is label-free (deterministic grounding only, no LLM).
This one runs the REAL auditor (S5 run_auditor + S6 audit_analysis, with the LLM)
over crafted claims whose correct verdict we know, and measures:

  false_confirm_rate  — planted FALSE claims that came back CONFIRMED  (target → 0)
                        THE metric. Confirming something false is the worst error.
  confirm_rate(true)  — TRUE claims (quoted from real article text) that confirm.
                        Sanity: the system must still be able to confirm real facts.

The headline planted case is the one we saw in production: a bill introduces a
NEW article "889-1"; it does not exist in the current law, so the only correct
verdict is UNVERIFIED — never CONFIRMED (e.g. by matching the adjacent article
889). See project memory `project-law-ground-truth`.

Grounding uses the real cache corpus (offline). Only the auditor LLM is called.

Usage:
  PYTHONPATH=. python scripts/eval/verdict_eval.py                 # law 235-V
  PYTHONPATH=. python scripts/eval/verdict_eval.py --law 226-V --new-article 12-9
  PYTHONPATH=. python scripts/eval/verdict_eval.py --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from dataclasses import dataclass

from zerde.config import get_settings
from zerde.models import (
    ClaimExtractionResult,
    ClaimSeverity,
    ClaimType,
    DocumentClaim,
    EvidenceChunk,
    QueryPlan,
)
from zerde.stages.s5_analyst import run_auditor
from zerde.stages.s6_auditor import audit_analysis
from zerde.utils.law_registry import get_registry


def _load_law_chunks(db_path: str, canonical_id: str) -> list[EvidenceChunk]:
    """Load all corpus chunks whose resolved law_id == canonical_id."""
    reg = get_registry()
    conn = sqlite3.connect(db_path)
    out: list[EvidenceChunk] = []
    for (cj,) in conn.execute("SELECT chunk_json FROM evidence_cache"):
        try:
            o = json.loads(cj)
        except Exception:
            continue
        lid = o.get("law_id")
        if not lid:
            continue
        if lid == canonical_id or reg.resolve(str(lid)) == canonical_id:
            try:
                out.append(EvidenceChunk.model_validate(o))
            except Exception:
                continue
    conn.close()
    return out


def _pick_real_article(chunks: list[EvidenceChunk], prefer: str | None = None) -> EvidenceChunk | None:
    """Pick a chunk for a real article with enough content to quote."""
    candidates = [c for c in chunks if c.article and c.content and len(c.content) > 200]
    if prefer:
        for c in candidates:
            if str(c.article) == prefer:
                return c
    return max(candidates, key=lambda c: len(c.content), default=None)


@dataclass
class Case:
    claim_id: str
    kind: str          # "true" | "planted"
    expect_not: str    # verdict that would be a FAILURE for this case
    label: str


def _build_cases(law: str, real_chunk: EvidenceChunk, new_article: str) -> tuple[list[DocumentClaim], list[Case]]:
    claims: list[DocumentClaim] = []
    cases: list[Case] = []

    # 1) TRUE claim — the real article text asserted directly (verbatim). Should CONFIRM.
    quote = real_chunk.content[:180].strip()
    claims.append(DocumentClaim(
        claim_id="true_real_article",
        claim_text=quote,
        quote=quote,
        claim_type=ClaimType.LEGAL_REF,
        severity=ClaimSeverity.MEDIUM,
        target_law_ids=[law],
    ))
    cases.append(Case("true_real_article", "true", expect_not="UNVERIFIED",
                      label=f"real art.{real_chunk.article} verbatim → expect CONFIRMED"))

    # 2) PLANTED: a NEW article the bill introduces (does NOT exist in the law).
    #    Only correct verdict = UNVERIFIED. CONFIRMED here = the 889-1 false-confirm.
    claims.append(DocumentClaim(
        claim_id="planted_new_article",
        claim_text=(f"Кодекс {law} дополняется новой статьёй {new_article}, вводящей "
                    f"цифровой реестр и алгоритмическое прекращение производства."),
        quote=f"«статья {new_article}». Цифровой реестр...",
        claim_type=ClaimType.LEGAL_REF,
        severity=ClaimSeverity.HIGH,
        target_law_ids=[law],
    ))
    cases.append(Case("planted_new_article", "planted", expect_not="CONFIRMED",
                      label=f"NEW art.{new_article} (does not exist) → must NOT be CONFIRMED"))

    # 3) PLANTED: absurd article number. Must NOT confirm.
    claims.append(DocumentClaim(
        claim_id="planted_absurd_article",
        claim_text=f"Статья 99999 закона {law} устанавливает обязательную биометрию для всех граждан.",
        quote="«статья 99999»",
        claim_type=ClaimType.LEGAL_REF,
        severity=ClaimSeverity.HIGH,
        target_law_ids=[law],
    ))
    cases.append(Case("planted_absurd_article", "planted", expect_not="CONFIRMED",
                      label="absurd art.99999 → must NOT be CONFIRMED"))

    # 4) PLANTED: fabricated factual content attributed to a real article.
    claims.append(DocumentClaim(
        claim_id="planted_fabricated_fact",
        claim_text=(f"Статья {real_chunk.article} закона {law} вводит смертную казнь "
                    f"за административные правонарушения и конфискацию всего имущества."),
        quote="«смертная казнь за административные правонарушения»",
        claim_type=ClaimType.FACTUAL,
        severity=ClaimSeverity.CRITICAL,
        target_law_ids=[law],
    ))
    cases.append(Case("planted_fabricated_fact", "planted", expect_not="CONFIRMED",
                      label=f"fabricated content on real art.{real_chunk.article} → must NOT be CONFIRMED"))

    return claims, cases


async def _run(law: str, new_article: str) -> dict:
    s = get_settings()
    reg = get_registry()
    canonical = reg.resolve(law)
    chunks = _load_law_chunks(s.cache_db_path, canonical)
    if not chunks:
        return {"error": f"no cached chunks for law {law} ({canonical})"}

    real = _pick_real_article(chunks)
    if real is None:
        return {"error": f"no usable real-article chunk for {law}"}

    claims_list, cases = _build_cases(law, real, new_article)
    plan = QueryPlan(plan_id="verdict_eval", source_doc_id="verdict_eval")

    # Each case is audited in ISOLATION (its own auditor call). A controlled test
    # varies one claim at a time — batching planted+true claims together makes the
    # LLM conservative across the whole batch and muddies the discrimination signal.
    by_claim: dict[str, str] = {}
    for dc in claims_list:
        single = ClaimExtractionResult(doc_id="verdict_eval", claims=[dc])
        analysis = await run_auditor(chunks=chunks, plan=plan, claims=single, doc_text="")
        analysis = audit_analysis(analysis, chunks, single)
        v = next((v for v in analysis.verdicts if v.claim_id == dc.claim_id), None)
        by_claim[dc.claim_id] = v.status.value if v else "MISSING"

    results = []
    planted_total = planted_confirmed = true_total = true_confirmed = 0
    for c in cases:
        status = by_claim.get(c.claim_id, "MISSING")
        if c.kind == "planted":
            planted_total += 1
            failed = (status == "CONFIRMED")
            if failed:
                planted_confirmed += 1
        else:
            true_total += 1
            if status == "CONFIRMED":
                true_confirmed += 1
            failed = (status != "CONFIRMED")
        results.append({"claim_id": c.claim_id, "kind": c.kind, "status": status,
                        "ok": not failed, "label": c.label})

    return {
        "law": law, "canonical": canonical, "chunks_loaded": len(chunks),
        "real_article_used": str(real.article),
        "false_confirm_rate": round(planted_confirmed / planted_total, 3) if planted_total else 0.0,
        "true_confirm_rate": round(true_confirmed / true_total, 3) if true_total else 0.0,
        "planted_total": planted_total, "planted_confirmed": planted_confirmed,
        "results": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--law", default="235-V", help="law short id (default 235-V КоАП)")
    ap.add_argument("--new-article", default="889-1", help="non-existent article to plant (default 889-1)")
    ap.add_argument("--json", metavar="PATH", help="write JSON report")
    args = ap.parse_args()

    print(f"Verdict-level eval — law {args.law}, planting new article {args.new_article}\n")
    out = asyncio.run(_run(args.law, args.new_article))
    if "error" in out:
        print(f"ERROR: {out['error']}")
        return 2

    for r in out["results"]:
        icon = "✅" if r["ok"] else "❌"
        print(f"{icon} [{r['kind']:<7}] {r['status']:<12} {r['label']}")

    print(f"\nLoaded {out['chunks_loaded']} chunks for {out['canonical']}; "
          f"true article used: {out['real_article_used']}")
    print(f"false_confirm_rate = {out['false_confirm_rate']}  "
          f"({out['planted_confirmed']}/{out['planted_total']} planted claims wrongly CONFIRMED)   [target 0.0]")
    print(f"true_confirm_rate  = {out['true_confirm_rate']}  (sanity: real facts still confirm)")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"JSON report → {args.json}")

    # Fail the run if any planted claim was confirmed (false-confirm regression gate).
    return 1 if out["planted_confirmed"] else 0


if __name__ == "__main__":
    sys.exit(main())
