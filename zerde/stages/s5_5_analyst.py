"""
Stage 5.5: Policy Analyst (Аналитическая оценка)

Запускается ПАРАЛЛЕЛЬНО с S6 (BM25 Audit).
Получает: текст документа + вердикты S5 + корпус.
Выдаёт: структурированную аналитическую записку.

Это НЕ верификация — это генерация экспертной оценки.
Результат маркируется как "рекомендательный".
"""

from __future__ import annotations

import json
import logging

from zerde.config import get_settings
from zerde.models import AnalysisJSON, EvidenceChunk
from zerde.utils.llm_client import cached_llm_call

logger = logging.getLogger(__name__)


_ANALYST_SCHEMA = """\
Верни JSON строго следующей структуры:
{
  "subject": "Предмет и цель документа (1-3 предложения)",
  "affected_parties": [
    {"party": "Название группы", "impact": "Как затрагивает (позитивно/негативно/нейтрально)", "details": "Подробности"}
  ],
  "key_changes": [
    {"change": "Краткое описание изменения", "significance": "HIGH|MEDIUM|LOW"}
  ],
  "risks": [
    {"risk": "Описание риска", "severity": "HIGH|MEDIUM|LOW", "mitigation": "Возможное решение или null"}
  ],
  "strengths": ["Сильные стороны документа"],
  "weaknesses": ["Слабые стороны (НЕ из вердиктов — те уже в отчёте)"],
  "recommendation": "ПРИНЯТЬ|ПРИНЯТЬ С ЗАМЕЧАНИЯМИ|ОТПРАВИТЬ НА ДОРАБОТКУ|ОТКЛОНИТЬ",
  "recommendation_reasoning": "Обоснование рекомендации (2-4 предложения)",
  "regulatory_context": "Место документа в системе НПА РК (какие законы дополняет/изменяет)"
}
"""

_ANALYST_PROMPT = """\
Ты — эксперт-аналитик правовой политики Республики Казахстан.
Тебе предоставлен текст законопроекта и результаты его технической верификации.

## Твоя задача:
Провести АНАЛИТИЧЕСКУЮ ОЦЕНКУ документа — не проверку фактов (это уже сделано),
а содержательную экспертизу: зачем этот закон, кого затрагивает, какие риски.

## Правила:
1. НЕ ДУБЛИРУЙ ошибки из технической верификации — они уже в отчёте.
2. Фокусируйся на ПОЛИТИКЕ, ЭКОНОМИКЕ и ПРАВОПРИМЕНЕНИИ.
3. Если документ вносит изменения в несколько законов — перечисли каждый.
4. В recommendation учитывай результаты верификации ниже.

## Текст документа:
__DOCUMENT__

## Результаты верификации (для контекста):
- Всего утверждений проверено: __TOTAL_VERDICTS__
- Подтверждено: __CONFIRMED__
- Опровергнуто: __CONTRADICTED__
- Не проверено: __UNVERIFIED__
- Найдено пробелов: __GAPS__

__VERDICT_SUMMARY__

__SCHEMA__
"""


async def run_policy_analyst(
    doc_text: str,
    analysis: AnalysisJSON,
    chunks: list[EvidenceChunk],
    max_doc_chars: int = 24_000,
) -> dict | None:
    """
    Запускает аналитический агент.
    
    Args:
        doc_text: Нормализованный текст документа.
        analysis: Результат S5 (вердикты).
        chunks: Активные evidence chunks.
        max_doc_chars: Лимит символов документа в промпте.
    
    Returns:
        Словарь с аналитической оценкой или None при ошибке.
    """
    settings = get_settings()
    logger.info("[S5.5/Analyst] Start policy analysis")

    # Подготавливаем промпт
    doc_excerpt = doc_text[:max_doc_chars]
    if len(doc_text) > max_doc_chars:
        doc_excerpt += f"\n[... обрезано, всего {len(doc_text)} символов ...]"

    # Статистика вердиктов
    n_confirmed = sum(1 for v in analysis.verdicts if v.status.value == "CONFIRMED")
    n_contradicted = sum(1 for v in analysis.verdicts if v.status.value == "CONTRADICTED")
    n_unverified = len(analysis.verdicts) - n_confirmed - n_contradicted
    n_gaps = len(analysis.negative_space)

    # Краткая сводка ключевых вердиктов
    verdict_lines = []
    for v in analysis.verdicts:
        if v.status.value == "CONTRADICTED":
            detail = v.contradiction_detail or v.found_value or ""
            verdict_lines.append(f"  ❌ {v.claim_id}: {detail[:150]}")
    for ns in analysis.negative_space:
        verdict_lines.append(f"  🕳️ {ns.item_id}: {ns.description[:150]}")

    verdict_summary = "\n".join(verdict_lines) if verdict_lines else "(нет критических находок)"

    prompt = (
        _ANALYST_PROMPT
        .replace("__DOCUMENT__", doc_excerpt)
        .replace("__TOTAL_VERDICTS__", str(len(analysis.verdicts)))
        .replace("__CONFIRMED__", str(n_confirmed))
        .replace("__CONTRADICTED__", str(n_contradicted))
        .replace("__UNVERIFIED__", str(n_unverified))
        .replace("__GAPS__", str(n_gaps))
        .replace("__VERDICT_SUMMARY__", verdict_summary)
        .replace("__SCHEMA__", _ANALYST_SCHEMA)
    )

    try:
        # H8 Fix: Используем make_llm_client(settings) для корректной передачи OpenRouter заголовков
        from zerde.utils.llm_client import make_llm_client
        client = make_llm_client(settings)

        messages = [
            {"role": "system", "content": "Ты — эксперт правовой политики Республики Казахстан. Отвечай строго в JSON."},
            {"role": "user", "content": prompt},
        ]

        raw_json = await cached_llm_call(
            client=client,
            model=settings.llm_model_policy_analyst,
            messages=messages,
            settings=settings,
            ttl_seconds=86400,
            max_tokens=4096,
            temperature=0.2,
        )

        if not raw_json or raw_json == "{}":
            logger.warning("[S5.5/Analyst] Empty response from LLM")
            return None

        result = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
        logger.info(
            f"[S5.5/Analyst] Done. recommendation={result.get('recommendation', 'N/A')} "
            f"changes={len(result.get('key_changes', []))} risks={len(result.get('risks', []))}"
        )
        return result

    except Exception as e:
        logger.error(f"[S5.5/Analyst] Failed: {e}")
        return None
