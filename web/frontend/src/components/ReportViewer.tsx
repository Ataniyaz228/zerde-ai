"use client";
import { useMemo, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { ChevronDown, Download, Printer, Link2, Check, Search, X } from "lucide-react";
import { AnimatePresence, motion } from "framer-motion";
import { humanizeMarkdown, withIcons, highlight, statusPill, calloutClass } from "@/lib/reportFormat";
import { useTranslation } from "@/lib/i18n";
import s from "./ReportViewer.module.css";

interface Props {
  content: string;
  /** Original filename for the .md download (defaults to "report.md"). */
  downloadName?: string;
  /** When set, a "copy link" button copies the current page URL. */
  reportId?: string;
  /** Optional muted notice above the toolbar (e.g. machine-translation disclaimer). */
  note?: string;
}

// Build the Markdown renderers. Markdown text is first highlighted for the
// active search query, then status emojis are swapped for lucide icons
// (withIcons recurses into the <mark> nodes produced by highlight).
function makeComponents(query: string): Components {
  const deco = (children: React.ReactNode) =>
    withIcons(highlight(children, query, s.hl));
  return {
    a: ({ node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" />,
    p: ({ node, children, ...props }) => <p {...props}>{deco(children)}</p>,
    li: ({ node, children, ...props }) => <li {...props}>{deco(children)}</li>,
    h1: ({ node, children, ...props }) => <h1 {...props}>{deco(children)}</h1>,
    h2: ({ node, children, ...props }) => <h2 {...props}>{deco(children)}</h2>,
    h3: ({ node, children, ...props }) => <h3 {...props}>{deco(children)}</h3>,
    h4: ({ node, children, ...props }) => <h4 {...props}>{deco(children)}</h4>,
    td: ({ node, children, ...props }) => <td {...props}>{deco(children)}</td>,
    th: ({ node, children, ...props }) => <th {...props}>{deco(children)}</th>,
    // Оборачиваем таблицу — на узких экранах прокручивается по горизонтали.
    table: ({ node, ...props }) => (
      <div className={s.tableScroll}>
        <table {...props} />
      </div>
    ),
    // Bold that is exactly a verdict status tag → coloured pill; otherwise bold.
    strong: ({ node, children, ...props }) => statusPill(children) ?? <strong {...props}>{deco(children)}</strong>,
    // Colour the callout border/tint by the verdict it carries.
    blockquote: ({ node, children, ...props }) => (
      <blockquote {...props} className={calloutClass(children)}>{deco(children)}</blockquote>
    ),
  };
}

interface Section {
  title: string;
  body: string;
}

function parseIntoSections(md: string): Section[] {
  const lines = md.split("\n");
  const sections: Section[] = [];
  let current: Section | null = null;

  for (const line of lines) {
    if (line.startsWith("## ")) {
      if (current) sections.push(current);
      current = { title: line.replace(/^## /, ""), body: "" };
    } else {
      if (current) {
        current.body += line + "\n";
      } else {
        // Intro before first h2
        if (!sections[0] || sections[0].title !== "_intro") {
          sections.unshift({ title: "_intro", body: "" });
        }
        sections[0].body += line + "\n";
      }
    }
  }
  if (current) sections.push(current);
  return sections.filter((s) => s.body.trim());
}

// Sections describing conflicts / contradictions get a danger accent and open
// by default — they carry the report's most important signal. Язык-независимо:
// только секция конфликтов несёт счётчик «(N)» в заголовке (RU и KZ) — это
// надёжный сигнал даже после перевода; RU-ключевые слова оставляем как запас.
function isDanger(title: string): boolean {
  return /конфликт|коллиз|опроверг/i.test(title) || /\(\s*\d+\s*\)/.test(title);
}

function AccordionSection({
  section,
  defaultOpen,
  query,
  components,
}: {
  section: Section;
  defaultOpen: boolean;
  query: string;
  components: Components;
}) {
  const matches = query.length > 0 && section.body.toLowerCase().includes(query.toLowerCase());
  const [open, setOpen] = useState(defaultOpen);
  const isIntro = section.title === "_intro";
  const danger = isDanger(section.title);
  // Force open while searching and this section has a hit.
  const isOpen = matches || (query.length === 0 && open);

  const body = (
    <div className={s.md}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {section.body}
      </ReactMarkdown>
    </div>
  );

  if (isIntro) {
    return (
      <div className={s.section}>
        <div className={s.sectionBody} style={{ paddingTop: 20 }}>{body}</div>
      </div>
    );
  }

  return (
    <div className={`${s.section} ${danger ? s.sectionDanger : ""}`}>
      <button className={s.sectionHeader} onClick={() => setOpen(!open)} disabled={matches}>
        <span className={s.sectionTitle}>{withIcons(section.title)}</span>
        <ChevronDown
          size={16}
          strokeWidth={1.5}
          className={`${s.sectionChevron} ${isOpen ? s.sectionChevronOpen : ""}`}
        />
      </button>
      <AnimatePresence initial={false}>
        {isOpen && (
          <motion.div
            key="body"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.25, ease: [0.22, 1, 0.36, 1] }}
            style={{ overflow: "hidden" }}
          >
            <div className={s.sectionBody}>{body}</div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

export default function ReportViewer({ content, downloadName, reportId, note }: Props) {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const [copied, setCopied] = useState(false);

  const humanized = useMemo(() => humanizeMarkdown(content), [content]);
  const sections = useMemo(() => parseIntoSections(humanized), [humanized]);
  const components = useMemo(() => makeComponents(query.trim()), [query]);

  const q = query.trim();
  const hitCount = q
    ? sections.filter((sec) => sec.body.toLowerCase().includes(q.toLowerCase())).length
    : 0;

  function handleDownload() {
    // Download the canonical Markdown (not the humanized presentation copy).
    const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = (downloadName ?? "report").replace(/\.md$/i, "") + ".md";
    a.click();
    URL.revokeObjectURL(url);
  }

  async function handleCopyLink() {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch { /* clipboard unavailable */ }
  }

  return (
    <div>
      {note && <p className={s.note}>{note}</p>}

      {/* Toolbar */}
      <div className={s.toolbar}>
        <div className={s.searchWrap}>
          <Search size={14} strokeWidth={1.8} className={s.searchIcon} />
          <input
            className={s.searchInput}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("report_search_placeholder")}
          />
          {q && (
            <button className={s.searchClear} onClick={() => setQuery("")} title={t("report_search_clear")}>
              <X size={13} strokeWidth={2} />
            </button>
          )}
          {q && (
            <span className={s.searchCount}>
              {hitCount > 0 ? hitCount : t("report_no_matches")}
            </span>
          )}
        </div>

        <div className={s.toolbarActions}>
          <button className={s.toolBtn} onClick={handleDownload}>
            <Download size={14} strokeWidth={1.8} />
            <span className={s.toolBtnLabel}>{t("report_download")}</span>
          </button>
          <button className={s.toolBtn} onClick={() => window.print()}>
            <Printer size={14} strokeWidth={1.8} />
            <span className={s.toolBtnLabel}>{t("report_print")}</span>
          </button>
          {reportId && (
            <button className={s.toolBtn} onClick={handleCopyLink}>
              {copied ? <Check size={14} strokeWidth={2} /> : <Link2 size={14} strokeWidth={1.8} />}
              <span className={s.toolBtnLabel}>
                {copied ? t("report_link_copied") : t("report_copy_link")}
              </span>
            </button>
          )}
        </div>
      </div>

      <div className={s.wrap}>
        {sections.map((section, i) => (
          <AccordionSection
            key={i}
            section={section}
            defaultOpen={i < 2 || isDanger(section.title)}
            query={q}
            components={components}
          />
        ))}
      </div>
    </div>
  );
}
