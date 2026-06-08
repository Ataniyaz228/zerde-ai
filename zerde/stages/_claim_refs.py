"""Извлечение ссылок на нормы из утверждений: law_id, номер статьи, синонимия id.

Вынесено из s6_auditor (F-A4), чтобы S5 и eval-скрипты не импортировали приватные
функции аудитора через границу стадий. s6_auditor реэкспортирует эти имена
(import из этого модуля), поэтому существующие `from s6_auditor import ...` работают.
"""

from __future__ import annotations

import re

from zerde.models import DocumentClaim
from zerde.reference_data import LAW_REGISTRY

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
    # Косвенные отсылки к другим Кодексам РК → канонические law_id из law_metadata
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

# _LAW_ID_SYNONYMS удалён: варианты написания law_id (short ID ↔ adilet-код)
# теперь выдаёт registry.id_variants() из реестра (БД + статика), без ручного
# словаря, который дрейфовал относительно law_metadata.


def _are_law_ids_synonymous(law_a: str, law_b: str) -> bool:
    if not law_a or not law_b:
        return False
    from zerde.utils.law_registry import get_registry
    reg = get_registry()
    a_canon = reg.resolve(law_a).upper()
    b_canon = reg.resolve(law_b).upper()
    return a_canon == b_canon


def _extract_referenced_law_ids(claim: DocumentClaim) -> list[str]:
    # V9.7: если S2.5 определил target_law_ids (поправочный закон с явной
    # привязкой к закону-мишени), считаем это authoritative и игнорируем
    # law_id'ы из entities/text/regex. Иначе alias-карты подмешивают чужие
    # законы (напр. "акт амнистии"/"исполнительном производстве" → 261-IV
    # в КоАП-документе), и LLM-аудитор привязывает claim к статье из
    # неправильного кодекса с BM25=1.0 (ложное подтверждение).
    target = getattr(claim, "target_law_ids", None) or []
    if target:
        return list(set(target))

    law_ids = []

    # 1. Search entities
    for ent in claim.entities:
        ent_clean = str(ent).strip().upper().replace(" ", "")
        ent_clean = re.sub(r"ЗРК|зрк", "", ent_clean).strip("-").strip()
        if re.match(r"^\d+-[-‐–A-Z]+$", ent_clean) or ent_clean in LAW_REGISTRY:
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
    matches = re.findall(r"\b\d+[-‐–][IVXivx\u0406\u0456]{1,5}\b", text_lower)
    for m in matches:
        clean_m = m.upper().replace(" ", "").replace("\u0406", "I").replace("\u0456", "i")
        law_ids.append(clean_m)

    # Fallback \u043d\u0430 target_law_ids \u0443\u0431\u0440\u0430\u043d \u0432 V9.7 \u2014 strict mode \u0432\u044b\u043f\u043e\u043b\u043d\u044f\u0435\u0442\u0441\u044f \u0432
    # \u043d\u0430\u0447\u0430\u043b\u0435 \u0444\u0443\u043d\u043a\u0446\u0438\u0438 (\u0441\u043c. \u0431\u043b\u043e\u043a V9.7 \u0432\u044b\u0448\u0435).

    return list(set(law_ids))


def _extract_article_from_claim(claim: DocumentClaim) -> str | None:
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
