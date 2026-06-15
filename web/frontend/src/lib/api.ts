export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export const WS_URL = API_URL.replace(/^http/, "ws");

// Общий API-ключ бэкенда. Задаётся через NEXT_PUBLIC_API_KEY на проде, когда
// бэкенд запущен с ZERDE_API_KEY. Пусто в локальной разработке.
const API_KEY = process.env.NEXT_PUBLIC_API_KEY ?? "";

/** Заголовки авторизации для запросов к бэкенду (пусто, если ключ не задан). */
export function authHeaders(): Record<string, string> {
  return API_KEY ? { "X-API-Key": API_KEY } : {};
}

/** fetch с автоматически проставленным API-ключом. */
export function apiFetch(input: string, init: RequestInit = {}): Promise<Response> {
  return fetch(input, { ...init, headers: { ...authHeaders(), ...(init.headers ?? {}) } });
}
