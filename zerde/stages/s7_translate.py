"""S7-translate — безопасный перевод готового отчёта RU→KZ (презентационный слой).

Аудит и вычисление метрики остаются в проверенном RU-пути; здесь только перевод
УЖЕ собранного Markdown-отчёта. Ключевой инвариант продукта (CITE-OR-ABSTAIN):
дословные улики — цитаты, номера/ID статей и законов, проценты, даты, URL,
статус-теги — переводить НЕЛЬЗЯ. Поэтому результат проходит детерминированный
guard: если хоть один защищаемый токен пропал/изменился, перевод отбрасывается и
возвращается оригинал RU (fail-safe). Лучше «отчёт на русском», чем «казахский
отчёт с искажённой нормой».
"""

from __future__ import annotations

import logging
import re

from zerde.config import Settings, get_settings
from zerde.utils.llm_client import cached_llm_call, make_llm_client

logger = logging.getLogger(__name__)

# Защищаемые токены — то, что обязано остаться байт-в-байт в обоих языках.
# Высокий сигнал, низкий шум: эти строки не должны «естественно» переводиться.
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{1,4}-[IVXLCDM]{1,7}\b"),          # law-ID: 253-V, 171-VIII
    re.compile(r"\b[A-Z]\d{6,}[A-Z]?\b"),               # adilet-код: K1400000226, Z1300000094
    re.compile(r"\bст\.?\s*\d+", re.IGNORECASE),        # ст. 14 (просим сохранять дословно)
    re.compile(r"\d+(?:[.,]\d+)?\s*%"),                 # 92%
    re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b"),         # 15.06.2026
    re.compile(r"https?://\S+"),                        # URL
    re.compile(r"adilet\.zan\.kz"),                     # домен-первоисточник
    re.compile(r"\[[А-ЯЁA-Z][А-ЯЁA-Z \-]{2,}\]"),       # [ПОДТВЕРЖДЕНО], [НЕ ПРОВЕРЕНО]
)

_SUPPORTED_LANGS = {"kz"}

_SYSTEM_PROMPT = (
    "Ты — профессиональный юридический переводчик. Переводишь готовый отчёт "
    "правового анализа с русского на КАЗАХСКИЙ язык.\n\n"
    "СТРОГИЕ ПРАВИЛА (нарушение недопустимо):\n"
    "1. Сохраняй ВСЮ Markdown-разметку и эмодзи БЕЗ ИЗМЕНЕНИЙ (заголовки ##, "
    "списки, таблицы, |, >, ---, ` `).\n"
    "2. НЕ переводи и НЕ изменяй: числа, проценты, даты; номера и идентификаторы "
    "статей и законов (например «ст. 14», «253-V», «K1400000226»); URL и "
    "adilet.zan.kz; текст внутри блок-цитат (`>`) — это дословные правовые цитаты; "
    "содержимое inline-кода (` `); бракет-теги статуса «[ПОДТВЕРЖДЕНО]», "
    "«[НЕ ПРОВЕРЕНО]», «[ОШИБКА]», «[ПРЕДУПРЕЖДЕНИЕ]» — оставляй их РОВНО как есть.\n"
    "3. Переводи ТОЛЬКО естественную прозу (заголовки секций, пояснения, выводы, "
    "рекомендации).\n"
    "4. Ничего не добавляй и не убирай — структура отчёта 1:1.\n\n"
    'Верни СТРОГО JSON вида {"md": "<переведённый markdown целиком>"}.'
)


def _protected_tokens(md: str) -> set[str]:
    """Множество защищаемых токенов (улик), которые обязаны выжить при переводе."""
    found: set[str] = set()
    for pat in _TOKEN_PATTERNS:
        for m in pat.finditer(md):
            # Нормализуем пробелы внутри токена (ст.  14 == ст. 14).
            found.add(re.sub(r"\s+", " ", m.group(0).strip()))
    return found


def _evidence_preserved(md_ru: str, md_kz: str) -> bool:
    """True, если КАЖДЫЙ защищаемый токен из RU присутствует в KZ.

    Детерминированно, без сети/LLM. Это страховка от искажения нормы переводом:
    пропал номер статьи / ID закона / процент / дата / URL / статус-тег → False.
    """
    if not md_kz.strip():
        return False
    ru_tokens = _protected_tokens(md_ru)
    if not ru_tokens:
        # Нечего защищать (отчёт без улик) — перевод безопасен по этому критерию.
        return True
    # Сравниваем по нормализованным пробелам, регистр статусных тегов сохраняем.
    kz_norm = re.sub(r"\s+", " ", md_kz)
    missing = [t for t in ru_tokens if t not in kz_norm]
    if missing:
        logger.warning(
            "[S7-translate] guard: отброшен перевод — пропали улики (%d): %s",
            len(missing), ", ".join(sorted(missing)[:8]),
        )
        return False
    return True


async def translate_report_md(
    md_ru: str,
    target_lang: str = "kz",
    settings: Settings | None = None,
) -> str:
    """Переводит готовый RU-отчёт в target_lang. Fail-safe: при любой проблеме
    (неподдерживаемый язык, ошибка LLM, пустой ответ, проваленный guard) —
    возвращает исходный RU-текст без изменений."""
    if target_lang not in _SUPPORTED_LANGS or not md_ru.strip():
        return md_ru

    s = settings or get_settings()
    try:
        client = make_llm_client(s)
        parsed = await cached_llm_call(
            client=client,
            model=s.llm_model_translator,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": md_ru},
            ],
            settings=s,
            max_tokens=s.llm_max_tokens_translator,
            temperature=0.0,
        )
    except Exception:
        logger.exception("[S7-translate] LLM-вызов упал — фолбэк на RU")
        return md_ru

    md_kz = parsed.get("md") if isinstance(parsed, dict) else None
    if not isinstance(md_kz, str) or not md_kz.strip():
        logger.warning("[S7-translate] пустой/неожиданный ответ перевода — фолбэк на RU")
        return md_ru

    if not _evidence_preserved(md_ru, md_kz):
        return md_ru

    return md_kz
