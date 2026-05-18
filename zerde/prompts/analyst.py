"""
ЗЕРДЕ v6.2 — LLM Analyst Prompt Builder (Этап 5)
"""

from __future__ import annotations

from zerde.models import EvidenceChunk, QueryPlan

_ANALYST_SCHEMA = """
Верни JSON строго следующей структуры (все поля обязательны):
{
  "facts": [
    {
      "claim": "Точное фактическое утверждение",
      "source_ids": ["СКОПИРУЙ_ТОЧНЫЙ_ID_ИЗ_SOURCE_ID"],
      "confidence": 0.95
    }
  ],
  "conclusions": [
    {
      "statement": "Логический вывод",
      "reasoning_type": "deduction|analogy|induction",
      "supporting_fact_ids": ["fact_0001"],
      "source_ids": ["СКОПИРУЙ_ТОЧНЫЙ_ID_ИЗ_SOURCE_ID"]
    }
  ],
  "negative_space": [
    {
      "description": "Описание пробела в регулировании",
      "gap_type": "regulatory_hole|intentional_silence|delegation_gap",
      "affected_domain": "Сфера применения",
      "source_ids": []
    }
  ],
  "normative": [
    {
      "norm_description": "Описание нормы",
      "economic_impact": "...",
      "social_impact": "...",
      "risk_level": "LOW|MEDIUM|HIGH|CRITICAL",
      "source_ids": ["СКОПИРУЙ_ТОЧНЫЙ_ID_ИЗ_SOURCE_ID"]
    }
  ],
  "pros": ["Преимущество 1", "Преимущество 2"],
  "cons": ["Недостаток 1", "Недостаток 2"],
  "affected_parties": [
    {
      "name": "Название стороны",
      "role": "beneficiary|obligated|regulator|third_party",
      "description": "Описание роли"
    }
  ],
  "recommendation": "Итоговая юридическая рекомендация",
  "conflict_chunk_ids_referenced": ["СКОПИРУЙ_ТОЧНЫЙ_ID конфликтных источников"]
}
"""

_ANALYST_USER_TEMPLATE = """
Ты — старший юридический аналитик Республики Казахстан.
Проведи глубокий правовой анализ на основе предоставленного корпуса доказательств.

## Контекст задачи:
Ожидаемые элементы: {expected_elements}
Потенциальные подзаконные акты: {bylaw_triggers}

## ⚠️ КРИТИЧЕСКИЕ ПРАВИЛА:
1. Каждый факт (fact) ОБЯЗАН содержать source_ids из предоставленного корпуса.
2. В поле source_ids КОПИРУЙ ДОСЛОВНО строку после "### SOURCE_ID:" — не сокращай и не изменяй.
   ПРАВИЛЬНО: "source_ids": ["0f41ba7f55cb31d1"]
   НЕПРАВИЛЬНО: "source_ids": ["0f41ba7f"] или ["chunk_1"]
3. Если укажешь неверный source_id — факт будет помечен UNVERIFIED и не попадёт в отчёт.
4. Следующие источники помечены как КОНФЛИКТНЫЕ и ОБЯЗАТЕЛЬНО должны быть упомянуты:
   {conflict_ids}
5. При анализе пробелов (negative_space) ищи: regulatory_hole (нет нормы), intentional_silence (намеренное умолчание), delegation_gap (отсылка к несуществующему акту)
6. Используй ТОЛЬКО информацию из предоставленного корпуса. Не выдумывай нормы.

## Корпус доказательств ({chunk_count} источников):

{corpus_text}

{schema}
"""


def build_analyst_prompt(
    chunks: list[EvidenceChunk],
    plan: QueryPlan,
    conflict_ids: list[str],
    max_corpus_chars: int = 60_000,
) -> str:
    """
    Строит промпт для LLM Analyst.

    Args:
        chunks: Активные (не дублированные) EvidenceChunk.
        plan: QueryPlan для контекста ожидаемых элементов.
        conflict_ids: chunk_id конфликтных чанков для принудительного упоминания.
        max_corpus_chars: Лимит символов корпуса в промпте.

    Returns:
        Готовый промпт-строка.
    """
    corpus_parts = []
    total_chars = 0

    # Сортируем: конфликтные чанки первыми, затем по рангу
    sorted_chunks = sorted(
        chunks,
        key=lambda c: (not c.is_conflict, int(c.legal_rank)),
    )

    for chunk in sorted_chunks:
        chunk_text = _format_chunk_for_prompt(chunk)
        if total_chars + len(chunk_text) > max_corpus_chars:
            corpus_parts.append(f"\n[... корпус обрезан — лимит {max_corpus_chars} символов ...]")
            break
        corpus_parts.append(chunk_text)
        total_chars += len(chunk_text)

    corpus_str = "\n\n".join(corpus_parts)

    conflict_str = (
        "\n".join(f"  - {cid[:16]}…" for cid in conflict_ids)
        if conflict_ids
        else "  (конфликтов не выявлено)"
    )

    return _ANALYST_USER_TEMPLATE.format(
        expected_elements=", ".join(plan.expected_elements) or "не указаны",
        bylaw_triggers=", ".join(plan.bylaw_triggers) or "не выявлены",
        conflict_ids=conflict_str,
        chunk_count=len(chunks),
        corpus_text=corpus_str,
        schema=_ANALYST_SCHEMA,
    )


def _format_chunk_for_prompt(chunk: EvidenceChunk) -> str:
    """Форматирует EvidenceChunk в читаемый блок для промпта."""
    conflict_marker = "⚠️ [КОНФЛИКТ]" if chunk.is_conflict else ""
    law_ref = ""
    if chunk.law_id:
        law_ref = f"[{chunk.law_title or chunk.law_id} | Ст. {chunk.article}]"
    if chunk.effective_date:
        law_ref += f" от {chunk.effective_date}"

    return (
        f"### SOURCE_ID: {chunk.chunk_id}\n"
        f"**{chunk.source_title}** {law_ref} {conflict_marker}\n"
        f"URL: {chunk.source_url}\n"
        f"Ранг: {int(chunk.legal_rank)} | Тир: {chunk.web_tier or 'Adilet'}\n"
        f"---\n"
        f"{chunk.content[:3000]}"  # Ограничение на один чанк
        + (" [ОБРЕЗАНО]" if len(chunk.content) > 3000 else "")
    )
