"""
Stage 2.5: Claim Extractor
Вход:  DocumentState
Выход: ClaimExtractionResult

Гибридный подход:
  1. Детерминированный regex-этап — ловит числовые/ссылочные claims без LLM
  2. LLM-этап — ловит контекстные утверждения (биометрия обязательна с 2026)
  3. Reference Injector — проверяет entities по reference_data.py без LLM
"""

from __future__ import annotations

import json
import logging
import re
import string

from openai import AsyncOpenAI

from zerde.config import get_settings
from zerde.models import (
    ClaimExtractionResult,
    ClaimSeverity,
    ClaimType,
    DocumentClaim,
    DocumentState,
    VerdictStatus,
)
from zerde.reference_data import (
    KOAP_MAX_FINES,
    LAW_REGISTRY,
    check_law_id,
    get_koap_article,
    get_mrp,
    get_uk_article,
)
from zerde.utils.llm_client import cached_llm_call, make_llm_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Regex patterns для детерминированного извлечения
# ---------------------------------------------------------------------------

_PATTERNS: list[tuple[re.Pattern, ClaimType, ClaimSeverity, str]] = [
    # Номера законов: "Закон № 94-V", "№ 87-IV ЗРК", "Законы / ҚРЗ"
    (re.compile(r"(?:Закон\w*|Кодекс\w*|ЗРК|Заңы*|ҚРЗ)\s+[^№]*?№\s*(\d{2,4}[-‐–]\w{1,5}(?:\s*(?:ЗРК|ҚРЗ))?)", re.I | re.U), ClaimType.LEGAL_ID, ClaimSeverity.CRITICAL, "law_id"),
    (re.compile(r"№\s*(\d{2,4}[-‐–][IVXivx\u0406\u0456]{1,5}(?:\s*(?:ЗРК|ҚРЗ))?)\b", re.I), ClaimType.LEGAL_ID, ClaimSeverity.CRITICAL, "law_id"),
    
    # Ссылки на статьи КоАП (RU + KK)
    (re.compile(r"стать[яиею]\s*(\d+(?:[-.]?\d+)?)\s*КоАП", re.I | re.U), ClaimType.LEGAL_REF, ClaimSeverity.CRITICAL, "koap_article"),
    (re.compile(r"ӘҚБтК\w*\s*(?:-\s*)?(\d+(?:[-.\d]+)?)\s*(?:-|–)?\s*(?:бап|бабы|бабында|бапта|баптың|бабының)", re.I | re.U), ClaimType.LEGAL_REF, ClaimSeverity.CRITICAL, "koap_article"),
    (re.compile(r"(\d+(?:[-.\d]+)?)\s*(?:-|–)?\s*(?:бап|бабы|бабында|бапта|баптың|бабының)(?:[^0-9\n]*?)ӘҚБтК", re.I | re.U), ClaimType.LEGAL_REF, ClaimSeverity.CRITICAL, "koap_article"),

    # Ссылки на статьи УК (RU + KK)
    (re.compile(r"стать[яиеюй]\w?\s+(\d+(?:[-.]?\d+)?)\s+(?:Уголовн\w+\s+[Кк]одекс\w*|УК)", re.I | re.U), ClaimType.LEGAL_REF, ClaimSeverity.CRITICAL, "uk_article"),
    (re.compile(r"ҚК\w*\s*(?:-\s*)?(\d+(?:[-.\d]+)?)\s*(?:-|–)?\s*(?:бап|бабы|бабында|бапта|баптың|бабының)", re.I | re.U), ClaimType.LEGAL_REF, ClaimSeverity.CRITICAL, "uk_article"),
    (re.compile(r"(\d+(?:[-.\d]+)?)\s*(?:-|–)?\s*(?:бап|бабы|бабында|бапта|баптың|бабының)(?:[^0-9\n]*?)ҚК", re.I | re.U), ClaimType.LEGAL_REF, ClaimSeverity.CRITICAL, "uk_article"),

    # Ссылки на статьи любого Кодекса (RU + KK)
    (re.compile(r"(?:в\s+)?стать[яиеюй]\w?\s+(\d+(?:[-.]?\d+)?)\b(?!\s*(?:КоАП|ӘҚБтК|УК|ҚК))", re.I | re.U), ClaimType.LEGAL_REF, ClaimSeverity.HIGH, "article_ref"),
    (re.compile(r"(\d+(?:[-.\d]+)?)\s*(?:-|–)?\s*(?:бап|бабы|бабында|бапта|баптың|бабының)\b(?!\s*(?:КоАП|ӘҚБтК|УК|ҚК))", re.I | re.U), ClaimType.LEGAL_REF, ClaimSeverity.HIGH, "article_ref"),
    
    # Ссылки на ППРК: Постановление Правительства №142, ППРК №909
    (re.compile(r"(?:ППРК|Постановлени[яею]\s+Правительства)[^№]*№?\s*(\d+)", re.I | re.U), ClaimType.LEGAL_REF, ClaimSeverity.CRITICAL, "pprkz_num"),
    
    # Штрафы в МРП / АЕК (RU + KK)
    (re.compile(r"(\d[\d\s]*)\s*(?:\([^)]*\)\s*)?(?:МРП|АЕК)", re.I | re.U), ClaimType.FINANCIAL, ClaimSeverity.CRITICAL, "fine_mrp"),
    (re.compile(r"айлық\s+есептік\s+көрсеткіш\w*\s*(?:дегеніміз\s*)?(\d[\d\s]*)\s*(?:\([^)]*\)\s*)?(?:еселенген|еселі|мөлшер)", re.I | re.U), ClaimType.FINANCIAL, ClaimSeverity.CRITICAL, "fine_mrp"),
    # Размер МРП / АЕК в тенге (RU + KK)
    (re.compile(r"(?:МРП|АЕК)[^.]*?(\d[\d\s]+)\s*тенге", re.I | re.U), ClaimType.FINANCIAL, ClaimSeverity.HIGH, "mrp_value"),
    
    # Сроки уведомлений в часах: "не позднее 24 часов", "в течение 72 часов"
    (re.compile(r"(?:в\s*течени[ие]|не\s*позднее)\s*(\d+)\s*(?:\([^)]*\)\s*)?час", re.I | re.U), ClaimType.TEMPORAL, ClaimSeverity.HIGH, "hours_deadline"),
    # Сроки в рабочих днях (RU + KK)
    (re.compile(r"в\s*течени[ие]\s*(\d+)\s*рабочих\s*дн", re.I | re.U), ClaimType.TEMPORAL, ClaimSeverity.HIGH, "workdays_deadline"),
    (re.compile(r"(\d+)\s*жұмыс\s*күні\s*ішінде", re.I | re.U), ClaimType.TEMPORAL, ClaimSeverity.HIGH, "workdays_deadline"),
    
    # Даты вступления в силу: с 1 января 2025 года, с 1 июля 2026
    (re.compile(r"с\s+(\d{1,2}\s+\w+\s+\d{4})\s*года", re.I | re.U), ClaimType.TEMPORAL, ClaimSeverity.HIGH, "effective_date"),
    # Вводится в действие / қолданысқа енгізіледі (RU + KK)
    (re.compile(r"вводится\s+в\s+действие\s+(.*?(?:дня|после))", re.I | re.U), ClaimType.TEMPORAL, ClaimSeverity.HIGH, "enforcement_date"),
    (re.compile(r"қолданысқа\s+енгізіледі\s+(.*?(?:бастап|кейін))", re.I | re.U), ClaimType.TEMPORAL, ClaimSeverity.HIGH, "enforcement_date"),
    (re.compile(r"қолданысқа\s+енгiзiледi\s+(.*?(?:бастап|кейін))", re.I | re.U), ClaimType.TEMPORAL, ClaimSeverity.HIGH, "enforcement_date"),
]

