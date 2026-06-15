"use client";

import { createContext, useContext, useState, ReactNode } from "react";

// ===== TRANSLATIONS =====

const translations = {
  ru: {
    // Navbar
    nav_history: "История",
    nav_new: "Новый анализ",
    // Landing
    hero_badge: "Pipeline Engine v2.1",
    hero_title_1: "Правовая экспертиза",
    hero_title_2: "нового поколения.",
    hero_sub: "Интеллектуальный анализ законопроектов и договоров. Мгновенная сверка с актуальной базой НПА Республики Казахстан.",
    hero_cta: "Начать анализ",
    hero_link: "История отчётов",
    feat_accuracy: "Юридическая точность",
    feat_accuracy_desc: "Сверка с актуальной базой НПА РК. Учёт иерархии законов и подзаконных актов.",
    feat_risk: "Выявление рисков",
    feat_risk_desc: "Поиск коррупциогенных факторов, пробелов регулирования и системных противоречий.",
    feat_report: "Структурированный отчёт",
    feat_report_desc: "Детализированный анализ с привязкой к конкретным статьям и источникам.",
    // Analyze
    upload_title: "Анализ документа",
    upload_sub: "Загрузите PDF или текстовый документ",
    upload_drop: "Перетащите файл сюда",
    upload_or: "или",
    upload_btn: "Выбрать файл",
    upload_hint: "PDF, DOC, DOCX, TXT — до 20 МБ",
    upload_selected: "Файл выбран",
    upload_start: "Запустить анализ",
    upload_change: "Изменить файл",
    progress_title: "Анализ выполняется",
    progress_header: "Прогресс анализа",
    // Pipeline steps
    step_extract: "Извлечение тезисов и утверждений",
    step_extract_short: "Извлечение тезисов",
    step_search: "Поиск в базе НПА Казахстана",
    step_search_short: "Поиск НПА",
    step_verify: "Верификация и анализ коллизий",
    step_verify_short: "Верификация",
    step_report: "Формирование отчёта",
    step_report_short: "Формирование отчёта",
    step_waiting: "Ожидание ответа модели...",
    card_footer_desc: "Проверка на соответствие законам РК",
    // Reports
    reports_title: "История отчётов",
    reports_empty: "Отчётов пока нет",
    reports_empty_sub: "Загрузите первый документ для анализа",
    reports_col_file: "Документ",
    reports_col_date: "Дата",
    reports_col_score: "Надёжность",
    reports_col_status: "Статус",
    reports_count_label: "отчётов",
    reports_search_placeholder: "Поиск по документам…",
    reports_sort_date: "По дате",
    reports_sort_score: "По надёжности",
    reports_filter_all: "Все",
    reports_filter_flagged: "С опровержениями",
    reports_nothing_found: "Ничего не найдено",
    report_status_done: "Готов",
    report_status_pending: "В обработке",
    report_back: "Назад к истории",
    report_reliability: "Надёжность",
    report_source_doc: "Исходный документ",
    // Verdict dashboard
    verdict_confirmed: "Подтверждено",
    verdict_contradicted: "Опровергнуто",
    verdict_unverified: "Не проверено",
    verdict_coverage: "Охват проверки",
    report_has_contradictions: "Есть опровержения",
    // Report toolbar
    report_download: "Скачать .md",
    report_print: "Печать / PDF",
    report_copy_link: "Копировать ссылку",
    report_link_copied: "Ссылка скопирована",
    report_search_placeholder: "Поиск по отчёту…",
    report_search_clear: "Сбросить",
    report_no_matches: "Совпадений нет",
    // Upload validation
    upload_invalid_type: "Неподдерживаемый формат. Разрешены PDF, DOCX, TXT.",
    upload_too_large: "Файл слишком большой (макс. 25 МБ).",
    upload_analyzing: "Анализ...",
    // Restore
    restore_running: "Восстановлен текущий анализ",
    // Common
    loading: "Загрузка...",
    not_found: "Не найдено",
  },
  kz: {
    nav_history: "Тарих",
    nav_new: "Жаңа талдау",
    hero_badge: "Pipeline Engine v2.1",
    hero_title_1: "Жаңа буынның",
    hero_title_2: "құқықтық сараптамасы.",
    hero_sub: "Заң жобалары мен шарттарды интеллектуалды талдау. ҚР НҚА-ның өзекті базасымен жедел салыстыру.",
    hero_cta: "Талдауды бастау",
    hero_link: "Есептер тарихы",
    feat_accuracy: "Заңдық дәлдік",
    feat_accuracy_desc: "ҚР НҚА-ның өзекті базасымен салыстыру. Заң иерархиясын есепке алу.",
    feat_risk: "Тәуекелдерді анықтау",
    feat_risk_desc: "Сыбайлас жемқорлық факторларын, реттеу олқылықтарын анықтау.",
    feat_report: "Құрылымдалған есеп",
    feat_report_desc: "Нақты баптарға сілтемелермен егжей-тегжейлі талдау.",
    upload_title: "Құжатты талдау",
    upload_sub: "PDF немесе мәтіндік құжатты жүктеңіз",
    upload_drop: "Файлды осында сүйреңіз",
    upload_or: "немесе",
    upload_btn: "Файл таңдау",
    upload_hint: "PDF, DOC, DOCX, TXT — 20 МБ дейін",
    upload_selected: "Файл таңдалды",
    upload_start: "Талдауды іске қосу",
    upload_change: "Файлды өзгерту",
    progress_title: "Талдау жүргізілуде",
    progress_header: "Талдау барысы",
    // Pipeline steps
    step_extract: "Тезистер мен тұжырымдарды шығару",
    step_extract_short: "Тезистерді шығару",
    step_search: "ҚР НҚА базасынан іздеу",
    step_search_short: "НҚА іздеу",
    step_verify: "Верификация және қайшылықтар талдауы",
    step_verify_short: "Верификация",
    step_report: "Есепті қалыптастыру",
    step_report_short: "Есепті қалыптастыру",
    step_waiting: "Модель жауабын күту...",
    card_footer_desc: "ҚР заңнамасына сәйкестігін тексеру",
    // Reports
    reports_title: "Есептер тарихы",
    reports_empty: "Есептер жоқ",
    reports_empty_sub: "Талдау үшін бірінші құжатты жүктеңіз",
    reports_col_file: "Құжат",
    reports_col_date: "Күні",
    reports_col_score: "Сенімділік",
    reports_col_status: "Күй",
    reports_count_label: "есеп",
    reports_search_placeholder: "Құжаттар бойынша іздеу…",
    reports_sort_date: "Күні бойынша",
    reports_sort_score: "Сенімділік бойынша",
    reports_filter_all: "Барлығы",
    reports_filter_flagged: "Теріске шығарулармен",
    reports_nothing_found: "Ештеңе табылмады",
    report_status_done: "Дайын",
    report_status_pending: "Өңделуде",
    report_back: "Тарихқа оралу",
    report_reliability: "Сенімділік",
    report_source_doc: "Бастапқы құжат",
    // Verdict dashboard
    verdict_confirmed: "Расталды",
    verdict_contradicted: "Теріске шығарылды",
    verdict_unverified: "Тексерілмеді",
    verdict_coverage: "Тексеру қамтуы",
    report_has_contradictions: "Теріске шығарулар бар",
    // Report toolbar
    report_download: "Жүктеу .md",
    report_print: "Басып шығару / PDF",
    report_copy_link: "Сілтемені көшіру",
    report_link_copied: "Сілтеме көшірілді",
    report_search_placeholder: "Есеп бойынша іздеу…",
    report_search_clear: "Тазарту",
    report_no_matches: "Сәйкестік жоқ",
    // Upload validation
    upload_invalid_type: "Қолдау көрсетілмейтін формат. PDF, DOCX, TXT рұқсат етілген.",
    upload_too_large: "Файл тым үлкен (макс. 25 МБ).",
    upload_analyzing: "Талдау...",
    // Restore
    restore_running: "Ағымдағы талдау қалпына келтірілді",
    loading: "Жүктелуде...",
    not_found: "Табылмады",
  },
} as const;

type Lang = keyof typeof translations;
type Keys = keyof typeof translations.ru;

// ===== CONTEXT =====

interface I18nContextType {
  lang: Lang;
  setLang: (l: Lang) => void;
  t: (key: Keys) => string;
}

const I18nContext = createContext<I18nContextType>({
  lang: "ru",
  setLang: () => {},
  t: (key) => translations.ru[key],
});

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLang] = useState<Lang>("ru");
  const t = (key: Keys): string => translations[lang][key];
  return (
    <I18nContext.Provider value={{ lang, setLang, t }}>
      {children}
    </I18nContext.Provider>
  );
}

export function useTranslation() {
  return useContext(I18nContext);
}
