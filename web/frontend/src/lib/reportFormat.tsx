"use client";
import { Children, cloneElement, Fragment, isValidElement, type ComponentType, ReactNode } from "react";
import {
  BarChart3,
  BookOpen,
  CircleDashed,
  ClipboardList,
  Files,
  Info,
  Landmark,
  Lightbulb,
  Pin,
  RefreshCw,
  Scale,
  Search,
  TrendingUp,
  Users,
} from "lucide-react";
import VerdictMark from "@/components/VerdictMark";
import s from "@/components/ReportViewer.module.css";

// Дженерик-кружки lucide для вердиктов «палят» шаблон → заменяем их фирменными
// марками-печатями (единый мотив с VerdictBadges и exhibit). Обёртки приводят
// VerdictMark к сигнатуре, которую дёргает splitEmoji.
type IconRender = ComponentType<{ size?: number; strokeWidth?: number; className?: string; "aria-hidden"?: boolean }>;
const MarkGood: IconRender = ({ size, className }) => <VerdictMark kind="good" size={size} className={className} />;
const MarkBad: IconRender = ({ size, className }) => <VerdictMark kind="bad" size={size} className={className} />;
const MarkWarn: IconRender = ({ size, className }) => <VerdictMark kind="warn" size={size} className={className} />;

/**
 * Presentation layer for pipeline reports.
 *
 * The Markdown the backend emits is the canonical, golden-locked artifact and
 * MUST NOT change. Everything here is frontend-only cosmetics:
 *   - humanizeMarkdown(): rewrites raw pipeline identifiers into human phrasing
 *   - withIcons(): swaps status emojis for crisp lucide icons at render time
 */

// ── Raw-identifier humanization (operates on the Markdown string) ────────────

export function humanizeMarkdown(md: string): string {
  return (
    md
      // GitHub-style alert markers (> [!NOTE] / [!WARNING] / …) — remark-gfm
      // doesn't render them, so they leak as literal text. Drop the marker line;
      // the verdict pill + coloured callout below convey the same meaning.
      .replace(/^>\s*\[!(?:NOTE|WARNING|CAUTION|TIP|IMPORTANT)\]\s*$/gim, "")
      // fact_claim_0114 / claim_0037 → «утверждение №114»
      .replace(/\bfact_claim_0*(\d+)/g, "утверждение №$1")
      .replace(/\bclaim_0*(\d+)/g, "утверждение №$1")
      // Redundant inline claim ref right after a status tag — the heading
      // already names the claim. «[утверждение №0]: '…'» → «'…'»
      .replace(/\s*\[утверждение №\d+\]:\s*/g, " ")
      // article_ref=18 → «статья 18»
      .replace(/\barticle_ref\s*=\s*(\d+)/g, "статья $1")
      // (BM25: 0.69) → (релевантность 0.69)
      .replace(/\(\s*BM25:\s*([\d.]+)\s*\)/g, "(релевантность $1)")
      // record_id-style conflict codes стают читабельнее
      .replace(/\bconflict_0*(\d+)/g, "коллизия №$1")
  );
}

// ── Verdict status tags → coloured pills ─────────────────────────────────────

