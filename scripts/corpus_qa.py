#!/usr/bin/env python3
"""Phase (a) corpus QA — $0 OFFLINE mislabel signal (no adilet, no LLM, read-only).

Flags chunks/articles whose `article` tag is likely WRONG, using ONLY the cached
corpus (evidence_cache + cached BGE vectors). No network, no model load, no writes.

  S1 header-disagreement : content начинается с 'Статья M.'/'M-бап', M != chunk.article
                           → высокоуверенный mislabel (но low recall: большинство
                           чанков — фрагменты без заголовка).
  S2 incoherent-article  : у одной статьи ≥2 чанка, а min попарный cosine их эмбеддингов
                           ниже порога → чанки семантически НЕ связаны → хотя бы один
                           подвешен не к той статье. Ловит «спилловер ст.3» (УК 187/182:
                           фрагмент определений ст.3, помеченный как 187). Кросс-язык
                           RU/KK одной статьи остаётся высоким (BGE-M3), порог его не трогает.

Это НИЖНЯЯ ОЦЕНКА mislabel-rate: чистый фрагмент без заголовка и без несогласного
соседа не ловится → точный масштаб подтверждать кросс-чеком с adilet (фаза b).

Usage:
  PYTHONPATH=. python scripts/corpus_qa.py
  PYTHONPATH=. python scripts/corpus_qa.py --laws 226-V,235-V --min-cohesion 0.45 --examples 5 --json qa.json
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict

import numpy as np

from zerde.config import get_settings
from zerde.utils.law_registry import get_registry

_HDR = re.compile(r"^\s*(?:Стать[яи]\s+(\d+(?:-\d+)?)|(\d+(?:-\d+)?)\s*-?\s*бап)\b")


def _leading_article(content: str) -> str | None:
    m = _HDR.match(content or "")
    return (m.group(1) or m.group(2)) if m else None


def _load(db: str):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    emb: dict[str, bytes] = {
        r["chunk_id"]: r["embedding"]
        for r in conn.execute("SELECT chunk_id, embedding FROM evidence_embeddings")
    }
    rows = []
    for r in conn.execute("SELECT chunk_id, chunk_json FROM evidence_cache"):
        try:
            rows.append((r["chunk_id"], json.loads(r["chunk_json"])))
        except Exception:
            continue
    conn.close()
    return rows, emb


def _vec(blob: bytes) -> np.ndarray:
    v = np.frombuffer(blob, dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--laws", default="235-V,226-V,1000-XIII,214-VII,414-I-NEW,442-II,350-VI,171-VIII")
    ap.add_argument("--min-cohesion", type=float, default=0.45, help="S2: ниже min-попарного cosine статья считается incoherent")
    ap.add_argument("--examples", type=int, default=4)
    ap.add_argument("--json", metavar="PATH")
    args = ap.parse_args()

    s = get_settings()
    reg = get_registry()
    rows, emb = _load(s.cache_db_path)
    by_law: dict[str, list] = defaultdict(list)
    for cid, o in rows:
        lid = o.get("law_id")
        if lid:
            by_law[reg.resolve(str(lid))].append((cid, o))

    report = []
    print(f"{'law':<14}{'chunks':>7}{'subst':>7}{'multiArt':>9}{'S1hdr':>7}{'S2incoh':>9}{'suspect%':>9}")
    for raw in [x.strip() for x in args.laws.split(",") if x.strip()]:
        canon = reg.resolve(raw)
        chunks = by_law.get(canon, [])
        subst = [(c, o) for c, o in chunks if o.get("article") and len(o.get("content") or "") > 250]
        if not subst:
            print(f"{canon:<14}{0:>7}  (no substantial chunks)")
            continue

        # S1: header disagreement
        s1 = [(c, o, la) for c, o in subst if (la := _leading_article(o["content"])) and la != str(o["article"])]

        # S2: incoherent articles (min intra-article pairwise cosine < threshold)
        by_art: dict[str, list] = defaultdict(list)
        for c, o in subst:
            if c in emb:
                by_art[str(o["article"])].append((c, o))
        multi = {a: v for a, v in by_art.items() if len(v) >= 2}
        s2 = []
        for art, members in multi.items():
            mat = np.stack([_vec(emb[c]) for c, _ in members])
            sims = mat @ mat.T
            np.fill_diagonal(sims, 1.0)
            mn = float(sims.min())
            if mn < args.min_cohesion:
                # the two least-similar members
                i, j = np.unravel_index(np.argmin(sims), sims.shape)
                s2.append((art, mn, members[i], members[j]))

        suspect = {c for c, _, _ in s1} | {m[0] for _, _, *pair in s2 for m in pair}
        rate = 100 * len(suspect) / len(subst)
        print(f"{canon:<14}{len(chunks):>7}{len(subst):>7}{len(multi):>9}{len(s1):>7}{len(s2):>9}{rate:>8.1f}%")
        report.append({
            "law": canon, "chunks": len(chunks), "substantial": len(subst),
            "multi_chunk_articles": len(multi), "s1_header_disagree": len(s1),
            "s2_incoherent_articles": len(s2), "suspect_chunks": len(suspect), "suspect_rate_pct": round(rate, 1),
        })
        for c, o, la in s1[:args.examples]:
            print(f"    S1 {c[:10]} tag={o['article']} header_says={la} :: {o['content'].strip()[:64]!r}")
        for art, mn, a, b in s2[:args.examples]:
            print(f"    S2 art={art} mincos={mn:.2f}")
            print(f"        {a[0][:10]} :: {a[1]['content'].strip()[:60]!r}")
            print(f"        {b[0][:10]} :: {b[1]['content'].strip()[:60]!r}")

    if args.json:
        json.dump(report, open(args.json, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"\nJSON → {args.json}")
    return 1 if any(r["suspect_chunks"] for r in report) else 0


if __name__ == "__main__":
    sys.exit(main())
