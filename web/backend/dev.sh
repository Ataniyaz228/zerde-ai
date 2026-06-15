#!/usr/bin/env bash
# Запуск backend в dev-режиме. Запускать из web/backend/ (или любой директории —
# пути относительные к расположению скрипта).
#
#   bash web/backend/dev.sh
#
# Оба --reload-dir обязательны: без --reload-dir ../../zerde uvicorn следит только
# за web/backend/ и НЕ подхватывает правки в пакете zerde/ (модуль остаётся
# устаревшим в памяти). При правке уже-импортированного zerde-файла, который не
# менялся после старта, нужен полный рестарт (Ctrl+C + повторный запуск).

set -euo pipefail
cd "$(dirname "$0")"

# --reload-exclude: рантайм-артефакты пишутся внутрь наблюдаемого web/backend/
# (лог, jobs.db + WAL/SHM, дампы JSON). Без исключений watchfiles логирует
# «1 change detected» на каждую такую запись (раз в секунду при активности) —
# шум и лишний I/O. Реальный reload всё равно только по *.py, но исключаем,
# чтобы заглушить детектор.
exec ../../.venv/bin/python -m uvicorn app:app \
    --host 0.0.0.0 --port 8000 --reload \
    --reload-dir . --reload-dir ../../zerde \
    --reload-include '*.py' \
    --reload-exclude '*.log' \
    --reload-exclude '*.db' --reload-exclude '*.db-wal' --reload-exclude '*.db-shm' \
    --reload-exclude '*.json'
