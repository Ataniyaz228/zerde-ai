interface Props {
  kind: "good" | "bad" | "warn";
  size?: number;
  className?: string;
}

/** Фирменные марки трёх вердиктов — единый мотив «печать-рамка» (скруглённый
 *  квадрат-оттиск), а не дженерик-кружки lucide:
 *    good — рамка + галка (подтверждено / заверено),
 *    bad  — рамка + диагональный вычерк (опровергнуто / вычеркнуто из записи),
 *    warn — пунктирная рамка + прочерк (нет данных / открытый вопрос).
 *  Цвет — через currentColor (наследует статус). */
export default function VerdictMark({ kind, size = 16, className }: Props) {
  const common = {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    className,
    "aria-hidden": true,
  } as const;

  if (kind === "good") {
    return (
      <svg {...common}>
        <rect x="3.4" y="3.4" width="17.2" height="17.2" rx="5" strokeWidth="1.6" />
        <path d="M7.6 12.3 10.7 15.4 16.4 8.9" strokeWidth="2.1" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }
  if (kind === "bad") {
    return (
      <svg {...common}>
        <rect x="3.4" y="3.4" width="17.2" height="17.2" rx="5" strokeWidth="1.6" />
        <path d="M7.8 16.2 16.2 7.8" strokeWidth="2.1" strokeLinecap="round" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <rect x="3.4" y="3.4" width="17.2" height="17.2" rx="5" strokeWidth="1.6" strokeDasharray="3 2.7" />
      <path d="M8.4 12 15.6 12" strokeWidth="2.1" strokeLinecap="round" />
    </svg>
  );
}