# Паттерн для извлечения года в контексте
_YEAR_RE = re.compile(r"\b(202[0-9])\b")

# V7.0: Стоп-слова для нормализации claims (дедупликация)
_STOP_WORDS_CLAIM = frozenset([
    "и", "в", "на", "с", "по", "от", "до", "за", "при", "о", "об", "из",
    "или", "а", "но", "что", "как", "это", "не", "к", "для", "то",
    "документ", "утверждает", "строка", "таблица", "присутствует", "существует",
])

# V7.0: Модальные глаголы — если есть, claim НЕ структурный (RU + KK)
_MODAL_VERBS = frozenset([
    "обязан", "должен", "запрещается", "влечет", "устанавливается",
    "предусмотрено", "установлено", "нарушение", "ответственность", "штраф",
    "вводится", "приостанавливается", "прекращается", "возобновляется",
    "подлежит", "является", "несет", "вправе", "может", "должны",
    # Kazakh modal verbs / terms
    "міндетті", "тиіс", "салынады", "әкеп", "соғады", "белгіленеді", "айқындалады",
    "көзделген", "жауаптылық", "айыппұл", "тоқтатылады", "құқылы", "болады",
])


# ---------------------------------------------------------------------------
# V9.6: Target Law Detection (для поправочных актов)
# ---------------------------------------------------------------------------

