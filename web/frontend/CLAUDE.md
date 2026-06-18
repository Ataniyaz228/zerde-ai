@AGENTS.md

## Design-sync → claude.ai/design

This component library is synced to **claude.ai/design** (project "Zerde UI") so
the design agent builds with the real Zerde components. Inputs live in
`.design-sync/` (committed); generated `dist/`, `ds-bundle/`, `.ds-sync/`,
`.cache/` are gitignored.

This is an **app, not a packaged library** (default exports, CSS Modules,
i18n/theme context, `next/*` imports), so `.design-sync/prebuild.mjs` first
compiles the real `.tsx` into a self-contained `dist` the converter can wrap:
CSS Modules resolved (`local-css`), `next/link`/`next/navigation` shimmed,
`globals.css` tokens + fonts folded into one stylesheet. No component is
rewritten — the bundle is the compiled source.

**To re-sync after changing components:** run `/design-sync`. ⚠ The driver does
**not** re-run the pre-build, so run `node .design-sync/prebuild.mjs` first
whenever component source (or a CSS module / shim / `globals.css`) changed,
else the synced bundle is stale. Full setup, config keys, and gotchas:
`.design-sync/NOTES.md`.
