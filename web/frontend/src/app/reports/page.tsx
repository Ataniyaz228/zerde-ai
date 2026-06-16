"use client";
import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { motion } from "framer-motion";
import { FileText, ChevronRight, Search, AlertTriangle } from "lucide-react";
import { useTranslation } from "@/lib/i18n";
import { API_URL, apiFetch } from "@/lib/api";
import { scoreTier } from "@/lib/score";
import { formatDate } from "@/lib/format";
import VerdictBadges from "@/components/VerdictBadges";
import Button from "@/components/ui/Button";
import SegmentedControl from "@/components/ui/SegmentedControl";
import s from "./Reports.module.css";

interface Report {
  id: string;
  filename: string;
  date: string;
  reliability_score: number;
  status: "done" | "pending";
  confirmed: number;
  contradicted: number;
  unverified: number;
  total: number;
  coverage_pct: number;
}

type SortKey = "date" | "score";
type FilterKey = "all" | "flagged";

const SCORE_CLASS = { good: "scoreGood", warn: "scoreWarn", bad: "scoreBad" } as const;
function scoreClass(score: number): string {
  return s[SCORE_CLASS[scoreTier(score)]];
}

export default function ReportsPage() {
  const { t } = useTranslation();
  const router = useRouter();
  const [reports, setReports] = useState<Report[]>([]);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("date");
  const [filter, setFilter] = useState<FilterKey>("all");

  useEffect(() => {
    apiFetch(`${API_URL}/api/reports`)
      .then((r) => r.json())
      .then((data) => { setReports(data); setLoading(false); })
      .catch(() => setLoading(false));
  }, []);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    let rows = reports;
    if (q) rows = rows.filter((r) => r.filename.toLowerCase().includes(q));
    if (filter === "flagged") rows = rows.filter((r) => r.contradicted > 0);
    rows = [...rows].sort((a, b) =>
      sortKey === "score"
        ? b.reliability_score - a.reliability_score
        : new Date(b.date).getTime() - new Date(a.date).getTime(),
    );
    return rows;
  }, [reports, query, filter, sortKey]);

  const hasReports = !loading && reports.length > 0;

  return (
    <div className={s.page}>
      <div className={s.inner}>
        <div className={s.pageHeader}>
          <div className={s.headingGroup}>
            <span className={`eyebrow ${s.eyebrow}`}>{t("hero_badge")}</span>
            <h1 className={s.title}>{t("reports_title")}</h1>
          </div>
          {hasReports && (
            <span className={s.count}>{reports.length} {t("reports_count_label")}</span>
          )}
        </div>

        {hasReports && (
          <div className={s.controls}>
            <div className={s.searchWrap}>
              <Search size={14} strokeWidth={1.8} className={s.searchIcon} />
              <input
                className={s.searchInput}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={t("reports_search_placeholder")}
              />
            </div>
            <SegmentedControl
              ariaLabel={t("reports_filter_label")}
              value={filter}
              onChange={setFilter}
              options={[
                { value: "all", label: t("reports_filter_all") },
                {
                  value: "flagged",
                  label: <><AlertTriangle size={12} strokeWidth={2} />{t("reports_filter_flagged")}</>,
                },
              ]}
            />
            <SegmentedControl
              ariaLabel={t("reports_sort_label")}
              value={sortKey}
              onChange={setSortKey}
              options={[
                { value: "date", label: t("reports_sort_date") },
                { value: "score", label: t("reports_sort_score") },
              ]}
            />
          </div>
        )}

        {loading ? (
          <div className={s.stateWrap}>
            <div className={s.loadingRow}>{t("loading")}</div>
          </div>
        ) : reports.length === 0 ? (
          <div className={s.stateWrap}>
            <div className={s.empty}>
              <div className={s.emptyIcon}><FileText size={20} strokeWidth={1.5} /></div>
              <p className={s.emptyTitle}>{t("reports_empty")}</p>
              <p className={s.emptySub}>{t("reports_empty_sub")}</p>
              <Button variant="primary" href="/analyze">
                {t("nav_new")}
              </Button>
            </div>
          </div>
        ) : visible.length === 0 ? (
          <div className={s.stateWrap}>
            <div className={s.loadingRow}>{t("reports_nothing_found")}</div>
          </div>
        ) : (
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
          >
            {/* Карточки — телефон (<md) */}
            <div className={s.cards}>
              {visible.map((r) => (
                <Link key={r.id} href={`/reports/${r.id}`} className={s.card}>
                  <div className={s.cardTop}>
                    <div className={s.fileIcon}><FileText size={15} strokeWidth={1.5} /></div>
                    <div className={s.fileMeta}>
                      <span className={s.fileName} title={r.filename}>{r.filename}</span>
                      <span className={s.dateText}>{formatDate(r.date)}</span>
                    </div>
                    <span className={`${s.score} ${scoreClass(r.reliability_score)}`}>
                      {r.reliability_score}%
                    </span>
                  </div>
                  <div className={s.cardBottom}>
                    <VerdictBadges counts={r} variant="compact" />
                    <span className={`${s.badge} ${r.status === "done" ? s.badgeDone : s.badgePending}`}>
                      {r.status === "done" ? t("report_status_done") : t("report_status_pending")}
                    </span>
                  </div>
                  {r.contradicted > 0 && (
                    <span className={s.flag}>
                      <AlertTriangle size={11} strokeWidth={2.2} />
                      {t("report_has_contradictions")}
                    </span>
                  )}
                </Link>
              ))}
            </div>

            {/* Таблица — планшет+ (≥md) */}
            <div className={s.tableWrap}>
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
                  {visible.map((r) => (
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
                          <div className={s.fileMeta}>
                            <span className={s.fileName} title={r.filename}>{r.filename}</span>
                            {r.contradicted > 0 && (
                              <span className={s.flag}>
                                <AlertTriangle size={11} strokeWidth={2.2} />
                                {t("report_has_contradictions")}
                              </span>
                            )}
                          </div>
                        </div>
                      </td>
                      <td className={s.td}>
                        <span className={s.dateText}>{formatDate(r.date)}</span>
                      </td>
                      <td className={s.td}>
                        <div className={s.scoreCell}>
                          <span className={`${s.score} ${scoreClass(r.reliability_score)}`}>
                            {r.reliability_score}%
                          </span>
                          <VerdictBadges counts={r} variant="compact" />
                        </div>
                      </td>
                      <td className={s.td}>
                        <span className={`${s.badge} ${r.status === "done" ? s.badgeDone : s.badgePending}`}>
                          {r.status === "done" ? t("report_status_done") : t("report_status_pending")}
                        </span>
                      </td>
                      <td className={s.td} style={{ width: 32 }}>
                        <ChevronRight size={15} strokeWidth={1.5} className={s.chevron} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </motion.div>
        )}
      </div>
    </div>
  );
}
