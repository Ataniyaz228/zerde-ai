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
    "документ", "утверждает", "строка", "таблица", "закон", "кодекс", "статья",
    "ст", "стат", "номер", "присутствует", "существует",
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

    # 1. Если есть модальные глаголы — всегда нормативное, не структурное
    if any(v in text_lower for v in _MODAL_VERBS):
        return False

    # 2. Если есть числовые показатели штрафов/сроков/МРП — нормативное
    if _NORMATIVE_UNITS_RE.search(text_lower):
        return False

    # 3. Ссылки на статьи/законы без модальных глаголов и без числовых норм — структурные
    if claim.claim_type in (ClaimType.LEGAL_REF, ClaimType.LEGAL_ID):
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


def _dedup_claims(claims: list[DocumentClaim]) -> list[DocumentClaim]:
    """V7.0: Детерминированная дедупликация по нормализованному тексту."""
    groups: dict[str, list[DocumentClaim]] = {}
    for c in claims:
        if c.entities:
            sorted_ents = sorted(str(e).strip().replace(" ", "").upper() for e in c.entities)
            key = f"regex_{c.claim_type.value}_{'_'.join(sorted_ents)}"
        else:
            key = _normalize_claim_text(c.claim_text)
            if not key:
                key = c.claim_text.lower()[:40]
        groups.setdefault(key, []).append(c)

    result: list[DocumentClaim] = []
    for group in groups.values():
        best = max(group, key=lambda c: (
            bool(c.deterministic_verdict),
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

3. НЕ извлекай:
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

    try:
        parsed = await cached_llm_call(
            client=client,
            model=settings.llm_model_extractor,
            messages=messages,
            settings=settings,
            ttl_seconds=None,
            max_tokens=3000,
        )
        if isinstance(parsed, list):
            raw_claims = parsed
        elif "_raw" in parsed and isinstance(parsed["_raw"], list):
            raw_claims = parsed["_raw"]
        else:
            raw_claims = parsed.get("claims", [])
    except Exception as e:
        logger.warning(f"[S2.5/LLM] Failed to extract contextual claims from window: {e}")
        return []

    result: list[DocumentClaim] = []
    for i, raw in enumerate(raw_claims):
        if isinstance(raw, str) and len(raw) > 10:
            raw = {"claim_text": raw, "claim_type": "factual", "severity": "medium"}
        if not isinstance(raw, dict):
            continue
        try:
            cid = f"claim_{start_idx + i:04d}"
            claim = DocumentClaim(
                claim_id=raw.get("claim_id", cid),
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

    # Шаг 1: Детерминированный regex
    regex_claims = _regex_extract(text)
    logger.info(f"[S2.5] Regex extracted {len(regex_claims)} claims")

    # Шаг 2: LLM для контекстных утверждений
    llm_claims = await _llm_extract(text, regex_claims, client, settings)
    logger.info(f"[S2.5] LLM extracted {len(llm_claims)} additional claims")

    all_claims = regex_claims + llm_claims

    # V7.0: Детерминированная дедупликация
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