/** Flatten a rendered node tree back to its plain text. */
export function nodeText(node: ReactNode): string {
  if (typeof node === "string") return node;
  if (typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (isValidElement(node)) {
    const el = node as React.ReactElement<{ children?: ReactNode }>;
    return nodeText(el.props?.children);
  }
  return "";
}

const STATUS_PILLS: { test: RegExp; cls: string }[] = [
  { test: /^\[ОШИБКА\]$/, cls: s.pillBad },
  { test: /^\[ПОДТВЕРЖДЕНО\]$/, cls: s.pillGood },
  { test: /РИСК РЕТРИВАЛА/, cls: s.pillWarn },
  { test: /^\[ПРЕДУПРЕЖДЕНИЕ\]$/, cls: s.pillWarn },
  { test: /^\[НЕ ПРОВЕРЕНО\]$/, cls: s.pillNeutral },
];

/** If `children` is a known verdict status tag, render it as a pill; else null. */
export function statusPill(children: ReactNode): ReactNode | null {
  const text = nodeText(children).trim();
  for (const p of STATUS_PILLS) {
    if (p.test.test(text)) {
      // Strip the brackets and any leading status emoji (⚠️ etc.) — colour carries it.
      const label = text.replace(/^\[\s*/, "").replace(/\s*\]$/, "").replace(/^[^\p{L}]+/u, "");
      return <span className={`${s.pill} ${p.cls}`}>{label}</span>;
    }
  }
  return null;
}

/** Colour-code a blockquote callout by the verdict it contains. */
export function calloutClass(children: ReactNode): string {
  const t = nodeText(children);
  if (/ОШИБКА/.test(t)) return s.calloutBad;
  if (/ПОДТВЕРЖДЕНО/.test(t)) return s.calloutGood;
  if (/РИСК РЕТРИВАЛА|ПРЕДУПРЕЖДЕНИЕ/.test(t)) return s.calloutWarn;
  if (/НЕ ПРОВЕРЕНО/.test(t)) return s.calloutNeutral;
  return "";
}

// ── Emoji → icon mapping ─────────────────────────────────────────────────────

type IconSpec = { Icon: IconRender; cls: string };

const EMOJI_ICON: Record<string, IconSpec> = {
  "🟢": { Icon: MarkGood, cls: s.icoGood },
  "✅": { Icon: MarkGood, cls: s.icoGood },
  "🔴": { Icon: MarkBad, cls: s.icoBad },
  "❌": { Icon: MarkBad, cls: s.icoBad },
  "🟡": { Icon: MarkWarn, cls: s.icoWarn },
  "🟠": { Icon: MarkWarn, cls: s.icoWarn },
  "⚠️": { Icon: MarkWarn, cls: s.icoWarn },
  "📋": { Icon: ClipboardList, cls: s.icoNeutral },
  "📑": { Icon: Files, cls: s.icoNeutral },
  "📊": { Icon: BarChart3, cls: s.icoNeutral },
  "📈": { Icon: TrendingUp, cls: s.icoNeutral },
  "🏛️": { Icon: Landmark, cls: s.icoNeutral },
  "👥": { Icon: Users, cls: s.icoNeutral },
  "🔄": { Icon: RefreshCw, cls: s.icoNeutral },
  "📚": { Icon: BookOpen, cls: s.icoNeutral },
  "⚖️": { Icon: Scale, cls: s.icoNeutral },
  "🔍": { Icon: Search, cls: s.icoNeutral },
  "🕳️": { Icon: CircleDashed, cls: s.icoNeutral },
  "🕳": { Icon: CircleDashed, cls: s.icoNeutral },
  "📌": { Icon: Pin, cls: s.icoNeutral },
  "ℹ️": { Icon: Info, cls: s.icoNeutral },
  "💡": { Icon: Lightbulb, cls: s.icoNeutral },
};

const EMOJI_RE = new RegExp(
  "(" + Object.keys(EMOJI_ICON).map((e) => e.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")",
  "g",
);

function splitEmoji(text: string): ReactNode {
  if (!EMOJI_RE.test(text)) return text;
  EMOJI_RE.lastIndex = 0;
  const parts = text.split(EMOJI_RE);
  return parts.map((part, i) => {
    const spec = EMOJI_ICON[part];
    if (spec) {
      const { Icon, cls } = spec;
      return <Icon key={i} size={15} strokeWidth={2} className={`${s.ico} ${cls}`} aria-hidden />;
    }
    return <Fragment key={i}>{part}</Fragment>;
  });
}

// ── In-report search highlight ───────────────────────────────────────────────

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function splitHighlight(text: string, query: string, cls: string): ReactNode {
  const re = new RegExp(`(${escapeRe(query)})`, "gi");
  if (!re.test(text)) return text;
  re.lastIndex = 0;
  const parts = text.split(re);
  return parts.map((part, i) =>
    part.toLowerCase() === query.toLowerCase() ? (
      <mark key={i} className={cls}>{part}</mark>
    ) : (
      <Fragment key={i}>{part}</Fragment>
    ),
  );
}

/** Recursively wrap occurrences of `query` in <mark> within rendered children. */
export function highlight(node: ReactNode, query: string, cls: string): ReactNode {
  if (!query) return node;
  if (typeof node === "string") return splitHighlight(node, query, cls);
  if (Array.isArray(node)) {
    return Children.map(node, (child, i) => <Fragment key={i}>{highlight(child, query, cls)}</Fragment>);
  }
  if (isValidElement(node)) {
    const el = node as React.ReactElement<{ children?: ReactNode }>;
    if (el.props?.children != null) {
      return cloneElement(el, undefined, highlight(el.props.children, query, cls));
    }
  }
  return node;
}

/** Recursively replace status emojis inside rendered Markdown children with icons. */
export function withIcons(node: ReactNode): ReactNode {
  if (typeof node === "string") return splitEmoji(node);
  if (Array.isArray(node)) {
    return Children.map(node, (child, i) => <Fragment key={i}>{withIcons(child)}</Fragment>);
  }
  if (isValidElement(node)) {
    const el = node as React.ReactElement<{ children?: ReactNode }>;
    if (el.props?.children != null) {
      return cloneElement(el, undefined, withIcons(el.props.children));
    }
  }
  return node;
}
