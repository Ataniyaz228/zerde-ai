"""Аутентификация по общему API-ключу и rate-limit на запуск анализа.

Оба механизма опциональны и не мешают локальной разработке: ключ включается
только когда задан ZERDE_API_KEY, а rate-limit считается в памяти процесса
(этого достаточно для одного инстанса; за несколькими репликами нужен Redis).
"""

import time

from config import settings
from fastapi import Header, HTTPException, Request


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Пропускает запрос, только если задан верный X-API-Key.

    Когда ZERDE_API_KEY не выставлен — аутентификация выключена (dev/тесты).
    """
    if settings.api_key is None:
        return
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Неверный или отсутствующий API-ключ")


# IP -> список таймстампов запусков в текущем окне.
_hits: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    # За reverse-proxy реальный адрес лежит в X-Forwarded-For (первый в списке).
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def rate_limit_analyze(request: Request) -> None:
    """Ограничивает частоту запуска анализа с одного IP (скользящее окно)."""
    limit = settings.analyze_rate_limit
    window = settings.analyze_rate_window_s
    if limit <= 0:
        return

    now = time.monotonic()
    ip = _client_ip(request)
    recent = [t for t in _hits.get(ip, []) if now - t < window]
    if len(recent) >= limit:
        retry_after = int(window - (now - recent[0])) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Слишком много запусков анализа. Повторите через {retry_after} с.",
            headers={"Retry-After": str(retry_after)},
        )
    recent.append(now)
    _hits[ip] = recent


def reset_rate_limit() -> None:
    """Сбрасывает счётчики — для изоляции тестов."""
    _hits.clear()
