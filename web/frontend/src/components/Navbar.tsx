"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { History, Plus, Sun, Moon } from "lucide-react";
import { useTranslation } from "@/lib/i18n";
import { useTheme } from "@/lib/theme";
import s from "./Navbar.module.css";

export default function Navbar() {
  const { lang, setLang, t } = useTranslation();
  const { theme, toggle } = useTheme();
  const pathname = usePathname();
  const [scrolled, setScrolled] = useState(false);

  // Уплотняем шапку при прокрутке — тонкий сигнал глубины, без липкой тени на старте.
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 8);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  const isReports = pathname?.startsWith("/reports");

  return (
    <nav className={`${s.navbar} ${scrolled ? s.scrolled : ""}`}>
      <div className={s.inner}>
        <Link href="/" className={s.logo} aria-label="Zerde — на главную">
          <span className={s.logoMark} aria-hidden />
          <span className={s.logoText}>Zerde</span>
        </Link>

        <div className={s.nav}>
          <div className={s.langToggle} role="group" aria-label="Язык / Тіл">
            <button
              className={`${s.langBtn} ${lang === "ru" ? s.langBtnActive : ""}`}
              onClick={() => setLang("ru")}
              aria-pressed={lang === "ru"}
            >
              RU
            </button>
            <button
              className={`${s.langBtn} ${lang === "kz" ? s.langBtnActive : ""}`}
              onClick={() => setLang("kz")}
              aria-pressed={lang === "kz"}
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

          <Link href="/reports" className={`${s.navLink} ${isReports ? s.navLinkActive : ""}`}>
            <History size={14} strokeWidth={1.6} />
            <span className={s.navLinkLabel}>{t("nav_history")}</span>
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
