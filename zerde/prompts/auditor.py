"""
LLM Auditor Prompt Builder (Этап 5 v2)
Парадигма: AUDITOR, не summarizer.
Аналитик получает чеклист claims и проверяет каждый против корпуса.
"""

from __future__ import annotations

from zerde.models import ClaimExtractionResult, EvidenceChunk, QueryPlan
from zerde.reference_data import build_reference_corpus_text

_AUDITOR_VERDICT_SCHEMA = """
Верни JSON строго следующей структуры:
{
  "verdicts": [
    {
      "claim_id": "claim_0000",
      "status": "CONFIRMED|CONTRADICTED|UNVERIFIED",
      "source_ids": ["chunk_id_1"],
      "found_value": "Что реально говорят источники (конкретно)",
      "document_value": "Что утверждает документ (конкретно)",
      "contradiction_detail": "Подробное описание противоречия если статус CONTRADICTED, иначе null",
      "confidence": "HIGH|MEDIUM|LOW"
    }
  ],
  "additional_findings": [
    {
      "description": "Важная находка НЕ связанная с конкретным claim (пробел регулирования, риск, etc.)",
      "gap_type": "regulatory_hole|intentional_silence|delegation_gap|error",
      "affected_domain": "Сфера применения"
    }
  ],
  "summary": {
    "total_claims": 0,
    "confirmed": 0,
    "contradicted": 0,
    "unverified": 0,
    "critical_errors_found": 0
  },
  "recommendation": "Итоговая рекомендация на основе вердиктов",
  "pros": ["Что в документе корректно"],
  "cons": ["Найденные ошибки и проблемы"]
}
"""

