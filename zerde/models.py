"""
Pydantic Models (Core Data Contracts)
Все обмены между этапами СТРОГО через эти модели.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from enum import IntEnum, StrEnum
from typing import Literal

from pydantic import BaseModel, Field, computed_field, model_validator

# ---------------------------------------------------------------------------
# 1. ENUMS & CONSTANTS
# ---------------------------------------------------------------------------


class LegalRank(IntEnum):
    """§1.1 — Иерархия источников КЗ (чем меньше — тем выше авторитет)."""

    INTERNATIONAL_TREATY = 1
    CODE = 2
    CONSTITUTIONAL_LAW = 3
    LAW_RK = 4
    PRESIDENTIAL_DECREE = 5
    GOVERNMENT_RESOLUTION = 6  # ППРК
    MINISTERIAL_ORDER = 7
    SC_PROSECUTORS_CLARIFICATION = 8
    SC_CASE_LAW = 9
    EXPERT_ANALYTICS = 10  # Tier 2/3 Web
    MEDIA_UNKNOWN = 11


class WebTier(StrEnum):
    """§1.2 — Категоризация Web-источников по домену."""

    TIER_1 = "TIER_1"  # Gov: gov.kz, adilet.zan.kz, supreme.kz, primeminister.kz
    TIER_2 = "TIER_2"  # Expert/Media: zakon.kz, tengrinews.kz, forbes.kz, profit.kz, vlast.kz
    TIER_3 = "TIER_3"  # Blogs: linkedin.com, vc.ru, medium.com
    BLACKLIST = "BLACKLIST"  # forum, otvet.mail.ru, anon, reddit.com


class ConflictType(StrEnum):
    """§1.3 — Типы юридических конфликтов."""

    HIERARCHY = "HIERARCHY"  # Разница legal_rank > 2 в одном домене
    TEMPORAL = "TEMPORAL"  # Совпадение law_id+article, разные effective_date
    FACTUAL = "FACTUAL"  # Расхождение чисел/дат (regex)
    ENFORCEMENT_GAP = "ENFORCEMENT_GAP"  # Норма vs. реальная практика


class ValidationStatus(StrEnum):
    """§6 — Статусы аудита фактов."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNVERIFIED = "UNVERIFIED"


