"use client";
import Link from "next/link";
import { motion } from "framer-motion";
import { ArrowRight, ShieldCheck } from "lucide-react";
import { useTranslation } from "@/lib/i18n";
import AnimatedNumber from "@/components/AnimatedNumber";
import s from "./Home.module.css";

const fadeUp = (delay = 0) => ({
  initial: { opacity: 0, y: 16 },
  whileInView: { opacity: 1, y: 0 },
  viewport: { once: true, margin: "-60px" },
  transition: { duration: 0.6, delay, ease: [0.22, 1, 0.36, 1] as const },
});

export default function Home() {
  const { t } = useTranslation();

  const STATS = [
    { to: 38, suffix: "", label: t("stats_codes") },
    { to: 20000, suffix: "+", label: t("stats_norms") },
    { text: "adilet.zan.kz", label: t("stats_source") },
  ] as const;

  const STEPS = [
    { n: "01", title: t("feat_accuracy"), desc: t("feat_accuracy_desc") },
    { n: "02", title: t("feat_risk"), desc: t("feat_risk_desc") },
    { n: "03", title: t("feat_report"), desc: t("feat_report_desc") },
  ];

  return (
    <div className={s.page}>
      {/* ── Hero: типографическое высказывание, без плавающих карточек ── */}
      <section className={s.hero}>
        <motion.span
          initial={{ opacity: 0, y: 14 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
          className={`eyebrow ${s.eyebrow}`}
        >
          {t("hero_badge")}
        </motion.span>

        <motion.h1
          initial={{ opacity: 0, y: 18 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.65, delay: 0.06, ease: [0.22, 1, 0.36, 1] }}
          className={s.title}
        >
          {t("hero_title_1")}{" "}
          <span className={s.titleAccent}>{t("hero_title_2")}</span>
        </motion.h1>

        <motion.p
          initial={{ opacity: 0, y: 18 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.65, delay: 0.14, ease: [0.22, 1, 0.36, 1] }}
          className={s.sub}
        >
          {t("hero_sub")}
        </motion.p>

        <motion.div
          initial={{ opacity: 0, y: 18 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.65, delay: 0.22, ease: [0.22, 1, 0.36, 1] }}
          className={s.actions}
        >
          <Link href="/analyze" className={s.cta}>
            {t("hero_cta")}
            <ArrowRight size={16} strokeWidth={2} />
          </Link>
          <Link href="/reports" className={s.ctaSecondary}>
            {t("hero_link")}
          </Link>
          <span className={s.trust}>
            <ShieldCheck size={15} strokeWidth={1.7} />
            {t("hero_trust")}
          </span>
        </motion.div>
      </section>

      {/* ── Принцип ── */}
      <motion.section {...fadeUp()} className={s.principle}>
        <span className="eyebrow">{t("principle_eyebrow")}</span>
        <blockquote className={s.quote} data-dropcap>{t("principle_text")}</blockquote>
      </motion.section>

      {/* ── Цифры ── */}
      <motion.section {...fadeUp()} className={s.stats}>
        {STATS.map((st) => (
          <div key={st.label} className={s.stat}>
            <span className={s.statValue}>
              {"to" in st ? <AnimatedNumber to={st.to} suffix={st.suffix} /> : st.text}
            </span>
            <span className={s.statLabel}>{st.label}</span>
          </div>
        ))}
      </motion.section>

      {/* ── Как это работает ── */}
      <motion.section {...fadeUp()} className={s.how}>
        <div className={s.howHead}>
          <span className="eyebrow">{t("how_eyebrow")}</span>
          <p className={s.howLead}>{t("how_lead")}</p>
        </div>
        <div className={s.steps}>
          {STEPS.map((step, i) => (
            <motion.div
              key={step.n}
              initial={{ opacity: 0, y: 18 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-40px" }}
              transition={{ duration: 0.55, delay: i * 0.1, ease: [0.22, 1, 0.36, 1] }}
              className={s.step}
            >
              <span className={s.stepNum}>{step.n}</span>
              <h3 className={s.stepTitle}>{step.title}</h3>
              <p className={s.stepDesc}>{step.desc}</p>
            </motion.div>
          ))}
        </div>
      </motion.section>

      {/* ── CTA-полоса ── */}
      <motion.section {...fadeUp()} className={s.ctaBand}>
        <div className={s.ctaBandText}>
          <h2 className={s.ctaBandTitle}>{t("cta_band_title")}</h2>
          <p className={s.ctaBandSub}>{t("cta_band_sub")}</p>
        </div>
        <Link href="/analyze" className={s.ctaBandBtn}>
          {t("hero_cta")}
          <ArrowRight size={16} strokeWidth={2} />
        </Link>
      </motion.section>
    </div>
  );
}
