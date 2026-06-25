"""Сессионная авторизация: вход по паролю, JWT в httpOnly-cookie.

Модель доступа:
  - Человек логинится (`POST /api/auth/login`) → получает httpOnly-cookie с JWT.
    Cookie уходит и с обычными запросами, и с WebSocket-хэндшейком (same-origin).
  - Скрипты могут ходить с заголовком `X-API-Key` (ZERDE_API_KEY) — без cookie.
  - Если НИ ZERDE_AUTH_SECRET, НИ ZERDE_API_KEY не заданы — доступ открыт
    (локальная разработка и тесты не ломаются).

Регистрация закрытая: пользователей заводит владелец через web/backend/manage.py.
"""

import datetime
import logging

import jwt
from config import settings
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket
from pydantic import BaseModel, Field
from services import users

logger = logging.getLogger(__name__)

router = APIRouter()

COOKIE_NAME = "zerde_session"
_ALG = "HS256"


def _make_token(user_id: int) -> str:
    now = datetime.datetime.now(datetime.UTC)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + datetime.timedelta(seconds=settings.auth_session_ttl_s),
    }
    return jwt.encode(payload, settings.auth_secret, algorithm=_ALG)


def _user_id_from_token(token: str) -> int | None:
    try:
        payload = jwt.decode(token, settings.auth_secret, algorithms=[_ALG])
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


async def _user_from_cookie(token: str | None) -> dict | None:
    """Validate a session cookie → user row, or None."""
    if not token or not settings.auth_secret:
        return None
    uid = _user_id_from_token(token)
    if uid is None:
        return None
    return await users.get_by_id(uid)


async def require_auth(request: Request) -> None:
    """Гейт для всех /api/*: валидная session-cookie ИЛИ верный X-API-Key.

    Когда не настроены ни ZERDE_AUTH_SECRET, ни ZERDE_API_KEY — пропускает всё
    (dev/тесты). Иначе требует один из двух способов аутентификации.
    """
    if settings.auth_secret is None and settings.api_key is None:
        return
    if await _user_from_cookie(request.cookies.get(COOKIE_NAME)) is not None:
        return
    if settings.api_key is not None and request.headers.get("x-api-key") == settings.api_key:
        return
    raise HTTPException(status_code=401, detail="Требуется вход")


async def authenticate_ws(websocket: WebSocket) -> bool:
    """True, если WebSocket-подключение разрешено (cookie-сессия или auth выключен)."""
    if settings.auth_secret is None and settings.api_key is None:
        return True
    return await _user_from_cookie(websocket.cookies.get(COOKIE_NAME)) is not None


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1024)


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        max_age=settings.auth_session_ttl_s,
        path="/",
    )


@router.post("/login")
async def login(body: LoginIn, response: Response):
    if settings.auth_secret is None:
        raise HTTPException(status_code=503, detail="Авторизация не настроена (нет ZERDE_AUTH_SECRET)")
    user = await users.get_by_username(body.username)
    if user is None or not users.verify_password(body.password, user["password_hash"]):
        # Единое сообщение — не раскрываем, существует ли логин.
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    _set_session_cookie(response, _make_token(int(user["id"])))
    return {"username": user["username"]}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    """Состояние сессии для фронт-guard'а.

    auth выключен (нет ZERDE_AUTH_SECRET) → 200 {auth_required: false} — гейта нет.
    auth включён + валидная cookie → 200 {auth_required: true, username}.
    auth включён + нет cookie → 401 (фронт редиректит на /login).
    """
    if settings.auth_secret is None:
        return {"auth_required": False, "username": None}
    user = await _user_from_cookie(request.cookies.get(COOKIE_NAME))
    if user is None:
        raise HTTPException(status_code=401, detail="Не аутентифицирован")
    return {"auth_required": True, "username": user["username"]}
