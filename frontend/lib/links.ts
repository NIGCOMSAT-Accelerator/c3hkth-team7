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

const API_URL = process.env.SHELTER_API_URL ?? "http://localhost:8000";
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
