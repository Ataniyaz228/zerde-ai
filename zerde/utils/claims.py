"""
Shared claim-introspection helpers, extracted from zerde/stages/s6_auditor.py. Pure functions over DocumentClaim used by S6's audit and
by eval/grounding scripts.
"""

from __future__ import annotations

import re

from zerde.models import DocumentClaim

# Косвенные отсылки к названиям/аббревиатурам кодексов РК -> канонические
# law_id из law_metadata (через registry). Единый источник; значения здесь
# CI-guarded by tests/test_law_dict_canonical.py (каждое значение должно быть
# locatable через registry, иначе тест падает на stale-значениях).
_COMMON_LAW_NAME_MAP = {
    "гк": ["1000-XIII", "309-II"],
    "гкрк": ["1000-XIII", "309-II"],
    "гражданск": ["1000-XIII", "309-II"],
    "азаматтық кодекс": ["1000-XIII", "309-II"],
    "ак": ["1000-XIII", "309-II"],
    "акрк": ["1000-XIII", "309-II"],
    "зк": ["442-II"],
    "зкрк": ["442-II"],
    "земельн": ["442-II"],
    "жк": ["442-II"],
    "жкрк": ["442-II"],
    "жер кодекс": ["442-II"],
    "коап": ["235-V"],
    "коапрк": ["235-V"],
    "әқбтк": ["235-V"],
    "әқбткрк": ["235-V"],
    "әкімшілік құқық бұзушылық": ["235-V"],
    "ук": ["226-V"],
    "укрк": ["226-V"],
    "уголовн": ["226-V"],
    "ққ": ["226-V"],
    "ққрк": ["226-V"],
    "қылмыстық кодекс": ["226-V"],
    "аппк": ["350-VI"],
    "госимуществ": ["413-IV"],
    "государственному имуществу": ["413-IV"],
    "государственного имущества": ["413-IV"],
    # Косвенные отсылки к другим Кодексам РК -> канонические law_id из law_metadata
    # (раньше тут были строки-названия + стейл: «Налоговый кодекс»=120-VI).
    "налогов": ["214-VII"],
    " нк": ["214-VII"],
    "бюджетн": ["171-VIII"],
    " бк": ["171-VIII"],
    "трудов": ["414-I-NEW"],
    " тк": ["414-I-NEW"],
    "экологическ": ["400-VI-NEW"],
    " эк": ["400-VI-NEW"],
    "электронном документе": ["370-II"],
    "эцп": ["370-II"],
    "информатизации": ["418-V"],
    "искусственном интеллекте": ["230-VIII"],
    "цифровой кодекс": ["255-VIII"],
    "цифрового кодекса": ["255-VIII"],
    "исполнительном производстве": ["261-IV"],
    "исполнительного производства": ["261-IV"],
    "судебных исполнителей": ["261-IV"],
    "сот орындаушы": ["261-IV"],
    "атқарушылық іс жүргізу": ["261-IV"],
    "233-IV": ["261-IV"],
}


def are_law_ids_synonymous(law_a: str, law_b: str) -> bool:
    if not law_a or not law_b:
        return False
    from zerde.utils.law_registry import get_registry
    reg = get_registry()
    a_canon = reg.resolve(law_a).upper()
    b_canon = reg.resolve(law_b).upper()
    return a_canon == b_canon


def extract_referenced_law_ids(claim: DocumentClaim) -> list[str]:
    # если S2.5 определил target_law_ids (поправочный закон с явной
    # привязкой к закону-мишени), считаем это authoritative и игнорируем
    # law_id'ы из entities/text/regex. Иначе alias-карты подмешивают чужие
    # законы (напр. "акт амнистии"/"исполнительном производстве" -> 261-IV
    # в КоАП-документе), и LLM-аудитор привязывает claim к статье из
    # неправильного кодекса с BM25=1.0 (ложное подтверждение).
    target = getattr(claim, "target_law_ids", None) or []
    if target:
        return list(set(target))

    law_ids = []
    from zerde.utils.law_registry import get_registry
    registry = get_registry()

    # 1. Search entities: id-форма ИЛИ закон, локализуемый реестром (единый
    #    источник вместо membership по reference_data.LAW_REGISTRY).
    for ent in claim.entities:
        ent_clean = str(ent).strip().upper().replace(" ", "")
        ent_clean = re.sub(r"ЗРК|зрк", "", ent_clean).strip("-").strip()
        if re.match(r"^\d+-[-‐–A-Z]+$", ent_clean) or registry.get_adilet_code(ent_clean):
            law_ids.append(ent_clean)

    # 2. Search text and entities for common aliases
    text_lower = (claim.claim_text + " " + claim.quote).lower()
    for alias, resolved in _COMMON_LAW_NAME_MAP.items():
        clean_alias = alias.strip()
        if not clean_alias:
            continue
        # Используем границы слов для коротких аббревиатур (длиной <= 3 символа),
        # чтобы избежать ложных совпадений внутри слов (например, "ак" в "акт", "актісі", "жақсы").
        if len(clean_alias) <= 3:
            pattern = rf"\b{re.escape(clean_alias)}\b"
        else:
            pattern = re.escape(clean_alias)

        if re.search(pattern, text_lower, re.I | re.U):
            law_ids.extend(resolved)

    # 3. Regex match standard law formats in text (e.g. № 413-IV or 1000-XIII)
    matches = re.findall(r"\b\d+[-‐–][IVXivxІі]{1,5}\b", text_lower)
    for m in matches:
        clean_m = m.upper().replace(" ", "").replace("І", "I").replace("і", "i")
        law_ids.append(clean_m)

    return list(set(law_ids))


def extract_article_from_claim(claim: DocumentClaim) -> str | None:
    """Извлекает номер статьи из утверждения с использованием regex."""
    text = (claim.claim_text + " " + claim.quote).lower()

    # 1. Русский вариант: слово статья/ст в разных падежах, затем число
    match_ru = re.search(r"\b(?:стать[яиюе]|ст\.?)\s*(\d+[\-\d]*)", text, re.IGNORECASE)
    if match_ru:
        return match_ru.group(1).strip()

    # 2. Казахский вариант: число, затем дефис и бап/бабы/бапта/баптың
    match_kk = re.search(r"\b(\d+[\-\d]*)-(?:бап|бабы|бабының|бапта|баптың|бабына)", text, re.IGNORECASE)
    if match_kk:
        return match_kk.group(1).strip()

    # 3. Fallback на просто бап/бабы, если число идет после
    match_kk_fallback = re.search(r"\b(?:бап|бабы|бабының|бапта|баптың|бабына)\s*(\d+[\-\d]*)", text, re.IGNORECASE)
    if match_kk_fallback:
        return match_kk_fallback.group(1).strip()

    return None
