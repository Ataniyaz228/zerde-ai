import Link from "next/link";
import { ButtonHTMLAttributes, ReactNode } from "react";
import s from "./Button.module.css";

type Variant = "primary" | "secondary" | "ghost" | "icon";
type Size = "sm" | "md" | "lg";

interface BaseProps {
  variant?: Variant;
  size?: Size;
  className?: string;
  /** Растянуть на всю ширину (удобно на мобильном). */
  block?: boolean;
  children: ReactNode;
}

type AsButton = BaseProps & Omit<ButtonHTMLAttributes<HTMLButtonElement>, "className"> & { href?: undefined };
type AsLink = BaseProps & { href: string; target?: string; rel?: string; "aria-label"?: string };

type Props = AsButton | AsLink;

/** Единая кнопка: primary / secondary / ghost / icon. Рендерит <Link> при href,
 *  иначе <button>. Тач-цель ≥ --tap-min. Заменяет .cta/.navCta/.toolBtn/… */
export default function Button(props: Props) {
  const { variant = "primary", size = "md", block, className = "" } = props;
  const cls = [s.btn, s[variant], s[size], block ? s.block : "", className].filter(Boolean).join(" ");

  if ("href" in props && props.href !== undefined) {
    const { href, target, rel, children } = props;
    const ariaLabel = "aria-label" in props ? props["aria-label"] : undefined;
    return (
      <Link href={href} target={target} rel={rel} aria-label={ariaLabel} className={cls}>
        {children}
      </Link>
    );
  }

  // Отделяем кастомные пропсы от нативных атрибутов <button>.
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const { variant: _v, size: _z, block: _b, className: _c, href: _h, children, ...buttonProps } = props;
  return (
    <button className={cls} {...buttonProps}>
      {children}
    </button>
  );
}
