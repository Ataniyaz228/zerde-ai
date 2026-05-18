"""
ЗЕРДЕ v6.2 — Configuration (pydantic-settings)
Все параметры читаются из .env. Меняй только .env, не хардкодь.

OpenRouter: используй OPENROUTER_API_KEY + OPENAI_BASE_URL=https://openrouter.ai/api/v1
            Для embeddings нужен отдельный OPENAI_API_KEY (или отключи cosine dedup).
"""

from functools import lru_cache

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Отдельный ключ для embeddings (опционально).
    # Нужен если LLM идёт через OpenRouter (не поддерживает /embeddings).
    # Если пустой — cosine dedup отключается, используется только SHA256.
    embedding_api_key: str = Field(
        default="",
        description="OpenAI API key для embeddings. Оставь пустым если используешь OpenRouter.",
    )

    # -----------------------------------------------------------------------
    # LLM Provider (OpenRouter или OpenAI)
    # -----------------------------------------------------------------------
    openai_base_url: str = Field(
        default="https://api.openai.com/v1",
        description=(
            "Base URL LLM провайдера.\n"
            "  OpenRouter: https://openrouter.ai/api/v1\n"
            "  OpenAI (default): https://api.openai.com/v1\n"
            "  Local Ollama: http://localhost:11434/v1"
        ),
    )

    # -----------------------------------------------------------------------
    # LLM Model
    # -----------------------------------------------------------------------
    llm_model: str = Field(
        default="deepseek/deepseek-r1-0528",
        description=(
            "ID модели для Этапов 2 и 5 (Planner + Analyst).\n"
            "  OpenRouter рекомендации:\n"
            "    deepseek/deepseek-r1-0528            — самый мощный reasoning, ~$0.5/1M\n"
            "    deepseek/deepseek-chat-v3-0324        — быстрый и дешёвый V3\n"
            "    moonshotai/kimi-k2                    — Kimi K2 (отличный для юр. анализа)\n"
            "    google/gemini-2.5-pro                 — Gemini 2.5 Pro\n"
            "    anthropic/claude-sonnet-4-5           — Claude Sonnet\n"
            "  OpenAI: gpt-4o, gpt-4.1"
        ),
    )
    llm_temperature: float = Field(default=0.0, ge=0.0, le=0.0)  # Zero — locked
    llm_max_tokens_planner: int = Field(default=4096)
    llm_max_tokens_analyst: int = Field(default=16384, description="DeepSeek R1 поддерживает до 32k")

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
        default="ЗЕРДЕ v6.2",
        description="Название приложения в OpenRouter dashboard",
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
        default=0.35,
        ge=0.0,
        le=1.0,
        description="BM25 порог для статуса HIGH (VALIDATION_THRESHOLD). Калибруй через scripts/calibrate_bm25.py",
    )
    bm25_medium_threshold: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description="BM25 порог для статуса MEDIUM (ниже → LOW)",
    )

    # -----------------------------------------------------------------------
    # Cache (SQLite)
    # -----------------------------------------------------------------------
    cache_db_path: str = Field(
        default="zerde_cache.db",
        description="Путь к SQLite кэшу. Ключ — SHA256 контента. Без TTL.",
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
    """Синглтон конфигурации. Используй везде вместо прямого импорта Settings()."""
    return Settings()
