// Общие форматтеры. Раньше formatDate жил в двух страницах, а formatBytes — в FileUpload.

/** Дата документа. "short" — для таблиц/карточек, "long" — для шапки отчёта. */
export function formatDate(iso: string, style: "short" | "long" = "short"): string {
  return new Date(iso).toLocaleDateString("ru-RU", {
    day: "2-digit",
    month: style === "long" ? "long" : "short",
    year: "numeric",
  });
}

/** Человекочитаемый размер файла (B / KB / MB). */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