# Паттерны для поиска закона-мишени в преамбуле/заголовке документа
_TARGET_LAW_PREAMBLE_RE = re.compile(
    # "внести изменения в ... Закон РК № 94-V" / "в ... Кодекс РК"
    r"(?:вносятся?|внести|внесении)\s+изменений?\s+(?:и\s+дополнений?\s+)?в\s+.{0,80}?(?:№\s*)?(\d{2,4}[-–]\w{1,5})",
    re.I | re.U | re.S,
)
_TARGET_LAW_TITLE_RE = re.compile(
    # Паттерн "О внесении изменений ... № 261-IV" из названия закона
    r"(?:Закон\w*|Кодекс\w*)\s+.{0,120}?(?:№\s*)?(\d{2,4}[-–][IVXivxІі]{1,5})\b",
    re.I | re.U | re.S,
)
_TARGET_LAW_FROM_BLOCK_RE = re.compile(
    # "Закон РК «Об исполнительном производстве и статусе судебных исполнителей»"
    # — ищем блок до 600 символов: от "изменения в" до конца ссылки
    r'(?:изменения|дополнения)\s+в\s+(?:[А-ЯЁа-яё\w\s«»"\',-]+?)'
    r'(?:от\s+\d{1,2}\s+\w+\s+\d{4}|№\s*(\d{2,4}[-–][IVXivxІі]{1,5}))',
    re.I | re.U | re.S,
)

# Словарь ключевых фраз названий законов → short ID
_TITLE_HINT_MAP: dict[str, str] = {
    "исполнительном производстве": "261-IV",
    "судебных исполнителей": "261-IV",
    "атқарушылық іс жүргізу": "261-IV",
    "сот орындаушылар": "261-IV",
    "персональных данных": "94-V",
    "дербес деректер": "94-V",
    "административных правонарушениях": "235-V",
    "әкімшілік құқық бұзушылық": "235-V",
    "уголовный кодекс": "226-V-UK",
    "қылмыстық кодекс": "226-V-UK",
    "гражданский кодекс": "1000-XIII",
    "азаматтық кодекс": "1000-XIII",
    "земельный кодекс": "442-II",
    "жер кодекс": "442-II",
    "трудовой кодекс": "414-I-NEW",
    "еңбек кодекс": "414-I-NEW",
    "налоговый кодекс": "214-VII",
    "салық кодекс": "214-VII",
    "об образовании": "319-III",
    "білім туралы": "319-III",
    "о государственных закупках": "106-VIII",
    "мемлекеттік сатып алу": "106-VIII",
    "о противодействии коррупции": "410-V-NEW",
    "сыбайлас жемқорлыққа қарсы": "410-V-NEW",
    "об информатизации": "418-V",
    "информатизация туралы": "418-V",
    "о связи": "567-II-NEW",
    "байланыс туралы": "567-II-NEW",
    "о банках": "258-VIII",
    "банктер туралы": "258-VIII",
    "об исполнительных документах": "261-IV",
    "о нотариате": "155-I",
    "нотариат туралы": "155-I",
}


def _detect_target_laws(text: str) -> list[str]:
    """
    V9.6: Определяет законы-мишени поправочного акта по преамбуле/заголовку.
    Возвращает список short ID (напр. ['261-IV', '235-V']).
    """
    from zerde.utils.law_registry import get_registry
    registry = get_registry()

    # Ищем только в первых 1200 символах (заголовок + преамбула)
    sample = text[:1200].lower()
    found: list[str] = []

    # 1. Прямые паттерны "вносятся изменения в ... № N-V"
    for m in _TARGET_LAW_PREAMBLE_RE.finditer(text[:1200]):
        raw = m.group(1).replace("–", "-").replace("‑", "-").upper()
        resolved = registry.resolve(raw)
        if resolved and resolved not in found:
            found.append(resolved)
    for m in _TARGET_LAW_TITLE_RE.finditer(text[:1200]):
        raw = m.group(1).replace("–", "-").replace("‑", "-").upper()
        resolved = registry.resolve(raw)
        if resolved and resolved not in found:
            found.append(resolved)

    # 2. Ключевые фразы названий законов в заголовке (если явный номер не найден)
    for hint, short_id in _TITLE_HINT_MAP.items():
        if hint in sample:
            resolved = registry.resolve(short_id)
            canonical = resolved if resolved else short_id
            if canonical not in found:
                found.append(canonical)

    return found


# ---------------------------------------------------------------------------
# V9.6: Meta-claim filter (отсев мусорных тривиальных claims)
# ---------------------------------------------------------------------------

# Паттерны для META claims — заголовков / самоописаний документа
_META_CLAIM_PATTERNS = (
    re.compile(r"документ\s+является\s+(?:проектом\s+)?закон", re.I | re.U),
    re.compile(r"документ\s+представляет\s+собой", re.I | re.U),
    re.compile(r"это\s+(?:проект\s+)?закон\w*\s+о\s+внесении", re.I | re.U),
    re.compile(r"настоящий\s+(?:проект\s+)?закон", re.I | re.U),
    re.compile(r"осы\s+заң", re.I | re.U),
    re.compile(r"законопроект\s+(?:вносит|направлен|посвящен)", re.I | re.U),
)


