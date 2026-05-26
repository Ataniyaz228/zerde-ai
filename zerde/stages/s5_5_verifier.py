"""
Stage 5.2: Contradiction Verifier (Anti-Hallucination Layer)
Исключает ложноположительные CONTRADICTED вердикты от LLM Auditor,
сопоставляя найденные противоречия с реальным текстом в Evidence Chunks.
"""

from __future__ import annotations

import logging
import re
from zerde.models import AnalysisJSON, ClaimVerdict, EvidenceChunk, VerdictStatus

logger = logging.getLogger(__name__)

async def verify_contradictions(
    analysis: AnalysisJSON,
    chunks: list[EvidenceChunk],
) -> AnalysisJSON:
    """
    Дополнительный детерминированный слой проверки противоречий.
    
    Правила:
    1. Если вердикт CONTRADICTED, но у него нет source_ids (или пустые) -> UNVERIFIED.
    2. Если вердикт CONTRADICTED, но в source_ids указан 'reference_data' -> Пропускаем (детерминированные верны).
    3. Если вердикт CONTRADICTED, проверяем наличие точной цитаты (или ключевых слов противоречия) 
       в содержании связанных Evidence Chunks.
       Если ни в одном из чанков из `source_ids` не найдено подтверждения противоречия -> UNVERIFIED.
    """
    logger.info(f"[S5.2/Verifier] Validating {len(analysis.verdicts)} verdicts...")
    
    chunk_map = {c.chunk_id: c for c in chunks}
    verified_verdicts: list[ClaimVerdict] = []
    
    contradicted_count = 0
    demoted_count = 0
    
    for v in analysis.verdicts:
        if v.status != VerdictStatus.CONTRADICTED:
            verified_verdicts.append(v)
            continue
            
        # Пропускаем детерминированные (из реестра)
        if v.is_deterministic or "reference_data" in v.source_ids:
            verified_verdicts.append(v)
            contradicted_count += 1
            continue
            
        # Правило 1: Отсутствие источников
        valid_sources = [sid for sid in v.source_ids if sid in chunk_map]
        if not valid_sources:
            logger.warning(
                f"[S5.2/Verifier] Claim '{v.claim_id}' has status=CONTRADICTED "
                f"but no valid source chunks found in plan! Demoting to UNVERIFIED."
            )
            v.status = VerdictStatus.UNVERIFIED
            v.contradiction_detail = (
                f"[Verifier: Понижено с CONTRADICTED] LLM заявила о противоречии, "
                f"но не смогла указать валидные источники из базы НПА."
            )
            verified_verdicts.append(v)
            demoted_count += 1
            continue
            
        # Правило 3: Проверка наличия опровергающих ключевых слов/чисел в чанках
        # Извлекаем все числа и ключевые слова из найденного значения и деталей
        detail_text = (v.contradiction_detail or "").lower() + " " + (v.found_value or "").lower()
        
        # Извлекаем числа (чтобы проверить расхождения в штрафах, сроках, номерах законов)
        numbers_to_find = re.findall(r"\b\d+\b", detail_text)
        
        # Проверяем связанные чанки
        corroborated = False
        for sid in valid_sources:
            chunk = chunk_map[sid]
            chunk_content_lower = (chunk.content or "").lower()
            
            # Если в чанке есть точное дословное совпадение деталей противоречия
            if v.contradiction_detail and v.contradiction_detail.lower() in chunk_content_lower:
                corroborated = True
                break
                
            # Или если это числовое расхождение (все ключевые числа есть в чанке)
            if numbers_to_find:
                if all(num in chunk_content_lower for num in numbers_to_find):
                    corroborated = True
                    break
                    
            # Или если это текстовый контент (хотя бы 2 ключевых слова из found_value есть в чанке)
            keywords = [w.strip() for w in re.split(r"[^\w]+", (v.found_value or "").lower()) if len(w.strip()) > 3]
            if keywords:
                matched_keywords = sum(1 for kw in keywords if kw in chunk_content_lower)
                if matched_keywords >= min(len(keywords), 2):
                    corroborated = True
                    break
                    
        if corroborated:
            verified_verdicts.append(v)
            contradicted_count += 1
        else:
            logger.warning(
                f"[S5.2/Verifier] Contradiction for claim '{v.claim_id}' is NOT corroborated "
                f"by actual text in sources {valid_sources}! Demoting to UNVERIFIED to avoid hallucination."
            )
            v.status = VerdictStatus.UNVERIFIED
            v.contradiction_detail = (
                f"[Verifier: Понижено с CONTRADICTED] Опровергающие факты ({v.found_value}) "
                f"отсутствуют в указанных источниках ({', '.join(valid_sources)})."
            )
            verified_verdicts.append(v)
            demoted_count += 1
            
    # Обновляем verdicts в объекте анализа
    analysis.verdicts = verified_verdicts
    
    # Синхронизируем facts с обновлёнными вердиктами
    updated_facts = []
    for fact in analysis.facts:
        # Находим соответствующий вердикт
        matching_v = next((v for v in verified_verdicts if v.claim_id == fact.claim_id), None)
        if matching_v:
            if matching_v.status == VerdictStatus.CONTRADICTED:
                fact.claim = f"[{matching_v.claim_id}]: документ='{matching_v.document_value}' vs найдено='{matching_v.found_value}'"
                fact.confidence = 0.1
            elif matching_v.status == VerdictStatus.CONFIRMED:
                fact.claim = f"[{matching_v.claim_id}]: '{matching_v.found_value or matching_v.document_value}'"
                fact.confidence = 0.9
            else:
                fact.claim = f"[{matching_v.claim_id}]: '{matching_v.document_value}'"
                fact.confidence = 0.4
                fact.source_ids = ["UNLINKED"]
        updated_facts.append(fact)
        
    analysis.facts = updated_facts
    
    logger.info(
        f"[S5.2/Verifier] Verification complete. Contradicted: {contradicted_count} | "
        f"Demoted to UNVERIFIED: {demoted_count}"
    )
    return analysis
