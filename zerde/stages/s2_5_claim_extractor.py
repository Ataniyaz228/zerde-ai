"""
Stage 2.5: Claim Extractor
Вход:  DocumentState
Выход: ClaimExtractionResult

Гибридный подход:
  1. Детерминированный regex-этап — ловит числовые/ссылочные claims без LLM
  2. LLM-этап — ловит контекстные утверждения (биометрия обязательна с 2026)
  3. Reference Injector — проверяет entities по reference_data.py без LLM

Ключевой принцип: claim ≠ факт. Claim — это утверждение из документа.
Аналитик потом проверит каждый claim против корпуса.
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
    # Номера законов: "Закон № 94-V", "№ 87-IV ЗРК", "Закона РК от ... № 235-VII"
    # НЕ ловим номера выпусков: "№ 13-14, ст. 205" (Ведомости Парламента)
    (re.compile(r"(?:Закон\w*|Кодекс\w*|ЗРК)\s+[^№]*?№\s*(\d{2,4}[-‐–]\w{1,5}(?:\s*ЗРК)?)", re.I | re.U), ClaimType.LEGAL_ID, ClaimSeverity.CRITICAL, "law_id"),
    (re.compile(r"№\s*(\d{2,4}[-‐–][IVXivx\u0406\u0456]{1,5}(?:\s*ЗРК)?)\b", re.I), ClaimType.LEGAL_ID, ClaimSeverity.CRITICAL, "law_id"),
    # Ссылки на статьи КоАП: ст. 79 КоАП, статье 640 КоАП
    (re.compile(r"стать[яиею]\s*(\d+(?:[-.]?\d+)?)\s*КоАП", re.I | re.U), ClaimType.LEGAL_REF, ClaimSeverity.CRITICAL, "koap_article"),
    # Ссылки на статьи УК: "статьей 207 Уголовного кодекса", "ст. 207 УК"
    (re.compile(r"стать[яиеюй]\w?\s+(\d+(?:[-.]?\d+)?)\s+(?:Уголовн\w+\s+[Кк]одекс\w*|УК)", re.I | re.U), ClaimType.LEGAL_REF, ClaimSeverity.CRITICAL, "uk_article"),
    # Ссылки на статьи любого Кодекса: "в статье 196", "статью 105 изложить"
    (re.compile(r"(?:в\s+)?стать[яиеюй]\w?\s+(\d+(?:[-.]?\d+)?)\b(?!\s*КоАП|\s*УК|\s*,\s*ст)", re.I | re.U), ClaimType.LEGAL_REF, ClaimSeverity.HIGH, "article_ref"),
    # Ссылки на ППРК: Постановление Правительства №142, ППРК №909
    (re.compile(r"(?:ППРК|Постановлени[яею]\s+Правительства)[^№]*№?\s*(\d+)", re.I | re.U), ClaimType.LEGAL_REF, ClaimSeverity.CRITICAL, "pprkz_num"),
    # Штрафы в МРП: 500 МРП, "10 000 (десяти тысяч) МРП"
    (re.compile(r"(\d[\d\s]*)\s*(?:\([^)]*\)\s*)?МРП", re.I | re.U), ClaimType.FINANCIAL, ClaimSeverity.CRITICAL, "fine_mrp"),
    # Размер МРП в тенге: МРП составляет 3 450 тенге
    (re.compile(r"МРП[^.]*?(\d[\d\s]+)\s*тенге", re.I | re.U), ClaimType.FINANCIAL, ClaimSeverity.HIGH, "mrp_value"),
    # Сроки уведомлений в часах: "не позднее 24 (двадцати четырех) часов", "в течение 72 часов"
    (re.compile(r"(?:в\s*течени[ие]|не\s*позднее)\s*(\d+)\s*(?:\([^)]*\)\s*)?час", re.I | re.U), ClaimType.TEMPORAL, ClaimSeverity.HIGH, "hours_deadline"),
    # Сроки в рабочих днях: в течение 10 рабочих дней
    (re.compile(r"в\s*течени[ие]\s*(\d+)\s*рабочих\s*дн", re.I | re.U), ClaimType.TEMPORAL, ClaimSeverity.HIGH, "workdays_deadline"),
    # Даты вступления в силу: с 1 января 2025 года, с 1 июля 2026
    (re.compile(r"с\s+(\d{1,2}\s+\w+\s+\d{4})\s*года", re.I | re.U), ClaimType.TEMPORAL, ClaimSeverity.HIGH, "effective_date"),
    # Вводится в действие: "по истечении шести месяцев со дня", "по истечении десяти дней"
    (re.compile(r"вводится\s+в\s+действие\s+(.*?(?:дня|после))", re.I | re.U), ClaimType.TEMPORAL, ClaimSeverity.HIGH, "enforcement_date"),
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

# V7.0: Модальные глаголы — если есть, claim НЕ структурный
_MODAL_VERBS = frozenset([
    "обязан", "должен", "запрещается", "влечет", "устанавливается",
    "предусмотрено", "установлено", "нарушение", "ответственность", "штраф",
    "вводится", "приостанавливается", "прекращается", "возобновляется",
    "подлежит", "является", "несет", "вправе", "может", "должны",
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


# V7.0: Числовые показатели норм — если есть, claim НЕ структурный
_NORMATIVE_UNITS_RE = re.compile(
    r"\b\d+[\s\xa0]*(?:мрп|мзп|тенге|часов?|дней?|месяц|лет|процент|%)",
    re.I,
)


def _is_structural_claim(claim: DocumentClaim) -> bool:
    """V7.0: Определяет, является ли claim чисто структурным (не идёт в Auditor).

    Structural = нет модальных глаголов И нет числовых норм (штрафов/сроков/МРП).
    Severity не участвует в решении (regex-claims тоже могут быть structural).
    """
    text_lower = claim.claim_text.lower()

    # 1. Если есть модальные глаголы — всегда нормативное, не структурное
    if any(v in text_lower for v in _MODAL_VERBS):
        return False

    # 2. Если есть числовые показатели штрафов/сроков/МРП — нормативное
    #    (Исключение: просто номер закона/статьи без единиц измерения)
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
            # Group regex/entity claims using their type and sorted normalized entities
            sorted_ents = sorted(str(e).strip().replace(" ", "").upper() for e in c.entities)
            key = f"regex_{c.claim_type.value}_{'_'.join(sorted_ents)}"
        else:
            key = _normalize_claim_text(c.claim_text)
            if not key:
                key = c.claim_text.lower()[:40]
        groups.setdefault(key, []).append(c)

    result: list[DocumentClaim] = []
    for group in groups.values():
        # Выбираем "лучший": с deterministic_verdict > с entities > длиннее текст
        best = max(group, key=lambda c: (
            bool(c.deterministic_verdict),
            len(c.entities),
            len(c.claim_text),
        ))
        # Собираем альтернативные цитаты
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
                # Нормализуем пробелы в числах
                entity_clean = re.sub(r"\s+", "", entity)

                claim_text = f"Документ утверждает: {tag}={entity_clean} (строка: «{line.strip()[:120]}»)"
                if claim_text in seen_texts:
                    continue
                seen_texts.add(claim_text)

                # Детерминированная проверка по реестру
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

    # --- Табличные штрафы: "МСБ | 500 | 3 450 | ..." ---
    # Ищем строки с pipe-разделителями, где одна из колонок — число (штраф)
    table_mrp_re = re.compile(r"МРП", re.I)
    if table_mrp_re.search(text):
        for line in lines:
            if "|" not in line:
                continue
            cells = [c.strip() for c in line.split("|")]
            if len(cells) < 3:
                continue
            # Первая ячейка — название категории (не число)
            if cells[0] and not cells[0][0].isdigit():
                # Вторая ячейка — размер штрафа в МРП
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
        # Убираем "ЗРК" и пробелы
        law_id_clean = re.sub(r"ЗРК|зрк", "", entity_normalized).strip("-").strip()
        entry = check_law_id(law_id_clean)
        if entry is not None:
            if not entry["valid"]:
                return (VerdictStatus.CONTRADICTED, f"'{law_id_clean}' — {entry['title']}")
            return (VerdictStatus.CONFIRMED, f"{law_id_clean} = «{entry['title']}» от {entry['date']}")
        return None  # Неизвестный закон — отдаём LLM

    if tag == "koap_article":
        art = get_koap_article(entity_normalized)
        if art:
            return (VerdictStatus.CONFIRMED, f"КоАП ст.{entity_normalized}: «{art['title']}». Макс. штраф: {art['max_fine_mrp']} МРП (физлица), {art['max_fine_mrp_entity']} МРП (юрлица)")
        return (VerdictStatus.UNVERIFIED, f"Статья {entity_normalized} КоАП не найдена в реестре")

    if tag == "uk_article":
        art = get_uk_article(entity_normalized)
        if art:
            # Возвращаем информацию о статье — LLM решит, соответствует ли контексту
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
        # Определяем год из контекста
        years_in_text = _YEAR_RE.findall(full_text)
        year = int(years_in_text[-1]) if years_in_text else 2025
        real_mrp = get_mrp(year)
        if real_mrp and val != real_mrp:
            return (VerdictStatus.CONTRADICTED, f"документ указывает МРП={val} тг, реальный МРП {year}={real_mrp} тг")
        if real_mrp and val == real_mrp:
            return (VerdictStatus.CONFIRMED, f"МРП {year} = {val} тг — верно")
        return None

    if tag == "hours_deadline":
        # Не делаем детерминированный вердикт — LLM проверит по корпусу
        # (24ч, 72ч и другие сроки зависят от конкретного закона)
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
        # Если штраф в пределах максимума — нужен контекст LLM
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
   - "Закон РК №[X] утрачивает силу с [дата]" → temporal, high

2. NORMATIVE claims — что закон/норма разрешает/запрещает/требует:
   - "Оператор обязан уведомить субъекта в течение X часов" → temporal, critical
   - "Штраф составляет X МРП за нарушение Y" → financial, critical

3. НЕ извлекай:
   - Общие рассуждения: "Закон защищает права граждан"
   - Описания без конкретных значений: "Предусмотрена ответственность"
   - Дубли уже найденных утверждений

{schema}
"""


