# Zerde UI — how to build with it

Zerde is a near-monochrome editorial system (white canvas, near-black ink) with **one** chromatic note — indigo `--accent` — used surgically. Verdict statuses are a separate traffic-light, deliberately *outside* the brand hue. Components are imported from `window.Zerde.*` (the bundle at the project root).

## Wrap every screen in the providers

Most components read **theme** and **i18n** from React context — `Navbar`, `Footer`, `ProductWindow`, `StageAccordion`, `PipelineProgress`, `VerdictBadges`, `FileUpload`, `ReportViewer` all do. Without the providers they throw at render. Wrap once at the root:

```jsx
const { ThemeProvider, I18nProvider, Navbar, Button } = window.Zerde;

<ThemeProvider>
  <I18nProvider>
    {/* your screen */}
  </I18nProvider>
</ThemeProvider>
```

`ThemeProvider` owns light/dark by setting `[data-theme="dark"]` on the root — every token re-resolves, so you never hard-code colors for dark mode.

## Styling idiom: pre-styled components + design tokens

This is a **CSS-Modules + design-token** system, *not* a utility-class one. Components ship fully styled — there are no styling class props (the only `className` escape hatches are `Button` and `VerdictMark`). Style **your own layout/glue** with the CSS custom properties below — never invent hex colors, raw px spacing, or font stacks.

| Family | Tokens (real names) |
|---|---|
| Surfaces | `--bg-primary` (canvas) · `--bg-surface` · `--bg-elevated` · `--bg-hover` · `--bg-sunken` |
| Text | `--text-primary` · `--text-secondary` · `--text-tertiary` · `--text-on-accent` |
| Brand accent (indigo — links/focus/accents only) | `--accent` · `--accent-strong` · `--accent-soft` · `--accent-tint` · `--accent-border` |
| Primary action (near-black ink, NOT the accent) | `--btn-primary-bg` · `--btn-primary-bg-hover` · `--btn-primary-fg` |
| Borders (hairlines) | `--border-subtle` · `--border` · `--border-strong` |
| Verdict status (traffic-light, outside the brand) | `--status-ok` / `--status-ok-bg` · `--status-warn` / `--status-warn-bg` · `--status-err` / `--status-err-bg` |
| Spacing scale | `--space-1` … `--space-10` |
| Type scale | `--step--1` · `--step-0` … `--step-6` · `--step-display` |
| Fonts | `--font-sans` (Inter) · `--font-mono` (JetBrains Mono) |
| Radius / shadow / focus | `--radius-sm` `--radius` `--radius-lg` `--radius-xl` · `--shadow-sm` `--shadow` `--shadow-lg` · `--ring` |
| Layout / motion / touch | `--container-max` · `--container-pad` · `--header-h` · `--section-gap` · `--duration` · `--ease-out` · `--tap-min` |

Rules of thumb: the **primary** `Button` is filled ink (`--btn-primary-bg`); use the indigo `--accent` for links/focus and small product accents only; show verdicts with `--status-*` (never the accent). Keep surfaces near-monochrome and let type carry the hierarchy.

## Where the truth lives

- `styles.css` → `_ds_bundle.css` — all token definitions and the component styles (read these before styling).
- `components/<group>/<Name>/<Name>.prompt.md` + `.d.ts` — per-component usage + the exact prop contract.

## One idiomatic screen

```jsx
const { ThemeProvider, I18nProvider, Navbar, Button, VerdictBadges } = window.Zerde;

function ReportScreen() {
  return (
    <ThemeProvider><I18nProvider>
      <Navbar />
      <main style={{ maxWidth: "var(--container-max)", margin: "0 auto", padding: "var(--space-8) var(--container-pad)" }}>
        <h1 style={{ font: "var(--step-display)/1.05 var(--font-sans)", color: "var(--text-primary)" }}>
          Отчёт о проверке
        </h1>
        <div style={{ marginTop: "var(--space-6)" }}>
          <VerdictBadges variant="full" counts={{ confirmed: 14, contradicted: 3, unverified: 5, total: 22, coverage_pct: 86 }} />
        </div>
        <div style={{ marginTop: "var(--space-6)", display: "flex", gap: "var(--space-3)" }}>
          <Button variant="primary">Новый анализ</Button>
          <Button variant="secondary" href="/reports">История</Button>
        </div>
      </main>
    </I18nProvider></ThemeProvider>
  );
}
```
