"use client";
import Link from "next/link";
import { Shield, History, Plus } from "lucide-react";
import { useTranslation } from "@/lib/i18n";
import s from "./Navbar.module.css";

export default function Navbar() {
  const { lang, setLang, t } = useTranslation();

  return (
    <nav className={s.navbar}>
      <div className={s.inner}>
        {/* Logo */}
        <Link href="/" className={s.logo}>
          <Shield size={16} className={s.logoIcon} strokeWidth={1.5} />
          <span className={s.logoText}>Zerde</span>
          <span className={s.logoBadge}>AI</span>
        </Link>

        {/* Navigation */}
        <div className={s.nav}>
          {/* Lang toggle */}
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

          <div className={s.divider} />

          <Link href="/reports" className={s.navLink}>
            <History size={14} strokeWidth={1.5} />
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
