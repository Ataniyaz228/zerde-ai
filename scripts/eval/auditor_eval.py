#!/usr/bin/env python3
"""Two-sided auditor eval — measures BOTH error directions, not just one.

  true_confirm_rate   — реальные статьи (дословная цитата) → CONFIRMED. RECALL,
                        выше = лучше. Низкий = over-abstention (бесполезный, но «безопасный»).
  false_confirm_rate  — подброшенные ложные claim'ы → CONFIRMED. THE метрика, target 0.

Adversarial planted-набор включает кейсы, которые ловят НАИВНЫЙ детерминированный
confirm (иначе он поднимет false-confirm):
  padded_false      — реальный текст статьи + ложное число (высокий bm25, но claim ложный)
  adjacent_article  — текст статьи N, приписанный другой существующей статье M

Grounding офлайн (кэш-корпус); зовётся только LLM-аудитор (кэшируется). Поэтому
baseline → fix → re-run: дельта = чистый эффект детерминированной правки (LLM из кэша).

Usage:
  PYTHONPATH=. python scripts/eval/auditor_eval.py --laws 235-V,226-V --true-k 4
  PYTHONPATH=. python scripts/eval/auditor_eval.py --json out.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from collections import defaultdict
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
    reg = get_registry()
    conn = sqlite3.connect(db_path)
    out: list[EvidenceChunk] = []
    for (cj,) in conn.execute("SELECT chunk_json FROM evidence_cache"):
        try:
            o = json.loads(cj)
        except Exception:
            continue
        lid = o.get("law_id")
        if lid and (lid == canonical_id or reg.resolve(str(lid)) == canonical_id):
            try:
                out.append(EvidenceChunk.model_validate(o))
            except Exception:
                continue
    conn.close()
    return out


def _leading_article(content: str) -> str | None:
    """Номер статьи из ЗАГОЛОВКА чанка (RU 'Статья N.' / KK 'N-бап'), если он есть."""
    head = content.lstrip()[:60]
    m = re.match(r"Стать[яи]\s+(\d+(?:-\d+)?)", head) or re.match(r"(\d+(?:-\d+)?)\s*-?\s*бап", head)
    return m.group(1) if m else None


def _real_articles(chunks: list[EvidenceChunk], k: int) -> list[EvidenceChunk]:
    # ТОЛЬКО корректно размеченные чанки: заголовок в тексте == chunk.article. Иначе
    # «true»-кейс тестирует кривую разметку корпуса, а не аудитор (см. УК 187/182).
    by_art: dict[str, EvidenceChunk] = {}
    for c in chunks:
        if not (c.article and c.content and len(c.content) > 250):
            continue
        if _leading_article(c.content) != str(c.article):
            continue
        a = str(c.article)
        if a not in by_art or len(c.content) > len(by_art[a].content):
            by_art[a] = c
    return sorted(by_art.values(), key=lambda c: len(c.content), reverse=True)[:k]


@dataclass
class Case:
    cid: str
    kind: str          # "true" | "planted"
    pattern: str
    expect_confirm: bool
    claim: DocumentClaim


def _mk(cid: str, text: str, quote: str, law: str) -> DocumentClaim:
    return DocumentClaim(
        claim_id=cid, claim_text=text, quote=quote,
        claim_type=ClaimType.LEGAL_REF, severity=ClaimSeverity.HIGH, target_law_ids=[law],
    )


def _build(law: str, arts: list[EvidenceChunk]) -> list[Case]:
    cases: list[Case] = []
    for c in arts:  # TRUE — recall
        q = c.content[:200].strip()
        cid = f"true_{law}_{c.article}"
        cases.append(Case(cid, "true", "verbatim", True, _mk(cid, q, q, law)))

    base = arts[0]
    bq = base.content[:160].strip()
    other = next((str(a.article) for a in arts if str(a.article) != str(base.article)), "1")
    planted = [
        ("new_article",     f"Кодекс {law} дополняется новой статьёй 889-1 о цифровом реестре.", "«статья 889-1»"),
        ("absurd_article",  f"Статья 99999 закона {law} вводит обязательную биометрию для всех граждан.", "«статья 99999»"),
        ("fabricated",      f"Статья {base.article} закона {law} вводит смертную казнь за все правонарушения.", "«смертная казнь»"),
        ("padded_false",    f"{bq} За это нарушение установлен штраф 999999 МРП и пожизненный срок.", bq),
        ("adjacent_article", f"Статья {other}. {bq}", bq),
    ]
    for pat, text, quote in planted:
        cid = f"plant_{law}_{pat}"
        cases.append(Case(cid, "planted", pat, False, _mk(cid, text, quote, law)))
    return cases


async def _verdict(chunks: list[EvidenceChunk], claim: DocumentClaim) -> str:
    single = ClaimExtractionResult(doc_id="auditor_eval", claims=[claim])
    plan = QueryPlan(plan_id="auditor_eval", source_doc_id="auditor_eval")
    analysis = await run_auditor(chunks=chunks, plan=plan, claims=single, doc_text="")
    analysis = audit_analysis(analysis, chunks, single)
    v = next((v for v in analysis.verdicts if v.claim_id == claim.claim_id), None)
    return v.status.value if v else "MISSING"


async def main_async(args: argparse.Namespace) -> int:
    s = get_settings()
    reg = get_registry()
    results: list[dict] = []
    for law in [x.strip() for x in args.laws.split(",") if x.strip()]:
        canon = reg.resolve(law)
        chunks = _load_law_chunks(s.cache_db_path, canon)
        arts = _real_articles(chunks, args.true_k)
        if not arts:
            print(f"  {law}: no usable articles, skip")
            continue
        # Corpus data-quality probe: доля чанков, где заголовок совпадает с chunk.article.
        subst = [c for c in chunks if c.article and c.content and len(c.content) > 250]
        hdr = [c for c in subst if _leading_article(c.content) is not None]
        good = [c for c in hdr if _leading_article(c.content) == str(c.article)]
        ql = f"{len(good)}/{len(hdr)} header==article ({100*len(good)//len(hdr) if hdr else 0}% well-labeled)"
        print(f"\n--- {law} ({canon}): {len(chunks)} chunks | {len(subst)} substantial, {ql} | {len(arts)} valid true-articles ---")
        for case in _build(law, arts):
            status = await _verdict(chunks, case.claim)
            confirmed = status == "CONFIRMED"
            ok = confirmed == case.expect_confirm
            results.append({
                "law": law, "cid": case.cid, "kind": case.kind, "pattern": case.pattern,
                "expect_confirm": case.expect_confirm, "status": status, "confirmed": confirmed, "ok": ok,
            })
            print(f"{'✅' if ok else '❌'} {law:<8} {case.kind:<7} {case.pattern:<16} → {status}")

    true_cases = [r for r in results if r["kind"] == "true"]
    planted = [r for r in results if r["kind"] == "planted"]
    tc, tt = sum(r["confirmed"] for r in true_cases), len(true_cases)
    fc, pt = sum(r["confirmed"] for r in planted), len(planted)
    agg = {
        "true_confirm_rate": round(tc / tt, 3) if tt else None,
        "false_confirm_rate": round(fc / pt, 3) if pt else None,
        "true_n": tt, "true_confirmed": tc, "planted_n": pt, "planted_confirmed": fc,
    }
    pat_stats: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in planted:
        pat_stats[r["pattern"]][1] += 1
        pat_stats[r["pattern"]][0] += 1 if r["confirmed"] else 0

    print("\n" + "=" * 64)
    print(f"true_confirm_rate  = {agg['true_confirm_rate']}  ({tc}/{tt} real confirmed — RECALL, higher=better)")
    print(f"false_confirm_rate = {agg['false_confirm_rate']}  ({fc}/{pt} planted confirmed — THE metric, target 0)")
    for p, (c, n) in sorted(pat_stats.items()):
        flag = "  ⚠️ FALSE-CONFIRM" if c else ""
        print(f"   planted[{p:<16}] confirmed {c}/{n}{flag}")
    print("=" * 64)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"aggregate": agg, "results": results}, f, ensure_ascii=False, indent=2)
        print(f"JSON → {args.json}")
    return 1 if fc else 0  # fail if any planted confirmed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--laws", default="235-V,226-V")
    ap.add_argument("--true-k", type=int, default=4, help="real articles per law (recall sample)")
    ap.add_argument("--json", metavar="PATH")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
