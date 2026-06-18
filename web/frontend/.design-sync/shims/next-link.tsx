import { AnchorHTMLAttributes, ReactNode } from "react";

// Design-sync shim for `next/link`. The real Link needs the Next App Router
// runtime, which doesn't exist outside the app — so previews render a plain
// anchor carrying the same props the components pass it (href, className,
// target, rel, aria-label, children). Navigation is inert by design; these
// cards are for look, not routing.
type Props = AnchorHTMLAttributes<HTMLAnchorElement> & {
  href: string;
  children?: ReactNode;
};

export default function Link({ href, children, ...rest }: Props) {
  return (
    <a href={href} {...rest}>
      {children}
    </a>
  );
}
