"""
ЗЕРДЕ v6.2 — BM25 Calibration Script (ПОЛНАЯ РЕАЛИЗАЦИЯ)
Запуск: python scripts/calibrate_bm25.py --test-data tests/calibration_data.json

Формат calibration_data.json:
[
  {
    "claim": "Минимальная заработная плата в 2024 году составляет 85000 тенге",
    "source_content": "...текст статьи с упоминанием 85000 тенге МЗП...",
    "expected_status": "HIGH"
  },
  ...
]

Выводит: precision, recall, F1 для каждого порога.
Обновляет .env при подтверждении.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Добавляем корень проекта
sys.path.insert(0, str(Path(__file__).parent.parent))


def run_calibration(
    test_data_path: str,
    threshold_range: tuple[float, float, float] = (0.30, 0.95, 0.05),
) -> dict:
    """Grid search по BM25 порогу."""
    from zerde.config import get_settings
    from zerde.stages.s6_auditor import ZerdeBM25, _tokenize
    from zerde.models import EvidenceChunk, LegalRank, ValidationStatus
    import hashlib

    settings = get_settings()

    # Загружаем тестовые данные
    test_file = Path(test_data_path)
    if not test_file.exists():
        _create_sample_calibration_data(test_file)
        print(f"📝 Создан пример файла калибровки: {test_file}")
        print("   Заполните его реальными данными и запустите снова.\n")
        return {}

    with open(test_file, encoding="utf-8") as f:
        test_cases = json.load(f)

    if not test_cases:
        print("❌ Тестовый файл пуст")
        return {}

    print(f"📊 Загружено {len(test_cases)} тестовых примеров")
    print(f"🔍 Текущий порог: VALIDATION_THRESHOLD={settings.validation_threshold}")
    print(f"   BM25_MEDIUM_THRESHOLD={settings.bm25_medium_threshold}")
    print()

    # Строим фиктивный корпус из source_content
    fake_corpus: dict[str, "EvidenceChunk"] = {}
    for i, case in enumerate(test_cases):
        content = case.get("source_content", "")
        if not content:
            continue
        chunk_id = hashlib.sha256(content.encode()).hexdigest()
        chunk = EvidenceChunk(
            chunk_id=chunk_id,
            source_url=f"https://test.example.com/doc/{i}",
            source_title=f"Test Source {i}",
            content=content,
            legal_rank=LegalRank.LAW_RK,
        )
        fake_corpus[chunk_id] = chunk
        test_cases[i]["_chunk_id"] = chunk_id

    # Строим BM25 индекс
    bm25 = ZerdeBM25(fake_corpus)

    # Grid search
    start, end, step = threshold_range
    grid_results = []

    threshold = start
    while threshold <= end + 0.001:
        threshold = round(threshold, 2)

        # Считаем метрики для этого порога
        tp = fp = fn = tn = 0

        for case in test_cases:
            claim = case.get("claim", "")
            expected = case.get("expected_status", "HIGH")
            chunk_id = case.get("_chunk_id")

            if not claim or not chunk_id:
                continue

            score = bm25.score(claim, [chunk_id])
            predicted_high = score >= threshold

            is_positive = expected in ("HIGH", "MEDIUM")

            if predicted_high and is_positive:
                tp += 1
            elif predicted_high and not is_positive:
                fp += 1
            elif not predicted_high and is_positive:
                fn += 1
            else:
                tn += 1

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

        grid_results.append({
            "threshold": threshold,
            "precision": precision,
            "recall": recall,
            "f1_score": f1,
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        })

        threshold += step

    if not grid_results:
        print("❌ Нет результатов grid search")
        return {}

    best = max(grid_results, key=lambda r: r["f1_score"])

    # Вывод таблицы
    print(f"{'Threshold':>12} | {'Precision':>10} | {'Recall':>8} | {'F1':>8} | TP/FP/FN/TN")
    print("-" * 72)
    for r in grid_results:
        marker = " ← BEST" if r["threshold"] == best["threshold"] else ""
        print(
            f"{r['threshold']:>12.2f} | {r['precision']:>10.3f} | "
            f"{r['recall']:>8.3f} | {r['f1_score']:>8.3f} | "
            f"{r['tp']}/{r['fp']}/{r['fn']}/{r['tn']}{marker}"
        )

    print(f"\n✅ Оптимальный порог: {best['threshold']}")
    print(f"   F1={best['f1_score']:.3f}  Precision={best['precision']:.3f}  Recall={best['recall']:.3f}")
    print(f"   TP={best['tp']}  FP={best['fp']}  FN={best['fn']}  TN={best['tn']}")

    # Сравнение с текущим
    current_result = next(
        (r for r in grid_results if abs(r["threshold"] - settings.validation_threshold) < 0.01),
        None,
    )
    if current_result:
        improvement = best["f1_score"] - current_result["f1_score"]
        print(f"\n📈 Текущий порог ({settings.validation_threshold}): F1={current_result['f1_score']:.3f}")
        print(f"   Улучшение F1: {improvement:+.3f}")

    # Обновление .env
    env_file = Path(".env")
    if env_file.exists() and best["threshold"] != settings.validation_threshold:
        try:
            answer = input(f"\n💾 Обновить VALIDATION_THRESHOLD={best['threshold']} в .env? [y/N] ")
        except EOFError:
            answer = "n"

        if answer.lower() == "y":
            _update_env_value(env_file, "VALIDATION_THRESHOLD", str(best["threshold"]))
            print(f"✅ .env обновлён: VALIDATION_THRESHOLD={best['threshold']}")
    else:
        print(f"\nℹ️  .env не найден или порог не изменился — обновление не требуется")

    return {
        "optimal_threshold": best["threshold"],
        "metrics": {k: v for k, v in best.items() if k != "threshold"},
        "all_results": grid_results,
    }


def _update_env_value(env_file: Path, key: str, value: str) -> None:
    """Обновляет одно значение в .env файле."""
    content = env_file.read_text(encoding="utf-8")
    lines = content.splitlines()
    updated = False

    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            updated = True
            break

    if not updated:
        lines.append(f"{key}={value}")

    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _create_sample_calibration_data(path: Path) -> None:
    """Создаёт пример файла калибровки."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sample = [
        {
            "claim": "Минимальная заработная плата в 2024 году составляет 85000 тенге",
            "source_content": (
                "Статья 15. Размер минимальной заработной платы. "
                "Минимальная заработная плата устанавливается ежегодно законом о республиканском бюджете. "
                "В 2024 году МЗП составляет 85000 тенге в месяц."
            ),
            "expected_status": "HIGH",
        },
        {
            "claim": "Срок исковой давности по трудовым спорам составляет 1 год",
            "source_content": (
                "Статья 164. Сроки обращения в суд. "
                "Работник имеет право обратиться в суд за разрешением индивидуального трудового спора "
                "в течение трёх месяцев со дня, когда он узнал о нарушении своего права."
            ),
            "expected_status": "LOW",
        },
        {
            "claim": "Работодатель обязан уведомить работника за 1 месяц до увольнения",
            "source_content": (
                "Статья 53. Расторжение трудового договора по инициативе работодателя. "
                "Работодатель обязан письменно уведомить работника о предстоящем расторжении "
                "трудового договора не позднее чем за один месяц."
            ),
            "expected_status": "HIGH",
        },
    ]

    with open(path, "w", encoding="utf-8") as f:
        json.dump(sample, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ЗЕРДЕ v6.2 — BM25 Calibration")
    parser.add_argument("--test-data", default="tests/calibration_data.json")
    parser.add_argument("--start", type=float, default=0.30)
    parser.add_argument("--end", type=float, default=0.95)
    parser.add_argument("--step", type=float, default=0.05)

    args = parser.parse_args()
    run_calibration(args.test_data, (args.start, args.end, args.step))
