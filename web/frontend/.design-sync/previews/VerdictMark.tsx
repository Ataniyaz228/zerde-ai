import { VerdictMark } from "frontend";

const Row = ({
  kind,
  label,
  color,
}: {
  kind: "good" | "bad" | "warn";
  label: string;
  color: string;
}) => (
  <span style={{ display: "inline-flex", alignItems: "center", gap: 8, color }}>
    <VerdictMark kind={kind} size={24} />
    <span style={{ fontSize: 14, color: "var(--text-primary)" }}>{label}</span>
  </span>
);

export const Marks = () => (
  <div style={{ display: "flex", gap: 28, alignItems: "center", flexWrap: "wrap" }}>
    <Row kind="good" label="Подтверждено" color="var(--status-ok)" />
    <Row kind="bad" label="Опровергнуто" color="var(--status-err)" />
    <Row kind="warn" label="Нет данных" color="var(--status-warn)" />
  </div>
);

export const Sizes = () => (
  <div style={{ display: "flex", gap: 16, alignItems: "center", color: "var(--text-primary)" }}>
    <VerdictMark kind="good" size={16} />
    <VerdictMark kind="good" size={24} />
    <VerdictMark kind="good" size={40} />
  </div>
);
