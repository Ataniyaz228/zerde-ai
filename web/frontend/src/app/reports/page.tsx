"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { motion } from "framer-motion";
import { FileText, Plus, ChevronRight } from "lucide-react";
import { useTranslation } from "@/lib/i18n";
import { API_URL } from "@/lib/api";
import s from "./Reports.module.css";

interface Report {
  id: string;
  filename: string;
  date: string;
  reliability_score: number;
  status: "done" | "pending";
}

function scoreClass(score: number): string {
  if (score >= 75) return s.scoreGood;
  if (score >= 50) return s.scoreWarn;
  return s.scoreBad;
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString("ru-RU", {
    day: "2-digit", month: "short", year: "numeric"
  });
}

export default function ReportsPage() {
  const { t } = useTranslation();
  const router = useRouter();
  const [reports, setReports] = useState<Report[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(`${API_URL}/api/reports`)
      .then((r) => r.json())
      .then((data) => { setReports(data); setLoading(false); })
      .catch(() => setLoading(false));
  }, []);

  return (
    <div className={s.page}>
      <div className={s.inner}>
        <div className={s.pageHeader}>
          <h1 className={s.title}>{t("reports_title")}</h1>
          {!loading && reports.length > 0 && (
            <span className={s.count}>{reports.length} отчётов</span>
          )}
        </div>

        {loading ? (
          <div className={s.tableWrap}>
            <div className={s.loadingRow}>{t("loading")}</div>
          </div>
        ) : reports.length === 0 ? (
          <div className={s.tableWrap}>
            <div className={s.empty}>
              <div className={s.emptyIcon}><FileText size={20} strokeWidth={1.5} /></div>
              <p className={s.emptyTitle}>{t("reports_empty")}</p>
              <p style={{ fontSize: 13, color: "var(--text-tertiary)" }}>{t("reports_empty_sub")}</p>
              <Link href="/analyze" className={s.emptyLink}>
                <Plus size={14} strokeWidth={2} />
                {t("nav_new")}
              </Link>
            </div>
          </div>
        ) : (
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            className={s.tableWrap}
          >
            <table className={s.table}>
              <thead className={s.thead}>
                <tr>
                  <th className={s.th}>{t("reports_col_file")}</th>
                  <th className={s.th}>{t("reports_col_date")}</th>
                  <th className={s.th}>{t("reports_col_score")}</th>
                  <th className={s.th}>{t("reports_col_status")}</th>
                  <th className={s.th} />
                </tr>
              </thead>
              <tbody>
                {reports.map((r) => (
                  <tr
                    key={r.id}
                    className={s.tr}
                    onClick={() => router.push(`/reports/${r.id}`)}
                  >
                    <td className={s.td}>
                      <div className={s.fileCell}>
                        <div className={s.fileIcon}>
                          <FileText size={14} strokeWidth={1.5} />
                        </div>
                        <span className={s.fileName}>{r.filename}</span>
                      </div>
                    </td>
                    <td className={s.td}>
                      <span className={s.dateText}>{formatDate(r.date)}</span>
                    </td>
                    <td className={s.td}>
                      <span className={`${s.score} ${scoreClass(r.reliability_score)}`}>
                        {r.reliability_score}%
                      </span>
                    </td>
                    <td className={s.td}>
                      <span className={`${s.badge} ${r.status === "done" ? s.badgeDone : s.badgePending}`}>
                        {r.status === "done" ? "Готов" : "В обработке"}
                      </span>
                    </td>
                    <td className={s.td} style={{ width: 32 }}>
                      <ChevronRight size={14} strokeWidth={1.5} style={{ color: "var(--text-tertiary)" }} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </motion.div>
        )}
      </div>
    </div>
  );
}
