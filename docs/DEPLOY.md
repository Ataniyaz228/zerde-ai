# Деплой Zerde: сайт в интернете, пайплайн — на твоём ПК

Бэкенд **обязан** работать на этой машине: на ней GPU (BGE-M3 + reranker),
корпус `zerde_cache.db` (~180 МБ) и Tesseract. Поэтому «деплой» = поднять
бэкенд+фронт локально и пробросить их наружу одним доменом через Cloudflare
Tunnel. LLM-аудит при этом всё равно уходит в облако (OpenRouter) — это его
штатный режим.

```
   интернет
      │   https://zerde.ТВОЙ-ДОМЕН/
      ▼
 ┌──────────────┐        ┌──────────── твой ПК (RTX 4050) ─────────────┐
 │  Cloudflare  │  WSS/  │  /api,/ws,/health,/ready → FastAPI  :8000     │
 │    Tunnel    │ HTTPS  │  всё остальное            → Next.js  :3000     │
 └──────────────┘ ─────► │      GPU: BGE-M3 + reranker, корпус .db        │
   (named tunnel)        └───────────────────────────────────────────────┘
        cloudflared подключается к 127.0.0.1 — наружу порты НЕ открыты
```

Один домен + роутинг по путям → **один origin, CORS не нужен**, WebSocket-прогресс
работает (Cloudflare проксирует upgrade сам).

> Пока ПК выключен — сайт недоступен. Это неизбежно: вся тяжёлая часть (GPU,
> корпус) на нём. Чтобы держать сайт онлайн — оставляй машину включённой и
> используй автозапуск через systemd (см. ниже).

---

## 0. Предусловия (один раз)

- `cloudflared` установлен (`cloudflared --version`). Уже стоит в `~/.local/bin`.
- `.env` в корне заполнен: `OPENAI_API_KEY` (OpenRouter), `ZERDE_USE_CUDA=1`.
- Фронт собран: `cd web/frontend && npm run build`.
- Аккаунт Cloudflare (бесплатный) — https://dash.cloudflare.com/sign-up

## 1. Домен → Cloudflare

Нужен домен, чьи nameservers указывают на Cloudflare (только тогда named-туннель
сможет привязать DNS).

1. Купи домен (дёшево: `.xyz`/`.top` ~1–3 $/год у Namecheap/Porkbun; либо
   бесплатный у https://www.duckdns.org — но он не на Cloudflare, тогда туннель
   придётся настраивать иначе, проще взять платный за доллар).
2. В Cloudflare Dashboard → **Add a site** → введи домен → план **Free**.
3. Cloudflare покажет два nameserver'а (вида `xxx.ns.cloudflare.com`). Пропиши
   их у регистратора домена вместо текущих.
4. Подожди активации зоны (от пары минут до пары часов; на дашборде статус
   станет **Active**).

## 2. Залогинить cloudflared

```bash
cloudflared tunnel login
```

Откроется браузер — выбери свой домен (зону). Создастся `~/.cloudflared/cert.pem`.

## 3. Создать туннель и DNS (скрипт)

```bash
bash deploy/setup-tunnel.sh zerde.ТВОЙ-ДОМЕН
```

Скрипт: создаёт туннель `zerde`, привязывает `zerde.ТВОЙ-ДОМЕН` к нему (CNAME)
и пишет `~/.cloudflared/config.yml` с роутингом по путям
(см. образец `deploy/cloudflared.config.example.yml`).

## 4. Запуск всего стека

```bash
bash deploy/serve.sh
```

Поднимает бэкенд (:8000, один воркер), фронт (:3000) и туннель. Логи —
`deploy/logs/{backend,frontend}.log`. **Ctrl+C** гасит всё.

Открывай `https://zerde.ТВОЙ-ДОМЕН` — должна открыться главная, загрузка
документа и живой прогресс по WebSocket.

---

## Защита публичного сайта

Каждый анализ жжёт GPU и токены OpenRouter — открытый сайт = вектор абьюза.

