#!/usr/bin/env bash
# Запуск всего прод-стека на этом ПК: бэкенд + фронт + Cloudflare-туннель.
#
#     bash deploy/serve.sh
#
# Ctrl+C гасит всё. Требует:
#   - собранный фронт (web/frontend/.next) — `cd web/frontend && npm run build`
#   - настроенный туннель — `bash deploy/setup-tunnel.sh <хостнейм>` (один раз)
#   - .env с OPENAI_API_KEY (OpenRouter) и ZERDE_USE_CUDA=1

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p deploy/logs

echo "==> Бэкенд (FastAPI :8000, один воркер — WS/rate-limit в памяти процесса)"
# ВАЖНО: --workers 1. Прогресс-WebSocket и rate-limit живут в памяти процесса;
# с несколькими воркерами джоба и её WS попали бы в разные процессы (прогресс
# не дошёл бы), а BGE-M3 загрузился бы в каждый воркер (на 6 ГБ VRAM не влезет).
( cd web/backend && exec ../../.venv/bin/python -m uvicorn app:app \
    --host 127.0.0.1 --port 8000 --workers 1 ) >deploy/logs/backend.log 2>&1 &
BACK=$!

echo "==> Фронт (Next.js :3000, прод-сборка)"
( cd web/frontend && exec ./node_modules/.bin/next start -H 127.0.0.1 -p 3000 ) \
    >deploy/logs/frontend.log 2>&1 &
FRONT=$!

cleanup() {
  echo
  echo "==> Останавливаю..."
  kill "$BACK" "$FRONT" 2>/dev/null || true
  wait "$BACK" "$FRONT" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "==> Жду готовности бэкенда (/health)..."
for _ in $(seq 1 90); do
  if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then break; fi
  if ! kill -0 "$BACK" 2>/dev/null; then
    echo "Бэкенд упал на старте. Лог: deploy/logs/backend.log" >&2
    tail -n 30 deploy/logs/backend.log >&2 || true
    exit 1
  fi
  sleep 1
done

echo "==> Жду готовности фронта (:3000)..."
for _ in $(seq 1 60); do
  curl -sf http://127.0.0.1:3000 >/dev/null 2>&1 && break
  sleep 1
done

echo "==> Поднимаю Cloudflare-туннель (Ctrl+C — стоп всего)"
cloudflared tunnel run zerde
