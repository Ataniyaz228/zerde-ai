# design-sync notes — Zerde frontend

## What this repo is (read before syncing)

`web/frontend` is a **Next.js 16 application**, not a packaged component library:

- every component is `export default` (no named library exports)
- styling is **CSS Modules** (`*.module.css`) — there is no shipped stylesheet
- 8/12 components read **i18n/theme context** (`@/lib/i18n`, `@/lib/theme`)
- `Button`, `Navbar`, `Footer` import `next/link`; `Navbar` imports `usePathname` from `next/navigation`
- design tokens live in `src/app/globals.css` (`:root`), fonts via `next/font/google` in `layout.tsx` (`--font-inter`, `--font-mono-jb`)
- path alias `@/* -> src/*` (tsconfig.json)

The package shape applies (no Storybook). 12 syncable components; `src/app/*` are routes, not components.

## Strategy: pre-compile to a dist, then let the converter wrap it

The converter expects a library dist with named exports and plain JS. We bridge with a **pre-build** so the converter never has to resolve CSS Modules, next/*, or `@/`:

1. `.design-sync/entry.tsx` — barrel that re-exports the 12 default exports as **named** exports, plus `I18nProvider` / `ThemeProvider` (for `cfg.provider`). Also imports `./fonts.css` + `../src/app/globals.css` first so all tokens land in the CSS.
2. `.design-sync/prebuild.mjs` — esbuilds the barrel into `.design-sync/dist/index.{mjs,css}`:
   - `loader: { ".module.css": "local-css" }` → CSS Modules resolve to scoped class maps; CSS emitted to `index.css`. **This is the whole reason for the pre-build** — the converter's `lib/bundle.mjs` deliberately avoids `local-css`, so letting it bundle src/ directly would render everything unstyled.
   - `next/link` / `next/navigation` aliased to `.design-sync/shims/*` via `tsconfig.build.json` paths.
   - external: `react`, `react-dom`, `react/jsx-runtime`, `react-dom/client`, `lucide-react`, `framer-motion`, `react-markdown`, `remark-gfm` (converter binds React to `window.React` and bundles the leaf libs from node_modules).
3. The converter runs with `--entry ./.design-sync/dist/index.mjs`, `cfg.cssEntry: .design-sync/dist/index.css`, and `componentSrcMap` pinning all 12 src `.tsx` (for `.d.ts`/JSDoc/group). This makes it a non-synth build; the bundle exports all 12 named + the providers.

`cfg.provider` = `ThemeProvider` outer, `I18nProvider` inner — Navbar/Footer/etc. read both contexts.

Fonts: shimmed via `.design-sync/fonts.css` (remote Google Fonts `@import` + `--font-inter`/`--font-mono-jb` binding). Expect informational `[FONT_REMOTE]`, not `[FONT_MISSING]`.

## Resume / re-sync commands (run from web/frontend)

```bash
# 1. stage converter (uses the live design-sync skill base dir — re-resolve it)
BASE=$(find /tmp -maxdepth 6 -name package-build.mjs -path '*design-sync*' 2>/dev/null | head -1 | xargs dirname)
mkdir -p .ds-sync
cp -r "$BASE"/package-build.mjs "$BASE"/package-validate.mjs "$BASE"/package-capture.mjs "$BASE"/resync.mjs "$BASE"/lib "$BASE"/storybook .ds-sync/
printf '{"name":"ds-sync-deps","private":true}\n' > .ds-sync/package.json
(cd .ds-sync && npm i esbuild ts-morph @types/react)

# 2. pre-compile the components (CSS Modules + next shims resolved)
node .design-sync/prebuild.mjs   # -> .design-sync/dist/index.{mjs,css}

# 3. converter build + validate (run from web/frontend; --node-modules = local)
node .ds-sync/package-build.mjs --config .design-sync/config.json \
  --node-modules ./node_modules --entry ./.design-sync/dist/index.mjs --out ./ds-bundle
node .ds-sync/package-validate.mjs ./ds-bundle
```

## ⚠ Re-sync: run prebuild FIRST (the driver does NOT)

`resync.mjs` chains build → diff → validate → capture, but it builds from the
**already-compiled** `.design-sync/dist/index.mjs`. It does **not** re-run
`prebuild.mjs`. So whenever component source (or globals.css / a CSS module / a
shim) changed, run the pre-build before the driver:

```bash
node .design-sync/prebuild.mjs        # refresh dist/index.{mjs,css}
node .ds-sync/resync.mjs --config .design-sync/config.json --node-modules ./node_modules \
  --entry ./.design-sync/dist/index.mjs --out ./ds-bundle \
  --remote .design-sync/.cache/remote-sync.json
```

(Fetch the project's `_ds_sync.json` → `.design-sync/.cache/remote-sync.json` first.)
If you skip the prebuild after a source change, the synced bundle is stale and
nothing downstream will catch it.

## Known render warns

- `[FONT_REMOTE]` for `Inter`, `JetBrains Mono`, `Fira Code` — **expected**. Fonts
  load at runtime via the Google-Fonts `@import` in `fonts.css` (Fira Code is only
  a fallback in `--font-mono`). Not `[FONT_MISSING]`; no action.

## Re-sync risks / watch-list

- **next/* shims** (`.design-sync/shims/`) and **fonts.css** are hand-authored stand-ins for app-runtime glue. If the app adds new `next/*` imports in a component (e.g. `next/image`, `useSearchParams` usage), add a shim/alias. If a component starts reading a new context/provider, add it to `cfg.provider`.
- **react-markdown + remark-gfm** (ReportViewer) are bundled by the converter from node_modules — heavy; watch for bundle/resolve issues there first.
- **SegmentedControl is generic** (`<T extends string>`) — `.d.ts` extraction may need `cfg.dtsPropsFor.SegmentedControl` if `[DTS_PARSE]` fires.
- The pre-build externals list must stay in sync with the converter's reactShim filter and with what's actually in node_modules. If a leaf lib is removed/renamed, update `prebuild.mjs` externals.
- `globals.css` is folded into `index.css` via the barrel import — if the token file moves, fix the barrel import path.

## Props / docs are config-driven (no .d.ts in dist)

The pre-built dist has no `.d.ts`, so the converter can't extract props or JSDoc.
Both are supplied by config: `cfg.dtsPropsFor` (hand-written prop bodies for the 8
components with props; empty string for the 4 propless ones) and `cfg.docsMap` →
`.design-sync/docs/<Name>.md` (group via frontmatter `category` + English
description for the agent's prompt.md). If you add a component, add both entries.

## History

- 2026-06-18: first-sync attempt. Scaffolding authored. **Blocked**: the skill bundle was cleaned off disk mid-run; re-ran `/design-sync` to restore the converter.
- 2026-06-18: **first sync COMPLETED**. Project "Zerde UI" (`1cc181e6-ad09-490b-b78d-e4d78136f9d1`). 12 components, all authored previews graded `good`, render check clean (bad/thin/variantsIdentical = 0), conventions header shipped. Uploaded via the incremental path (2 batches + close-out). Groups: controls (Button, SegmentedControl), layout (Navbar, Footer), verdict (VerdictBadges, VerdictMark), product (the rest).
