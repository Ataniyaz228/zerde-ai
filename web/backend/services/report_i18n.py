"""On-demand локализация отчёта (RU → KZ).

RU — канонический артефакт (`<name>.md`), его не трогаем. KZ собирается лениво по
запросу из RU-версии и кэшируется в сайдкар `<name>.kz.md`. Перевод безопасный
(см. zerde/stages/s7_translate): при провале guard'а или ошибке — возвращается RU.
"""

from __future__ import annotations

import logging
import os

from services import storage

logger = logging.getLogger(__name__)

_SUPPORTED = {"ru", "kz"}


async def get_report_localized(report_id: str, lang: str = "ru") -> dict | None:
    """Возвращает dict отчёта (как storage.get_report) на нужном языке.

    lang="ru" → канонический RU (без LLM). lang="kz" → кэш `.kz.md` или ленивый
    безопасный перевод. Метаданные (счёт/вердикты) всегда из RU-meta — числа не
    меняются переводом.
    """
    if lang not in _SUPPORTED:
        lang = "ru"

    # storage.get_report санитизирует report_id и читает RU .md + meta.
    base = storage.get_report(report_id)
    if base is None or lang == "ru":
        return base

    # lang == "kz"
    kz_path = os.path.join(storage.OUTPUT_DIR, report_id + ".kz.md")
    if os.path.exists(kz_path):
        try:
            with open(kz_path, encoding="utf-8") as f:
                base["content"] = f.read()
            return base
        except OSError as e:
            logger.warning("[report_i18n] не прочитать кэш %s: %s — переводим заново", kz_path, e)

    ru_content = base["content"]
    # Lazy import: тянуть zerde в backend только когда реально нужен перевод.
    from zerde.stages.s7_translate import translate_report_md

    kz_content = await translate_report_md(ru_content, "kz")

    # Кэшируем только реальный перевод; фолбэк (== RU) не записываем, чтобы при
    # следующем запросе можно было попробовать снова.
    if kz_content != ru_content:
        try:
            with open(kz_path, "w", encoding="utf-8") as f:
                f.write(kz_content)
        except OSError as e:
            logger.warning("[report_i18n] не записать кэш %s: %s", kz_path, e)

    base["content"] = kz_content
    return base
