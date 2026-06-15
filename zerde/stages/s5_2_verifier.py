"""S5.2 — защита от ложных противоречий, придуманных аудитором.

Каждый вердикт CONTRADICTED должен опираться на дословную цитату из чанка. Если
такой цитаты в Evidence Chunks нет — понижаем вердикт до UNVERIFIED. Это страховка
от главной ошибки: уверенно объявить противоречие там, где его в доказательствах нет.
"""

from __future__ import annotations

import logging
import re

from zerde.models import AnalysisJSON, ClaimVerdict, EvidenceChunk, VerdictStatus

logger = logging.getLogger(__name__)

# Короткие служебные слова (предлоги/союзы/частицы), не несущие смысла для
# сравнения "документ vs найдено" — отбрасываем перед сопоставлением.
_RU_STOPWORDS = frozenset({
    "в", "во", "на", "и", "с", "со", "по", "о", "об", "от", "до", "за", "к",
    "ко", "из", "у", "же", "бы", "ли", "а", "но", "the", "of",
})

# Маркеры отрицания: если в одной строке отрицание есть, а в другой нет, фразы
# могут быть ПРОТИВОПОЛОЖНЫ ('не применяется' vs 'применяется') — не считаем их
# одной фразой, чтобы не замаскировать реальное противоречие.
_NEG_RE = re.compile(r"(?:\bне\b|\bнет\b|\bни\b|\bбез\b|запрещ|не\s+допуск|отказ)", re.I | re.U)

# Поправочный claim («заменить/исключить/дополнить …»).
_AMEND_RE = re.compile(r"замен|заменить|исключ|дополн|слова.{0,40}заменить", re.I | re.U)
# «Противоречие» через ОТСУТСТВИЕ изменяемого текста / несовпадение места, а не
# через наличие конфликтующего значения. Корпус хранит чанки на уровне СТАТЬИ
# (без точных абзацев/подпунктов), поэтому вывод «фразы тут нет» недоказателен —
# по CITE-OR-ABSTAIN это UNVERIFIED, а не CONTRADICTED.
_ABSENCE_RE = re.compile(
    r"отсутству|не\s+содержит|не\s+найд|не\s+обнаруж|неверно\s+идентифиц|"
    r"вовсе\s+не|в\s+друг\w*\s+(?:абзац|пункт|подпункт|мест|част)|не\s+та\s+стат",
    re.I | re.U,
)


def _content_stems(text: str) -> tuple[frozenset[str], frozenset[str]]:
    """
    Возвращает (stems, numbers) для строки: набор «корней» содержательных слов
    (первые 5 символов слов длиной ≥4 после отброса служебных) и набор чисел.
    Прокси-стемминг по префиксу устойчив к падежным окончаниям рус/каз.
    """
    low = text.lower().replace("ё", "е")
    numbers = frozenset(re.findall(r"\d+", low))
    words = [w for w in re.split(r"[^\w]+", low) if w]
    stems = frozenset(
        w[:5] for w in words
        if len(w) >= 4 and w not in _RU_STOPWORDS and not w.isdigit()
    )
    return stems, numbers


def _amendment_targets_existing_text(document_value: str, found_value: str) -> bool:
    """
    True, если поправочный claim вида «заменить «A» на «B»» адресует РЕАЛЬНО
    существующий в законе текст A: первая закавыченная фраза документа (заменяемый
    термин A) присутствует в найденной норме (по стемам и числам).

    Тогда «противоречие» мнимое: то, что закон содержит A (а не B), — это и есть
    дозаменное состояние, которое поправка как раз меняет, а НЕ опровержение.
    Если же A в норме НЕ найден (числа/стемы расходятся) — это возможный misquote,
    не глушим.
    """
    if not document_value or not found_value:
        return False
    m = re.search(r"[«\"]([^»\"]{2,60})[»\"]", document_value)
    if not m:
        return False
    src_stems, src_nums = _content_stems(m.group(1))
    f_stems, f_nums = _content_stems(found_value)
    if not src_stems:
        return False
    return src_stems <= f_stems and src_nums <= f_nums