_AUDITOR_USER_TEMPLATE = """
Ты — аудитор юридических документов РК. Проверь каждое утверждение документа против корпуса доказательств.

## ⚠️ РЕЖИМ: АУДИТ, НЕ ПЕРЕСКАЗ
Ты должен строго выявить любые несоответствия действующему законодательству РК. 
Оставить claim без вердикта ЗАПРЕЩЕНО.

## Анализируемый документ (проект):
__DOCUMENT_EXCERPT__

## Чеклист утверждений для верификации (__CLAIM_COUNT__ утверждений):

__CLAIMS_CHECKLIST__

## ⚠️ ДЕТЕРМИНИРОВАННЫЕ ВЕРДИКТЫ (уже проверено без LLM, включи их в verdicts):
__DETERMINISTIC_VERDICTS__

## Справочные данные (детерминированные, верифицированные):
__REFERENCE_DATA__

## Корпус доказательств (__CHUNK_COUNT__ источников):

__CORPUS_TEXT__

## Правила верификации:

### Для claim_type = legal_id:
- Найди в корпусе реальный номер закона с таким же названием
- Если номера не совпадают → CONTRADICTED

### Для claim_type = legal_ref (ссылка на статью):
- ВАЖНО: Проверь СОДЕРЖАНИЕ статьи, а не только её существование!
- Найди содержание конкретной статьи в корпусе
- Если статья существует, но О ДРУГОМ (например, документ ссылается на "ст. 207 УК — нарушение ПД", а реально ст. 207 = "Лжепредпринимательство") → CONTRADICTED
- Если статья не найдена в корпусе → UNVERIFIED

### Для claim_type = financial (МРП, штрафы):
- Найди в корпусе конкретную сумму МРП или размер штрафа
- Если числа расходятся → CONTRADICTED
- ВАЖНО: Проверь РЕАЛИСТИЧНОСТЬ штрафов! Максимум по ст.79 КоАП = 200 МРП (физлица), 2000 МРП (юрлица). Если документ указывает штраф 5000+ МРП — это ФИКТИВНАЯ НОРМА → CONTRADICTED
- Проверь, существует ли вообще указанный порог/штраф в действующем НПА. Если норма выдумана — CONTRADICTED

### Для claim_type = temporal (сроки, даты):
- Найди в корпусе упоминание этого срока/даты
- Если расходится → CONTRADICTED
- ВАЖНО: Проверь хронологическую логику (закон не может вступить раньше принятия)

### Для claim_type = factual (фактические утверждения):
- Найди в корпусе норму, которая подтверждает/опровергает
- Если норма не найдена → UNVERIFIED

### Внутренние противоречия документа:
- ИЩИ коллизии сроков: если в одной статье п.1 = 180 дней и п.2 = 60 дней для одного требования → CONTRADICTED + добавь в additional_findings
- ИЩИ нестыковки между разными частями документа

### Критическое разграничение: UNVERIFIED vs CONTRADICTED

⛔ **UNVERIFIED** — документа/статьи просто нет в корпусе:
- Гражданский кодекс не был загружен → source_ids пусты → **UNVERIFIED** (не CONTRADICTED!)
- Статья упоминается, но её текст отсутствует в корпусе → **UNVERIFIED**
- Закон есть, но нужная статья не найдена ни в одном чанке → **UNVERIFIED**

✅ **CONTRADICTED** — документ присутствует в корпусе, но текст явно опровергает claim:
- Закон загружен в корпус, статья найдена, и её текст говорит ДРУГОЕ → **CONTRADICTED**
- Числа в корпусе явно расходятся с claim → **CONTRADICTED**
- Внутреннее противоречие документа (п.1 vs п.2) → **CONTRADICTED**

⛔ **ПРАВИЛО ЦИТИРОВАНИЯ (CITE-OR-ABSTAIN):**
- Вердикт **CONTRADICTED** допускается **ТОЛЬКО** если ты приводишь **ТОЧНУЮ, дословную цитату** из корпуса доказательств (в `contradiction_detail`) с указанием его `source_ids`.
- Если ты не можешь привести дословную цитату из текста источника, которая опровергает claim — ты **ОБЯЗАН** поставить **UNVERIFIED**, а не CONTRADICTED.
- Галлюцинировать противоречия и ставить CONTRADICTED без точной цитаты — категорически запрещено!

⛔ **ПРАВИЛО ПРИВЯЗКИ ИСТОЧНИКОВ (source_ids):**
- Для каждого вердикта в поле `source_ids` укажи массив 16-символьных идентификаторов (например, `["cf08a1c97ef08a1c"]`), которые написаны сразу после `### ` в начале каждого источника в разделе «Корпус доказательств».
- Если вердикт вынесен на основе «Справочных данных», используй `"reference_data"`.
- Не оставляй `source_ids` пустым для CONFIRMED/CONTRADICTED вердиктов, иначе они будут автоматически отклонены как недостоверные!

### Общие правила:
- НЕ используй знания вне корпуса для вынесения вердикта CONFIRMED
- Используй знания вне корпуса ТОЛЬКО для пометки CONTRADICTED (ты знаешь что 87-IV не существует, ст.207 УК — это лжепредпринимательство, МРП 2025 = 3932 тг)
- Если детерминированный вердикт уже есть → используй его, дополни source_ids из корпуса
- ЗАПРЕЩЕНО ставить CONTRADICTED только потому что нужный закон не был загружен в корпус

__SCHEMA__
"""


