"""
Smoke-тест для всего corpus без LLM-вызовов (бесплатно).

Прогоняет S1 ingest + детерминированные части S2.5 (regex-extract, _detect_target_laws)
по всем .docx/.pdf/.txt в data/corpus/ (или произвольной директории) и пишет CSV-сводку.

Цель — ловить регрессии в:
  - ingest (S1)
  - target_law detection (правильно ли определяется закон-мишень)
  - regex claim extraction (не пропустили ли упоминания статей)

Запуск:
    python scripts/smoke_test.py
    python scripts/smoke_test.py --corpus data/corpus --out smoke_results.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
import time
import traceback
from pathlib import Path

# Делаем `zerde.*` импортируемым при прямом запуске `python scripts/smoke_test.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zerde.stages.s1_ingest import ingest_document
from zerde.stages.s2_5_claim_extractor import _detect_target_laws, _regex_extract

SUPPORTED_EXTS = {".docx", ".pdf", ".txt"}


def find_documents(corpus_dir: Path) -> list[Path]:
    files = []
    for p in corpus_dir.rglob("*"):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
            files.append(p)
    return sorted(files)


async def smoke_one(path: Path) -> dict:
    t0 = time.perf_counter()
    row = {
        "file": str(path),
        "ok": False,
        "char_count": 0,
        "target_law_ids": "",
        "regex_claims": 0,
        "articles": "",
        "elapsed_s": 0.0,
        "error": "",
    }
    try:
        ds = await ingest_document(str(path))
        text = ds.normalized_text
        row["char_count"] = len(text)
        row["target_law_ids"] = ";".join(_detect_target_laws(text))
        claims = _regex_extract(text)
        row["regex_claims"] = len(claims)
        # уникальные номера статей из entities
        arts = set()
        for c in claims:
            for ent in getattr(c, "entities", []):
                s = str(ent)
                if s.isdigit() or any(ch.isdigit() for ch in s):
                    arts.add(s)
        row["articles"] = ";".join(sorted(arts))
        row["ok"] = True
    except Exception as e:
        row["error"] = f"{type(e).__name__}: {e}"
        traceback.print_exc(file=sys.stderr)
    row["elapsed_s"] = round(time.perf_counter() - t0, 2)
    return row


async def main_async(corpus_dir: Path, out_csv: Path) -> int:
    files = find_documents(corpus_dir)
    if not files:
        print(f"No documents found in {corpus_dir}", file=sys.stderr)
        return 1
    print(f"Smoke test over {len(files)} documents")

    rows = []
    for i, f in enumerate(files, 1):
        row = await smoke_one(f)
        status = "OK" if row["ok"] else "ERR"
        marker = " [target=∅]" if row["ok"] and not row["target_law_ids"] else ""
        print(
            f"[{i}/{len(files)}] {status} {f.name}  "
            f"chars={row['char_count']} target={row['target_law_ids'] or '-'} "
            f"claims={row['regex_claims']} ({row['elapsed_s']}s){marker}"
        )
        if not row["ok"]:
            print(f"    ERROR: {row['error']}", file=sys.stderr)
        rows.append(row)

    # Сводка
    n_ok = sum(1 for r in rows if r["ok"])
    n_with_target = sum(1 for r in rows if r["ok"] and r["target_law_ids"])
    n_with_claims = sum(1 for r in rows if r["ok"] and r["regex_claims"] > 0)
    print()
    print("=== Summary ===")
    print(f"  Ingested OK         : {n_ok}/{len(rows)}")
    print(f"  Detected target_law : {n_with_target}/{n_ok}")
    print(f"  Regex found claims  : {n_with_claims}/{n_ok}")

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["file", "ok", "char_count", "target_law_ids", "regex_claims", "articles", "elapsed_s", "error"],
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"Results: {out_csv}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", type=Path, default=Path("data/corpus"))
    ap.add_argument("--out", type=Path, default=Path("data/smoke_results.csv"))
    ap.add_argument(
        "--ocr",
        action="store_true",
        help="Включить Tesseract OCR для сканированных PDF (медленно, ~6с/страница)",
    )
    args = ap.parse_args()
    # По умолчанию OCR отключён в smoke, чтобы прогон 1000+ файлов оставался быстрым
    if not args.ocr:
        os.environ["ZERDE_PDF_OCR_ENABLED"] = "0"
    return asyncio.run(main_async(args.corpus, args.out))


if __name__ == "__main__":
    sys.exit(main())
