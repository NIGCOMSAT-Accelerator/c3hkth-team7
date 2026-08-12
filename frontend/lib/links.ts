import "server-only";

/**
 * Links that point at the BACKEND origin rather than at this app.
 *
 * ## Why this cannot be a relative path
 *
 * The API consoles are served by FastAPI, not by Next.js. A bare `/shelter/v1/api/dev-docs`
 * resolves against the frontend origin — `localhost:3000` in development, the Netlify domain
 * in production — where nothing serves it, so the link 404s. Verified: that exact href
 * returns 404 from the dev server.
 *
 * In the target deployment the two are on different hosts entirely (Netlify + a VPS), so the
 * absolute form is not merely tidier, it is the only one that works.
 *
 * `server-only` because it reads `SHELTER_API_URL`, which must not reach the client bundle —
 * and because every consumer is a server component rendering an `href`.
 */

/**
 * The backend's PUBLIC origin — what a browser can reach.
 *
 * ## Why this is not `SHELTER_API_URL`
 *
 * `SHELTER_API_URL` is the origin *this container* uses to reach the API, and in a single-node
 * deployment that is a Docker service name:
 *
 *     SHELTER_API_URL: http://shelter-api:8000
 *
 * Correct for server-to-server calls, and unusable in an `href`. Signed in as an aggregator on
 * production, the portal rendered:
 *
 *     <a href="http://shelter-api:8000/shelter/v1/api/dev-docs">Developer docs</a>
 *
 * ...which no browser can resolve. It affected three surfaces — the header link (commercial
 * accounts only, which is why it looked like an aggregator-specific bug), the API-keys page and
 * the webhooks empty state.
 *
 * So the two are separated by intent rather than by value:
 *
 *     SHELTER_API_URL         where the SERVER calls the API   (may be internal)
 *     SHELTER_API_PUBLIC_URL  where the BROWSER should go      (must be routable)
 *
 * The fallback chain degrades safely rather than silently: the public URL if set, else
 * `SHELTER_API_URL` when it is *not* an internal hostname, else the relative path. A relative
 * link 404s visibly on the frontend origin; an internal hostname fails with
 * ERR_NAME_NOT_RESOLVED and looks like the docs are down.
 */
const INTERNAL_HOST = /^https?:\/\/(shelter-api|api|localhost|127\.0\.0\.1|host\.docker\.internal)(:|\/|$)/i;

const SERVER_URL = process.env.SHELTER_API_URL ?? "";
const PUBLIC_URL =
  process.env.SHELTER_API_PUBLIC_URL ??
  (SERVER_URL && !INTERNAL_HOST.test(SERVER_URL) ? SERVER_URL : "");

// Empty string yields a root-relative link. Wrong origin, but it 404s in a way an operator can
// diagnose — unlike a Docker service name, which a browser cannot even attempt.
const API_URL = PUBLIC_URL;
const API_PREFIX = process.env.SHELTER_API_PREFIX ?? "/shelter/v1/api";

/**
 * The partner-facing developer reference (ReDoc).
 *
 * The FILTERED spec — 21 paths, no IAM internals. `/docs` is the internal console and is
 * deliberately not linked from the portal: it lists operator endpoints and every IAM route,
 * which is not what a partner should be reading.
 */
export const DEV_DOCS_URL = `${API_URL}${API_PREFIX}/dev-docs`;

/** The webhook section of that reference, for the webhooks empty state. */
export const WEBHOOK_DOCS_URL = `${DEV_DOCS_URL}#tag/webhook`;
