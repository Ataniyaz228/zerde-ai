"use client";
import { ReactNode } from "react";
import s from "./SegmentedControl.module.css";

export interface Segment<T extends string> {
  value: T;
  label: ReactNode;
}

interface Props<T extends string> {
  value: T;
  onChange: (value: T) => void;
  options: Segment<T>[];
  ariaLabel: string;
  /** "md" — фильтры/сортировка; "sm" — компактный (переключатель языка). */
  size?: "sm" | "md";
}

/** Сегментированный переключатель. Используется в Reports (фильтр/сортировка)
 *  и Navbar (RU/KZ). Кнопки c aria-pressed для доступности. */
export default function SegmentedControl<T extends string>({
  value,
  onChange,
  options,
  ariaLabel,
  size = "md",
}: Props<T>) {
  return (
    <div className={`${s.group} ${size === "sm" ? s.sm : ""}`} role="group" aria-label={ariaLabel}>
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          className={`${s.segment} ${value === opt.value ? s.active : ""}`}
          aria-pressed={value === opt.value}
          onClick={() => onChange(opt.value)}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}