class DocumentFormat(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"


class AdiletFallbackStrategy(StrEnum):
    XHR = "XHR"
    CSS_SELECTOR = "CSS_SELECTOR"
    PDF_OCR = "PDF_OCR"
    LOCAL_CACHE = "LOCAL_CACHE"


# ---------------------------------------------------------------------------
# 2. ЭТАП 1 — DocumentState
# ---------------------------------------------------------------------------


class DocumentState(BaseModel):
    """Выход Этапа 1: нормализованный документ."""

    # Идентификация
    doc_id: str = Field(description="SHA256 от сырого контента")
    original_path: str = Field(description="Путь к исходному файлу")
    format: DocumentFormat

    # Контент
    raw_text: str = Field(description="Оригинальный текст до нормализации")
    normalized_text: str = Field(description="Текст после KZ-транслитерации кириллицей")

    # Метаданные
    char_count: int = Field(ge=0)
    language_detected: Literal["ru", "kk", "en", "mixed"] = "ru"
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @computed_field  # type: ignore[misc]
    @property
    def word_count(self) -> int:
        return len(self.normalized_text.split())


# ---------------------------------------------------------------------------
# 3. ЭТАП 2 — QueryPlan
# ---------------------------------------------------------------------------


class AdiletQuery(BaseModel):
    """Запрос к базе данных Адилет."""

    query_text: str
    law_ids: list[str] = Field(default_factory=list, description="Идентификаторы НПА (напр. '550-IV')")
    articles: list[str] = Field(default_factory=list, description="Статьи для поиска (напр. '15', '15-1')")
    date_from: date | None = None
    date_to: date | None = None


class WebQuery(BaseModel):
    """Запрос к Web (через Tavily)."""

    query_text: str
    language: Literal["ru", "kk", "en"]
    include_domains: list[str] = Field(default_factory=list)
    exclude_tiers: list[WebTier] = Field(default_factory=lambda: [WebTier.BLACKLIST])
    max_results: int = Field(default=10, ge=1, le=50)


class QueryPlan(BaseModel):
    """Выход Этапа 2: план сбора данных."""

    plan_id: str = Field(description="SHA256 от normalized_text документа")
    source_doc_id: str

    # Запросы
    adilet_queries: list[AdiletQuery] = Field(default_factory=list)
    web_queries_ru: list[WebQuery] = Field(default_factory=list)
    web_queries_kk: list[WebQuery] = Field(default_factory=list)
    web_queries_en: list[WebQuery] = Field(default_factory=list)

    # Ожидаемые элементы анализа
    expected_elements: list[str] = Field(
        default_factory=list,
        description="Что LLM ожидает найти (напр. ['нормы о ЕСП', 'ставки НДС'])",
    )
    bylaw_triggers: list[str] = Field(
        default_factory=list,
        description="Подзаконные акты, которые могут быть упомянуты",
    )

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @computed_field  # type: ignore[misc]
    @property
    def total_queries(self) -> int:
        return (
            len(self.adilet_queries)
            + len(self.web_queries_ru)
            + len(self.web_queries_kk)
            + len(self.web_queries_en)
        )


# ---------------------------------------------------------------------------
# 3.5. ЭТАП 2.5 — DocumentClaim (Claim Extractor)
# ---------------------------------------------------------------------------


class ClaimType(StrEnum):
    """Тип проверяемого утверждения."""
    LEGAL_ID = "legal_id"         # Номер закона, кодекса (87-IV, 94-V)
    LEGAL_REF = "legal_ref"       # Ссылка на статью (ст. 207 УК, ст. 79 КоАП)
    FINANCIAL = "financial"       # Числа, штрафы, МРП, суммы
    TEMPORAL = "temporal"         # Сроки, даты вступления, сроки уведомлений
    FACTUAL = "factual"           # Фактические утверждения (биометрия обязательна с 2026)
    NORMATIVE = "normative"       # Утверждения о нормах (закон запрещает X)


class VerdictStatus(StrEnum):
    CONFIRMED = "CONFIRMED"         # Утверждение подтверждено источниками
    CONTRADICTED = "CONTRADICTED"   # Утверждение опровергнуто источниками
    UNVERIFIED = "UNVERIFIED"       # Нет данных в корпусе для проверки


class ClaimSeverity(StrEnum):
    """Приоритет проверки: чем критичнее — тем выше угроза легализовать ошибку."""
    CRITICAL = "critical"   # Номера законов, статьи УК/КоАП, размеры штрафов
    HIGH = "high"           # Даты, сроки уведомлений, МРП
    MEDIUM = "medium"       # Фактические утверждения, нормы
    LOW = "low"             # Общие описания, контекст


class DocumentClaim(BaseModel):
    """Одно проверяемое утверждение, извлечённое из входного документа."""

    claim_id: str = Field(description="Уникальный ID утверждения (claim_0001 и т.д.)")
    claim_text: str = Field(description="Точная формулировка утверждения")
    quote: str = Field(default="", description="Прямая цитата из документа")
    claim_type: ClaimType
    severity: ClaimSeverity
    entities: list[str] = Field(
        default_factory=list,
        description="Конкретные сущности (87-IV, 3450, ст.207, 500 МРП)",
    )
    # Заполняется детерминированно из reference_data.py (без LLM)
    deterministic_verdict: str | None = Field(
        default=None,
        description="Вердикт из реестра НПА без LLM (например: '87-IV INVALID → правильный 94-V')",
    )
    deterministic_status: VerdictStatus | None = Field(
        default=None,
        description="Точный статус (CONFIRMED/CONTRADICTED) для детерминированных проверок",
    )

    # V7.0: Структурные claims ("Статья N присутствует") — не идут в Auditor
    is_structural: bool = Field(
        default=False,
        description="True если claim не несёт аналитической ценности (только структура)",
    )
    # V7.0: Дедупликация — храним альтернативные цитаты одного и того же claim
    quote_variants: list[str] = Field(
        default_factory=list,
        description="Альтернативные цитаты из документа (при дедупликации)",
    )
    # V9.6: Законы, которые изменяет/дополняет данный документ (поправочный акт).
    # Используется S6 для привязки claim → корпус при отсутствии явной ссылки на закон.
    target_law_ids: list[str] = Field(
        default_factory=list,
        description="ID законов-мишеней поправочного акта (напр. ['261-IV', '235-V'])",
    )


class ClaimVerdict(BaseModel):
    """Результат верификации одного утверждения аналитиком."""

    claim_id: str
    status: VerdictStatus
    source_ids: list[str] = Field(default_factory=list, description="chunk_id доказательств")
    found_value: str | None = Field(
        default=None,
        description="Что реально найдено в источниках (напр: '94-V', '3932 тенге')",
    )
    document_value: str | None = Field(
        default=None,
        description="Что утверждает документ (напр: '87-IV', '3450 тенге')",
    )
    contradiction_detail: str | None = Field(
        default=None,
        description="Подробное описание противоречия",
    )
    confidence: Literal["HIGH", "MEDIUM", "LOW"] = "MEDIUM"
    severity: ClaimSeverity = ClaimSeverity.MEDIUM
    is_deterministic: bool = Field(
        default=False,
        description="True если вердикт вынесен из reference_data без LLM",
    )


class ClaimExtractionResult(BaseModel):
    """Выход Stage 2.5/2.6: список извлечённых утверждений + структурный чеклист."""

    doc_id: str
    claims: list[DocumentClaim] = Field(
        default_factory=list,
        description="Аналитические claims (идут в Auditor)",
    )
    structural_claims: list[DocumentClaim] = Field(
        default_factory=list,
        description="Структурные claims (не идут в Auditor, рендерятся отдельно)",
    )
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @computed_field  # type: ignore[misc]
    @property
    def critical_claims(self) -> list[DocumentClaim]:
        return [c for c in self.claims if c.severity == ClaimSeverity.CRITICAL]

    @computed_field  # type: ignore[misc]
    @property
    def total_count(self) -> int:
        return len(self.claims) + len(self.structural_claims)


# ---------------------------------------------------------------------------
# 4. ЭТАП 3 — EvidenceChunk
# ---------------------------------------------------------------------------


class EvidenceChunk(BaseModel):
    """Один фрагмент доказательной базы (1 статья или 1 web-источник)."""

    # Идентификация
    chunk_id: str = Field(description="SHA256 от контента")
    source_url: str
    source_title: str

    # Контент
    content: str = Field(description="Текст статьи или фрагмента")
    content_summary: str = Field(default="", description="Краткое резюме для промпта")
    search_provider: str | None = Field(default=None, description="Провайдер поиска (tavily, serper, google, duckduckgo, local)")

    # Правовая атрибуция
    legal_rank: LegalRank
    web_tier: WebTier | None = Field(default=None, description="Только для Web-источников")
    inferred_rank: LegalRank | None = Field(default=None, description="Inferred rank from content multi-signal scorer")
    inferred_rank_confidence: float | None = Field(default=None, description="Confidence of inferred rank [0.0, 1.0]")
    inference_reason: str | None = Field(default=None, description="Explanation of why this rank was inferred")

    # Атрибуты НПА (только для Adilet-источников)
    law_id: str | None = None
    law_title: str | None = None
    article: str | None = None  # Номер статьи
    paragraph: str | None = None
    effective_date: date | None = None
    expiry_date: date | None = None

    # [NEW] Поля метаданных v7.1
    language: str | None = None        # "ru" или "kk"
    source_version: str | None = None  # Дата редакции НПА (например, "2026-03-11")
    ingest_date: str | None = None     # Дата импорта/индексации (ISO-8601)

    # Технические поля
    adilet_fallback_used: AdiletFallbackStrategy | None = None
    embedding: list[float] | None = Field(default=None, exclude=True, description="Вектор для Cosine dedup")

    # Флаги Этапа 4 (заполняются на Fusion)
    is_duplicate: bool = False
    is_conflict: bool = False
    conflict_types: list[ConflictType] = Field(default_factory=list)
    conflict_with_ids: list[str] = Field(default_factory=list, description="chunk_id конфликтующих чанков")

    gathered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def compute_chunk_id(self) -> EvidenceChunk:
        if not self.chunk_id:
            self.chunk_id = hashlib.sha256(self.content.encode()).hexdigest()
        return self

    @computed_field  # type: ignore[misc]
    @property
    def is_authoritative(self) -> bool:
        """True если источник высокого доверия (rank <= 6)."""
        return self.legal_rank <= LegalRank.GOVERNMENT_RESOLUTION


# ---------------------------------------------------------------------------
# 5. ЭТАП 5 — AnalysisJSON (LLM Core Output)
# ---------------------------------------------------------------------------


class Fact(BaseModel):
    """§1.4 — Утверждение с привязкой к источникам."""

    fact_id: str
    claim_id: str | None = Field(default=None, description="Связанный ID утверждения (claim_id)")
    claim: str = Field(description="Формулировка утверждения")
    source_ids: list[str] = Field(
        min_length=1,
        description="chunk_id источников. Обязателен минимум один.",
    )
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)

    # Заполняется Аудитором (Этап 6)
    validation_status: ValidationStatus = ValidationStatus.UNVERIFIED
    bm25_score: float | None = None