def _is_meta_claim(claim: DocumentClaim) -> bool:
    """V9.6: True если claim — мета-описание документа, а не проверяемое утверждение."""
    text = claim.claim_text.strip()
    # Слишком короткие/общие
    if len(text) < 20:
        return True
    for pat in _META_CLAIM_PATTERNS:
        if pat.search(text):
            return True
    return False


# ---------------------------------------------------------------------------
# V7.0: Structural Filter & Claim Deduplication
# ---------------------------------------------------------------------------


def _normalize_claim_text(text: str) -> str:
    """Нормализация для детерминированной дедупликации claims."""
    text = text.lower()
    text = text.translate(str.maketrans("", "", string.punctuation + "«»—–\xa0"))
    tokens = [t for t in text.split() if t not in _STOP_WORDS_CLAIM and len(t) > 2]
    return " ".join(tokens)


# V7.0: Числовые показатели норм — если есть, claim НЕ структурный (RU + KK)
_NORMATIVE_UNITS_RE = re.compile(
    r"\b\d+[\s\xa0]*(?:мрп|аек|мзп|тәм|тенге|теңге|часов?|сағат|дней?|күн|күні|месяц|ай|лет|жыл|процент|пайыз|%)",
    re.I | re.U,
)


def _is_structural_claim(claim: DocumentClaim) -> bool:
    """V7.0: Определяет, является ли claim чисто структурным (не идёт в Auditor)."""
    text_lower = claim.claim_text.lower()

    # 0. Исключаем переходные/вступительные положения самого законопроекта (commencement clauses)
    commencement_markers = [
        "вводится в действие",
        "вступает в силу",
        "вступлении в силу",
        "по истечении шести месяцев со дня",
        "по истечении десяти календарных дней",
        "по истечении одного года со дня",
        "қолданысқа енгізіледі",
        "қолданысқа енгiзiледi",
        "жарияланған күнінен бастап",
        "алғашқы ресми жарияланған",
    ]
    if any(m in text_lower for m in commencement_markers):
        return True

    # 0.5. Конкретная инструкция-правка («дополнить/исключить/изложить … словами»)
    # — это ВЕРИФИЦИРУЕМАЯ поправка: аудитор должен проверить, что изменяемый текст
    # реально существует в действующей норме (а не мнимый misquote). Поэтому она
    # АНАЛИТИЧЕСКАЯ, не структурная. Раньше тут стоял `return True` (поправки вовсе
    # не проверялись — дыра в покрытии для доминирующего типа НПА). False-CONTRADICTED
    # гасят правила S5.5 (0.5/0.7), false-CONFIRM закрыт metadata-first фиксом (C1).
    # Зонтичное «внести изменения в …» (без конкретного глагола-правки) остаётся
    # структурным ниже (шаг 4) — это просто заголовок поправочного блока.
    amendment_verbs = [
        "дополнить", "исключить", "изложить", "после слов", "словами", "точку заменить",
        "толықтырылсын", "алып тасталсын", "ауыстырылсын", "сөздерінен кейін"
    ]
    if any(v in text_lower for v in amendment_verbs):
        return False

    # 1. Если есть модальные глаголы — всегда нормативное, не структурное
    if any(v in text_lower for v in _MODAL_VERBS):
        return False

    # 2. Если есть числовые показатели штрафов/сроков/МРП — нормативное
    if _NORMATIVE_UNITS_RE.search(text_lower):
        return False

    # 2.5 Если явно указано, что это фактическое или нормативное утверждение
    if claim.claim_type in (ClaimType.FACTUAL, ClaimType.NORMATIVE):
        return False

    # 3. Ссылки на законы (LEGAL_ID) — всегда структурные (просто упоминание)
    if claim.claim_type == ClaimType.LEGAL_ID:
        if claim.deterministic_verdict:
            return False
        return True

    # 3.5 Ссылки на статьи (LEGAL_REF): простые "ст. 207" — структурные,
    # но если есть описание содержания (например, "ст. 207 УК о лжепредпринимательстве") — это factual
    if claim.claim_type == ClaimType.LEGAL_REF:
        if claim.deterministic_verdict:
            return False
        # V9.6: если знаем целевой закон, простую ссылку на статью можно верифицировать
        if getattr(claim, "target_law_ids", None):
            return False
        if re.search(r'стать[яи]\s*\d+.*[а-яё\s]{10,}', text_lower):
            return False
        return True

    # 4. Простые констатации присутствия/упоминания/изменения структуры
    structural_phrases = (
        "присутствует", "существует", "указано в документе",
        "содержится в", "в документе упоминается", "статья изложена",
        "внести изменения в", "изложить в редакции",
    )
    if any(p in text_lower for p in structural_phrases):
        return True

    return False


