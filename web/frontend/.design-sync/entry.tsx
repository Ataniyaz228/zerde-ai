// Design-sync barrel entry.
//
// The Zerde frontend is a Next.js *app*, not a component library: every
// component is an `export default`, styles live in CSS Modules, and several
// read i18n/theme context or import next/link & next/navigation. The
// design-sync converter expects a library `dist/` with NAMED exports and plain
// JS (no CSS-Module or next/* imports to resolve).
//
// This barrel + `.design-sync/prebuild.mjs` bridge the gap: prebuild esbuilds
// this file into `.design-sync/dist/index.{mjs,css}` with CSS Modules resolved
// (local-css), next/* aliased to the shims in ./shims, and the global token
// sheets folded into the CSS. react / lucide / framer / markdown stay external
// for the converter to bind (window.React) or bundle from node_modules.
//
// Order matters: fonts + global tokens first so the CSS custom properties the
// component modules consume are defined up top.
import "./fonts.css";
import "../src/app/globals.css";

export { default as Button } from "@/components/ui/Button";
export { default as SegmentedControl } from "@/components/ui/SegmentedControl";
export { default as Navbar } from "@/components/Navbar";
export { default as Footer } from "@/components/Footer";
export { default as FileUpload } from "@/components/FileUpload";
export { default as PipelineProgress } from "@/components/PipelineProgress";
export { default as ProductWindow } from "@/components/ProductWindow";
export { default as ReportViewer } from "@/components/ReportViewer";
export { default as VerdictBadges } from "@/components/VerdictBadges";
export { default as VerdictMark } from "@/components/VerdictMark";
export { default as StageAccordion } from "@/components/StageAccordion";
export { default as AnimatedNumber } from "@/components/AnimatedNumber";

// Context providers the preview cards wrap with (cfg.provider). Not components —
// excluded from the synced list, present only so window.Zerde.* exposes them.
export { I18nProvider } from "@/lib/i18n";
export { ThemeProvider } from "@/lib/theme";
