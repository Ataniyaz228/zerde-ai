"use client";
import VerdictMark from "./VerdictMark";
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
  /** "full" — заголовок-ответ + полоса + легенда (шапка отчёта); "compact" — инлайн-пилюли (строки таблицы). */
  variant?: "full" | "compact";
}

type Kind = "good" | "bad" | "warn";

export default function VerdictBadges({ counts, variant = "full" }: Props) {
  const { t } = useTranslation();
  const { confirmed, contradicted, unverified, total, coverage_pct } = counts;

  // No verdict data (old reports) — render nothing.
  if (!total) return null;

  if (variant === "compact") {
    return (
      <div className={s.compact}>
        <span className={`${s.pill} ${s.good}`} title={t("verdict_confirmed")}>
          <VerdictMark kind="good" size={12} />{confirmed}
        </span>
        <span className={`${s.pill} ${s.bad}`} title={t("verdict_contradicted")}>
          <VerdictMark kind="bad" size={12} />{contradicted}
        </span>
        <span className={`${s.pill} ${s.warn}`} title={t("verdict_unverified")}>
          <VerdictMark kind="warn" size={12} />{unverified}
        </span>
      </div>
    );
  }

  // «Ответ» вперёд: контрадикции — главный сигнал для юриста; затем чистота;
  // иначе — нехватка данных.
  const headlineKind: Kind = contradicted > 0 ? "bad" : confirmed > 0 ? "good" : "warn";
  const headlineTitle =
    contradicted > 0 ? t("verdict_headline_bad")
    : confirmed > 0 ? t("verdict_headline_good")
    : t("verdict_headline_warn");
  const headlineSub =
    contradicted > 0
      ? `${contradicted} ${t("verdict_contradicted").toLowerCase()} · ${t("verdict_needs_review")}`
      : confirmed > 0
        ? `${confirmed} ${t("verdict_confirmed").toLowerCase()} · ${t("verdict_coverage").toLowerCase()} ${coverage_pct}%`
        : `${t("verdict_coverage")} ${coverage_pct}%`;

  const pct = (n: number) => (total ? `${(n / total) * 100}%` : "0%");
  const legend: { kind: Kind; n: number; label: string }[] = [
    { kind: "good", n: confirmed, label: t("verdict_confirmed") },
    { kind: "bad", n: contradicted, label: t("verdict_contradicted") },
    { kind: "warn", n: unverified, label: t("verdict_unverified") },
  ];

  return (
    <div className={s.full}>
      <div className={`${s.headline} ${s[headlineKind]}`}>
        <VerdictMark kind={headlineKind} size={24} className={s.headlineMark} />
        <div className={s.headlineBody}>
          <span className={s.headlineTitle}>{headlineTitle}</span>
          <span className={s.headlineSub}>{headlineSub}</span>
        </div>
      </div>

      <div className={s.bar} role="presentation">
        {confirmed > 0 && <div className={`${s.barSeg} ${s.barGood}`} style={{ width: pct(confirmed) }} />}
        {contradicted > 0 && <div className={`${s.barSeg} ${s.barBad}`} style={{ width: pct(contradicted) }} />}
        {unverified > 0 && <div className={`${s.barSeg} ${s.barWarn}`} style={{ width: pct(unverified) }} />}
      </div>

      <div className={s.legend}>
        {legend.map((it) => (
          <div key={it.label} className={`${s.legendItem} ${s[it.kind]}`}>
            <span className={s.legendTop}>
              <VerdictMark kind={it.kind} size={14} className={s.legendMark} />
              <span className={s.legendNum}>{it.n}</span>
            </span>
            <span className={s.legendLabel}>{it.label}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
