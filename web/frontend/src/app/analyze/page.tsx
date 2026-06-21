"use client";
import { useState, useEffect, useRef, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import FileUpload from "@/components/FileUpload";
import PipelineProgress, { PipelineStep } from "@/components/PipelineProgress";
import ReportViewer from "@/components/ReportViewer";
import VerdictBadges, { VerdictCounts } from "@/components/VerdictBadges";
import { useTranslation } from "@/lib/i18n";
import { API_URL, WS_URL, apiFetch } from "@/lib/api";
import { scoreTier } from "@/lib/score";
import s from "./Analyze.module.css";

const SCORE_CLASS = { good: "scoreGood", warn: "scoreWarn", bad: "scoreBad" } as const;

type Phase = "idle" | "uploading" | "progress" | "done" | "error";

type ReportState = {
  content: string;
  score: number;
  filename: string;
  reportId: string;
} & VerdictCounts;

// Persisted across reloads so an in-flight analysis can be resumed (#4).
const ACTIVE_KEY = "zerde_active_analysis";

export default function AnalyzePage() {
  const { t, lang } = useTranslation();

  // Build steps from i18n at render time
  const makeSteps = useCallback((): PipelineStep[] => [
    { id: "extract", name: t("step_extract"), status: "pending" },
    { id: "search",  name: t("step_search"),  status: "pending" },
    { id: "verify",  name: t("step_verify"),  status: "pending" },
    { id: "report",  name: t("step_report"),  status: "pending" },
  ], [t]);

  const [phase, setPhase] = useState<Phase>("idle");
  const [steps, setSteps] = useState<PipelineStep[]>(makeSteps);
  const [report, setReport] = useState<ReportState | null>(null);
  // Казахская версия готового отчёта (ленивый машинный перевод с бэкенда).
  const [kzContent, setKzContent] = useState<string | null>(null);
  const [analysisId, setAnalysisId] = useState<string | null>(null);
  const [fileName, setFileName] = useState<string>("report");
  const [error, setError] = useState<string | null>(null);
  const restoredRef = useRef(false);

  // Elapsed timer for active stage
  const [elapsed, setElapsed] = useState(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Rebuild steps when language changes (only if not in progress)
  useEffect(() => {
    if (phase === "idle" || phase === "error") {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setSteps(makeSteps());
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [t]);

  function startTimer() {
    setElapsed(0);
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = setInterval(() => setElapsed((e) => e + 1), 1000);
  }

  function stopTimer() {
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
  }

  useEffect(() => () => stopTimer(), []);

  // Resume an in-flight analysis after a page reload (#4). The WebSocket
  // replays persisted events (including the final `done`), so re-attaching to
  // the same analysis_id restores progress or the finished report. This is a
  // one-shot init from sessionStorage after mount (avoids SSR hydration
  // mismatch a lazy initializer would cause).
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (restoredRef.current) return;
    restoredRef.current = true;
    try {
      const raw = sessionStorage.getItem(ACTIVE_KEY);
      if (!raw) return;
      const { id, filename } = JSON.parse(raw);
      if (!id) return;
      setFileName(filename ?? "report");
      setAnalysisId(id);
      setSteps((prev) => prev.map((step, i) =>
        i === 0 ? { ...step, status: "active", message: t("restore_running") } : step
      ));
      startTimer();
      setPhase("progress");
    } catch { /* ignore corrupt state */ }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  /* eslint-enable react-hooks/set-state-in-effect */

  async function handleAnalyze(file: File) {
    setPhase("uploading");
    setError(null);
    setReport(null);
    setSteps(makeSteps());
    setFileName(file.name);
    stopTimer();

    try {
      const form = new FormData();
      form.append("file", file);
      const res = await apiFetch(`${API_URL}/api/analyze`, { method: "POST", body: form });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(`${res.status}: ${text}`);
      }
      const data = await res.json();
      setAnalysisId(data.analysis_id);
      try {
        sessionStorage.setItem(ACTIVE_KEY, JSON.stringify({ id: data.analysis_id, filename: file.name }));
      } catch { /* storage unavailable */ }
      // Immediately mark first step as active — WS may arrive late
      setSteps((prev) => prev.map((step, i) =>
        i === 0 ? { ...step, status: "active", message: t("step_waiting") } : step
      ));
      startTimer();
      setPhase("progress");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Upload error");
      setPhase("error");
    }
  }

  // WebSocket progress listener
  useEffect(() => {
    if (!analysisId || phase !== "progress") return;

    // Buffer messages that arrive before onopen
    const pendingMsgs: string[] = [];
    let ready = false;

    const ws = new WebSocket(`${WS_URL}/ws/progress/${analysisId}`);

    function processMsg(data: string) {
      try {
        const msg = JSON.parse(data);

        if (msg.type === "stage_start") {
          startTimer();
          // Игнорируем серверный msg.message (он захардкожен на русском) —
          // под-строку активного шага локализуем сами, как и initial-active.
          setSteps((prev) => prev.map((step) =>
            step.id === msg.stage
              ? { ...step, status: "active", message: t("step_waiting") }
              : step
          ));
        }
        if (msg.type === "stage_done") {
          setElapsed(0);
          setSteps((prev) => prev.map((step) =>
            step.id === msg.stage ? { ...step, status: "done", message: undefined } : step
          ));
        }
        if (msg.type === "stage_error") {
          stopTimer();
          setSteps((prev) => prev.map((step) =>
            step.id === msg.stage ? { ...step, status: "error", message: msg.message } : step
          ));
        }
        if (msg.type === "done") {
          stopTimer();
          // Mark every step done — a replayed/late connection may have
          // missed intermediate stage_done events.
          setSteps((prev) => prev.map((step) => ({ ...step, status: "done", message: undefined })));
          setReport({
            content: msg.report,
            score: msg.score,
            filename: fileName,
            reportId: msg.report_id,
            confirmed: msg.confirmed ?? 0,
            contradicted: msg.contradicted ?? 0,
            unverified: msg.unverified ?? 0,
            total: (msg.confirmed ?? 0) + (msg.contradicted ?? 0) + (msg.unverified ?? 0),
            coverage_pct: msg.coverage_pct ?? 0,
          });
          setPhase("done");
          try { sessionStorage.removeItem(ACTIVE_KEY); } catch { /* noop */ }
          ws.close();
        }
        if (msg.type === "error") {
          stopTimer();
          setError(msg.message);
          setPhase("error");
          try { sessionStorage.removeItem(ACTIVE_KEY); } catch { /* noop */ }
          ws.close();
        }
      } catch { /* malformed json */ }
    }

    ws.onopen = () => {
      ready = true;
      pendingMsgs.forEach(processMsg);
      pendingMsgs.length = 0;
    };

    ws.onmessage = (ev) => {
      if (ready) processMsg(ev.data);
      else pendingMsgs.push(ev.data);
    };

    ws.onerror = () => {
      stopTimer();
      setError("WebSocket connection error");
      setPhase("error");
    };

    return () => ws.close();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [analysisId, phase]);

  // Когда отчёт готов и UI на казахском — догружаем безопасный перевод с бэкенда
  // (RU-версия из WS уже показана; KZ подменяется по готовности). На RU — сброс.
  useEffect(() => {
    if (lang !== "kz") {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setKzContent(null);
      return;
    }
    if (phase !== "done" || !report?.reportId) return;
    let alive = true;
    apiFetch(`${API_URL}/api/reports/${report.reportId}?lang=kz`)
      .then((r) => r.json())
      .then((data) => { if (alive && data?.content) setKzContent(data.content); })
      .catch(() => { /* фолбэк — остаётся русская версия */ });
    return () => { alive = false; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lang, phase, report?.reportId]);

  const scoreClass = report ? s[SCORE_CLASS[scoreTier(report.score)]] : "";

  // Inject elapsed hint into active step message
  const displaySteps = steps.map((step) => {
    if (step.status === "active" && elapsed > 5) {
      return { ...step, message: `${step.message ?? t("step_waiting")} (${elapsed}с)` };
    }
    return step;
  });

  return (
    <div className={s.page}>
      <div className={s.inner}>
        <div className={s.header}>
          <span className={`eyebrow ${s.eyebrow}`}>{t("hero_badge")}</span>
          <h1 className={s.title}>{t("upload_title")}</h1>
          <p className={s.sub}>{t("upload_sub")}</p>
        </div>

        {/* Upload zone */}
        {(phase === "idle" || phase === "uploading" || phase === "error") && (
          <div className={s.uploadSection}>
            <FileUpload onAnalyze={handleAnalyze} isLoading={phase === "uploading"} />
            {error && (
              <p style={{ marginTop: "1rem", fontSize: 13, color: "var(--status-err)" }}>
                {t("not_found")}: {error}
              </p>
            )}
          </div>
        )}

        {/* Progress */}
        <AnimatePresence>
          {(phase === "progress" || phase === "done") && (
            <motion.div
              key="progress"
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              className={s.progressSection}
            >
              <PipelineProgress steps={displaySteps} />
            </motion.div>
          )}
        </AnimatePresence>

        {/* Report */}
        <AnimatePresence>
          {phase === "done" && report && (
            <motion.div
              key="report"
              initial={{ opacity: 0, y: 16 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.2 }}
              className={s.reportSection}
            >
              <div className={s.reportHeader}>
                <h2 className={s.reportTitle}>{t("progress_title")}</h2>
                <div className={s.scoreWrap}>
                  <span className={s.scoreLabel}>{t("report_reliability")}</span>
                  <span className={`${s.scoreValue} ${scoreClass}`}>{report.score}%</span>
                </div>
              </div>
              <VerdictBadges counts={report} />
              <div style={{ height: 16 }} />
              <ReportViewer
                content={lang === "kz" && kzContent ? kzContent : report.content}
                downloadName={report.filename}
                note={lang === "kz" ? (kzContent ? t("report_machine_translation") : t("report_translating")) : undefined}
              />
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}
