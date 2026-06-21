"""
Configuration (pydantic-settings)
Все параметры читаются из .env. Меняй только .env, не хардкодь.

OpenRouter: используй OPENROUTER_API_KEY + OPENAI_BASE_URL=https://openrouter.ai/api/v1
            Для embeddings нужен отдельный OPENAI_API_KEY (или отключи cosine dedup).
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict

# Корень проекта — позволяет резолвить пути независимо от cwd процесса.
# config.py лежит в zerde/, поэтому корень — на два уровня выше (zerde/.. = repo root).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CACHE_DB = str(_PROJECT_ROOT / "zerde_cache.db")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -----------------------------------------------------------------------
    # API Keys
    # -----------------------------------------------------------------------
    openai_api_key: str = Field(
        ...,
        description="API ключ LLM провайдера. OpenRouter: sk-or-v1-... | OpenAI: sk-...",
    )
    tavily_api_key: str = Field(default="", description="Tavily Search API Key")
    search_provider: str = Field(default="duckduckgo", description="Web search provider (tavily, google, serper, duckduckgo)")
    serper_api_key: str = Field(default="", description="Serper.dev Search API Key")
    google_api_key: str = Field(default="", description="Google CSE JSON API Key")
    google_cse_id: str = Field(default="", description="Google Custom Search Engine ID (CX)")

    # Отдельный ключ для embeddings (опционально).
    # Нужен если LLM идёт через OpenRouter (не поддерживает /embeddings).
    # Если пустой — cosine dedup отключается, используется только SHA256.
    embedding_api_key: str = Field(
        default="",
        description="OpenAI API key для embeddings. Оставь пустым если используешь OpenRouter.",
    )

    # -----------------------------------------------------------------------
    # LLM Provider (OpenRouter, OpenAI, или Gemini)
    # -----------------------------------------------------------------------
    openai_base_url: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta/openai/",
        description=(
            "Base URL LLM провайдера.\n"
            "  Gemini: https://generativelanguage.googleapis.com/v1beta/openai/\n"
            "  OpenRouter: https://openrouter.ai/api/v1\n"
            "  OpenAI: https://api.openai.com/v1\n"
        ),
    )

    # -----------------------------------------------------------------------
    # LLM Models
    # -----------------------------------------------------------------------
    llm_model_planner: str = Field(
        default="gemini-3.5-flash",
        description="ID модели для Этапа 2 (Planner).",
    )
    llm_model_extractor: str = Field(
        default="gemini-3.5-flash",
        description="ID модели для Этапа 2.5 (Claim Extractor).",
    )
    llm_model_analyst: str = Field(
        default="gemini-3.5-flash",
        description="ID модели для Этапа 5 (Auditor).",
    )
    llm_model_policy_analyst: str = Field(
        default="gemini-3.5-flash",
        description="ID модели для Этапа 5.5 (Policy Analyst).",
    )
    llm_model_translator: str = Field(
        default="gemini-3.5-flash",
        description="ID модели для перевода готового отчёта RU→KZ (презентационный слой, вне аудита).",
    )
    llm_max_tokens_planner: int = Field(default=8192, description="Увеличено с 4096: QueryPlan с 10+ запросами занимает 5-7K токенов")
    llm_max_tokens_analyst: int = Field(default=16384, description="DeepSeek R1 поддерживает до 32k")
    llm_max_tokens_translator: int = Field(default=16384, description="Перевод всего отчёта целиком — отчёты длинные")

    embedding_model: str = Field(
        default="text-embedding-3-small",
        description="OpenAI embedding model (только если embedding_api_key задан)",
    )

    # -----------------------------------------------------------------------
    # OpenRouter HTTP Headers (рекомендуется для трекинга)
    # -----------------------------------------------------------------------
    openrouter_site_url: str = Field(
        default="https://github.com/zerde",
        description="Твой сайт для OpenRouter dashboard",
    )
    openrouter_app_name: str = Field(
        default="",
        description="Название приложения в OpenRouter dashboard",
    )

    # -----------------------------------------------------------------------
    # Translator provider (опциональный, ТОЛЬКО для переводчика отчёта S7)
    # -----------------------------------------------------------------------
    # Переводчик — презентационный слой ВНЕ аудита. Его можно направить на
    # отдельного провайдера (напр. бесплатный шлюз с Anthropic Messages-протоколом
    # вроде openmodel.ai → deepseek-v4-flash), не трогая аудит-путь (OPENAI_BASE_URL).
    # Если translator_base_url пустой — переводчик идёт тем же OpenAI-клиентом,
    # что и остальные стадии (обратная совместимость, default).
    translator_protocol: str = Field(
        default="openai",
        description="Протокол переводчика: 'openai' (chat.completions) или 'anthropic' (/v1/messages).",
    )
    translator_base_url: str = Field(
        default="",
        description="Base URL провайдера переводчика. Пусто → использовать общий OPENAI_BASE_URL.",
    )
    translator_api_key: str = Field(
        default="",
        description="API-ключ провайдера переводчика. Пусто → использовать общий OPENAI_API_KEY.",
    )
    audit_fallback_enabled: bool = Field(
        default=False,
        description=(
            "Автофолбэк аудит-пути (planner/extractor/analyst/policy) на openmodel "
            "(Anthropic /v1/messages) при 403 'Key limit exceeded' от OpenRouter. "
            "Использует translator_base_url/translator_api_key (тот же шлюз). "
            "Прод-метрика остаётся на OpenRouter — openmodel лишь запасной, чтобы "
            "пайплайн не блокировался лимитом ключа."
        ),
    )

    # -----------------------------------------------------------------------
    # Adilet Agent
    # -----------------------------------------------------------------------
    adilet_base_url: HttpUrl = Field(
        default="https://adilet.zan.kz",  # type: ignore[assignment]
        description="Base URL для парсинга Адилет",
    )
    adilet_timeout_seconds: int = Field(default=30)
    adilet_max_articles_per_law: int = Field(default=50)
    adilet_tls_verify: bool = Field(
        default=False,
        description=(
            "Проверять TLS-сертификат adilet.zan.kz. По умолчанию False: цепочка "
            "сертификатов Adilet часто не валидируется (запрос падает в HTTP 000). "
            "scripts/update_corpus.py исторически тоже отключает проверку."
        ),
    )

    # -----------------------------------------------------------------------
    # Web Agent
    # -----------------------------------------------------------------------
    tavily_base_url: str = Field(default="https://api.tavily.com")
    web_max_results_per_query: int = Field(default=10)

    # -----------------------------------------------------------------------
    # Fusion / Dedup
    # -----------------------------------------------------------------------
    cosine_similarity_threshold: float = Field(
        default=0.92,
        ge=0.0,
        le=1.0,
        description="Порог для семантической дедупликации (Этап 4)",
    )
    hierarchy_conflict_rank_delta: int = Field(
        default=2,
        description="Минимальная разница legal_rank для HIERARCHY конфликта",
    )

    # -----------------------------------------------------------------------
    # Auditor / BM25
    # -----------------------------------------------------------------------
    validation_threshold: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description="BM25 порог для статуса HIGH (VALIDATION_THRESHOLD). Калибруй через scripts/calibrate_bm25.py",
    )
    bm25_medium_threshold: float = Field(
        default=0.12,
        ge=0.0,
        le=1.0,
        description="BM25 порог для статуса MEDIUM (ниже → LOW)",
    )
    bm25_fallback_threshold: float = Field(
        default=0.20,
        ge=0.0,
        le=1.0,
        description="BM25 порог для автоматического corpus-wide поиска (fallback).",
    )

    # -----------------------------------------------------------------------
    # Cache (SQLite)
    # -----------------------------------------------------------------------
    cache_db_path: str = Field(
        default=_DEFAULT_CACHE_DB,
        description=(
            "Путь к SQLite кэшу. По умолчанию — <project_root>/zerde_cache.db, "
            "чтобы CLI и web/backend смотрели в один кеш независимо от cwd. "
            "Переопределяется через .env: CACHE_DB_PATH=..."
        ),
    )

    # -----------------------------------------------------------------------
    # Output
    # -----------------------------------------------------------------------
    output_dir: str = Field(default="output", description="Директория для Markdown-отчётов")
    log_level: str = Field(default="INFO")

    # -----------------------------------------------------------------------
    # Computed helpers
    # -----------------------------------------------------------------------

    @property
    def is_openrouter(self) -> bool:
        """True если используется OpenRouter."""
        return "openrouter.ai" in self.openai_base_url

    @property
    def effective_embedding_key(self) -> str:
        """Ключ для embeddings (embedding_api_key или openai_api_key как fallback)."""
        return self.embedding_api_key or self.openai_api_key

    @property
    def can_use_embeddings(self) -> bool:
        """
        False если OpenRouter используется без отдельного embedding_api_key.
        В этом случае cosine dedup пропускается.
        """
        if not self.is_openrouter:
            return True
        return bool(self.embedding_api_key)

    @property
    def openrouter_headers(self) -> dict[str, str]:
        """HTTP заголовки для OpenRouter (идентификация приложения)."""
        if not self.is_openrouter:
            return {}
        return {
            "HTTP-Referer": self.openrouter_site_url,
            "X-Title": self.openrouter_app_name,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
