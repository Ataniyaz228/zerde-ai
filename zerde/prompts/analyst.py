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
      "source_ids": ["chunk_id_1", "chunk_id_2"],  // ОБЯЗАТЕЛЬНО минимум 1
      "confidence": 0.95
    }
  ],
  "conclusions": [
    {
      "statement": "Логический вывод",
      "reasoning_type": "deduction|analogy|induction",
      "supporting_fact_ids": ["fact_0001"],
      "source_ids": ["chunk_id_1"]
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
      "source_ids": ["chunk_id_1"]
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
  "conflict_chunk_ids_referenced": ["chunk_id конфликтных источников, упомянутых в анализе"]
}
"""

_ANALYST_USER_TEMPLATE = """
Ты — старший юридический аналитик-верификатор Республики Казахстан.
Твоя ГЛАВНАЯ задача: ВЕРИФИЦИРОВАТЬ каждое утверждение анализируемого документа,
сравнивая его с доказательствами из корпуса.

## АНАЛИЗИРУЕМЫЙ ДОКУМЕНТ (проект закона):
__DOCUMENT_EXCERPT__

## Контекст задачи:
Ожидаемые элементы: __EXPECTED_ELEMENTS__
Потенциальные подзаконные акты: __BYLAW_TRIGGERS__

## ⚠️ КРИТИЧЕСКИЕ ПРАВИЛА ВЕРИФИКАЦИИ:

### 1. CLAIM-BY-CLAIM ПРОВЕРКА (ОБЯЗАТЕЛЬНО)
Для КАЖДОГО числового/фактического утверждения в документе ты ОБЯЗАН:
- Найти подтверждение или опровержение в корпусе
- Если утверждение документа РАСХОДИТСЯ с корпусом — это ОШИБКА, и это ВАЖНЕЙШИЙ факт
- Если подтверждение НЕ НАЙДЕНО — указать в negative_space

Примеры утверждений, которые ОБЯЗАТЕЛЬНО проверить:
- Номера и ID законов (87-IV, 94-V, 235-V) — совпадают ли с реальными?
- Размеры штрафов (в МРП) — подтверждены ли КоАП/УК?
- Размер МРП (тенге) — актуален ли для указанного года?
- Сроки уведомлений (24 часа, 72 часа) — что говорит действующий закон?
- Ссылки на статьи (ст. 207 УК, ст. 79 КоАП) — существуют ли и о чём они?
- Ссылки на постановления (ППРК №142 и т.д.) — существуют ли?
- Даты вступления в силу — подтверждены ли?
- Фактические утверждения (обязательность биометрии, локализация серверов) — закреплены ли в действующем праве?

### 2. ФАКТЫ — ЭТО НЕ ПЕРЕСКАЗ
- Каждый факт (fact) должен быть РЕЗУЛЬТАТОМ ВЕРИФИКАЦИИ, а не пересказом источника
- Формат: "Документ утверждает X, но источник Y показывает Z" — это правильный факт
- Формат: "Закон определяет понятие X" — это НЕ полезный факт (не связан с документом)

### 3. ОШИБКИ В ДОКУМЕНТЕ — НАИВЫСШИЙ ПРИОРИТЕТ
Если обнаружено расхождение между документом и действующим НПА — это:
- ОБЯЗАТЕЛЬНЫЙ факт с confidence < 0.5
- Должен быть отражён в cons и recommendation
- Тип: fact с claim вида "ОШИБКА: документ указывает X, но действующая норма — Y"

### 4. Каждый факт (fact) ОБЯЗАН содержать source_ids из предоставленного корпуса
### 5. Следующие источники помечены как КОНФЛИКТНЫЕ и ОБЯЗАТЕЛЬНО должны быть упомянуты:
   __CONFLICT_IDS__
### 6. При анализе пробелов (negative_space) ищи: regulatory_hole, intentional_silence, delegation_gap
### 7. Используй ТОЛЬКО информацию из предоставленного корпуса. Не выдумывай нормы.
### 8. НЕ ДУБЛИРУЙ выводы. Каждый negative_space, conclusion и fact должен быть уникальным.

## Корпус доказательств (__CHUNK_COUNT__ источников):

__CORPUS_TEXT__

__SCHEMA__
"""


def build_analyst_prompt(
    chunks: list[EvidenceChunk],
    plan: QueryPlan,
    conflict_ids: list[str],
    doc_text: str = "",
    max_corpus_chars: int = 80_000,
    max_doc_chars: int = 4_000,
) -> str:
    """
    Строит промпт для LLM Analyst.

    Args:
        chunks: Активные (не дублированные) EvidenceChunk.
        plan: QueryPlan для контекста ожидаемых элементов.
        conflict_ids: chunk_id конфликтных чанков для принудительного упоминания.
        doc_text: Текст анализируемого документа для claim-by-claim верификации.
        max_corpus_chars: Лимит символов корпуса в промпте.
        max_doc_chars: Лимит символов документа в промпте.

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

    # Подготовка выдержки из документа
    if doc_text:
        doc_excerpt = doc_text[:max_doc_chars]
        if len(doc_text) > max_doc_chars:
            doc_excerpt += f"\n[... документ обрезан — всего {len(doc_text)} символов ...]"
    else:
        doc_excerpt = "(текст документа не предоставлен — анализируй только корпус)"

    # Используем .replace() вместо str.format() — JSON-схемы и юр. тексты
    # содержат {} скобки → KeyError при str.format()
    safe_doc = doc_excerpt      # .replace() не трактует {} как плейсхолдеры
    safe_corpus = corpus_str    # оставляем как есть, без экранирования
    return (
        _ANALYST_USER_TEMPLATE
        .replace("__DOCUMENT_EXCERPT__", safe_doc)
        .replace("__EXPECTED_ELEMENTS__", ", ".join(plan.expected_elements) or "не указаны")
        .replace("__BYLAW_TRIGGERS__", ", ".join(plan.bylaw_triggers) or "не выявлены")
        .replace("__CONFLICT_IDS__", conflict_str)
        .replace("__CHUNK_COUNT__", str(len(chunks)))
        .replace("__CORPUS_TEXT__", safe_corpus)
        .replace("__SCHEMA__", _ANALYST_SCHEMA)
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