async def _llm_extract(
    text: str,
    already_found: list[DocumentClaim],
    client: AsyncOpenAI,
    settings,
) -> list[DocumentClaim]:
    """LLM-экстрактор для контекстных утверждений которые не поймал regex. Кэшируется."""

    already_str = "\n".join(f"- {c.claim_text[:100]}" for c in already_found[:20])
    prompt = _LLM_CLAIM_PROMPT.format(
        document_text=text[:32000],  # deepseek-v4-flash поддерживает большой контекст
        already_found=already_str or "(ничего)",
        schema=_LLM_CLAIM_SCHEMA,
    )

    system_msg = "JSON. Экстрактор утверждений из документа. Ключ 'claims'. Без рассуждений."
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": prompt},
    ]

    try:
        # TTL=None: один документ = одинаковые claims, постоянное кэширование
        parsed = await cached_llm_call(
            client=client,
            model=settings.llm_model_extractor,
            messages=messages,
            settings=settings,
            ttl_seconds=None,
            max_tokens=3000,
        )
        # Поддерживаем как массив напрямую так и {"claims": [...]} и {"_raw": [...]}
        if isinstance(parsed, list):
            raw_claims = parsed
        elif "_raw" in parsed and isinstance(parsed["_raw"], list):
            raw_claims = parsed["_raw"]
        else:
            raw_claims = parsed.get("claims", [])
    except Exception as e:
        logger.warning(f"[S2.5/LLM] Failed to extract contextual claims: {e}")
        return []

    # Маппинг в модели
    result: list[DocumentClaim] = []
    start_idx = len(already_found)
    for i, raw in enumerate(raw_claims):
        # LLM может вернуть строки вместо объектов — конвертируем
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def extract_claims(doc_state: DocumentState) -> ClaimExtractionResult:
    """
    Stage 2.5: Извлекает все проверяемые утверждения из документа.

    Гибридный подход:
    1. Regex → детерминированные claims + reference_data вердикты
    2. LLM → контекстные claims (биометрия, локализация, etc.)
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

    # V7.0: Детерминированная дедупликация по нормализованному тексту
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

    # Логируем критические (только аналитические)
    critical = result.critical_claims
    logger.info(
        f"[S2.5] Done. total={result.total_count} critical={len(critical)}"
    )
    for c in critical:
        verdict_str = f" → {c.deterministic_verdict}" if c.deterministic_verdict else ""
        logger.info(f"[S2.5/CRITICAL] {c.claim_id}: {c.claim_text[:100]}{verdict_str}")

    return result
