"""
Entry Point
"""

from __future__ import annotations

import asyncio
import logging
import sys

from zerde.config import get_settings
from zerde.pipeline import run_pipeline


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)-30s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("zerde.log", encoding="utf-8"),
        ],
    )


async def main(file_path: str) -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    logger = logging.getLogger("zerde.main")
    logger.info(f"Input: {file_path}")

    try:
        result = await run_pipeline(file_path)
        print(f"\n✅ Отчёт сохранён: {result['report_path']}")
        print(f"⏱️  Время выполнения: {result['elapsed_seconds']:.2f}с")
        print(f"📊 Фактов: {len(result['analysis'].facts)}")
        print(f"🔒 Надёжность: {result['analysis'].overall_reliability or 'N/A'}")
    except FileNotFoundError as e:
        print(f"❌ Файл не найден: {e}")
        sys.exit(1)
    except Exception as e:
        logging.getLogger("zerde.main").exception(f"Pipeline error: {e}")
        sys.exit(1)


def cli() -> None:
    """Синхронная точка входа для console-script `zerde` (main() — корутина,
    поэтому [project.scripts] не может ссылаться на неё напрямую: L4)."""
    if len(sys.argv) < 2:
        print("Использование: python main.py <путь_к_документу>")
        print("Пример:        python main.py docs/contract.pdf")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))


if __name__ == "__main__":
    cli()