# V9.6: Karaim-ный регекс для извлечения article reference из любого языка.
# Ловит "статья 47", "ст. 47-1", "47-бап", "47-баптың", "(47-1)" и т.д.
_ARTICLE_REF_RE = re.compile(
    r"(?:стать[яиеюй]\s*|ст\.?\s*|(?:^|\s))(\d{1,4}(?:[-]\d{1,3})?)\s*(?:-?\s*(?:бап|бабы|бабының|бапта|баптың|бабына))?",
    re.I | re.U,
)

# V9.6: Ключевые "юр.события" — для группировки claims разных языков об одной поправке
_SEMANTIC_EVENT_KEYWORDS = (
    # Каждый кортеж = (ключи поиска, нормализованный ID события)
    (("амнист", "рақымшылық"), "amnesty"),
    (("согласи", "келісім"), "consent"),
    (("ЭЦП", "цифрлық қолтаңба", "электронн", "электрондық"), "edsig"),
    (("реестр", "тізілім"), "registry"),
    (("прекращ", "тоқтат"), "termination"),
    (("освобожд", "босату"), "release"),
    (("исполнен", "орындал", "атқарушылық"), "enforcement"),
    (("штраф", "айыппұл"), "fine"),
    (("уведомл", "хабарлам"), "notification"),
    (("конституц"), "constitution"),
)


def _extract_article_ref_for_key(text: str) -> str | None:
    """Извлекает первую ссылку на статью (например '47' или '889-1') для нормализации ключа."""
    m = _ARTICLE_REF_RE.search(text)
    if m:
        return m.group(1).strip()
    return None


def _extract_semantic_events(text: str) -> list[str]:
    """Возвращает нормализованные ID юридических событий, упомянутых в claim."""
    text_lower = text.lower()
    events = []
    for keys, event_id in _SEMANTIC_EVENT_KEYWORDS:
        if isinstance(keys, str):
            keys = (keys,)
        if any(k in text_lower for k in keys):
            events.append(event_id)
    return sorted(set(events))


def _semantic_dedup_key(claim: DocumentClaim) -> str | None:
    """
    V9.6: Строит кросс-языковой ключ дедупликации.
    Возвращает None, если claim слишком общий (нет article + events).

    Идея: claims "В статью 61 добавляется абзац: согласие не требуется"
    и "61-бап ... келісімі талап етілмейді" → одинаковый ключ
    `art61|consent`.
    """
    text = (claim.claim_text + " " + claim.quote).lower()
    article = _extract_article_ref_for_key(text)
    events = _extract_semantic_events(text)
    if not article and not events:
        return None
    parts = []
    if article:
        parts.append(f"art{article}")
    if events:
        parts.append("|".join(events))
    return ":".join(parts)


def _dedup_claims(claims: list[DocumentClaim]) -> list[DocumentClaim]:
    """V7.0+V9.6: Детерминированная дедупликация по нормализованному тексту + кросс-языковая по семантическому ключу."""
    groups: dict[str, list[DocumentClaim]] = {}
    for c in claims:
        if c.entities and c.deterministic_verdict is not None:
            sorted_ents = sorted(str(e).strip().replace(" ", "").upper() for e in c.entities)
            key = f"regex_{c.claim_type.value}_{'_'.join(sorted_ents)}"
        else:
            # V9.6: сначала пробуем семантический ключ (кросс-язык),
            # потом fallback на нормализованный текст
            sem_key = _semantic_dedup_key(c)
            if sem_key:
                key = f"sem_{sem_key}"
            else:
                key = _normalize_claim_text(c.claim_text)
                if not key:
                    key = c.claim_text.lower()[:40]
        groups.setdefault(key, []).append(c)

    result: list[DocumentClaim] = []
    for group in groups.values():
        # Предпочитаем claim с большим контентом и непустым target_law_ids
        best = max(group, key=lambda c: (
            bool(c.deterministic_verdict),
            bool(getattr(c, "target_law_ids", None)),
            len(c.entities),
            len(c.claim_text),
        ))
        variants = [c.quote for c in group if c.quote and c.quote != best.quote]
        best.quote_variants = variants
        result.append(best)

    return result


# ---------------------------------------------------------------------------
# Детерминированный экстрактор
# ---------------------------------------------------------------------------