class Conclusion(BaseModel):
    """Логический вывод из фактов."""

    conclusion_id: str
    statement: str
    reasoning_type: Literal["deduction", "analogy", "induction"]
    supporting_fact_ids: list[str] = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    validation_status: ValidationStatus = ValidationStatus.UNVERIFIED


class NegativeSpaceItem(BaseModel):
    """Пробел в регулировании."""

    item_id: str
    description: str
    gap_type: Literal["regulatory_hole", "intentional_silence", "delegation_gap"]
    affected_domain: str
    source_ids: list[str] = Field(default_factory=list)


class NormativeAssessment(BaseModel):
    """Нормативная оценка влияния нормы."""

    assessment_id: str
    norm_description: str
    economic_impact: str | None = None
    social_impact: str | None = None
    risk_level: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    source_ids: list[str] = Field(min_length=1)


class AffectedParty(BaseModel):
    name: str
    role: Literal["beneficiary", "obligated", "regulator", "third_party"]
    description: str


class ConflictRecord(BaseModel):
    """V7.0: Bridge-конфликт из Auditor (S5/S6) → Renderer (S7).

    Любой вердикт CONTRADICTED порождает ConflictRecord для единой
    секции 'Выявленные конфликты и коллизии' в отчёте.
    """

    record_id: str
    conflict_type: ConflictType
    claim_id: str
    claim_text: str
    document_value: str | None = None
    found_value: str | None = None
    detail: str
    severity: ClaimSeverity


