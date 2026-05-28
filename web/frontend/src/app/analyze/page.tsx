"use client";
import { useState, useEffect, useRef, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import FileUpload from "@/components/FileUpload";
import PipelineProgress, { PipelineStep } from "@/components/PipelineProgress";
import ReportViewer from "@/components/ReportViewer";
import { useTranslation } from "@/lib/i18n";
import s from "./Analyze.module.css";

type Phase = "idle" | "uploading" | "progress" | "done" | "error";

export default function AnalyzePage() {
  const { t } = useTranslation();

  // Build steps from i18n at render time
  const makeSteps = useCallback((): PipelineStep[] => [
    { id: "extract", name: t("step_extract"), status: "pending" },
    { id: "search",  name: t("step_search"),  status: "pending" },
    { id: "verify",  name: t("step_verify"),  status: "pending" },
    { id: "report",  name: t("step_report"),  status: "pending" },
  ], [t]);

  const [phase, setPhase] = useState<Phase>("idle");
  const [steps, setSteps] = useState<PipelineStep[]>(makeSteps);
  const [report, setReport] = useState<{ content: string; score: number } | null>(null);
  const [analysisId, setAnalysisId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Elapsed timer for active stage
  const [elapsed, setElapsed] = useState(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Rebuild steps when language changes (only if not in progress)
  useEffect(() => {
    if (phase === "idle" || phase === "error") {
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

  async function handleAnalyze(file: File) {
    setPhase("uploading");
    setError(null);
    setReport(null);
    setSteps(makeSteps());
    stopTimer();

    try {
      const form = new FormData();
      form.append("file", file);
      const res = await fetch("http://localhost:8000/api/analyze", { method: "POST", body: form });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(`${res.status}: ${text}`);
      }
      const data = await res.json();
      setAnalysisId(data.analysis_id);
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

    const ws = new WebSocket(`ws://localhost:8000/ws/progress/${analysisId}`);

    function processMsg(data: string) {
      try {
        const msg = JSON.parse(data);

        if (msg.type === "stage_start") {
          startTimer();
          setSteps((prev) => prev.map((step) =>
            step.id === msg.stage
              ? { ...step, status: "active", message: msg.message ?? t("step_waiting") }
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
          setReport({ content: msg.report, score: msg.score });
          setPhase("done");
          ws.close();
        }
        if (msg.type === "error") {
          stopTimer();
          setError(msg.message);
          setPhase("error");
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

  const scoreClass = !report ? "" :
    report.score >= 75 ? s.scoreGood :
    report.score >= 50 ? s.scoreWarn : s.scoreBad;

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
              <ReportViewer content={report.content} />
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}
