"""
Pydantic Models (Core Data Contracts)
Все обмены между этапами СТРОГО через эти модели.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
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
    ingested_at: datetime = Field(default_factory=datetime.utcnow)

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

    created_at: datetime = Field(default_factory=datetime.utcnow)

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


class VerdictStatus(StrEnum):
    CONFIRMED = "CONFIRMED"         # Утверждение подтверждено источниками
    CONTRADICTED = "CONTRADICTED"   # Утверждение опровергнуто источниками
    UNVERIFIED = "UNVERIFIED"       # Нет данных в корпусе для проверки


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
    is_deterministic: bool = Field(
        default=False,
        description="True если вердикт вынесен из reference_data без LLM",
    )


class ClaimExtractionResult(BaseModel):
    """Выход Stage 2.5: список извлечённых утверждений."""

    doc_id: str
    claims: list[DocumentClaim] = Field(default_factory=list)
    extracted_at: datetime = Field(default_factory=datetime.utcnow)

    @computed_field  # type: ignore[misc]
    @property
    def critical_claims(self) -> list[DocumentClaim]:
        return [c for c in self.claims if c.severity == ClaimSeverity.CRITICAL]

    @computed_field  # type: ignore[misc]
    @property
    def total_count(self) -> int:
        return len(self.claims)


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

    # Правовая атрибуция
    legal_rank: LegalRank
    web_tier: WebTier | None = Field(default=None, description="Только для Web-источников")

    # Атрибуты НПА (только для Adilet-источников)
    law_id: str | None = None
    law_title: str | None = None
    article: str | None = None  # Номер статьи
    paragraph: str | None = None
    effective_date: date | None = None
    expiry_date: date | None = None

    # Технические поля
    adilet_fallback_used: AdiletFallbackStrategy | None = None
    embedding: list[float] | None = Field(default=None, exclude=True, description="Вектор для Cosine dedup")

    # Флаги Этапа 4 (заполняются на Fusion)
    is_duplicate: bool = False
    is_conflict: bool = False
    conflict_types: list[ConflictType] = Field(default_factory=list)
    conflict_with_ids: list[str] = Field(default_factory=list, description="chunk_id конфликтующих чанков")

    gathered_at: datetime = Field(default_factory=datetime.utcnow)

    @model_validator(mode="after")
    def compute_chunk_id(self) -> "EvidenceChunk":
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


class AnalysisJSON(BaseModel):
    """Выход Этапа 5: полная аналитическая структура."""

    analysis_id: str
    source_doc_id: str
    plan_id: str

    # §1.4 — Обязательные массивы
    facts: list[Fact] = Field(default_factory=list)
    conclusions: list[Conclusion] = Field(default_factory=list)
    negative_space: list[NegativeSpaceItem] = Field(default_factory=list)
    normative: list[NormativeAssessment] = Field(default_factory=list)

    # Итоговые блоки
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    affected_parties: list[AffectedParty] = Field(default_factory=list)
    recommendation: str = Field(default="")

    # Мета
    conflict_chunk_ids_referenced: list[str] = Field(
        default_factory=list,
        description="Все конфликтные чанки должны быть упомянуты",
    )
    llm_model_used: str = ""
    analyzed_at: datetime = Field(default_factory=datetime.utcnow)

    # Заполняется Аудитором (BM25 reliability)
    overall_reliability: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Средний BM25 score всех фактов",
    )

    # Заполняется Claim Verifier (Stage 2.5 + Auditor v2)
    verdicts: list[ClaimVerdict] = Field(
        default_factory=list,
        description="Вердикты по каждому утверждению документа",
    )


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
