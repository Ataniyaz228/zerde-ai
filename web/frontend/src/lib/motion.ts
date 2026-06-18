// Общие framer-motion пресеты. Раньше fadeUp дублировался inline на страницах.
// Базовая кривая совпадает с --ease-out из globals.css.

const EASE = [0.22, 1, 0.36, 1] as const;
// Выразительная кривая (overshoot-выход) — совпадает с --ease-expressive.
const EASE_X = [0.16, 1, 0.3, 1] as const;

/** Появление снизу при попадании в зону видимости (once). */
export const fadeUp = (delay = 0) => ({
  initial: { opacity: 0, y: 16 },
  whileInView: { opacity: 1, y: 0 },
  viewport: { once: true, margin: "-60px" },
  transition: { duration: 0.6, delay, ease: EASE },
});

/** Мгновенное появление сверху-вниз (для hero — animate, не whileInView). */
export const fadeInUp = (delay = 0) => ({
  initial: { opacity: 0, y: 18 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.7, delay, ease: EASE },
});

/** Плавное проявление без сдвига. */
export const fadeIn = (delay = 0) => ({
  initial: { opacity: 0 },
  whileInView: { opacity: 1 },
  viewport: { once: true, margin: "-60px" },
  transition: { duration: 0.6, delay, ease: EASE },
});

/** Контейнер с поэтапным появлением детей. */
export const stagger = (staggerChildren = 0.08) => ({
  initial: "hidden",
  whileInView: "show",
  viewport: { once: true, margin: "-60px" },
  variants: {
    hidden: {},
    show: { transition: { staggerChildren } },
  },
});

/** Дочерний элемент для stagger-контейнера. */
export const staggerItem = {
  hidden: { opacity: 0, y: 16 },
  show: { opacity: 1, y: 0, transition: { duration: 0.55, ease: EASE } },
};

/** Контейнер построчного reveal display-титула (hero — animate, не whileInView).
 *  Строки проявляются из-под маски одна за другой. */
export const lineRevealGroup = (delay = 0, staggerChildren = 0.09) => ({
  initial: "hidden",
  animate: "show",
  variants: {
    hidden: {},
    show: { transition: { delayChildren: delay, staggerChildren } },
  },
});

/** Строка display-титула: выезжает из-под собственной маски (clip снизу).
 *  Родитель строки должен иметь overflow: hidden (класс .lineMask в CSS). */
export const lineRevealItem = {
  hidden: { y: "110%" },
  show: {
    y: "0%",
    transition: { duration: 0.9, ease: EASE_X },
  },
};
