"use client";
import Link from "next/link";
import { useTranslation } from "@/lib/i18n";
import s from "./Footer.module.css";

export default function Footer() {
  const { t } = useTranslation();
  const year = new Date().getFullYear();

  return (
    <footer className={s.footer}>
      <div className={s.inner}>
        <div className={s.brandCol}>
          <Link href="/" className={s.logo}>
            <span className={s.logoMark} aria-hidden />
            <span className={s.logoText}>Zerde</span>
          </Link>
          <p className={s.tagline}>{t("footer_tagline")}</p>
        </div>

        <nav className={s.navCol} aria-label={t("footer_nav")}>
          <span className={s.colHead}>{t("footer_nav")}</span>
          <Link href="/" className={s.link}>{t("footer_home")}</Link>
          <Link href="/analyze" className={s.link}>{t("nav_new")}</Link>
          <Link href="/reports" className={s.link}>{t("nav_history")}</Link>
        </nav>

        <div className={s.sourceCol}>
          <span className={s.colHead}>{t("footer_source")}</span>
          <a
            href="https://adilet.zan.kz"
            target="_blank"
            rel="noopener noreferrer"
            className={s.sourceLink}
          >
            adilet.zan.kz
          </a>
        </div>
      </div>

      <div className={s.baseline}>
        <span>© {year} Zerde</span>
        <span className={s.made}>{t("footer_made")}</span>
      </div>
    </footer>
  );
}