def _regex_extract(text: str) -> list[DocumentClaim]:
    """Извлекает claims регулярными выражениями — без LLM."""
    claims: list[DocumentClaim] = []
    seen_texts: set[str] = set()
    counter = 0

    lines = text.split("\n")

    for line in lines:
        for pattern, ctype, severity, tag in _PATTERNS:
            for m in pattern.finditer(line):
                entity = m.group(1).strip().replace("\u2011", "-").replace("\u2010", "-")
                entity = entity.replace("\u0406", "I").replace("\u0456", "i")
                if tag in ("enforcement_date", "effective_date"):
                    entity_clean = re.sub(r"\s+", " ", entity).strip()
                else:
                    entity_clean = re.sub(r"\s+", "", entity)

                claim_text = f"Документ утверждает: {tag}={entity_clean} (строка: «{line.strip()[:120]}»)"
                if claim_text in seen_texts:
                    continue
                seen_texts.add(claim_text)

                deterministic = _check_entity(tag, entity_clean, text)

                status, msg = None, None
                if deterministic:
                    status, msg = deterministic

                cid = f"claim_{counter:04d}"
                counter += 1
                claims.append(
                    DocumentClaim(
                        claim_id=cid,
                        claim_text=claim_text,
                        quote=line.strip()[:200],
                        claim_type=ctype,
                        severity=severity,
                        entities=[entity_clean],
                        deterministic_verdict=msg,
                        deterministic_status=status,
                    )
                )

    # --- Табличные штрафы ---
    table_mrp_re = re.compile(r"МРП", re.I)
    if table_mrp_re.search(text):
        for line in lines:
            if "|" not in line:
                continue
            cells = [c.strip() for c in line.split("|")]
            if len(cells) < 3:
                continue
            if cells[0] and not cells[0][0].isdigit():
                val_raw = re.sub(r"\s+", "", cells[1])
                if val_raw.isdigit():
                    val = int(val_raw)
                    entity_str = str(val)
                    claim_text = f"Документ утверждает: fine_mrp={val} (таблица: «{line.strip()[:120]}»)"
                    if claim_text in seen_texts:
                        continue
                    seen_texts.add(claim_text)
                    deterministic = _check_entity("fine_mrp", entity_str, text)
                    status, msg = (deterministic if deterministic else (None, None))
                    cid = f"claim_{counter:04d}"
                    counter += 1
                    claims.append(
                        DocumentClaim(
                            claim_id=cid,
                            claim_text=claim_text,
                            quote=line.strip()[:200],
                            claim_type=ClaimType.FINANCIAL,
                            severity=ClaimSeverity.CRITICAL,
                            entities=[entity_str],
                            deterministic_verdict=msg,
                            deterministic_status=status,
                        )
                    )

    return claims


def _check_entity(tag: str, entity: str, full_text: str) -> tuple[VerdictStatus, str] | None:
    """Детерминированная проверка entity по reference_data. Возвращает вердикт или None."""
    entity_normalized = entity.strip().upper().replace(" ", "").replace("\u0406", "I").replace("\u0456", "i")

    if tag == "law_id":
        law_id_clean = re.sub(r"ЗРК|зрк", "", entity_normalized).strip("-").strip()
        entry = check_law_id(law_id_clean)
        if entry is not None:
            if not entry["valid"]:
                return (VerdictStatus.CONTRADICTED, f"'{law_id_clean}' — {entry['title']}")
            return (VerdictStatus.CONFIRMED, f"{law_id_clean} = «{entry['title']}» от {entry['date']}")
        return None

    if tag == "koap_article":
        art = get_koap_article(entity_normalized)
        if art:
            return (VerdictStatus.CONFIRMED, f"КоАП ст.{entity_normalized}: «{art['title']}». Макс. штраф: {art['max_fine_mrp']} МРП (физлица), {art['max_fine_mrp_entity']} МРП (юрлица)")
        return (VerdictStatus.UNVERIFIED, f"Статья {entity_normalized} КоАП не найдена в реестре")

    if tag == "uk_article":
        art = get_uk_article(entity_normalized)
        if art:
            return (VerdictStatus.UNVERIFIED, f"УК РК ст.{entity_normalized} = «{art['title']}». {art['notes']}")
        return None

    if tag == "pprkz_num":
        pprkz_key = f"ППРК-{entity_normalized}"
        entry = LAW_REGISTRY.get(pprkz_key)
        if entry is not None:
            if not entry["valid"]:
                return (VerdictStatus.CONTRADICTED, f"{pprkz_key} — {entry['title']}")
            return (VerdictStatus.CONFIRMED, f"{pprkz_key} = «{entry['title']}»")
        return None

    if tag == "mrp_value":
        val_clean = re.sub(r"\s+", "", entity_normalized)
        try:
            val = int(val_clean)
        except ValueError:
            return None
        years_in_text = _YEAR_RE.findall(full_text)
        year = int(years_in_text[-1]) if years_in_text else 2025
        real_mrp = get_mrp(year)
        if real_mrp and val != real_mrp:
            return (VerdictStatus.CONTRADICTED, f"документ указывает МРП={val} тг, реальный МРП {year}={real_mrp} тг")
        if real_mrp and val == real_mrp:
            return (VerdictStatus.CONFIRMED, f"МРП {year} = {val} тг — верно")
        return None

    if tag == "fine_mrp":
        val_clean = re.sub(r"\s+", "", entity_normalized)
        try:
            val = int(val_clean)
        except ValueError:
            return None
        absolute_max = KOAP_MAX_FINES["absolute_max"]
        if val > absolute_max:
            return (VerdictStatus.CONTRADICTED,
                    f"Штраф {val} МРП превышает абсолютный максимум КоАП ({absolute_max} МРП). Требует проверки.")
        return None

    return None


