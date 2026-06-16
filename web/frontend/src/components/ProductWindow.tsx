"use client";
import { Check, FileText } from "lucide-react";
import { useTranslation } from "@/lib/i18n";
import s from "./ProductWindow.module.css";

/** Декоративное «окно продукта» на чистом CSS — центральный визуал hero.
 *  Показывает образец вердикта: утверждение → статус → цитата-источник → надёжность.
 *  Чисто презентационный, поэтому aria-hidden. */
export default function ProductWindow() {
  const { t } = useTranslation();

  return (
    <div className={s.window} aria-hidden>
      <div className={s.titlebar}>
        <span className={s.dots}>
          <i /><i /><i />
        </span>
        <span className={s.file}>
          <FileText size={12} strokeWidth={1.6} />
          zakon-proekt.docx
        </span>
        <span className={s.url}>adilet.zan.kz</span>
      </div>

      <div className={s.body}>
        <div className={s.card}>
          <div className={s.cardHead}>
            <span className={s.claimLabel}>{t("pw_claim_label")}</span>
            <span className={s.statusPill}>
              <Check size={12} strokeWidth={2.6} />
              {t("verdict_confirmed")}
            </span>
          </div>

          <p className={s.claim}>{t("sources_card_claim")}</p>

          <blockquote className={s.quote}>{t("sources_card_quote")}</blockquote>

          <div className={s.sourceRow}>
            <span className={s.sourceTag}>ст. 14 · adilet.zan.kz</span>
            <span className={s.verified}>{t("sources_verified")}</span>
          </div>
        </div>

        <div className={s.scoreRow}>
          <span className={s.scoreLabel}>{t("report_reliability")}</span>
          <div className={s.bar}>
            <div className={s.barFill} style={{ width: "92%" }} />
          </div>
          <span className={s.scoreVal}>92%</span>
        </div>
      </div>
    </div>
  );
}