- **Логин по паролю (встроен в приложение).** Включается заданием
  `ZERDE_AUTH_SECRET` в `.env`. Тогда все `/api/*` и WebSocket-прогресс — за
  сессией (httpOnly-cookie с JWT), а фронт редиректит гостей на `/login`.
  Регистрация **закрытая** — пользователей заводишь ты:
  ```bash
  cd web/backend && ../../.venv/bin/python manage.py create-user <логин>   # спросит пароль
  ../../.venv/bin/python manage.py list-users
  ../../.venv/bin/python manage.py delete-user <логин>
  ```
  Скрипты могут ходить в обход cookie с заголовком `X-API-Key` (если задан
  `ZERDE_API_KEY`). Если `ZERDE_AUTH_SECRET` пуст — авторизация выключена (dev).
- **Rate-limit (уже включён):** `ZERDE_ANALYZE_RATE_LIMIT=10` запусков/час на IP.
  За Cloudflare реальный IP приходит в `X-Forwarded-For` — бэкенд его уже читает.
- **Cloudflare Access (опционально, доп. слой).** Zero Trust → Access →
  Applications → Add → Self-hosted → домен `zerde.ТВОЙ-ДОМЕН` → политика «разрешить
  только эти email / вход через Google». Бесплатно до 50 пользователей — гейт ещё
  на edge, до приложения. Можно поверх логина или вместо него.

---

## Постоянная работа (автозапуск через systemd --user)

Чтобы стек поднимался сам после перезагрузки и падений. Положи три юнита в
`~/.config/systemd/user/`:

**`zerde-backend.service`**
```ini
[Unit]
Description=Zerde backend (FastAPI :8000)
After=network-online.target

[Service]
WorkingDirectory=%h/ai projects/zerde/web/backend
ExecStart=%h/ai projects/zerde/.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

**`zerde-frontend.service`**
```ini
[Unit]
Description=Zerde frontend (Next.js :3000)
After=network-online.target

[Service]
WorkingDirectory=%h/ai projects/zerde/web/frontend
ExecStart=%h/ai projects/zerde/web/frontend/node_modules/.bin/next start -H 127.0.0.1 -p 3000
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

**`zerde-tunnel.service`**
```ini
[Unit]
Description=Cloudflare tunnel for Zerde
After=zerde-backend.service zerde-frontend.service

[Service]
ExecStart=%h/.local/bin/cloudflared tunnel run zerde
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

Включить:
```bash
systemctl --user daemon-reload
systemctl --user enable --now zerde-backend zerde-frontend zerde-tunnel
sudo loginctl enable-linger "$USER"   # чтобы работало без активного логина
```

> Путь `%h/ai projects/zerde` содержит пробел — в systemd `WorkingDirectory`/
> `ExecStart` это ок (юнит не шелл), кавычки не нужны. Логи: `journalctl --user -u zerde-backend -f`.

---

## Траблшутинг

- **VRAM/OOM при анализе.** RTX 4050 — 6 ГБ, BGE-M3 + reranker впритык. Если
  ловишь CUDA OOM — в `.env` поставь `ZERDE_USE_CUDA=0` (модели уедут на CPU:
  медленнее, но стабильно).
- **Сайт грузится, но анализ висит / нет прогресса.** Проверь, что бэкенд
  поднят в **один** воркер (см. `deploy/serve.sh`). Несколько воркеров ломают
  WS-прогресс (джоба и сокет в разных процессах).
- **502/неответ на /api.** Проверь `deploy/logs/backend.log` и `/ready`
  (`curl https://zerde.ТВОЙ-ДОМЕН/ready`) — он проверяет, что корпус-БД на месте.
- **WebSocket не коннектится.** Cloudflare поддерживает WSS из коробки; убедись,
  что путь `/ws/...` уходит на :8000 в `~/.cloudflared/config.yml` (правило с
  `path: ^/(api|ws|health|ready)...` должно быть ВЫШЕ catch-all на :3000).
- **Сменил домен.** Фронт пересобирать НЕ надо (он same-origin). Перенастрой
  только туннель: `bash deploy/setup-tunnel.sh новый.домен`.
