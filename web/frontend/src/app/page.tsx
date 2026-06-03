"use client";
import Link from "next/link";
import { motion } from "framer-motion";
import { ArrowRight, Scale, ShieldCheck, FileText, Activity } from "lucide-react";
import { useTranslation } from "@/lib/i18n";
import s from "./Home.module.css";

const fadeUp = (delay = 0) => ({
  initial: { opacity: 0, y: 14 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.55, delay, ease: "easeOut" as const },
});

export default function Home() {
  const { t } = useTranslation();

  const STAGES = [
    { label: t("step_extract_short"), pct: 100 },
    { label: t("step_search_short"), pct: 78 },
    { label: t("step_verify_short"), pct: 54 },
    { label: t("step_report_short"), pct: 30 },
  ];

  const FEATURES = [
    { icon: Scale, title: t("feat_accuracy"), desc: t("feat_accuracy_desc") },
    { icon: ShieldCheck, title: t("feat_risk"), desc: t("feat_risk_desc") },
    { icon: FileText, title: t("feat_report"), desc: t("feat_report_desc") },
  ];

  return (
    <div className={s.page}>
      <div className={s.inner}>
        <div className={s.grid}>
          {/* ── Left: Hero ── */}
          <div>
            <motion.div {...fadeUp(0)} className={s.badge}>
              <Activity size={12} strokeWidth={2} />
              {t("hero_badge")}
            </motion.div>

            <motion.h1 {...fadeUp(0.08)} className={s.title}>
              <span className={s.titleLight}>{t("hero_title_1")}<br /></span>
              <span className={s.titleDim}>{t("hero_title_2")}</span>
            </motion.h1>

            <motion.p {...fadeUp(0.16)} className={s.sub}>
              {t("hero_sub")}
            </motion.p>

            <motion.div {...fadeUp(0.24)} className={s.actions}>
              <Link href="/analyze" className={s.cta}>
                {t("hero_cta")}
                <ArrowRight size={15} strokeWidth={2} />
              </Link>
              <Link href="/reports" className={s.ctaSecondary}>
                {t("hero_link")}
              </Link>
            </motion.div>
          </div>

          {/* ── Right: Decorative card ── */}
          <motion.div
            initial={{ opacity: 0, scale: 0.97 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ duration: 0.7, delay: 0.3, ease: [0.22, 1, 0.36, 1] }}
            className={s.decorativeCol}
          >
            <div className={s.card}>
              <div className={s.cardHeader}>
                <div className={s.cardDots}>
                  <div className={s.dot} /><div className={s.dot} /><div className={s.dot} />
                </div>
                <span className={s.cardLabel}>zerde.pipeline</span>
              </div>

              {STAGES.map((stage, i) => (
                <div key={i} className={s.cardRow}>
                  <div className={s.cardRowIcon}>
                    <Activity size={12} strokeWidth={2} />
                  </div>
                  <div className={s.cardRowBody}>
                    <p className={s.cardRowLabel}>{stage.label}</p>
                    <div className={s.cardRowBar}>
                      <motion.div
                        className={s.cardRowFill}
                        initial={{ width: 0 }}
                        animate={{ width: `${stage.pct}%` }}
                        transition={{ duration: 1.2, delay: 0.6 + i * 0.15, ease: [0.22, 1, 0.36, 1] }}
                      />
                    </div>
                  </div>
                </div>
              ))}

              <div className={s.cardFooter}>
                <span className={s.cardFooterDesc}>{t("card_footer_desc")}</span>
              </div>
            </div>
          </motion.div>
        </div>

        {/* ── Features row ── */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, delay: 0.5 }}
          className={s.features}
        >
          {FEATURES.map((f, i) => (
            <div key={i} className={s.feature}>
              <div className={s.featureIconWrap}>
                <f.icon size={18} strokeWidth={1.5} />
              </div>
              <h3 className={s.featureTitle}>{f.title}</h3>
              <p className={s.featureDesc}>{f.desc}</p>
            </div>
          ))}
        </motion.div>
      </div>
    </div>
  );
}
