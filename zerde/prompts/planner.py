"""
ЗЕРДЕ v6.2 — LLM Planner Prompt Builder (Этап 2)
"""

from __future__ import annotations

_PLANNER_SYSTEM_SCHEMA = """
Верни JSON строго следующей структуры:
{
  "adilet_queries": [
    {
      "query_text": "...",
      "law_ids": ["550-IV", ...],
      "articles": ["15", "15-1", ...],
      "date_from": "YYYY-MM-DD",  // optional
      "date_to": "YYYY-MM-DD"    // optional
    }
  ],
  "web_queries_ru": [
    {
      "query_text": "...",
      "include_domains": ["gov.kz", ...],  // optional
      "max_results": 10
    }
  ],
  "web_queries_kk": [...],
  "web_queries_en": [...],
  "expected_elements": ["список ожидаемых элементов анализа"],
  "bylaw_triggers": ["список потенциальных подзаконных актов"]
}
"""

_PLANNER_USER_TEMPLATE = """
Ты — юридический аналитик-планировщик для Республики Казахстан.
Проанализируй следующий документ и составь план поиска нормативно-правовой базы.

## Документ:
{document_text}

## Задача:
1. Определи все НПА (нормативно-правовые акты) РК, которые НЕОБХОДИМО проверить.
2. Составь конкретные запросы к базе данных Адилет (adilet.zan.kz).
3. Составь Web-запросы на русском, казахском и английском языках.
4. Укажи ожидаемые элементы анализа (expected_elements).
5. Укажи подзаконные акты, которые могут быть вовлечены (bylaw_triggers).

## Контекст КЗ права:
- Источники по убыванию авторитета: Международные договоры > Кодексы > Конституционные законы > Законы РК > Указы Президента > Постановления Правительства > Приказы министерств
- Для Адилет: ID закона обычно вида "550-IV", "94-V", "418-V", статьи — "15", "15-1"
- Web Gov источники (TIER_1): gov.kz, adilet.zan.kz, supreme.kz, primeminister.kz

## ⚠️ КРИТИЧЕСКИЕ ТРЕБОВАНИЯ к запросам (v6.3):

### Приоритет первичных источников:
1. **adilet_queries ОБЯЗАТЕЛЬНЫ** для КАЖДОГО упомянутого НПА — без них анализ невалиден.
2. Для каждого закона указывай ТОЧНЫЙ law_id (например "94-V", "418-V", "285-IV").
   Если ID неизвестен — пиши query_text с полным названием закона.
3. Если adilet.zan.kz не содержит документ → добавь web_query_ru:
   `site:legalacts.egov.kz "<полное название закона>"` или `site:adilet.zan.kz "<название>"`
4. **web_queries_ru** должны начинаться с государственных источников:
   - `site:adilet.zan.kz <запрос>`
   - `site:legalacts.egov.kz <запрос>`
   - `site:gov.kz <запрос>`
5. Коммерческие аналитические источники (zakon.kz, tengrinews.kz) — ТОЛЬКО если
   первичные источники не найдены. Указывай их в отдельных web_queries.
6. Для законопроектов на стадии рассмотрения: `site:parlam.kz <название>` или
   `site:mazhilis.kz <название>`.

{schema}
"""



def build_planner_prompt(document_text: str, max_chars: int = 12_000) -> str:
    """
    Строит промпт для LLM Planner.

    Args:
        document_text: Нормализованный текст документа.
        max_chars: Максимальное количество символов документа в промпте.

    Returns:
        Готовый промпт-строка.
    """
    # Обрезаем если слишком длинный документ
    if len(document_text) > max_chars:
        truncated = document_text[:max_chars]
        truncated += f"\n\n[... текст обрезан — оригинал содержит {len(document_text)} символов ...]"
    else:
        truncated = document_text

    return _PLANNER_USER_TEMPLATE.format(
        document_text=truncated,
        schema=_PLANNER_SYSTEM_SCHEMA,
    )
