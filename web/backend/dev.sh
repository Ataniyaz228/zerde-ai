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

exec ../../.venv/bin/python -m uvicorn app:app \
    --host 0.0.0.0 --port 8000 --reload \
    --reload-dir . --reload-dir ../../zerde