def _same_phrase_modulo_inflection(a: str, b: str) -> bool:
    """
    True, если `a` и `b` — одна и та же фраза с точностью до падежа/порядка слов
    и при ИДЕНТИЧНОМ наборе чисел. Тогда «противоречие» документ↔норма мнимое
    (морфологический вариант, напр. 'во всенародном референдуме' vs
    'всенародного референдума'), а не фактическое расхождение.
    """
    if not a or not b:
        return False
    sa, na = _content_stems(a)
    sb, nb = _content_stems(b)
    if not sa or not sb:
        return False
    if na != nb:                      # разные числа → реальное расхождение, не трогаем
        return False
    if bool(_NEG_RE.search(a)) != bool(_NEG_RE.search(b)):   # отрицание только в одной → возможна противоположность
        return False
    # Совпадение с точностью до падежа/порядка ИЛИ document — подмножество нормы.
    # Если все содержательные слова документа присутствуют в норме (та же лексика,
    # лишь иной порядок/доп. контекст в норме), фактического противоречия нет —
    # это типичная картина поправочного claim'а («изменить порядок перечисления …»).
    return sa == sb or sa <= sb


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

        # Правило 0: Мнимое противоречие из-за склонения/порядка слов.
        # Если document_value и found_value — одна фраза с точностью до падежа
        # и при том же наборе чисел, это не фактическое расхождение.
        if _same_phrase_modulo_inflection(v.document_value or "", v.found_value or ""):
            logger.warning(
                f"[S5.2/Verifier] Claim '{v.claim_id}' CONTRADICTED, но document='{v.document_value}' "
                f"и found='{v.found_value}' — одна фраза с точностью до склонения. Понижаю до UNVERIFIED."
            )
            v.status = VerdictStatus.UNVERIFIED
            v.contradiction_detail = (
                f"[Verifier: Понижено с CONTRADICTED] Различие document↔норма "
                f"('{v.document_value}' / '{v.found_value}') — грамматическое (падеж/порядок слов), "
                f"а не фактическое."
            )
            verified_verdicts.append(v)
            demoted_count += 1
            continue

        # Правило 0.5: Поправочный claim, «опровергнутый» через ОТСУТСТВИЕ
        # изменяемой фразы / несовпадение абзаца. Чанки корпуса — на уровне статьи,
        # поэтому «фразы нет в этом абзаце» недоказуемо → UNVERIFIED (CITE-OR-ABSTAIN:
        # отсутствие доказательства ≠ опровержение).
        _det = (v.contradiction_detail or "") + " " + (v.found_value or "")
        if _AMEND_RE.search(v.document_value or "") and _ABSENCE_RE.search(_det):
            logger.warning(
                f"[S5.2/Verifier] Claim '{v.claim_id}' CONTRADICTED поправочного claim'а "
                f"обоснован отсутствием/несовпадением места, а не конфликтующим текстом. "
                f"Чанки на уровне статьи не дают абзацной точности → UNVERIFIED."
            )
            v.status = VerdictStatus.UNVERIFIED
            v.contradiction_detail = (
                "[Verifier: Понижено с CONTRADICTED] Опровержение основано на отсутствии "
                "изменяемой фразы в показанном фрагменте статьи, а не на конфликтующем "
                "тексте. Корпус хранит статьи без абзацной детализации, поэтому точное "
                "местоположение правки не верифицируемо (CITE-OR-ABSTAIN → UNVERIFIED)."
            )
            verified_verdicts.append(v)
            demoted_count += 1
            continue

        # Правило 0.7: Поправка адресует существующий текст. «Заменить «A» на «B»»,
        # и в норме найден заменяемый A → закон содержит дозаменную форму, это норма
        # для поправки, а НЕ противоречие. (Напр. «заменить «тенге» на «теңге»», а в
        # НК сейчас «тенге».) Понижаем до UNVERIFIED (безопасно: не подтверждаем).
        if _AMEND_RE.search(v.document_value or "") and \
                _amendment_targets_existing_text(v.document_value or "", v.found_value or ""):
            logger.warning(
                f"[S5.2/Verifier] Claim '{v.claim_id}' CONTRADICTED: поправка заменяет текст, "
                f"который РЕАЛЬНО есть в норме (дозаменная форма) — это не противоречие. → UNVERIFIED."
            )
            v.status = VerdictStatus.UNVERIFIED
            v.contradiction_detail = (
                "[Verifier: Понижено с CONTRADICTED] Поправка заменяет/исключает текст, "
                "который присутствует в действующей норме — наличие заменяемой формы в законе "
                "является дозаменным состоянием, а не опровержением (это и есть смысл поправки)."
            )
            verified_verdicts.append(v)
            demoted_count += 1
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
                "[Verifier: Понижено с CONTRADICTED] LLM заявила о противоречии, "
                "но не смогла указать валидные источники из базы НПА."
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
