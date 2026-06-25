"use client";

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { usePathname, useRouter } from "next/navigation";
import { API_URL, apiFetch } from "@/lib/api";

type AuthState = {
  /** Имя вошедшего пользователя, либо null. */
  user: string | null;
  /** Включён ли гейт на бэкенде (есть ZERDE_AUTH_SECRET). */
  authRequired: boolean;
  /** Первичная проверка сессии завершена. */
  ready: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthState | null>(null);

export const PUBLIC_PATHS = new Set<string>(["/login"]);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<string | null>(null);
  const [authRequired, setAuthRequired] = useState(false);
  const [ready, setReady] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const r = await apiFetch(`${API_URL}/api/auth/me`);
      if (r.ok) {
        const data = await r.json();
        setAuthRequired(Boolean(data.auth_required));
        setUser(data.username ?? null);
      } else {
        // 401 — гейт включён, но мы не вошли.
        setAuthRequired(true);
        setUser(null);
      }
    } catch {
      setAuthRequired(true);
      setUser(null);
    } finally {
      setReady(true);
    }
  }, []);

  useEffect(() => {
    // refresh() — async: setState срабатывает после await, не синхронно в effect.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
  }, [refresh]);

  const login = useCallback(async (username: string, password: string) => {
    const r = await apiFetch(`${API_URL}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (!r.ok) {
      const detail = await r.json().catch(() => ({}));
      throw new Error(detail.detail || "Не удалось войти");
    }
    const data = await r.json();
    setAuthRequired(true);
    setUser(data.username);
  }, []);

  const logout = useCallback(async () => {
    await apiFetch(`${API_URL}/api/auth/logout`, { method: "POST" }).catch(() => {});
    setUser(null);
  }, []);

  return (
    <AuthContext.Provider value={{ user, authRequired, ready, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within <AuthProvider>");
  return ctx;
}

/**
 * Защита контента: пока auth включён и пользователь не вошёл — редирект на /login.
 * /login и прочие PUBLIC_PATHS пропускаются всегда. Если auth выключен на бэкенде
 * (dev), гейта нет.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const { user, authRequired, ready } = useAuth();
  const pathname = usePathname();
  const router = useRouter();
  const isPublic = pathname ? PUBLIC_PATHS.has(pathname) : false;

  useEffect(() => {
    if (ready && authRequired && !user && !isPublic) {
      router.replace("/login");
    }
  }, [ready, authRequired, user, isPublic, router]);

  if (isPublic) return <>{children}</>;
  if (!ready) {
    return <div className="authLoading">Загрузка…</div>;
  }
  if (authRequired && !user) {
    // идёт редирект на /login
    return null;
  }
  return <>{children}</>;
}
