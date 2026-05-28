"use client";
import { Check, X, FileSearch, Globe, ShieldCheck, FileText, Loader } from "lucide-react";
import s from "./PipelineProgress.module.css";

export interface PipelineStep {
  id: string;
  name: string;
  status: "pending" | "active" | "done" | "error";
  message?: string;
}

interface Props {
  steps: PipelineStep[];
}

const ICONS: Record<string, React.ElementType> = {
  extract: FileSearch,
  search: Globe,
  verify: ShieldCheck,
  report: FileText,
};

export default function PipelineProgress({ steps }: Props) {
  const done = steps.filter((s) => s.status === "done").length;
  const total = steps.length;
  const pct = total ? Math.round((done / total) * 100) : 0;

  return (
    <div className={s.wrap}>
      <div className={s.header}>
        <span className={s.title}>Прогресс анализа</span>
        <span className={s.stepNum}>{done}/{total}</span>
      </div>

      <div className={s.totalBar}>
        <div className={s.totalBarFill} style={{ width: `${pct}%` }} />
      </div>

      <div className={s.steps}>
        {steps.map((step) => {
          const Icon = ICONS[step.id] ?? FileText;
          const isActive = step.status === "active";
          const isDone = step.status === "done";
          const isErr = step.status === "error";

          const iconClass = [
            s.stepIcon,
            isActive ? s.stepIconActive : "",
            isDone ? s.stepIconDone : "",
            isErr ? s.stepIconError : "",
          ].filter(Boolean).join(" ");

          const nameClass = [
            s.stepName,
            isActive ? s.stepNameActive : "",
            isDone ? s.stepNameDone : "",
          ].filter(Boolean).join(" ");

          return (
            <div key={step.id} className={s.step}>
              <div className={iconClass}>
                {isDone ? <Check size={15} strokeWidth={2.5} /> :
                 isErr  ? <X size={15} strokeWidth={2.5} /> :
                 isActive ? <Loader size={15} strokeWidth={1.5} style={{ animation: "spin 1s linear infinite" }} /> :
                 <Icon size={15} strokeWidth={1.5} />}
              </div>
              <div className={s.stepBody}>
                <p className={nameClass}>{step.name}</p>
                {step.message && <p className={s.stepMsg}>{step.message}</p>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
