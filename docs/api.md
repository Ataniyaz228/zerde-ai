# REST API и WebSocket

Backend — FastAPI-приложение, работает на порту 8000.

## Base URL

```
http://localhost:8000
```

## Endpoints

### Health Check

```
GET /health
```

**Ответ:**
```json
{"status": "ok"}
```

---

### Загрузить документ и начать анализ

```
POST /api/analyze
Content-Type: multipart/form-data
```

**Параметры:**
| Поле | Тип | Описание |
|---|---|---|
| `file` | `UploadFile` | Документ для анализа (.docx, .pdf, .txt) |

**Ответ (200):**
```json
{
  "analysis_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "started"
}
```

**Ошибка (400):**
```json
{
  "detail": "Only .docx, .pdf, and .txt files are supported."
}
```

**Логика:** файл сохраняется в `data/raw/`, анализ запускается в фоновом процессе через `BackgroundTasks`. Прогресс отслеживается через WebSocket.

---

### Статус анализа

```
GET /api/analyze/{analysis_id}
```

**Ответ (200):**
```json
{
  "analysis_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "running"
}
```

Возможные значения `status`: `running`, `completed`, `error`, `unknown`.

**Ошибка (404):**
```json
{
  "detail": "Analysis not found"
}
```

---

### Список отчётов

```
GET /api/reports
```

**Ответ (200):**
```json
[
  {
    "id": "zerde_report_document_20260612_120000.md",
    "filename": "zerde_report_document_20260612_120000.md",
    "date": "2026-06-12",
    "reliability_score": 0.78
  }
]
```

Отчёты читаются из `output/`. Для каждого `.md`-файла ищется `.md.meta.json` сайдкар с метаданными и `reliability_score`.

---

### Содержимое отчёта

```
GET /api/reports/{report_id}
```

**Ответ (200):**
```json
{
  "id": "zerde_report_document_20260612_120000.md",
  "content": "# Отчёт Zerde AI...",
  "metadata": {
    "reliability_pct": 78,
    "confirmed": 12,
    "contradicted": 2,
    "unverified": 3
  }
}
```

---

## WebSocket протокол

```
ws://localhost:8000/ws/progress/{analysis_id}
```

Подключение происходит после `POST /api/analyze`. Сервер отправляет JSON-сообщения по мере прогресса пайплайна.

### Типы сообщений

#### `stage_start` — начало стадии

```json
{
  "type": "stage_start",
  "stage": "extract",
  "message": "Загрузка и извлечение тезисов"
}
```

Стадии UI (упрощённые):
| `stage` | Описание | Pipeline стадии |
|---|---|---|
| `extract` | Извлечение тезисов | S1, S2, S2.5, S2.7 |
| `search` | Поиск в базе НПА | S3, S4 |
| `verify` | Верификация и анализ | S5, S5.2, S5.5, S6 |
| `report` | Формирование отчёта | S7 |

#### `stage_done` — стадия завершена

```json
{
  "type": "stage_done",
  "stage": "extract"
}
```

#### `stage_error` — ошибка на стадии

```json
{
  "type": "stage_error",
  "stage": "verify",
  "message": "OpenAI API rate limit exceeded"
}
```

#### `done` — анализ завершён

```json
{
  "type": "done",
  "report": "# Отчёт Zerde AI...",
  "score": 78,
  "report_id": "zerde_report_document_20260612_120000.md"
}
```

`report` — полный Markdown-отчёт. `score` — reliability в процентах (0–100).

#### `error` — фатальная ошибка

```json
{
  "type": "error",
  "message": "Pipeline error: Connection refused"
}
```

### Пример подключения (JavaScript)

```javascript
const ws = new WebSocket(`ws://localhost:8000/ws/progress/${analysisId}`);

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);

  switch (data.type) {
    case 'stage_start':
      console.log(`Начало: ${data.message}`);
      break;
    case 'stage_done':
      console.log(`Завершено: ${data.stage}`);
      break;
    case 'done':
      console.log(`Отчёт готов, reliability: ${data.score}%`);
      renderReport(data.report);
      break;
    case 'error':
      console.error(`Ошибка: ${data.message}`);
      break;
  }
};
```
