"""
BM25 Calibration Script
Запуск: python scripts/calibrate_bm25.py --test-data tests/calibration_data.json

Позволяет найти оптимальный VALIDATION_THRESHOLD через grid search.
Результаты записываются в .env (с подтверждением).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Добавляем корень проекта в PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent))

from zerde.config import get_settings


def run_calibration(test_data_path: str, threshold_range: tuple[float, float, float] = (0.3, 0.95, 0.05)) -> dict:
    """
    Запускает grid search по BM25 порогу.

    Args:
        test_data_path: Путь к JSON файлу с тестовыми данными.
            Формат: [{"claim": "...", "source_content": "...", "expected_status": "HIGH|MEDIUM|LOW"}]
        threshold_range: (start, end, step) для grid search.

    Returns:
        dict с optimal_threshold и metrics.
    """
    settings = get_settings()

    test_file = Path(test_data_path)
    if not test_file.exists():
        print(f"❌ Тестовый файл не найден: {test_data_path}")
        print("Создайте файл с данными для калибровки.")
        return {}

    with open(test_file) as f:
        test_cases = json.load(f)

    print(f"📊 Загружено {len(test_cases)} тестовых примеров")
    print(f"🔍 Текущий порог: VALIDATION_THRESHOLD={settings.validation_threshold}")
    print()

    # TODO (Шаг 2): Реализовать BM25 scoring и метрику precision/recall
    # Grid search по порогам
    start, end, step = threshold_range
    results = []

    threshold = start
    while threshold <= end:
        # Stub: f1_score будет вычислен в Шаге 2
        results.append({
            "threshold": round(threshold, 2),
            "precision": 0.0,  # TODO
            "recall": 0.0,     # TODO
            "f1_score": 0.0,   # TODO
        })
        threshold += step

    if not results:
        return {}

    # Находим оптимальный порог (максимальный F1)
    best = max(results, key=lambda r: r["f1_score"])

    print("📈 Grid Search Results:")
    print(f"{'Threshold':>12} | {'Precision':>10} | {'Recall':>8} | {'F1':>8}")
    print("-" * 50)
    for r in results:
        marker = " ← BEST" if r["threshold"] == best["threshold"] else ""
        print(
            f"{r['threshold']:>12.2f} | {r['precision']:>10.3f} | "
            f"{r['recall']:>8.3f} | {r['f1_score']:>8.3f}{marker}"
        )

    print(f"\n✅ Оптимальный порог: {best['threshold']}")
    print(f"   F1={best['f1_score']:.3f}, P={best['precision']:.3f}, R={best['recall']:.3f}")

    # Предложение обновить .env
    env_file = Path(".env")
    if env_file.exists():
        answer = input(f"\n💾 Обновить VALIDATION_THRESHOLD={best['threshold']} в .env? [y/N] ")
        if answer.lower() == "y":
            _update_env_threshold(env_file, best["threshold"])
            print(f"✅ .env обновлён: VALIDATION_THRESHOLD={best['threshold']}")

    return {
        "optimal_threshold": best["threshold"],
        "metrics": best,
        "all_results": results,
    }


def _update_env_threshold(env_file: Path, threshold: float) -> None:
    """Обновляет VALIDATION_THRESHOLD в .env файле."""
    content = env_file.read_text()
    lines = content.splitlines()
    updated = False

    for i, line in enumerate(lines):
        if line.startswith("VALIDATION_THRESHOLD="):
            lines[i] = f"VALIDATION_THRESHOLD={threshold}"
            updated = True
            break

    if not updated:
        lines.append(f"VALIDATION_THRESHOLD={threshold}")

    env_file.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="ЗЕРДЕ v6.2 — BM25 Calibration Script"
    )
    parser.add_argument(
        "--test-data",
        default="tests/calibration_data.json",
        help="Путь к JSON файлу с тестовыми данными",
    )
    parser.add_argument(
        "--start",
        type=float,
        default=0.3,
        help="Начальный порог (default: 0.3)",
    )
    parser.add_argument(
        "--end",
        type=float,
        default=0.95,
        help="Конечный порог (default: 0.95)",
    )
    parser.add_argument(
        "--step",
        type=float,
        default=0.05,
        help="Шаг grid search (default: 0.05)",
    )

    args = parser.parse_args()
    run_calibration(args.test_data, (args.start, args.end, args.step))
