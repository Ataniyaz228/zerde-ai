"use client";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ChevronDown } from "lucide-react";
import { AnimatePresence, motion } from "framer-motion";
import s from "./ReportViewer.module.css";

interface Props {
  content: string;
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

function AccordionSection({ section, defaultOpen }: { section: Section; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const isIntro = section.title === "_intro";

  if (isIntro) {
    return (
      <div className={s.section}>
        <div className={s.sectionBody} style={{ paddingTop: 20 }}>
          <div className={s.md}>
            <ReactMarkdown 
              remarkPlugins={[remarkGfm]}
              components={{
                a: ({node, ...props}) => <a {...props} target="_blank" rel="noopener noreferrer" />
              }}
            >
              {section.body}
            </ReactMarkdown>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={s.section}>
      <button className={s.sectionHeader} onClick={() => setOpen(!open)}>
        <span className={s.sectionTitle}>{section.title}</span>
        <ChevronDown
          size={16}
          strokeWidth={1.5}
          className={`${s.sectionChevron} ${open ? s.sectionChevronOpen : ""}`}
        />
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            key="body"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.25, ease: [0.22, 1, 0.36, 1] }}
            style={{ overflow: "hidden" }}
          >
            <div className={s.sectionBody}>
              <div className={s.md}>
                <ReactMarkdown 
                  remarkPlugins={[remarkGfm]}
                  components={{
                    a: ({node, ...props}) => <a {...props} target="_blank" rel="noopener noreferrer" />
                  }}
                >
                  {section.body}
                </ReactMarkdown>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

export default function ReportViewer({ content }: Props) {
  const sections = parseIntoSections(content);

  return (
    <div className={s.wrap}>
      {sections.map((section, i) => (
        <AccordionSection key={i} section={section} defaultOpen={i < 2} />
      ))}
    </div>
  );
}
