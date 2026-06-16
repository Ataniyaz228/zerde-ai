// Единый источник правды для порогов надёжности (reliability score).
// Раньше пороги 75/50 дублировались в analyze, reports и reports/[id].

export const SCORE = { good: 75, warn: 50 } as const;

export type ScoreTier = "good" | "warn" | "bad";

/** Уровень score: ≥75 — good, ≥50 — warn, иначе — bad. */
export function scoreTier(value: number): ScoreTier {
  if (value >= SCORE.good) return "good";
  if (value >= SCORE.warn) return "warn";
  return "bad";
}
