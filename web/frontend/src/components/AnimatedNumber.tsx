"use client";
import { useEffect, useRef, useState } from "react";

interface Props {
  /** Конечное число, к которому досчитываем от нуля. */
  to: number;
  /** Суффикс после числа (например, "+"). */
  suffix?: string;
  /** Длительность анимации, мс. */
  duration?: number;
}

/** Счётчик «0 → to», запускается один раз при попадании в зону видимости.
 *  Разделитель тысяч — узкий пробел (редакционная подача чисел).
 *  Уважает prefers-reduced-motion: при включённом — сразу финальное значение. */
export default function AnimatedNumber({ to, suffix = "", duration = 1300 }: Props) {
  const [value, setValue] = useState(0);
  const ref = useRef<HTMLSpanElement>(null);
  const started = useRef(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce) {
      // Финальное значение сразу — без анимации. Чтение внешнего источника
      // (media query) на маунте, не каскад состояния.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setValue(to);
      return;
    }

    const run = () => {
      if (started.current) return;
      started.current = true;
      const start = performance.now();
      const tick = (now: number) => {
        const p = Math.min((now - start) / duration, 1);
        // easeOutExpo — быстрый старт, плавная остановка.
        const eased = p === 1 ? 1 : 1 - Math.pow(2, -10 * p);
        setValue(Math.round(eased * to));
        if (p < 1) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    };

    const io = new IntersectionObserver(
      (entries) => entries.forEach((e) => e.isIntersecting && run()),
      { threshold: 0.4 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [to, duration]);

  const formatted = value.toLocaleString("ru-RU").replace(/ /g, " ");

  return (
    <span ref={ref} className="tnum">
      {formatted}
      {suffix}
    </span>
  );
}
