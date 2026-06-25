// Куда фронт ходит за бэкендом. Приоритет:
//   1. NEXT_PUBLIC_API_URL — явный адрес, вшивается при сборке (override).
//   2. В браузере на проде — same-origin: фронт и бэкенд за одним доменом
//      Cloudflare-туннеля (роутинг по путям: /api,/ws → :8000, остальное → фронт).
//      Тогда сборка не зависит от домена и CORS не нужен (один origin).
//   3. Локальная разработка (frontend :3000) — бэкенд на :8000 того же хоста.
//   4. SSR/пререндер (window нет) — дев-фолбэк.
function resolveApiUrl(): string {
  const explicit = process.env.NEXT_PUBLIC_API_URL;
  if (explicit) return explicit;
  if (typeof window !== "undefined") {
    const { origin, hostname } = window.location;
    if (hostname === "localhost" || hostname === "127.0.0.1") {
      return "http://localhost:8000";
    }
    return origin;
  }
  return "http://localhost:8000";
}

export const API_URL = resolveApiUrl();

export const WS_URL = API_URL.replace(/^http/, "ws");

// Общий API-ключ бэкенда. Задаётся через NEXT_PUBLIC_API_KEY на проде, когда
// бэкенд запущен с ZERDE_API_KEY. Пусто в локальной разработке.
const API_KEY = process.env.NEXT_PUBLIC_API_KEY ?? "";

/** Заголовки авторизации для запросов к бэкенду (пусто, если ключ не задан). */
export function authHeaders(): Record<string, string> {
  return API_KEY ? { "X-API-Key": API_KEY } : {};
}

/** fetch с API-ключом и session-cookie (credentials) — для логина по сессии. */
export function apiFetch(input: string, init: RequestInit = {}): Promise<Response> {
  return fetch(input, {
    credentials: "include",
    ...init,
    headers: { ...authHeaders(), ...(init.headers ?? {}) },
  });
}
