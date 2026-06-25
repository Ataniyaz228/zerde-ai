#!/usr/bin/env python
"""Управление пользователями (закрытая регистрация).

Запуск из корня репозитория:
    cd web/backend && ../../.venv/bin/python manage.py create-user <логин>
    cd web/backend && ../../.venv/bin/python manage.py list-users
    cd web/backend && ../../.venv/bin/python manage.py delete-user <логин>

Пароль для create-user вводится интерактивно (не виден в истории команд).
"""

import asyncio
import getpass
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from services import users  # noqa: E402


async def _create(username: str) -> int:
    await users.init_db()
    if await users.get_by_username(username) is not None:
        print(f"Пользователь '{username}' уже существует.", file=sys.stderr)
        return 1
    pw1 = getpass.getpass("Пароль: ")
    pw2 = getpass.getpass("Повтори пароль: ")
    if pw1 != pw2:
        print("Пароли не совпадают.", file=sys.stderr)
        return 1
    if len(pw1) < 8:
        print("Пароль слишком короткий (минимум 8 символов).", file=sys.stderr)
        return 1
    try:
        await users.create_user(username, pw1)
    except sqlite3.IntegrityError:
        print(f"Пользователь '{username}' уже существует.", file=sys.stderr)
        return 1
    print(f"Готово: пользователь '{username}' создан.")
    return 0


async def _list() -> int:
    await users.init_db()
    rows = await users.list_users()
    if not rows:
        print("Пользователей нет.")
        return 0
    for r in rows:
        print(f"  {r['id']:>3}  {r['username']:<24}  {r['created_at']}")
    return 0


async def _delete(username: str) -> int:
    await users.init_db()
    if await users.get_by_username(username) is None:
        print(f"Пользователь '{username}' не найден.", file=sys.stderr)
        return 1

    def _del() -> None:
        conn = users._connect(users.settings.users_db_path)
        try:
            conn.execute("DELETE FROM users WHERE username = ?", (username,))
            conn.commit()
        finally:
            conn.close()

    await asyncio.to_thread(_del)
    print(f"Удалён: '{username}'.")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    cmd, rest = args[0], args[1:]
    if cmd == "create-user" and len(rest) == 1:
        return asyncio.run(_create(rest[0]))
    if cmd == "list-users" and not rest:
        return asyncio.run(_list())
    if cmd == "delete-user" and len(rest) == 1:
        return asyncio.run(_delete(rest[0]))
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
