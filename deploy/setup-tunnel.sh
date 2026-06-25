#!/usr/bin/env bash
# Один раз: создаёт named-туннель Cloudflare, привязывает DNS и пишет
# ~/.cloudflared/config.yml с роутингом по путям (фронт + бэкенд за одним доменом).
#
# ПЕРЕД запуском выполни (откроет браузер, выбери там свой домен):
#     cloudflared tunnel login
#
# Затем:
#     bash deploy/setup-tunnel.sh zerde.example.com
#
# где zerde.example.com — поддомен, на котором будет жить сайт.

set -euo pipefail

HOST="${1:-}"
if [[ -z "$HOST" ]]; then
  echo "Использование: bash deploy/setup-tunnel.sh <хостнейм>" >&2
  echo "Пример:        bash deploy/setup-tunnel.sh zerde.example.com" >&2
  exit 1
fi

NAME="zerde"
CF_DIR="$HOME/.cloudflared"

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared не найден в PATH." >&2
  exit 1
fi

if [[ ! -f "$CF_DIR/cert.pem" ]]; then
  echo "Нет $CF_DIR/cert.pem — сначала выполни:  cloudflared tunnel login" >&2
  exit 1
fi

# Создать туннель, если ещё нет.
if ! cloudflared tunnel list --output json | grep -q "\"name\":\"$NAME\""; then
  echo "==> Создаю туннель '$NAME'..."
  cloudflared tunnel create "$NAME"
else
  echo "==> Туннель '$NAME' уже существует, переиспользую."
fi

# Достать UUID туннеля по имени (через JSON, без хрупкого парсинга таблицы).
UUID="$(cloudflared tunnel list --output json \
  | python3 -c 'import sys,json; n=sys.argv[1]; print(next(t["id"] for t in json.load(sys.stdin) if t["name"]==n))' "$NAME")"
CRED="$CF_DIR/$UUID.json"

if [[ ! -f "$CRED" ]]; then
  echo "Не найден credentials-file $CRED — что-то пошло не так при create." >&2
  exit 1
fi

echo "==> Привязываю DNS: $HOST -> туннель $UUID"
cloudflared tunnel route dns "$NAME" "$HOST"

# Для апекса (один домен.зона, например zerde.site) дополнительно заводим www,
# чтобы привычный www.<host> тоже открывал сайт, а не упирался в NXDOMAIN/парковку.
WWW=""
if [[ "$HOST" == *.*.* ]]; then
  : # уже поддомен (app.zerde.site) — www не добавляем
else
  WWW="www.$HOST"
  echo "==> Привязываю DNS: $WWW -> туннель $UUID"
  cloudflared tunnel route dns "$NAME" "$WWW"
fi

echo "==> Пишу $CF_DIR/config.yml"
{
  echo "tunnel: $UUID"
  echo "credentials-file: $CRED"
  echo
  echo "ingress:"
  for h in "$HOST" ${WWW:+$WWW}; do
    echo "  - hostname: $h"
    echo "    path: ^/(api|ws|health|ready)(/.*)?\$"
    echo "    service: http://127.0.0.1:8000"
    echo "  - hostname: $h"
    echo "    service: http://127.0.0.1:3000"
  done
  echo "  - service: http_status:404"
} > "$CF_DIR/config.yml"

echo
echo "Готово. Хостнейм: https://$HOST"
echo "Запускай весь стек:  bash deploy/serve.sh"
