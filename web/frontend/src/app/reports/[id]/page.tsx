"use client";
import { useEffect, useState, use } from "react";
import Link from "next/link";
import { motion } from "framer-motion";
import { ArrowLeft, Shield, ShieldAlert, AlertTriangle } from "lucide-react";
import ReportViewer from "@/components/ReportViewer";
import VerdictBadges from "@/components/VerdictBadges";
import { useTranslation } from "@/lib/i18n";
import { API_URL, apiFetch } from "@/lib/api";
import { scoreTier } from "@/lib/score";
import { formatDate } from "@/lib/format";
import s from "./ReportDetail.module.css";

interface ReportData {
  id: string;
  content: string;
  metadata: {
    filename: string;
    date: string;
    reliability_score: number;
    confirmed?: number;
    contradicted?: number;
    unverified?: number;
    total?: number;
    coverage_pct?: number;
  };
}

const SCORE_CLASS = { good: "scoreGood", warn: "scoreWarn", bad: "scoreBad" } as const;
const SCORE_ICON = { good: Shield, warn: ShieldAlert, bad: AlertTriangle } as const;

export default function ReportDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const { t, lang } = useTranslation();
  const [report, setReport] = useState<ReportData | null>(null);
  const [loading, setLoading] = useState(true);

  // Перезапрашиваем при смене языка: KZ — безопасный машинный перевод с бэкенда
  // (кэшируется там). RU — канонический отчёт.
  useEffect(() => {
    let alive = true;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    apiFetch(`${API_URL}/api/reports/${id}?lang=${lang}`)
      .then((r) => r.json())
      .then((data) => { if (alive) { setReport(data); setLoading(false); } })
      .catch(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [id, lang]);

  if (loading) {
    return (
      <div className={s.page}>
        <div className={s.inner}>
          <div className={s.center}>{t("loading")}</div>
        </div>
      </div>
    );
  }

  if (!report) {
    return (
      <div className={s.page}>
        <div className={s.inner}>
          <div className={s.center}>
            <AlertTriangle size={32} strokeWidth={1.5} />
            <p>{t("not_found")}</p>
            <Link href="/reports" className={s.back}>{t("report_back")}</Link>
          </div>
        </div>
      </div>
    );
  }

  const score = report.metadata.reliability_score;
  const tier = scoreTier(score);
  const scoreClass = s[SCORE_CLASS[tier]];
  const ScoreIcon = SCORE_ICON[tier];

  return (
    <div className={s.page}>
      <div className={s.inner}>
        <Link href="/reports" className={s.back}>
          <ArrowLeft size={14} className={s.backIcon} strokeWidth={1.5} />
          {t("report_back")}
        </Link>

        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.45 }}
        >
          {/* Header */}
          <div className={s.headerCard}>
            <div className={s.docInfo}>
              <p className={s.docMeta}>{t("report_source_doc")}</p>
              <h1 className={s.docName}>{report.metadata.filename}</h1>
              <p className={s.docDate}>{formatDate(report.metadata.date, "long")}</p>
            </div>

            <div className={`${s.scoreWrap} ${scoreClass}`}>
              <ScoreIcon size={28} strokeWidth={1.2} />
              <div>
                <p className={s.scoreLabel}>{t("report_reliability")}</p>
                <div className={s.scoreValue}>{score}%</div>
              </div>
            </div>
          </div>

          {/* Verdict dashboard */}
          <div className={s.dashboard}>
            <VerdictBadges
              counts={{
                confirmed: report.metadata.confirmed ?? 0,
                contradicted: report.metadata.contradicted ?? 0,
                unverified: report.metadata.unverified ?? 0,
                total: report.metadata.total ?? 0,
                coverage_pct: report.metadata.coverage_pct ?? 0,
              }}
            />
          </div>

          {/* Report body */}
          <ReportViewer
            content={report.content}
            downloadName={report.metadata.filename}
            reportId={report.id}
            note={lang === "kz" ? t("report_machine_translation") : undefined}
          />
        </motion.div>
      </div>
    </div>
  );
}
