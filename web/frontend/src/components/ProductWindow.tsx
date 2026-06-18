"use client";
import { motion, useReducedMotion } from "framer-motion";
import { Check } from "lucide-react";
import { useTranslation } from "@/lib/i18n";
import s from "./ProductWindow.module.css";

const SCORE = 92;
const R = 26;
const CIRC = 2 * Math.PI * R;

/** Продуктовый визуал hero — карточка-вердикт: дословная цитата как улика +
 *  круговой индикатор надёжности. На скролле «оживает»: цитата проявляется,
 *  печать впечатывается, кольцо дозаполняется. Презентационный → aria-hidden. */
export default function ProductWindow() {
  const { t } = useTranslation();
  const reduced = useReducedMotion();
  const view = { once: true, margin: "-60px" } as const;
  const offset = CIRC * (1 - SCORE / 100);

  return (
    <div className={s.exhibit} aria-hidden>
      <div className={s.head}>
        <span className={s.kicker}>{t("pw_exhibit_label")}</span>
        <span className={s.source}>adilet.zan.kz</span>
      </div>

      <div className={s.sheet}>
        <div className={s.sheetHead}>
          <span className={s.claimLabel}>{t("pw_claim_label")}</span>
          <motion.span
            className={s.stamp}
            initial={reduced ? false : { opacity: 0, scale: 1.3 }}
            whileInView={{ opacity: 1, scale: 1 }}
            viewport={view}
            transition={{ duration: 0.45, delay: 0.5, ease: [0.16, 1, 0.3, 1] }}
          >
            <Check size={12} strokeWidth={3} />
            {t("verdict_confirmed")}
          </motion.span>
        </div>

        <p className={s.claim}>{t("sources_card_claim")}</p>

        <div className={s.quoteMask}>
          <motion.blockquote
            className={s.quote}
            initial={reduced ? false : { clipPath: "inset(0 100% 0 0)" }}
            whileInView={{ clipPath: "inset(0 0% 0 0)" }}
            viewport={view}
            transition={{ duration: 0.8, delay: 0.15, ease: [0.65, 0, 0.35, 1] }}
          >
            {t("sources_card_quote")}
          </motion.blockquote>
        </div>

        <span className={s.sourceTag}>ст. 14 · adilet.zan.kz</span>
      </div>

      <div className={s.scoreRow}>
        <div className={s.ring}>
          <svg viewBox="0 0 64 64" className={s.ringSvg}>
            <circle className={s.ringTrack} cx="32" cy="32" r={R} />
            <motion.circle
              className={s.ringFill}
              cx="32"
              cy="32"
              r={R}
              strokeDasharray={CIRC}
              initial={reduced ? false : { strokeDashoffset: CIRC }}
              whileInView={{ strokeDashoffset: offset }}
              viewport={view}
              transition={{ duration: 1.1, delay: 0.3, ease: [0.16, 1, 0.3, 1] }}
            />
          </svg>
          <span className={s.ringVal}>{SCORE}%</span>
        </div>
        <div className={s.scoreText}>
          <span className={s.scoreLabel}>{t("report_reliability")}</span>
          <span className={s.scoreHint}>{t("sources_verified")}</span>
        </div>
      </div>
    </div>
  );
}
