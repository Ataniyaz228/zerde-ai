"use client";
import { motion } from "framer-motion";
import { ArrowRight, ShieldCheck } from "lucide-react";
import { useTranslation } from "@/lib/i18n";
import { fadeUp, fadeInUp } from "@/lib/motion";
import AnimatedNumber from "@/components/AnimatedNumber";
import ProductWindow from "@/components/ProductWindow";
import Button from "@/components/ui/Button";
import s from "./Home.module.css";

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
      {/* ── Hero: светлый премиум на чистом CSS ── */}
      <section className={s.hero}>
        <div className={s.heroGlow} aria-hidden />
        <div className={s.heroGrid} aria-hidden />

        <div className={s.heroInner}>
          <motion.span {...fadeInUp(0)} className={s.pill}>
            <span className={s.pillDot} />
            {t("hero_badge")}
          </motion.span>

          <motion.h1 {...fadeInUp(0.06)} className={s.title}>
            {t("hero_title_1")}
            <br />
            <span className={s.titleAccent}>{t("hero_title_2")}</span>
          </motion.h1>

          <motion.p {...fadeInUp(0.14)} className={s.sub}>
            {t("hero_sub")}
          </motion.p>

          <motion.div {...fadeInUp(0.22)} className={s.actions}>
            <Button variant="primary" size="lg" href="/analyze">
              {t("hero_cta")}
              <ArrowRight size={16} strokeWidth={2} />
            </Button>
            <Button variant="secondary" size="lg" href="/reports">
              {t("hero_link")}
            </Button>
          </motion.div>

          <motion.span {...fadeInUp(0.34)} className={s.trust}>
            <ShieldCheck size={14} strokeWidth={1.7} />
            {t("hero_trust")}
          </motion.span>
        </div>

        <motion.div
          initial={{ opacity: 0, y: 70, scale: 0.96 }}
          whileInView={{ opacity: 1, y: 0, scale: 1 }}
          viewport={{ once: true, margin: "-80px" }}
          transition={{ duration: 0.9, delay: 0.1, ease: [0.22, 1, 0.36, 1] }}
          className={s.windowWrap}
        >
          <ProductWindow />
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
            <motion.div key={step.n} {...fadeUp(i * 0.1)} className={s.step}>
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
        <Button variant="primary" size="lg" href="/analyze">
          {t("hero_cta")}
          <ArrowRight size={16} strokeWidth={2} />
        </Button>
      </motion.section>
    </div>
  );
}