# ---------------------------------------------------------------------------
# LLM-экстрактор для контекстных claims
# ---------------------------------------------------------------------------

_LLM_CLAIM_SCHEMA = """
Верни JSON-массив объектов:
[
  {
    "claim_id": "claim_XXXX",
    "claim_text": "Документ утверждает: ...",
    "quote": "Точная цитата из документа (до 150 символов)",
    "claim_type": "legal_id|legal_ref|financial|temporal|factual|normative",
    "severity": "critical|high|medium|low",
    "entities": ["конкретные сущности"]
  }
]
"""

_LLM_CLAIM_PROMPT = """
Ты — аудитор юридических документов. Прочитай документ и извлеки ВСЕ конкретные ПРОВЕРЯЕМЫЕ утверждения.

## Документ:
{document_text}

## Уже найденные утверждения (regex):
{already_found}

## Твоя задача:
Найди утверждения, которые НЕ попали в список выше. Особенно:

1. FACTUAL claims — конкретные факты о применении законов:
   - "Биометрическая идентификация станет обязательной с 2026 года" → factual, critical
   - "Локализация серверов обязательна для всех операторов" → factual, high

2. NORMATIVE claims — что закон/норма разрешает/запрещает/требует:
   - "Оператор обязан уведомить субъекта в течение X часов" → temporal, critical

3. ПОПРАВОЧНЫЕ ЗАКОНЫ ("О внесении изменений..."):
   - Если документ вносит изменения в действующий закон — извлекай СУТЬ вносимых изменений:
   - "Документ вводит новое основание для прекращения исполнительного производства (ст. 47 пп. 5-3)" → factual, high
   - "Документ освобождает должника от уплаты расходов ЧСИ при амнистии (ст. 118)" → normative, high
   - "Документ добавляет в ст. 821 КоАП постановление об освобождении от наказания по амнистии" → legal_ref, critical

4. НЕ извлекай:
   - Общие рассуждения: "Закон защищает права граждан"
   - Описания без конкретных значений
   - Дубли уже найденных утверждений

{schema}
"""


async def _llm_extract_chunk(
    text: str,
    already_found: list[DocumentClaim],
    client: AsyncOpenAI,
    settings,
    start_idx: int,
) -> list[DocumentClaim]:
    """LLM-экстрактор для отдельного окна/чанка текста."""
    already_str = "\n".join(f"- {c.claim_text[:120]}" for c in already_found[:30])
    prompt = _LLM_CLAIM_PROMPT.format(
        document_text=text,
        already_found=already_str or "(ничего)",
        schema=_LLM_CLAIM_SCHEMA,
    )

    system_msg = "JSON. Экстрактор утверждений из документа. Ключ 'claims'. Без рассуждений."
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": prompt},
    ]

    MAX_RETRIES = 2
    raw_claims: list = []
    for attempt in range(1, MAX_RETRIES + 1):
        # V9.6: На повторной попытке повышаем температуру чтобы получить другой ответ
        temperature = 0.0 if attempt == 1 else 0.4
        try:
            parsed = await cached_llm_call(
                client=client,
                model=settings.llm_model_extractor,
                messages=messages,
                settings=settings,
                ttl_seconds=None,
                max_tokens=3000,
                temperature=temperature,
            )
            if isinstance(parsed, list):
                raw_claims = parsed
            elif "_raw" in parsed and isinstance(parsed["_raw"], list):
                raw_claims = parsed["_raw"]
            else:
                raw_claims = parsed.get("claims", [])

            if raw_claims:
                break

            logger.warning(
                f"[S2.5/LLM] Attempt {attempt}/{MAX_RETRIES}: empty claims, retrying (temp={temperature})..."
            )
            # Инвалидируем кэш чтобы получить новый ответ.
            # H3: тот же build_prompt_key с ТЕКУЩИМИ temperature/max_tokens, что
            # ушли в cached_llm_call (иначе удалялся бы не тот ключ).
            from zerde.utils.cache import LLMCache
            from zerde.utils.llm_client import build_prompt_key
            llm_cache = LLMCache(settings.cache_db_path)
            prompt_key = build_prompt_key(messages, temperature=temperature, max_tokens=3000)
            cache_key = LLMCache._make_key(settings.llm_model_extractor, prompt_key)
            await llm_cache._delete(cache_key)
        except Exception as e:
            logger.warning(f"[S2.5/LLM] Failed attempt {attempt}/{MAX_RETRIES}: {e}")
            if attempt == MAX_RETRIES:
                return []

    result: list[DocumentClaim] = []
    for i, raw in enumerate(raw_claims):
        if isinstance(raw, str) and len(raw) > 10:
            raw = {"claim_text": raw, "claim_type": "factual", "severity": "medium"}
        if not isinstance(raw, dict):
            continue
        try:
            # Всегда используем детерминированный cid и игнорируем raw["claim_id"]:
            # модель иногда отдаёт свою нумерацию ("claim_007"), которая сталкивается
            # с нашей зеро-паддинговой ("claim_0007") и ломает сопоставление в S6/S7.
            cid = f"claim_{start_idx + i:04d}"
            claim = DocumentClaim(
                claim_id=cid,
                claim_text=raw.get("claim_text", ""),
                quote=raw.get("quote", "")[:200],
                claim_type=ClaimType(raw.get("claim_type", "factual")),
                severity=ClaimSeverity(raw.get("severity", "medium")),
                entities=raw.get("entities", []),
                deterministic_verdict=None,
            )
            if claim.claim_text:
                result.append(claim)
        except (ValueError, KeyError) as e:
            logger.debug(f"[S2.5/LLM] Skip malformed claim: {e}")

    return result


