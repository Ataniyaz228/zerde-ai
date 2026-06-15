"use client";
import Link from "next/link";
import { motion } from "framer-motion";
import { ArrowRight } from "lucide-react";
import { useTranslation } from "@/lib/i18n";
import s from "./Home.module.css";

const fadeUp = (delay = 0) => ({
  initial: { opacity: 0, y: 14 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.5, delay, ease: [0.22, 1, 0.36, 1] as const },
});

export default function Home() {
  const { t } = useTranslation();

  const VERDICTS = [
    { kind: "ok", label: t("verdict_confirmed"), text: t("verdict_sample_confirmed") },
    { kind: "err", label: t("verdict_contradicted"), text: t("verdict_sample_contradicted") },
    { kind: "warn", label: t("verdict_unverified"), text: t("verdict_sample_unverified") },
  ] as const;

  const STATS = [
    { value: "38", label: t("stats_codes") },
    { value: "20 000+", label: t("stats_norms") },
    { value: "adilet.zan.kz", label: t("stats_source") },
  ];

  const STEPS = [
    { n: "01", title: t("feat_accuracy"), desc: t("feat_accuracy_desc") },
    { n: "02", title: t("feat_risk"), desc: t("feat_risk_desc") },
    { n: "03", title: t("feat_report"), desc: t("feat_report_desc") },
  ];

  return (
    <div className={s.page}>
      {/* ── Hero ── */}
      <section className={s.hero}>
        <div className={s.heroText}>
          <motion.span {...fadeUp(0)} className={`eyebrow ${s.eyebrow}`}>
            {t("hero_badge")}
          </motion.span>

          <motion.h1 {...fadeUp(0.06)} className={s.title}>
            {t("hero_title_1")}{" "}
            <span className={s.titleAccent}>{t("hero_title_2")}</span>
          </motion.h1>

          <motion.p {...fadeUp(0.12)} className={s.sub}>
            {t("hero_sub")}
          </motion.p>

          <motion.div {...fadeUp(0.18)} className={s.actions}>
            <Link href="/analyze" className={s.cta}>
              {t("hero_cta")}
              <ArrowRight size={16} strokeWidth={2} />
            </Link>
            <Link href="/reports" className={s.ctaSecondary}>
              {t("hero_link")}
            </Link>
          </motion.div>
        </div>

        {/* Образец вердиктов — показывает суть продукта вместо декоративной заглушки */}
        <motion.div
          initial={{ opacity: 0, y: 18 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, delay: 0.24, ease: [0.22, 1, 0.36, 1] }}
          className={s.sample}
        >
          {VERDICTS.map((v) => (
            <div key={v.kind} className={s.sampleRow}>
              <span className={`${s.tag} ${s[`tag_${v.kind}`]}`}>{v.label}</span>
              <p className={s.sampleText}>{v.text}</p>
            </div>
          ))}
        </motion.div>
      </section>

      {/* ── Принцип ── */}
      <section className={s.principle}>
        <span className="eyebrow">{t("principle_eyebrow")}</span>
        <blockquote className={s.quote}>{t("principle_text")}</blockquote>
      </section>

      {/* ── Цифры ── */}
      <section className={s.stats}>
        {STATS.map((st) => (
          <div key={st.label} className={s.stat}>
            <span className={s.statValue}>{st.value}</span>
            <span className={s.statLabel}>{st.label}</span>
          </div>
        ))}
      </section>

      {/* ── Как это работает ── */}
      <section className={s.how}>
        <span className="eyebrow">{t("how_eyebrow")}</span>
        <div className={s.steps}>
          {STEPS.map((step) => (
            <div key={step.n} className={s.step}>
              <span className={s.stepNum}>{step.n}</span>
              <h3 className={s.stepTitle}>{step.title}</h3>
              <p className={s.stepDesc}>{step.desc}</p>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
