"use client";
import { CheckCircle2, XCircle, AlertTriangle } from "lucide-react";
import { useTranslation } from "@/lib/i18n";
import s from "./VerdictBadges.module.css";

export interface VerdictCounts {
  confirmed: number;
  contradicted: number;
  unverified: number;
  total: number;
  coverage_pct: number;
}

interface Props {
  counts: VerdictCounts;
  /** "full" — chips + coverage bar (report header); "compact" — inline pills (table rows). */
  variant?: "full" | "compact";
}

export default function VerdictBadges({ counts, variant = "full" }: Props) {
  const { t } = useTranslation();
  const { confirmed, contradicted, unverified, total, coverage_pct } = counts;

  // No verdict data (old reports) — render nothing.
  if (!total) return null;

  if (variant === "compact") {
    return (
      <div className={s.compact}>
        <span className={`${s.pill} ${s.good}`} title={t("verdict_confirmed")}>
          <CheckCircle2 size={12} strokeWidth={2.2} />{confirmed}
        </span>
        <span className={`${s.pill} ${s.bad}`} title={t("verdict_contradicted")}>
          <XCircle size={12} strokeWidth={2.2} />{contradicted}
        </span>
        <span className={`${s.pill} ${s.warn}`} title={t("verdict_unverified")}>
          <AlertTriangle size={12} strokeWidth={2.2} />{unverified}
        </span>
      </div>
    );
  }

  return (
    <div className={s.full}>
      <div className={s.chips}>
        <div className={`${s.chip} ${s.good}`}>
          <CheckCircle2 size={16} strokeWidth={2} />
          <div className={s.chipBody}>
            <span className={s.chipNum}>{confirmed}</span>
            <span className={s.chipLabel}>{t("verdict_confirmed")}</span>
          </div>
        </div>
        <div className={`${s.chip} ${s.bad}`}>
          <XCircle size={16} strokeWidth={2} />
          <div className={s.chipBody}>
            <span className={s.chipNum}>{contradicted}</span>
            <span className={s.chipLabel}>{t("verdict_contradicted")}</span>
          </div>
        </div>
        <div className={`${s.chip} ${s.warn}`}>
          <AlertTriangle size={16} strokeWidth={2} />
          <div className={s.chipBody}>
            <span className={s.chipNum}>{unverified}</span>
            <span className={s.chipLabel}>{t("verdict_unverified")}</span>
          </div>
        </div>
      </div>

      <div className={s.coverage}>
        <div className={s.coverageHead}>
          <span className={s.coverageLabel}>{t("verdict_coverage")}</span>
          <span className={s.coveragePct}>{coverage_pct}%</span>
        </div>
        <div className={s.coverageBar}>
          <div className={s.coverageFill} style={{ width: `${coverage_pct}%` }} />
        </div>
      </div>
    </div>
  );
}
