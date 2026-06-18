// Pre-compiles the Zerde component barrel into a self-contained dist that the
// design-sync converter can wrap as if it were a published library build.
//
// Why a pre-build instead of letting the converter bundle src/ directly:
//   - CSS Modules: the converter deliberately does NOT use esbuild's local-css
//     loader (lib/bundle.mjs), so `import s from "./X.module.css"` would yield
//     {} and every class would be undefined -> unstyled components. We resolve
//     CSS Modules HERE with local-css, emitting one plain index.css.
//   - next/link & next/navigation are aliased to ./shims via tsconfig paths.
//   - @/ -> src/ via the same tsconfig.
// What stays external (the converter binds/bundles it): react & friends
// (-> window.React via the converter's reactShim) and the heavy leaf libs
// (lucide-react, framer-motion, react-markdown, remark-gfm) from node_modules.
//
// Run from web/frontend AFTER the converter scripts are staged into .ds-sync
// (that's where esbuild gets installed):  node .design-sync/prebuild.mjs
import { build } from "../.ds-sync/node_modules/esbuild/lib/main.js";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url)); // web/frontend/.design-sync

const result = await build({
  entryPoints: [resolve(here, "entry.tsx")],
  outfile: resolve(here, "dist/index.mjs"),
  bundle: true,
  format: "esm",
  platform: "browser",
  target: "es2020",
  jsx: "automatic",
  tsconfig: resolve(here, "tsconfig.build.json"),
  loader: {
    ".module.css": "local-css",
    ".svg": "dataurl",
    ".png": "dataurl",
    ".jpg": "dataurl",
    ".woff": "dataurl",
    ".woff2": "dataurl",
  },
  external: [
    "react",
    "react-dom",
    "react/jsx-runtime",
    "react-dom/client",
    "lucide-react",
    "framer-motion",
    "react-markdown",
    "remark-gfm",
  ],
  define: { "process.env.NODE_ENV": '"development"' },
  logLevel: "info",
  metafile: false,
});

if (result.errors?.length) {
  console.error(result.errors);
  process.exit(1);
}
console.log("prebuilt -> .design-sync/dist/index.{mjs,css}");