class AnalysisStats(BaseModel):
    """Иммутабельное агрегированное состояние статистики аудита (v9.4)."""

    n_total: int
    n_confirmed: int
    n_contradicted: int
    n_unverified: int
    n_critical_contradicted: int
    n_high_contradicted: int
    n_unverified_risks: int
    n_real_confirmed: int
    reliability: float | None
    pros: list[str]
    recommendation: str


class AnalysisJSON(BaseModel):
    """Выход Этапа 5: полная аналитическая структура."""

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_fields(cls, data: dict) -> dict:
        if isinstance(data, dict):
            # Migrate 'pros' -> 'custom_pros'
            if "pros" in data and "custom_pros" not in data:
                val = data.pop("pros")
                data["custom_pros"] = val if isinstance(val, list) else [str(val)]
            elif "pros" in data:
                data.pop("pros")

            # Migrate 'recommendation' -> 'custom_recommendation'
            if "recommendation" in data and "custom_recommendation" not in data:
                data["custom_recommendation"] = data.pop("recommendation")
            elif "recommendation" in data:
                data.pop("recommendation")
        return data

    analysis_id: str
    source_doc_id: str
    plan_id: str

    # §1.4 — Обязательные массивы
    facts: list[Fact] = Field(default_factory=list)
    conclusions: list[Conclusion] = Field(default_factory=list)
    negative_space: list[NegativeSpaceItem] = Field(default_factory=list)
    normative: list[NormativeAssessment] = Field(default_factory=list)

    # Итоговые блоки
    custom_pros: list[str] = Field(default_factory=list, description="Пользовательские или сгенерированные LLM плюсы")
    cons: list[str] = Field(default_factory=list)
    affected_parties: list[AffectedParty] = Field(default_factory=list)
    custom_recommendation: str = Field(default="", description="Пользовательские или сгенерированные LLM рекомендации")

    # Мета
    conflict_chunk_ids_referenced: list[str] = Field(
        default_factory=list,
        description="Все конфликтные чанки должны быть упомянуты",
    )
    llm_model_used: str = ""
    analyzed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Заполняется Аудитором (BM25 reliability)
    overall_reliability: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Штрафная модель надёжности (V7.0)",
    )

    # Заполняется Claim Verifier (Stage 2.5 + Auditor v2)
    verdicts: list[ClaimVerdict] = Field(
        default_factory=list,
        description="Вердикты по каждому утверждению документа",
    )

    # V7.0: Структурные claims (из Stage 2.6) — для рендеринга чеклиста
    structural_claims: list[DocumentClaim] = Field(default_factory=list)

    # V7.0: Bridge-конфликты из S6 — для единой секции конфликтов
    conflicts: list[ConflictRecord] = Field(
        default_factory=list,
        description="Конфликты, выявленные Auditor (S5/S6), для рендеринга",
    )

    # V9.4: Иммутабельное состояние статистики
    stats: AnalysisStats | None = None


    @computed_field  # type: ignore[misc]
    @property
    def validated_facts_count(self) -> int:
        return sum(
            1
            for f in self.facts
            if f.validation_status in (ValidationStatus.HIGH, ValidationStatus.MEDIUM)
        )

    @computed_field  # type: ignore[misc]
    @property
    def unverified_facts_count(self) -> int:
        return sum(1 for f in self.facts if f.validation_status == ValidationStatus.UNVERIFIED)

    @computed_field  # type: ignore[misc]
    @property
    def pros(self) -> list[str]:
        if self.stats is not None:
            return self.stats.pros
        if not self.custom_pros:
            confirmed_count = sum(1 for v in self.verdicts if v.status == VerdictStatus.CONFIRMED)
            return [f"Подтверждено {confirmed_count} из {len(self.verdicts)} анализируемых утверждений законопроекта."]
        return self.custom_pros

    @computed_field  # type: ignore[misc]
    @property
    def recommendation(self) -> str:
        if self.stats is not None:
            return self.stats.recommendation
        if not self.custom_recommendation:
            verdicts = self.verdicts
            contradictions = [v for v in verdicts if v.status == VerdictStatus.CONTRADICTED]
            confirmed_list = [v for v in verdicts if v.status == VerdictStatus.CONFIRMED]
            unverified_list = [v for v in verdicts if v.status == VerdictStatus.UNVERIFIED]

            rec = (
                f"Юридический аудит завершен. Из {len(verdicts)} выдвинутых утверждений: "
                f"{len(confirmed_list)} подтверждено действующим законодательством Республики Казахстан, "
                f"{len(contradictions)} опровергнуто, {len(unverified_list)} не верифицировано (отсутствуют в предоставленном корпусе)."
            )
            if contradictions:
                rec += f" КРИТИЧЕСКИ: {len(contradictions)} ошибок."
            return rec
        return self.custom_recommendation
