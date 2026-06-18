// Design-sync shim for `next/navigation`. Outside the Next runtime there is no
// router, so these hooks return inert stubs. Navbar reads usePathname() to
// highlight the active link — in a preview it resolves to "/" (nothing active),
// which renders fine. The rest are provided so any component that reaches for
// them doesn't throw.
export function usePathname(): string {
  return "/";
}

export function useRouter() {
  return {
    push: () => {},
    replace: () => {},
    back: () => {},
    forward: () => {},
    refresh: () => {},
    prefetch: () => {},
  };
}

export function useSearchParams(): URLSearchParams {
  return new URLSearchParams();
}

export function useParams(): Record<string, string> {
  return {};
}

export function redirect(): void {}
export function notFound(): void {}
