"use client";
import { motion } from "framer-motion";
import { ArrowRight, ShieldCheck } from "lucide-react";
import { useTranslation } from "@/lib/i18n";
import { fadeUp, fadeInUp, lineRevealGroup, lineRevealItem } from "@/lib/motion";
import Button from "@/components/ui/Button";
import s from "./Home.module.css";

export default function Home() {
  const { t } = useTranslation();

  // Реальные цифры корпуса (38 законов / ~20k норм / первоисточник adilet).
  const STATS = [
    { value: "38", label: t("stats_codes") },
    { value: "20 000+", label: t("stats_norms") },
    { value: "adilet.zan.kz", label: t("stats_source") },
  ] as const;

  // Реальные этапы пайплайна — те же, что пользователь видит при анализе.
  const STEPS = [
    { n: "01", name: t("step_extract_short") },
    { n: "02", name: t("step_search_short") },
    { n: "03", name: t("step_verify_short") },
    { n: "04", name: t("step_report_short") },
  ];

  return (
    <div className={s.page}>
      {/* ── Hero ── */}
      <section className={s.hero}>
        <div className={s.heroLayout}>
          <div className={s.heroInner}>
            <motion.span {...fadeInUp(0)} className={s.eyebrow}>
              {t("hero_eyebrow")}
            </motion.span>

            <motion.h1 className={s.display} {...lineRevealGroup(0.1)}>
              <span className={s.lineMask}>
                <motion.span variants={lineRevealItem} className={s.line}>
                  {t("hero_title_1")}
                </motion.span>
              </span>
              <span className={s.lineMask}>
                <motion.span variants={lineRevealItem} className={`${s.line} ${s.lineMuted}`}>
                  {t("hero_title_2")}
                </motion.span>
              </span>
            </motion.h1>

            <motion.p {...fadeInUp(0.45)} className={s.sub}>
              {t("hero_sub")}
            </motion.p>

            <motion.div {...fadeInUp(0.55)} className={s.actions}>
              <Button variant="primary" size="lg" href="/analyze">
                {t("hero_cta")}
                <ArrowRight size={16} strokeWidth={2} />
              </Button>
              <Button variant="ghost" size="lg" href="/reports">
                {t("hero_link")}
                <ArrowRight size={15} strokeWidth={2} />
              </Button>
            </motion.div>

            <motion.span {...fadeInUp(0.63)} className={s.trust}>
              <ShieldCheck size={14} strokeWidth={1.8} />
              {t("hero_trust")}
            </motion.span>
          </div>

          {/* Реальный казахстанский ландмарк — Дворец мира и согласия (Астана). */}
          <motion.figure
            className={s.heroVisual}
            initial={{ opacity: 0, y: 24 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.8, delay: 0.25, ease: [0.16, 1, 0.3, 1] }}
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src="/hero-pyramid.jpg"
              alt="Дворец мира и согласия, Астана"
              width={1600}
              height={1132}
              loading="eager"
              decoding="async"
            />
          </motion.figure>
        </div>
      </section>

      {/* ── Цифры корпуса ── */}
      <motion.section {...fadeUp()} className={s.stats}>
        {STATS.map((st) => (
          <div key={st.label} className={s.stat}>
            <span className={s.statValue}>{st.value}</span>
            <span className={s.statLabel}>{st.label}</span>
          </div>
        ))}
      </motion.section>

      {/* ── Этапы (минимальная честная полоса) ── */}
      <motion.section {...fadeUp()} className={s.how}>
        <span className="eyebrow">{t("how_eyebrow")}</span>
        <ol className={s.steps}>
          {STEPS.map((st) => (
            <li key={st.n} className={s.step}>
              <span className={s.stepDot}>{st.n}</span>
              <span className={s.stepName}>{st.name}</span>
            </li>
          ))}
        </ol>
      </motion.section>

      {/* ── CTA ── */}
      <motion.section {...fadeUp()} className={s.ctaBand}>
        <div className={s.ctaBandText}>
          <h2 className={s.ctaBandTitle}>{t("cta_band_title")}</h2>
          <p className={s.ctaBandSub}>{t("cta_band_sub")}</p>
        </div>
        <Button variant="primary" size="lg" href="/analyze">
          {t("hero_cta")}
          <ArrowRight size={16} strokeWidth={2} />
        </Button>
      </motion.section>
    </div>
  );
}
