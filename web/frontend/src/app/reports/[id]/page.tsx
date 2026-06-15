"use client";
import { useEffect, useState, use } from "react";
import Link from "next/link";
import { motion } from "framer-motion";
import { ArrowLeft, Shield, ShieldAlert, AlertTriangle } from "lucide-react";
import ReportViewer from "@/components/ReportViewer";
import VerdictBadges from "@/components/VerdictBadges";
import { useTranslation } from "@/lib/i18n";
import { API_URL } from "@/lib/api";
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

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString("ru-RU", {
    day: "2-digit", month: "long", year: "numeric"
  });
}

export default function ReportDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const { t } = useTranslation();
  const [report, setReport] = useState<ReportData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(`${API_URL}/api/reports/${id}`)
      .then((r) => r.json())
      .then((data) => { setReport(data); setLoading(false); })
      .catch(() => setLoading(false));
  }, [id]);

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
  const scoreClass = score >= 75 ? s.scoreGood : score >= 50 ? s.scoreWarn : s.scoreBad;
  const ScoreIcon = score >= 75 ? Shield : score >= 50 ? ShieldAlert : AlertTriangle;

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
              <p className={s.docDate}>{formatDate(report.metadata.date)}</p>
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
          <ReportViewer content={report.content} downloadName={report.metadata.filename} reportId={report.id} />
        </motion.div>
      </div>
    </div>
  );
}
