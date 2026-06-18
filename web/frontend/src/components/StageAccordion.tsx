"use client";
import { useState } from "react";
import { Upload, FileSearch, Globe, ShieldCheck, FileText } from "lucide-react";
import { useTranslation } from "@/lib/i18n";
import s from "./StageAccordion.module.css";

/** Интерактивный аккордеон этапов: каждая панель показывает, что делает один шаг
 *  пайплайна — теми же именами, что пользователь видит при анализе
 *  (загрузка → извлечение → поиск НПА → верификация → отчёт). На десктопе панели
 *  раскрываются по горизонтали при наведении, на мобиле — раскрываются вниз.
 *  Без стоковых фото: визуал держится на нашей иконографике и моно-нумерации. */
export default function StageAccordion() {
  const { t } = useTranslation();

  const STAGES = [
    { id: "ingest",  n: "01", Icon: Upload,      title: t("stage_ingest_short"), desc: t("acc_d1") },
    { id: "extract", n: "02", Icon: FileSearch,  title: t("step_extract_short"), desc: t("acc_d2") },
    { id: "search",  n: "03", Icon: Globe,       title: t("step_search_short"),  desc: t("acc_d3") },
    { id: "verify",  n: "04", Icon: ShieldCheck, title: t("step_verify_short"),  desc: t("acc_d4") },
    { id: "report",  n: "05", Icon: FileText,    title: t("step_report_short"),  desc: t("acc_d5") },
  ];

  const [active, setActive] = useState(0);

  return (
    <div className={s.accordion}>
      {STAGES.map((st, i) => {
        const { Icon } = st;
        const isActive = i === active;
        return (
          <button
            key={st.id}
            type="button"
            className={`${s.panel} ${isActive ? s.active : ""}`}
            onMouseEnter={() => setActive(i)}
            onFocus={() => setActive(i)}
            onClick={() => setActive(i)}
            aria-expanded={isActive}
          >
            <span className={s.head}>
              <span className={s.num}>{st.n}</span>
              <span className={s.label}>{st.title}</span>
              <span className={s.icon} aria-hidden><Icon size={20} strokeWidth={1.6} /></span>
            </span>
            <span className={s.body}>
              <span className={s.bodyInner}>
                <span className={s.desc}>{st.desc}</span>
              </span>
            </span>
          </button>
        );
      })}
    </div>
  );
}
