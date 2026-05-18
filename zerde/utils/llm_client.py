"""
ЗЕРДЕ v6.2 — LLM Client Factory
Создаёт AsyncOpenAI клиентов для разных провайдеров.
OpenRouter: base_url + HTTP headers
OpenAI: стандартный клиент
Embeddings: всегда OpenAI (отдельный ключ или тот же)
"""

from __future__ import annotations

from openai import AsyncOpenAI

from zerde.config import Settings, get_settings


def make_llm_client(settings: Settings | None = None) -> AsyncOpenAI:
    """
    Создаёт AsyncOpenAI клиент для LLM (Planner, Analyst).
    При OpenRouter — добавляет base_url и X-Title / HTTP-Referer заголовки.
    """
    s = settings or get_settings()
    return AsyncOpenAI(
        api_key=s.openai_api_key,
        base_url=s.openai_base_url,
        default_headers=s.openrouter_headers,
    )


def make_embedding_client(settings: Settings | None = None) -> AsyncOpenAI | None:
    """
    Создаёт AsyncOpenAI клиент для embeddings.
    Возвращает None если embeddings недоступны (OpenRouter без отдельного ключа).
    Embeddings всегда идут через api.openai.com — OpenRouter их не поддерживает.
    """
    s = settings or get_settings()

    if not s.can_use_embeddings:
        return None

    # Embeddings ВСЕГДА через OpenAI (даже если LLM через OpenRouter)
    return AsyncOpenAI(
        api_key=s.effective_embedding_key,
        base_url="https://api.openai.com/v1",  # Фиксировано — не OpenRouter
    )
