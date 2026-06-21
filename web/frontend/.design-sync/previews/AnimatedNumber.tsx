import { AnimatedNumber } from "frontend";

export const Stats = () => (
  <div style={{ display: "flex", gap: 48, color: "var(--text-primary)" }}>
    <div>
      <div style={{ fontSize: 40, fontWeight: 700, letterSpacing: "-0.02em" }}>
        <AnimatedNumber to={12480} duration={1} />
      </div>
      <div style={{ fontSize: 14, color: "var(--text-secondary)" }}>проверенных тезисов</div>
    </div>
    <div>
      <div style={{ fontSize: 40, fontWeight: 700, letterSpacing: "-0.02em" }}>
        <AnimatedNumber to={38} suffix="+" duration={1} />
      </div>
      <div style={{ fontSize: 14, color: "var(--text-secondary)" }}>кодексов в корпусе</div>
    </div>
  </div>
);