def build_auditor_prompt(
    chunks: list[EvidenceChunk],
    claims: ClaimExtractionResult,
    plan: QueryPlan,
    doc_text: str = "",
    max_corpus_chars: int = 80_000,
    max_doc_chars: int = 5_000,
) -> str:
    """
    Строит промпт для LLM Auditor v2 (чеклист-режим).

    Args:
        chunks: Активные EvidenceChunk из корпуса.
        claims: Результат Claim Extractor (DocumentClaim[]).
        plan: QueryPlan для контекста.
        doc_text: Текст анализируемого документа.
        max_corpus_chars: Лимит символов корпуса.
        max_doc_chars: Лимит символов документа.

    Returns:
        Готовый промпт-строка.
    """
    # 1. Корпус — приоритет: язык документа
    doc_lang_is_ru = not doc_text or any(c in doc_text[:500] for c in "абвгдежзиклмнопрстуфхцчшщ")

    def _lang_penalty(c: EvidenceChunk) -> int:
        url = (c.source_url or "").lower()
        is_kaz = "/kaz/" in url or "/kk/" in url
        if doc_lang_is_ru:
            return 1 if is_kaz else 0
        return 0 if is_kaz else 1

    corpus_parts = []
    total_chars = 0
    # Chunks are assumed BM25-ranked by caller (run_auditor); only prioritize conflicts + language match
    sorted_chunks = sorted(chunks, key=lambda c: (not c.is_conflict, _lang_penalty(c)))

    for chunk in sorted_chunks:
        chunk_text = _format_chunk(chunk)
        if total_chars + len(chunk_text) > max_corpus_chars:
            corpus_parts.append(f"\n[... корпус обрезан — лимит {max_corpus_chars} символов ...]")
            break
        corpus_parts.append(chunk_text)
        total_chars += len(chunk_text)

    corpus_str = "\n\n".join(corpus_parts)

    # 2. Чеклист claims (критические первыми)
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    sorted_claims = sorted(claims.claims, key=lambda c: severity_order.get(c.severity, 9))

    checklist_parts = []
    deterministic_parts = []

    for c in sorted_claims:
        severity_emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}.get(c.severity, "⚪")
        checklist_parts.append(
            f"{severity_emoji} [{c.claim_id}] ({c.claim_type}) {c.claim_text}\n"
            f"   Entities: {', '.join(c.entities) if c.entities else 'нет'}\n"
            f"   Цитата: «{c.quote[:120]}»"
        )
        if c.deterministic_verdict:
            deterministic_parts.append(f"  • {c.claim_id}: {c.deterministic_verdict}")

    claims_str = "\n\n".join(checklist_parts) or "(утверждений не извлечено)"
    det_str = "\n".join(deterministic_parts) or "(нет детерминированных вердиктов)"

    # 3. Документ
    if doc_text:
        doc_excerpt = doc_text[:max_doc_chars]
        if len(doc_text) > max_doc_chars:
            doc_excerpt += f"\n[... обрезано, всего {len(doc_text)} символов ...]"
    else:
        doc_excerpt = "(текст документа не предоставлен)"

    # Reference data — детерминированные факты для LLM
    from zerde.reference_data import build_reference_corpus_text
    ref_text = build_reference_corpus_text()

    # .replace() вместо str.format(): JSON-схема и юр.тексты содержат {} скобки
    return (
        _AUDITOR_USER_TEMPLATE
        .replace("__DOCUMENT_EXCERPT__", doc_excerpt)
        .replace("__CLAIM_COUNT__", str(claims.total_count))
        .replace("__CLAIMS_CHECKLIST__", claims_str)
        .replace("__DETERMINISTIC_VERDICTS__", det_str)
        .replace("__REFERENCE_DATA__", ref_text)
        .replace("__CHUNK_COUNT__", str(len(chunks)))
        .replace("__CORPUS_TEXT__", corpus_str)
        .replace("__SCHEMA__", _AUDITOR_VERDICT_SCHEMA)
    )


def _format_chunk(chunk: EvidenceChunk) -> str:
    """
    ID-формат чанка: [AD|law_id|article|status] или [WB|tier|domain|status]
    """
    if chunk.law_id:
        src_type = "AD"  # Adilet
        law_part = chunk.law_id
        art_part = chunk.article or "*"
        status = "a" if (chunk.expiry_date is None) else "x"
        prefix = f"[{src_type}|{law_part}|{art_part}|{status}]"
    else:
        src_type = "WB"  # Web
        tier = (chunk.web_tier or "T?").replace("TIER_", "t")
        domain = (chunk.source_url or "").split("/")[2][:20] if chunk.source_url else "?"
        conflict = "!" if chunk.is_conflict else "-"
        prefix = f"[{src_type}|{tier}|{domain}|{conflict}]"

    conflict_marker = " ⚠️КОНФЛИКТ" if chunk.is_conflict else ""

    return (
        f"### {chunk.chunk_id[:16]}{conflict_marker}\n"
        f"{prefix} {chunk.source_title[:60]}\n"
        f"---\n"
        f"{chunk.content[:3000]}"
        + (" [ОБРЕЗАНО]" if len(chunk.content) > 3000 else "")
    )
