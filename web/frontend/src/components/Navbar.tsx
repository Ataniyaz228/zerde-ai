"use client";
import Link from "next/link";
import { History, Plus, Sun, Moon } from "lucide-react";
import { useTranslation } from "@/lib/i18n";
import { useTheme } from "@/lib/theme";
import s from "./Navbar.module.css";

export default function Navbar() {
  const { lang, setLang, t } = useTranslation();
  const { theme, toggle } = useTheme();

  return (
    <nav className={s.navbar}>
      <div className={s.inner}>
        <Link href="/" className={s.logo}>
          <span className={s.logoMark} aria-hidden />
          <span className={s.logoText}>Zerde</span>
        </Link>

        <div className={s.nav}>
          <div className={s.langToggle}>
            <button
              className={`${s.langBtn} ${lang === "ru" ? s.langBtnActive : ""}`}
              onClick={() => setLang("ru")}
            >
              RU
            </button>
            <button
              className={`${s.langBtn} ${lang === "kz" ? s.langBtnActive : ""}`}
              onClick={() => setLang("kz")}
            >
              KZ
            </button>
          </div>

          <button
            className={s.iconBtn}
            onClick={toggle}
            aria-label={theme === "dark" ? "Светлая тема" : "Тёмная тема"}
            title={theme === "dark" ? "Светлая тема" : "Тёмная тема"}
          >
            {theme === "dark" ? <Sun size={15} strokeWidth={1.6} /> : <Moon size={15} strokeWidth={1.6} />}
          </button>

          <div className={s.divider} />

          <Link href="/reports" className={s.navLink}>
            <History size={14} strokeWidth={1.6} />
            {t("nav_history")}
          </Link>

          <Link href="/analyze" className={s.navCta}>
            <Plus size={14} strokeWidth={2} />
            {t("nav_new")}
          </Link>
        </div>
      </div>
    </nav>
  );
}
