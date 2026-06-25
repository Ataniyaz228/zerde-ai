"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { History, Plus, Sun, Moon, Menu, X, LogOut } from "lucide-react";
import { useTranslation } from "@/lib/i18n";
import { useTheme } from "@/lib/theme";
import { useAuth } from "@/lib/auth";
import Button from "@/components/ui/Button";
import SegmentedControl from "@/components/ui/SegmentedControl";
import s from "./Navbar.module.css";

export default function Navbar() {
  const { lang, setLang, t } = useTranslation();
  const { theme, toggle } = useTheme();
  const { user, authRequired, logout } = useAuth();
  const pathname = usePathname();
  const router = useRouter();
  const [scrolled, setScrolled] = useState(false);
  const [open, setOpen] = useState(false);

  // Показывать действия (история/новый анализ) только когда доступ открыт:
  // гейт выключен ИЛИ пользователь вошёл. На /login до входа — скрыто.
  const showNav = !authRequired || !!user;
  const showLogout = authRequired && !!user;

  const handleLogout = async () => {
    await logout();
    router.replace("/login");
  };

  const logoutButton = (
    <Button
      variant="icon"
      onClick={handleLogout}
      aria-label={`Выйти (${user})`}
      title={`Выйти (${user})`}
    >
      <LogOut size={16} strokeWidth={1.6} />
    </Button>
  );

  // Уплотняем шапку при прокрутке — тонкий сигнал глубины.
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 8);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  // Закрываем мобильное меню при переходе на другую страницу.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setOpen(false);
  }, [pathname]);

  // Пока меню открыто — Esc закрывает, скролл body заблокирован.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [open]);

  const isReports = pathname?.startsWith("/reports");

  const langControl = (
    <SegmentedControl
      size="sm"
      ariaLabel="Язык / Тіл"
      value={lang}
      onChange={setLang}
      options={[{ value: "ru", label: "RU" }, { value: "kz", label: "KZ" }]}
    />
  );

  const themeButton = (
    <Button
      variant="icon"
      onClick={toggle}
      aria-label={theme === "dark" ? "Светлая тема" : "Тёмная тема"}
      title={theme === "dark" ? "Светлая тема" : "Тёмная тема"}
    >
      {theme === "dark" ? <Sun size={16} strokeWidth={1.6} /> : <Moon size={16} strokeWidth={1.6} />}
    </Button>
  );

  return (
    <nav className={`${s.navbar} ${scrolled ? s.scrolled : ""}`}>
      <div className={s.inner}>
        <Link href="/" className={s.logo} aria-label="Zerde — на главную">
          <span className={s.logoMark} aria-hidden />
          <span className={s.logoText}>Zerde</span>
        </Link>

        {/* Десктоп: всё в строку (≥md) */}
        <div className={s.desktopNav}>
          {showNav && (
            <Link href="/reports" className={`${s.navLink} ${isReports ? s.navLinkActive : ""}`}>
              <History size={14} strokeWidth={1.6} />
              {t("nav_history")}
            </Link>
          )}
          <div className={s.controls}>
            {langControl}
            {themeButton}
            {showLogout && logoutButton}
          </div>
          {showNav && (
            <Button variant="primary" size="sm" href="/analyze">
              <Plus size={14} strokeWidth={2} />
              {t("nav_new")}
            </Button>
          )}
        </div>

        {/* Мобильный триггер (<md) */}
        <button
          className={s.burger}
          aria-label={open ? t("nav_close") : t("nav_menu")}
          aria-expanded={open}
          aria-controls="mobile-menu"
          onClick={() => setOpen((v) => !v)}
        >
          {open ? <X size={20} strokeWidth={1.8} /> : <Menu size={20} strokeWidth={1.8} />}
        </button>
      </div>

      {/* Мобильная панель */}
      {open && (
        <>
          <div className={s.scrim} onClick={() => setOpen(false)} aria-hidden />
          <div className={s.mobileMenu} id="mobile-menu">
            {showNav && (
              <Link href="/reports" className={`${s.mobileLink} ${isReports ? s.mobileLinkActive : ""}`}>
                <History size={16} strokeWidth={1.6} />
                {t("nav_history")}
              </Link>
            )}
            {showNav && (
              <Button variant="primary" block href="/analyze">
                <Plus size={16} strokeWidth={2} />
                {t("nav_new")}
              </Button>
            )}
            <div className={s.mobileRow}>
              {langControl}
              {themeButton}
              {showLogout && logoutButton}
            </div>
          </div>
        </>
      )}
    </nav>
  );
}