async def _llm_extract(
    text: str,
    already_found: list[DocumentClaim],
    client: AsyncOpenAI,
    settings,
) -> list[DocumentClaim]:
    """LLM-экстрактор для контекстных утверждений, использующий скользящее окно (sliding window)."""
    window_size = 8000
    overlap = 1500

    if len(text) <= window_size:
        return await _llm_extract_chunk(text, already_found, client, settings, start_idx=len(already_found))

    chunks = []
    start = 0
    while start < len(text):
        end = start + window_size
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start += window_size - overlap

    logger.info(f"[S2.5/SlidingWindow] Splitting text of length {len(text)} into {len(chunks)} windows.")

    all_contextual_claims = []
    current_already_found = list(already_found)

    for idx, chunk_text in enumerate(chunks):
        logger.info(f"[S2.5/SlidingWindow] Processing window {idx+1}/{len(chunks)}")
        window_claims = await _llm_extract_chunk(
            chunk_text,
            current_already_found,
            client,
            settings,
            start_idx=len(already_found) + len(all_contextual_claims)
        )
        all_contextual_claims.extend(window_claims)
        current_already_found.extend(window_claims)

    return all_contextual_claims


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def extract_claims(doc_state: DocumentState) -> ClaimExtractionResult:
    """
    Stage 2.5: Извлекает все проверяемые утверждения из документа.
    """
    settings = get_settings()
    client = make_llm_client(settings)
    text = doc_state.normalized_text

    logger.info(f"[S2.5] Claim extraction start. doc_id={doc_state.doc_id[:8]}…")

    # V9.6: Определяем законы-мишени документа (для поправочных актов)
    target_law_ids = _detect_target_laws(text)
    if target_law_ids:
        logger.info(f"[S2.5] Detected target laws: {target_law_ids}")

    # Шаг 1: Детерминированный regex
    regex_claims = _regex_extract(text)
    logger.info(f"[S2.5] Regex extracted {len(regex_claims)} claims")

    # Шаг 2: LLM для контекстных утверждений
    llm_claims = await _llm_extract(text, regex_claims, client, settings)
    logger.info(f"[S2.5] LLM extracted {len(llm_claims)} additional claims")

    all_claims = regex_claims + llm_claims

    # V9.6: Проставляем target_law_ids во все claims (чтобы S6 знал контекст закона)
    if target_law_ids:
        for c in all_claims:
            if not c.target_law_ids:
                c.target_law_ids = target_law_ids

    # V9.6: Фильтр мусорных meta-claims
    before_meta = len(all_claims)
    all_claims = [c for c in all_claims if not _is_meta_claim(c)]
    if before_meta != len(all_claims):
        logger.info(f"[S2.5] Meta-filter: dropped {before_meta - len(all_claims)} trivial claims")

    # V7.0+V9.6: Детерминированная дедупликация (включая кросс-языковую)
    deduped = _dedup_claims(all_claims)
    logger.info(f"[S2.5] Dedup: {len(all_claims)} → {len(deduped)} unique claims")

    # V7.0: Разделение на аналитические и структурные claims
    analytical: list[DocumentClaim] = []
    structural: list[DocumentClaim] = []
    for c in deduped:
        if _is_structural_claim(c):
            c.is_structural = True
            structural.append(c)
        else:
            analytical.append(c)

    logger.info(
        f"[S2.5] Split: analytical={len(analytical)} structural={len(structural)}"
    )

    result = ClaimExtractionResult(
        doc_id=doc_state.doc_id,
        claims=analytical,
        structural_claims=structural,
    )

    return result
