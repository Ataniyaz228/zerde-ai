"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Button from "@/components/ui/Button";
import { useAuth } from "@/lib/auth";
import s from "./Login.module.css";

export default function LoginPage() {
  const { login, user, authRequired, ready } = useAuth();
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // Уже вошли (или гейт выключен) — на главную.
  useEffect(() => {
    if (ready && (user || !authRequired)) router.replace("/");
  }, [ready, user, authRequired, router]);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      await login(username.trim(), password);
      router.replace("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось войти");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className={s.wrap}>
      <form className={s.card} onSubmit={onSubmit}>
        <div className={s.brand}>
          <span className={s.mark} aria-hidden />
          <span className={s.brandText}>Zerde</span>
        </div>
        <h1 className={s.title}>Вход</h1>
        <p className={s.subtitle}>Доступ к анализу — по приглашению.</p>

        <label className={s.field}>
          <span className={s.label}>Логин</span>
          <input
            className={s.input}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
            required
          />
        </label>

        <label className={s.field}>
          <span className={s.label}>Пароль</span>
          <input
            className={s.input}
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>

        {error && (
          <div className={s.error} role="alert">
            {error}
          </div>
        )}

        <Button type="submit" variant="primary" block disabled={loading}>
          {loading ? "Вход…" : "Войти"}
        </Button>
      </form>
    </div>
  );
}
