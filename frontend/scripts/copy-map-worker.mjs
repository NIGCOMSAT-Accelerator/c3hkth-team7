/**
 * Copy MapLibre's worker bundle into `public/` so it is served from a real HTTP URL.
 *
 * ## The bug this fixes
 *
 * MapLibre 6 derives its worker URL from `import.meta.url` and, when that is not an
 * `http(s):` URL, **returns an empty string**:
 *
 *     function vi(){ let e = import.meta.url;
 *                    if (!/^https?:/.test(e)) return ``;   // <- here
 *                    ... }
 *
 * `new Worker("")` resolves against the current document, so the browser fetches the *page*
 * and refuses it:
 *
 *     Loading Worker from "http://localhost:3000/subscribe" was blocked because of a
 *     disallowed MIME type ("text/html")
 *
 * The map then hangs on "Loading the map" forever, because no worker ever answers — and it
 * never reaches maplibre's own `error` event, so the component's failure path does not fire
 * either.
 *
 * Under Turbopack the dynamic `import("maplibre-gl")` is bundled, so `import.meta.url` is not
 * an http URL at runtime and the empty-string branch is always taken. That is why this
 * reproduces in dev and in a production build alike.
 *
 * ## Why a copied file rather than a bundler alias
 *
 * The worker has to be a separate network resource by definition — a worker cannot be part of
 * the bundle that spawns it. Options were:
 *
 *   * a Turbopack `new Worker(new URL(...))` rewrite — needs us to fork maplibre's internals;
 *   * serving it from a CDN — a third-party dependency on the critical path of the one screen
 *     a farmer uses to register their land, for a file we already have locally;
 *   * copying it into `public/`, which is what this does. One file, versioned by the lockfile,
 *     served by Next with the correct `text/javascript` type.
 *
 * Run from `predev` and `prebuild` so it cannot drift: a `npm ci` on a fresh clone or in CI
 * produces the file before anything imports it. `public/maplibre-gl-worker.mjs` is gitignored
 * for the same reason `node_modules` is — it is a build artefact of a pinned dependency, and a
 * committed copy would silently disagree with the version in the lockfile after an upgrade.
 */

import { copyFile, mkdir, readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";

const require = createRequire(import.meta.url);

const packageJson = require.resolve("maplibre-gl/package.json");
const dist = join(dirname(packageJson), "dist");
const target = join(process.cwd(), "public");

// The non-dev bundle. `maplibre-gl.mjs` (which is what we import) asks for
// `maplibre-gl-worker.mjs`; only the `-dev` build asks for `-worker-dev`.
const WORKER = "maplibre-gl-worker.mjs";

await mkdir(target, { recursive: true });
await copyFile(join(dist, WORKER), join(target, WORKER));

// The worker imports the shared chunk by a relative specifier, so that has to sit beside it
// or the worker itself 404s on its own dependency — a failure that looks identical to the
// one above and is considerably more confusing to diagnose.
const source = await readFile(join(dist, WORKER), "utf8");
for (const match of source.matchAll(/from\s*["']\.\/([\w.-]+\.mjs)["']/g)) {
  await copyFile(join(dist, match[1]), join(target, match[1]));
  console.log(`  + public/${match[1]} (imported by the worker)`);
}

const { version } = require("maplibre-gl/package.json");
console.log(`  + public/${WORKER} (maplibre-gl ${version})`);
