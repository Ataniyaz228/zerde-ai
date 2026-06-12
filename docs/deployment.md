# Деплой

## Docker Compose (рекомендуется)

### Требования

- Docker 20.10+
- Docker Compose v2
- ~10 ГБ свободного места (backend ~9 ГБ из-за torch, frontend ~250 МБ)
- `.env` с API-ключами в корне проекта

### Сборка

```bash
cd zerde
docker compose build
```

Первая сборка занимает 10-15 минут (torch ~2 ГБ). Повторные — секунды (Docker layer cache).

Можно собирать по частям:
```bash
docker compose build backend   # ~10 мин первый раз
docker compose build frontend  # ~2 мин первый раз
```

### Запуск

```bash
# Переднем плане (с логами)
docker compose up

# В фоне
docker compose up -d
```

Сервисы:
| Сервис | Порт | URL |
|---|---|---|
| Backend (FastAPI) | 8000 | http://localhost:8000 |
| Frontend (Next.js) | 3000 | http://localhost:3000 |

### Проверка

```bash
# Статус контейнеров
docker compose ps

# Health check
curl http://localhost:8000/health

# Логи
docker compose logs -f           # все
docker compose logs -f backend   # только backend
docker compose logs -f frontend  # только frontend
```

### Остановка

```bash
docker compose stop       # остановить (контейнеры сохраняются)
docker compose down       # остановить + удалить контейнеры
docker compose down -v    # + удалить volumes
```

### Пересборка после изменений

```bash
# Пересобрать и запустить
docker compose up --build

# Пересобрать только backend
docker compose build backend && docker compose up -d
```

### docker-compose.yml

```yaml
services:
  backend:
    build:
      context: .
      dockerfile: web/backend/Dockerfile
    ports:
      - "8000:8000"
    env_file: .env
    volumes:
      - ./zerde_cache.db:/app/zerde_cache.db
      - ./output:/app/output
      - ./data:/app/data

  frontend:
    build:
      context: .
      dockerfile: web/frontend/Dockerfile
    ports:
      - "3000:3000"
    environment:
      - NEXT_PUBLIC_API_URL=http://backend:8000
    depends_on:
      - backend
```

### Размеры образов

| Образ | Размер | Причина |
|---|---|---|
| `zerde-backend` | ~9 ГБ | Python 3.12 + torch + sentence-transformers + tesseract OCR |
| `zerde-frontend` | ~250 МБ | Node.js Alpine + Next.js standalone |

Для уменьшения backend до ~800 МБ нужно вынести torch/sentence-transformers в optional extra и сделать ленивый import.

---

## Локальный запуск (без Docker)

### Backend

```bash
bash web/backend/dev.sh
```

Скрипт:
1. Активирует `.venv`
2. Запускает uvicorn с hot-reload
3. Указывает два reload-dir (backend + zerde)

### Frontend

```bash
cd web/frontend
npm install
npm run dev
```

### OCR (для PDF)

На Arch Linux:
```bash
sudo pacman -S tesseract tesseract-data-rus tesseract-data-kaz
```

На Debian/Ubuntu:
```bash
sudo apt install tesseract-ocr tesseract-ocr-rus tesseract-ocr-kaz
```

---

## Troubleshooting

### Порт занят

```bash
lsof -i :8000
fuser -k 8000/tcp
```

### Backend не видит .env

`.env` монтируется через `env_file` в docker-compose. При изменении:
```bash
docker compose down && docker compose up -d
```

### zerde_cache.db не найден

Файл монтируется из корня проекта:
```bash
ls -lh zerde_cache.db  # должен существовать
```

### Мало места

```bash
docker system df           # использование
docker system prune        # очистка
docker image prune -a      # удалить неиспользуемые образы
```

### Adilet TLS ошибка

Это известная проблема. Сертификат adilet.zan.kz нестабильный. Параметр `ADILET_TLS_VERIFY=False` установлен по умолчанию.

### uvicorn не подхватывает изменения в zerde/

Скрипт `dev.sh` должен передавать `--reload-dir ../../zerde`. Без этого модуль `zerde` остаётся стейл после импорта.
