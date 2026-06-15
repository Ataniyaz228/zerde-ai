"use client";
import { Children, cloneElement, Fragment, isValidElement, ReactNode } from "react";
import {
  AlertCircle,
  AlertTriangle,
  BarChart3,
  BookOpen,
  CheckCircle2,
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
  XCircle,
  type LucideIcon,
} from "lucide-react";
import s from "@/components/ReportViewer.module.css";

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
      // fact_claim_0114 / claim_0037 → «утверждение №114»
      .replace(/\bfact_claim_0*(\d+)/g, "утверждение №$1")
      .replace(/\bclaim_0*(\d+)/g, "утверждение №$1")
      // article_ref=18 → «статья 18»
      .replace(/\barticle_ref\s*=\s*(\d+)/g, "статья $1")
      // (BM25: 0.69) → (релевантность 0.69)
      .replace(/\(\s*BM25:\s*([\d.]+)\s*\)/g, "(релевантность $1)")
      // record_id-style conflict codes стают читабельнее
      .replace(/\bconflict_0*(\d+)/g, "коллизия №$1")
  );
}

// ── Emoji → icon mapping ─────────────────────────────────────────────────────

type IconSpec = { Icon: LucideIcon; cls: string };

const EMOJI_ICON: Record<string, IconSpec> = {
  "🟢": { Icon: CheckCircle2, cls: s.icoGood },
  "✅": { Icon: CheckCircle2, cls: s.icoGood },
  "🔴": { Icon: XCircle, cls: s.icoBad },
  "❌": { Icon: XCircle, cls: s.icoBad },
  "🟡": { Icon: AlertCircle, cls: s.icoWarn },
  "🟠": { Icon: AlertTriangle, cls: s.icoWarn },
  "⚠️": { Icon: AlertTriangle, cls: s.icoWarn },
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
