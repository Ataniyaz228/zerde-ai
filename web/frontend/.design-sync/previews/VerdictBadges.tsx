import { VerdictBadges } from "frontend";

const sample = {
  confirmed: 14,
  contradicted: 3,
  unverified: 5,
  total: 22,
  coverage_pct: 86,
};

export const Full = () => (
  <div style={{ maxWidth: 720 }}>
    <VerdictBadges counts={sample} variant="full" />
  </div>
);

export const Compact = () => <VerdictBadges counts={sample} variant="compact" />;

export const HighRisk = () => (
  <div style={{ maxWidth: 720 }}>
    <VerdictBadges
      counts={{ confirmed: 4, contradicted: 11, unverified: 9, total: 24, coverage_pct: 62 }}
      variant="full"
    />
  </div>
);
