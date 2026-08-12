/**
 * Server-side API client.
 *
 * Every function here runs on the server only. `SHELTER_API_KEY` is read from
 * the server environment and is deliberately NOT prefixed `NEXT_PUBLIC_` — the
 * key authorises subscriber registration, so it must never be bundled into client
 * JavaScript.
 *
 * The value should be a scoped service-account key (`shltky…`) provisioned with
 * `make iam-service-account`. Those carry platform:subscribers:write, platform:read
 * and platform:assess — deliberately NOT platform:broadcast, so a leak of this
 * environment cannot page a district. The old shared key granted that.
 */

import "server-only";

import type {
  Account,
  AggregatorMembership,
  Alert,
  AoiPreview,
  ApiKeyPublic,
  AreaOfInterest,
  AuditPage,
  AuditSummary,
  BBox,
  ChannelBinding,
  CommercialSignupRequest,
  HealthResponse,
  IndividualSignupRequest,
  LoginChallenge,
  MyAccess,
  PasswordChangeCodeSent,
  PendingInvitation,
  PlaceResult,
  PlaceSuggestion,
  ResolvedArea,
  RiskAssessment,
  RoleOption,
  SessionState,
  SessionToken,
  SignupResponse,
  Subscriber,
  TeamMember,
  TotpState,
  Track,
  TrustedDeviceList,
  WebhookPublic,
  Workspace,
  WorkspaceAreaRow,
  WorkspaceCustomer,
  WorkspaceGrant,
} from "./types";

const API_URL = process.env.SHELTER_API_URL ?? "http://localhost:8000";
const API_PREFIX = process.env.SHELTER_API_PREFIX ?? "/shelter/v1/api";
const API_KEY = process.env.SHELTER_API_KEY;

/**
 * Warn once, at module load, when a hosted deployment is misconfigured.
 *
 * ## Why this exists rather than trusting the deployment docs
 *
 * Both variables fail SILENTLY and identically. A near-miss name — `API_BASE_URL`,
 * `API_URL`, `NEXT_PUBLIC_SHELTER_API_URL` — is simply not read, so `API_URL` falls back to
 * `http://localhost:8000`. On a hosted renderer that resolves to the render container itself,
 * which has no backend, so every request throws `ApiError(503)`.
 *
 * And `safeApi` swallows those by design, because a downed backend should degrade a page
 * rather than 500 it. The combination is the problem: the site builds, serves, and renders —
 * with empty data everywhere. It reads as "the backend is down" rather than "a variable is
 * misnamed", and the two have very different fixes.
 *
 * So the misconfiguration is announced in the build and function logs, where an operator
 * looking for the cause will actually find it.
 *
 * Deliberately a warning and not a throw: a contributor running `npm run dev` with no
 * backend at all is a legitimate state — the pages are built to SSR against a dead API — and
 * crashing the module would break that.
 */
// ## No environment gate at all, and that is deliberate
//
// This was `NETLIFY || VERCEL || CI`, which meant a **self-hosted container** — how this actually
// deploys — matched nothing and neither warning ever fired. A VPS with no `SHELTER_API_KEY` then
// 401'd every platform read, `/portal/areas` said "Areas temporarily unavailable" with a healthy
// API behind it, and there was **nothing in any log** to say why.
//
// Adding `NODE_ENV === "production"` did not work, and the reason is worth recording: Next.js
// INLINES `process.env.NODE_ENV` at build time, so the term became a literal and the whole
// expression was constant-folded. Verified by reading the emitted chunk — the compiled guard was
// still `(NETLIFY||VERCEL||CI)` with the NODE_ENV term gone entirely.
//
// So the gate is removed. Each check already fires only when its value is **absent**, which is the
// condition actually worth reporting — a developer running `npm run dev` against a local backend
// with a populated `.env.local` sees nothing, and one running with no backend at all sees a line
// that is true and useful rather than noise.
if (!process.env.SHELTER_API_URL) {
  console.error(
    "[shelter] SHELTER_API_URL is not set. Falling back to http://localhost:8000, which " +
      "inside a container is that container — every API call will fail while pages still " +
      "render (safeApi degrades them). Note the exact name: API_BASE_URL and API_URL are " +
      "NOT read.",
  );
}

if (!API_KEY) {
  console.error(
    "[shelter] SHELTER_API_KEY is not set. Every platform read will 401 while sign-in keeps " +
      "working — that uses the session cookie — so the SYMPTOM is: /portal/areas says 'Areas " +
      "temporarily unavailable', the alert feed is empty, and the area picker cannot resolve a " +
      "place, all with a healthy API. Provision one with: make iam-service-account " +
      "NAME=shelter-portal EMAIL=ops@example.com — it must be a scoped 'shltky…' key, not the " +
      "legacy API_KEY, which is refused once IAM is configured.",
  );
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

interface RequestOptions extends RequestInit {
  /** Seconds. Omit for no caching, which is the default for live data. */
  revalidate?: number;
  /**
   * Milliseconds after which the request is aborted. Omit for no client-side limit.
   *
   * Only `POST /risk/assess` sets this. Every other endpoint answers from Postgres or
   * Dragonfly in milliseconds, so a timeout there would add a failure mode without
   * removing one. `assess` is different in kind: it does live STAC search, windowed COG
   * reads and two forward passes, so it runs 10-40s normally and can hang much longer
   * when a catalogue is degraded.
   *
   * Without a limit that hang becomes the *renderer's* problem — the platform kills the
   * function first and the subscriber gets an opaque 502 instead of a sentence. Aborting
   * ourselves is what makes the failure explainable.
   */
  timeoutMs?: number;
}

/**
 * The real visitor's IP and User-Agent, forwarded to the backend.
 *
 * ## Why this is needed at all
 *
 * Every call in this module runs **server-side**, inside the Next.js container. So without
 * forwarding, the API sees the *renderer* rather than the visitor:
 *
 *     request.client.host     ->  10.0.1.45   (the UI container on the private network)
 *     header "user-agent"     ->  undici      (Node's fetch implementation)
 *
 * `geo.lookup("10.0.1.45")` correctly answers **"Local network"** — an RFC1918 address is a
 * private address, and saying so is more honest than inventing a city — and
 * `useragent.parse("undici")` answers **"Desktop"**, because it is not a known mobile string.
 * Both were reported in production: the security page said "Local network / This device" for
 * every session, on a VPS, for every user.
 *
 * Neither is a backend bug. The backend correctly describes what reached it; the frontend was
 * describing itself.
 *
 * ## Why `x-forwarded-for` is appended rather than replaced
 *
 * Traefik already sets it to the visitor's IP on the way in. Appending the renderer keeps the
 * chain honest — `client, proxy, renderer` — and uvicorn (`--proxy-headers`) takes the
 * left-most entry, which is the visitor. Overwriting it with a single value would work today
 * and lose the audit trail the header exists to carry.
 *
 * Returns nothing outside a request scope: `headers()` throws there, and a background task
 * legitimately has no visitor to describe.
 */
async function forwardedClientHeaders(): Promise<Record<string, string>> {
  try {
    const { headers } = await import("next/headers");
    const incoming = await headers();

    const out: Record<string, string> = {};

    // The visitor's own UA, so the backend records "iPhone · Safari" rather than Node.
    const agent = incoming.get("user-agent");
    if (agent) out["user-agent"] = agent;

    // Prefer the chain Traefik built; fall back to whatever single hop is known.
    const chain =
      incoming.get("x-forwarded-for") ?? incoming.get("x-real-ip") ?? null;
    if (chain) out["x-forwarded-for"] = chain;

    // Preserved so the backend can build absolute URLs (verification links) that match the
    // scheme the visitor actually used, rather than assuming http inside the network.
    const proto = incoming.get("x-forwarded-proto");
    if (proto) out["x-forwarded-proto"] = proto;

    return out;
  } catch {
    // Not in a request scope — a module-level call, or a background task. Nothing to forward,
    // and the backend's own fallbacks are correct for that case.
    return {};
  }
}

/**
 * Pulls a human-readable sentence out of a FastAPI error body.
 *
 * FastAPI reports a validation failure as a list of Pydantic errors, and the raw body
 * reached subscribers verbatim:
 *
 * ```
 * {"detail":[{"type":"string_too_short","loc":["body","password"],
 *   "msg":"String should have at least 12 characters","input":"Password@1", …}]}
 * ```
 *
 * Two problems, and the second is the serious one. It is unreadable — `loc`, `type` and
 * `ctx` are for whoever wrote the schema. And **`input` echoes the submitted value**, so a
 * password appeared in the UI, and from there in screenshots, support tickets and any
 * error tracker the page reports to. Reading only `msg` drops it.
 *
 * Handles the three shapes the backend actually returns:
 *
 *   * `{"detail": "A sentence."}` — the IAM routes' own `HTTPException`s, already written
 *     for end users. Passed through unchanged.
 *   * `{"detail": [{"msg": ..., "loc": [...]}, ...]}` — Pydantic validation. Each entry
 *     becomes "Field: message"; multiple entries are joined so a form reporting two
 *     problems does not silently mention one.
 *   * anything else (HTML from a proxy, an empty body) — returns "", and the caller falls
 *     back to the status line rather than rendering a stack trace or a 502 page.
 */
function readableDetail(body: string): string {
  if (!body) return "";

  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch {
    // Not JSON — a gateway's HTML error page, most likely. Never surfaced: it would put
    // an nginx version string in front of a subscriber.
    return "";
  }

  const detail = (parsed as { detail?: unknown })?.detail;
  if (typeof detail === "string") return detail;

  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        const entry = item as { msg?: unknown; loc?: unknown };
        const msg = typeof entry.msg === "string" ? entry.msg : "";
        if (!msg) return "";

        // Pydantic prefixes value-error messages with "Value error, ", which is framework
        // noise in front of a sentence written for a person.
        const clean = msg.replace(/^Value error,\s*/i, "");

        // `loc` is like ["body", "password"] — the last element is the field. Naming it
        // matters when a form has several: "String should have at least 12 characters"
        // alone does not say which field is too short.
        const loc = Array.isArray(entry.loc) ? entry.loc : [];
        const field = loc.filter((p) => typeof p === "string" && p !== "body").pop();

        return field ? `${String(field).replace(/_/g, " ")}: ${clean}` : clean;
      })
      .filter(Boolean);

    if (messages.length) {
      // Sentence-case the first character so "password: String should…" does not read as
      // though the sentence started mid-way.
      const joined = messages.join(" · ");
      return joined.charAt(0).toUpperCase() + joined.slice(1);
    }
  }

  return "";
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { revalidate, timeoutMs, ...init } = options;

  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  // Describe the VISITOR, not this container. Without these the backend records every session
  // as "Local network / Desktop" — see `forwardedClientHeaders`.
  //
  // Applied before the credential headers below and via `set`, so an explicit caller-supplied
  // value still wins: `init.headers` is seeded into `headers` above, and these only fill in what
  // the caller did not specify.
  for (const [name, value] of Object.entries(await forwardedClientHeaders())) {
    if (!headers.has(name)) headers.set(name, value);
  }
  // Both headers during migration, and the backend prefers the scoped one.
  //
  // A scoped service-account key (shltky…) goes in X-SHELTER-API-Key; the legacy
  // shared key goes in X-SHELTER-Key. Sending both means the same build works against
  // a backend that has migrated and one that has not — a hard cutover would 401
  // subscriber registration in front of real users the moment either side moved
  // first.
  //
  // Detected by prefix rather than configured separately, so there is one environment
  // variable to rotate instead of two that can disagree.
  if (API_KEY) {
    if (API_KEY.startsWith("shltky") || API_KEY.startsWith("shlttk")) {
      headers.set("X-SHELTER-API-Key", API_KEY);
    } else {
      headers.set("X-SHELTER-Key", API_KEY);
    }
  }

  // `AbortSignal.timeout` rather than a hand-rolled controller plus `setTimeout`: it needs no
  // clearing, so it cannot leak a pending timer when the fetch settles first, and it aborts with
  // a `TimeoutError` that is distinguishable from a caller's own cancellation below.
  const timeout =
    timeoutMs === undefined ? undefined : AbortSignal.timeout(timeoutMs);

  let response: Response;
  try {
    response = await fetch(`${API_URL}${API_PREFIX}${path}`, {
      ...init,
      headers,
      signal: timeout,
      next: revalidate === undefined ? undefined : { revalidate },
      cache: revalidate === undefined ? "no-store" : undefined,
    });
  } catch (cause) {
    // Server-side only, so this reaches the operator's logs and never the browser.
    console.error(
      `[shelter] request failed: ${API_URL}${API_PREFIX}${path}`,
      cause,
    );

    // A timeout is NOT an outage, and conflating the two would misinform the one person who
    // can act on the difference. "Temporarily unreachable" tells a subscriber to come back
    // later; a slow assessment means the request was received and is very likely still
    // running server-side — the scan completes and the reading lands on the next page load.
    //
    // 504 rather than 503 so a caller can tell them apart without string-matching a message.
    if (cause instanceof Error && cause.name === "TimeoutError") {
      throw new ApiError(
        "The assessment is taking longer than expected.",
        504,
      );
    }

    // The backend being down should degrade the page, not crash the render.
    throw new ApiError(
      // Deliberately does NOT include API_URL. This message can reach a rendered
      // page, and echoing an internal hostname there discloses deployment topology
      // to anyone who loads the site during a restart. The URL is logged instead,
      // where an operator can see it and a subscriber cannot.
      "The SHELTER service is temporarily unreachable.",
      503,
    );
  }

  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new ApiError(
      readableDetail(body) || `${response.status} ${response.statusText}`,
      response.status,
    );
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** Never throws — pages use this to render a degraded state instead of a 500. */
async function safe<T>(promise: Promise<T>, fallback: T): Promise<T> {
  try {
    return await promise;
  } catch {
    return fallback;
  }
}

export const api = {
  health: () => request<HealthResponse>("/health", { revalidate: 15 }),

  listAlerts: (limit = 20, subscriberId?: string) =>
    request<Alert[]>(
      `/alerts?limit=${limit}` +
        (subscriberId ? `&subscriber_id=${encodeURIComponent(subscriberId)}` : ""),
    ),

  getAlert: (id: string) => request<Alert>(`/alerts/${encodeURIComponent(id)}`),

  listSubscribers: (activeOnly = false) =>
    request<Subscriber[]>(`/subscribers?active_only=${activeOnly}`),

  /**
   * One subscriber by id.
   *
   * Prefer this over `listSubscribers()` + `.find()` anywhere a single record is wanted. The
   * list endpoint is scoped server-side now, but fetching one record means another tenant's
   * data is never loaded into this process even momentarily — and a `.find()` on a list is a
   * client-side filter, which is the shape of scoping that fails silently when the list widens.
   *
   * Returns 404 for a subscriber this caller may not see, deliberately indistinguishable from
   * one that does not exist.
   */
  getSubscriber: (id: string) =>
    request<Subscriber>(`/subscribers/${encodeURIComponent(id)}`),

  /**
   * Replace where alerts are delivered.
   *
   * The **full desired set**, not a diff — that is what makes "stop using WhatsApp" expressible.
   * A binding with `aoi_id` applies to that plot only and OVERRIDES the general ones rather than
   * adding to them; `aoi_id: null` applies to every plot.
   *
   * Sent with the session token so the backend scopes it to this subscriber: the platform key
   * alone would resolve as unrestricted, which was a real write vulnerability before
   * `resolve_audience` was reordered to prefer a session.
   */
  replaceChannels: (
    sessionToken: string,
    subscriberId: string,
    channels: ChannelBinding[],
  ) =>
    request<Subscriber>(
      `/subscribers/${encodeURIComponent(subscriberId)}/channels`,
      {
        method: "PUT",
        headers: { Authorization: `Bearer ${sessionToken}` },
        body: JSON.stringify({ channels }),
      },
    ),

  /**
   * Change who contacts the subscriber about one plot.
   *
   * `direct` (SHELTER does), `webhook` (the aggregator relays, SHELTER sends nothing directly), or
   * `both`. `webhook` is refused for an area with no aggregator behind it — there would be nobody
   * to relay, so it would silence the alerts entirely.
   */
  setAreaDelivery: (
    sessionToken: string,
    subscriberId: string,
    aoiId: string,
    mode: "direct" | "webhook" | "both",
  ) =>
    request<AreaOfInterest>(
      `/subscribers/${encodeURIComponent(subscriberId)}/areas/${encodeURIComponent(aoiId)}`,
      {
        method: "PATCH",
        headers: { Authorization: `Bearer ${sessionToken}` },
        body: JSON.stringify({ delivery_mode: mode }),
      },
    ),

  /**
   * Assess one area NOW, synchronously, and return the reading.
   *
   * The slow endpoint in the API: live STAC search, windowed COG reads over HTTP range
   * requests, and two PyTorch forward passes. 10-40s normally. Nothing is dispatched — this
   * is a measurement, not an alert — so a subscriber pressing it cannot page anybody.
   *
   * ## Why the whole area is sent rather than an id
   *
   * `POST /risk/assess` takes an `AreaOfInterest`, not an `aoi_id`, because it is also the
   * demo and Partner API entry point for ground that is not registered at all. Passing the
   * plot back verbatim as it was read from `GET /subscribers/{id}` is what makes the result
   * comparable with the scheduled scan: `agents/pipeline.enqueue_scan` serialises that same
   * record onto the queue, so both paths measure identical geometry. Re-deriving any field
   * here — a bbox from a ring, say — would produce a reading of subtly different ground and
   * an unexplainable disagreement between this button and the watch loop.
   *
   * Requires `platform:assess`, which the portal's service-account key holds and which is
   * deliberately separate from `platform:read` — this spends real catalogue quota.
   */
  assess: (aoi: AreaOfInterest, timeoutMs?: number) =>
    request<RiskAssessment>("/risk/assess", {
      method: "POST",
      body: JSON.stringify(aoi),
      timeoutMs,
    }),

  latestAssessment: (aoiId: string) =>
    request<RiskAssessment>(`/risk/areas/${encodeURIComponent(aoiId)}`),

  createSubscriber: (payload: unknown) =>
    request<Subscriber>("/subscribers", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  // --- IAM ---------------------------------------------------------------
  // Every call below runs server-side from a Server Action, so a password or a
  // session token never transits the browser's JS bundle. The session is set as an
  // httpOnly cookie by the action, not returned to client code.

  signupIndividual: (payload: IndividualSignupRequest) =>
    request<SignupResponse>("/iam/signup/individual", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  signupCommercial: (payload: CommercialSignupRequest) =>
    request<SignupResponse>("/iam/signup/commercial", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  /**
   * Returns a session, OR an MFA challenge when the account has two-factor enabled.
   * The union is why the caller must discriminate on `mfa_required` rather than
   * assuming a token is present.
   */
  login: (email: string, password: string) =>
    request<SessionToken | LoginChallenge>("/iam/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  verifyMfa: (challenge_token: string, code: string) =>
    request<SessionToken>("/iam/auth/mfa/verify", {
      method: "POST",
      body: JSON.stringify({ challenge_token, code }),
    }),

  requestMagicLink: (email: string, next?: string) =>
    request<{ sent: boolean; detail: string }>("/iam/auth/magic-link", {
      method: "POST",
      body: JSON.stringify({ email, next }),
    }),

  redeemMagicLink: (token: string) =>
    request<SessionToken & { next: string }>("/iam/auth/magic-link/verify", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),

  requestPasswordReset: (email: string) =>
    request<{ sent: boolean; detail: string }>("/iam/auth/password-reset", {
      method: "POST",
      body: JSON.stringify({ email }),
    }),

  confirmPasswordReset: (token: string, password: string) =>
    request<SessionToken>("/iam/auth/password-reset/confirm", {
      method: "POST",
      body: JSON.stringify({ token, password }),
    }),

  // ----------------------------------------------------------------------- //
  // Places — public, no credential. The signup form uses them before an account exists.
  // ----------------------------------------------------------------------- //

  searchPlaces: (q: string, country = "ng") =>
    request<{ results: PlaceResult[]; attribution: string }>(
      `/places/search?q=${encodeURIComponent(q)}&country=${country}&limit=6`,
      { revalidate: 3600 },
    ),

  /**
   * Type-ahead suggestions for a partially typed place.
   *
   * Distinct from `searchPlaces`, and the split is not arbitrary. That one is a **full-text**
   * geocoder (Nominatim): it resolves "Argungu, Kebbi" well, resolves the prefix "Argun" poorly,
   * and its upstream policy is one request per second — so it cannot serve a keystroke. This one
   * queries a self-hosted Photon prefix index built for exactly that.
   *
   * **Check `available` before showing "no matches".** It is false when no Photon instance is
   * deployed, and an empty list means the same thing either way — telling someone their query
   * found nothing because a container is missing is a lie. When false, fall back to the debounced
   * `searchPlaces`.
   *
   * `revalidate: 300` rather than an hour: suggestions are cheap to recompute and a shorter window
   * keeps a freshly imported OSM index visible. Next's cache also collapses the duplicate requests
   * that typing the same prefix twice produces.
   */
  suggestPlaces: (q: string) =>
    request<{
      results: PlaceSuggestion[];
      available: boolean;
      attribution: string;
    }>(`/places/suggest?q=${encodeURIComponent(q)}&limit=8`, {
      revalidate: 300,
    }),

  /**
   * State → LGA → extent, for when a place name finds nothing.
   *
   * OSM's Nigerian coverage is good for cities and thin for villages, and most subscribers are
   * not in cities: "Kobape, Ogun State" returns zero results from Nominatim while its LGA
   * resolves fine. So a name search failing is the normal rural case, not an error, and the
   * answer is to let someone browse to their LGA instead.
   *
   * Cached for a day — an LGA list changes when Nigeria creates an LGA.
   */
  adminStates: () =>
    request<{ names: string[]; attribution: string }>("/places/admin/states", {
      revalidate: 86400,
    }),

  adminLgas: (state: string) =>
    request<{ names: string[]; attribution: string }>(
      `/places/admin/lgas?state=${encodeURIComponent(state)}`,
      { revalidate: 86400 },
    ),

  /**
   * Wards in one LGA — the tier that actually makes the map usable.
   *
   * A ward narrows the search ~13x against its LGA (measured: Kajola is 18x16 km inside Obafemi
   * Owode's 58x63 km), which moves a farm from 22 km off-centre to 6 km — the difference
   * between recognising your own land and panning around.
   *
   * **Empty is a normal answer.** GRID3 covers 24 of 37 states; Lagos, Rivers, FCT and 11 others
   * have no ward layer, and geoBoundaries publishes no ADM3 for Nigeria to fall back on. The
   * caller must skip the step rather than treat it as a failure.
   */
  adminWards: (state: string, lga: string) =>
    request<{ names: string[]; attribution: string }>(
      `/places/admin/wards?state=${encodeURIComponent(state)}&lga=${encodeURIComponent(lga)}`,
      { revalidate: 86400 },
    ),

  /**
   * Where to point the map for one LGA.
   *
   * **Not a monitoring area** — `is_monitorable_area` is always false and the response says
   * why. An LGA is tens of kilometres across; submitting one as an AOI would average a whole
   * district into a single reading.
   */
  adminExtent: (state: string, lga: string, ward?: string) =>
    request<{
      bbox: BBox | null;
      centroid_lon: number | null;
      centroid_lat: number | null;
      is_monitorable_area: boolean;
      note: string;
      attribution: string;
    }>(
      `/places/admin/extent?state=${encodeURIComponent(state)}&lga=${encodeURIComponent(lga)}` +
        (ward ? `&ward=${encodeURIComponent(ward)}` : ""),
      { revalidate: 86400 },
    ),

  /**
   * Turn a place and a size in words into a submittable area.
   *
   * The endpoint the whole picker is built on: no bounding box is ever computed client-side.
   */
  resolveArea: (body: {
    lat?: number;
    lon?: number;
    place?: string;
    size?: string;
    name?: string;
  }) =>
    request<ResolvedArea>("/places/resolve", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** Validate a drawn outline. Same checks as the write path. */
  previewAoi: (body: { ring?: number[][]; lat?: number; lon?: number; radius_km?: number }) =>
    request<AoiPreview>("/places/preview", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** The assignable roles and what each grants. Served from the backend's own table. */
  listRoles: (sessionToken: string) =>
    request<
      {
        value: string;
        label: string;
        description: string;
        permissions: string[];
        scopes: string[];
      }[]
    >("/iam/roles", { headers: { Authorization: `Bearer ${sessionToken}` } }),

  /**
   * The intelligence tracks that exist, and whether each one delivers anything yet.
   *
   * Fetched rather than hardcoded in the frontend. `deliverable` comes from reading
   * `OracleAgent._classify` — a track with no primary hazard produces no alerts — and a copy
   * of that table here would drift the moment the risk model gains a hazard.
   */
  listTracks: (sessionToken: string) =>
    request<Track[]>("/iam/tracks", {
      headers: { Authorization: `Bearer ${sessionToken}` },
    }),

  listWorkspaces: (sessionToken: string) =>
    request<Workspace[]>("/iam/workspaces", {
      headers: { Authorization: `Bearer ${sessionToken}` },
    }),

  createWorkspace: (
    sessionToken: string,
    body: { name: string; tracks: string[] },
  ) =>
    request<Workspace>("/iam/workspaces", {
      method: "POST",
      headers: { Authorization: `Bearer ${sessionToken}` },
      body: JSON.stringify(body),
    }),

  updateWorkspace: (
    sessionToken: string,
    id: string,
    body: { name?: string; tracks?: string[] },
  ) =>
    request<Workspace>(`/iam/workspaces/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { Authorization: `Bearer ${sessionToken}` },
      body: JSON.stringify(body),
    }),

  deleteWorkspace: (sessionToken: string, id: string) =>
    request<void>(`/iam/workspaces/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${sessionToken}` },
    }),

  /**
   * Customers in one workspace, via a portal SESSION.
   *
   * Distinct from the Partner API's key-gated `/iam/customers` on purpose: an operator signed
   * into the portal must not have to paste their own API key into a browser form — a key in a
   * form is a key in browser history, in a screenshot and in a support chat.
   */
  workspaceCustomers: (sessionToken: string, workspaceId: string) =>
    request<WorkspaceCustomer[]>(
      `/iam/workspaces/${encodeURIComponent(workspaceId)}/customers`,
      { headers: { Authorization: `Bearer ${sessionToken}` } },
    ),

  addWorkspaceCustomer: (
    sessionToken: string,
    workspaceId: string,
    body: Record<string, unknown>,
  ) =>
    request<WorkspaceCustomer>(
      `/iam/workspaces/${encodeURIComponent(workspaceId)}/customers`,
      {
        method: "POST",
        headers: { Authorization: `Bearer ${sessionToken}` },
        body: JSON.stringify(body),
      },
    ),

  workspaceCustomerAreas: (
    sessionToken: string,
    workspaceId: string,
    accountId: string,
  ) =>
    request<AreaOfInterest[]>(
      `/iam/workspaces/${encodeURIComponent(workspaceId)}/customers/${encodeURIComponent(accountId)}/areas`,
      { headers: { Authorization: `Bearer ${sessionToken}` } },
    ),

  addWorkspaceCustomerArea: (
    sessionToken: string,
    workspaceId: string,
    accountId: string,
    body: Record<string, unknown>,
  ) =>
    request<AreaOfInterest>(
      `/iam/workspaces/${encodeURIComponent(workspaceId)}/customers/${encodeURIComponent(accountId)}/areas`,
      {
        method: "POST",
        headers: { Authorization: `Bearer ${sessionToken}` },
        body: JSON.stringify(body),
      },
    ),

  renameWorkspaceCustomerArea: (
    sessionToken: string,
    workspaceId: string,
    accountId: string,
    aoiId: string,
    body: { name?: string; crop?: string },
  ) =>
    request<AreaOfInterest>(
      `/iam/workspaces/${encodeURIComponent(workspaceId)}/customers/${encodeURIComponent(accountId)}/areas/${encodeURIComponent(aoiId)}`,
      {
        method: "PATCH",
        headers: { Authorization: `Bearer ${sessionToken}` },
        body: JSON.stringify(body),
      },
    ),

  removeWorkspaceCustomerArea: (
    sessionToken: string,
    workspaceId: string,
    accountId: string,
    aoiId: string,
  ) =>
    request<void>(
      `/iam/workspaces/${encodeURIComponent(workspaceId)}/customers/${encodeURIComponent(accountId)}/areas/${encodeURIComponent(aoiId)}`,
      { method: "DELETE", headers: { Authorization: `Bearer ${sessionToken}` } },
    ),

  listTeam: (sessionToken: string) =>
    request<TeamMember[]>("/iam/team", {
      headers: { Authorization: `Bearer ${sessionToken}` },
    }),

  /**
   * Roles this member may assign — narrower than every role that exists.
   *
   * Separate from `listRoles` because only an owner may create another owner. Rendering the
   * full list would offer a choice the API refuses.
   */
  assignableRoles: (sessionToken: string) =>
    request<RoleOption[]>("/iam/team/assignable-roles", {
      headers: { Authorization: `Bearer ${sessionToken}` },
    }),

  resendInvitation: (sessionToken: string, email: string) =>
    request<{ resent: string; email_sent: boolean; expires_at: string }>(
      "/iam/team/invitations/resend",
      {
        method: "POST",
        headers: { Authorization: `Bearer ${sessionToken}` },
        body: JSON.stringify({ email }),
      },
    ),

  listInvitations: (sessionToken: string) =>
    request<PendingInvitation[]>("/iam/team/invitations", {
      headers: { Authorization: `Bearer ${sessionToken}` },
    }),

  inviteMember: (
    sessionToken: string,
    body: { email: string; grants: WorkspaceGrant[] },
  ) =>
    request<{ invited: string; email_sent: boolean; expires_at: string }>(
      "/iam/team/invitations",
      {
        method: "POST",
        headers: { Authorization: `Bearer ${sessionToken}` },
        body: JSON.stringify(body),
      },
    ),

  /**
   * Redeem a team invitation. Unauthenticated — the token is the proof.
   *
   * Returns a session that can do nothing but set a password, so the invited colleague lands
   * signed in without a temporary password ever existing.
   */
  redeemInvitation: (token: string) =>
    request<{
      access_token: string;
      expires_in: number;
      must_set_password: boolean;
      email: string;
      organisation: string | null;
      workspaces: number;
    }>("/iam/team/invitations/redeem", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),

  /** Set the first password. Returns a full, unscoped session. */
  setFirstPassword: (sessionToken: string, password: string) =>
    request<SessionToken>("/iam/team/first-password", {
      method: "POST",
      headers: { Authorization: `Bearer ${sessionToken}` },
      body: JSON.stringify({ password }),
    }),

  setMemberGrants: (
    sessionToken: string,
    accountId: string,
    grants: WorkspaceGrant[],
  ) =>
    request<{ updated: number }>(
      `/iam/team/${encodeURIComponent(accountId)}/grants`,
      {
        method: "PUT",
        headers: { Authorization: `Bearer ${sessionToken}` },
        body: JSON.stringify(grants),
      },
    ),

  removeMember: (sessionToken: string, accountId: string) =>
    request<void>(`/iam/team/${encodeURIComponent(accountId)}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${sessionToken}` },
    }),

  /**
   * The caller's own role, permissions and grantable scopes.
   *
   * One call so the portal never derives permissions from a role name — see `MyAccess`.
   */
  myAccess: (sessionToken: string) =>
    request<MyAccess>("/iam/me/access", {
      headers: { Authorization: `Bearer ${sessionToken}` },
    }),

  /**
   * Bind a plot to the SIGNED-IN account and go autonomous.
   *
   * ## Why this and not `createSubscriber`
   *
   * `createSubscriber` is the platform endpoint an aggregator uses to onboard somebody else. It
   * writes the Postgres subscriber row and **does not touch the IAM account**, so
   * `account.subscriber_id` stays null — and every portal page gates on that field. The
   * subscription runs, assessments accumulate, and the dashboard says "nothing is being
   * monitored", because the two stores were never linked.
   *
   * `/iam/activate` does both: it persists the subscriber AND calls `bind_subscriber`, which is
   * what makes the portal able to find it. It also takes the session rather than the platform
   * key, so the plot is bound to whoever is actually signed in instead of to an id supplied by
   * the caller.
   */
  activateSubscription: (
    sessionToken: string,
    body: { area: unknown; channels: unknown[] },
  ) =>
    request<Subscriber>("/iam/activate", {
      method: "POST",
      headers: { Authorization: `Bearer ${sessionToken}` },
      body: JSON.stringify(body),
    }),

  /**
   * Add another monitored area to the signed-in subscriber.
   *
   * There is no limit on areas — a farmer with four scattered plots is the normal case, and each
   * is assessed independently on every satellite pass.
   */
  addMyArea: (
    sessionToken: string,
    subscriberId: string,
    body: Record<string, unknown>,
  ) =>
    request<AreaOfInterest>(
      `/subscribers/${encodeURIComponent(subscriberId)}/areas`,
      {
        method: "POST",
        headers: { Authorization: `Bearer ${sessionToken}` },
        body: JSON.stringify(body),
      },
    ),

  /**
   * Rename or re-crop one of the signed-in subscriber's areas.
   *
   * Deliberately does NOT send geometry. Renaming is an in-place edit that keeps the `aoi_id`, so
   * the plot's assessment history stays attached and stays meaningful. Moving or resizing would
   * leave one timeline mixing readings of two different footprints under a single name.
   */
  renameMyArea: (
    sessionToken: string,
    subscriberId: string,
    aoiId: string,
    body: { name?: string; crop?: string },
  ) =>
    request<AreaOfInterest>(
      `/subscribers/${encodeURIComponent(subscriberId)}/areas/${encodeURIComponent(aoiId)}`,
      {
        method: "PATCH",
        headers: { Authorization: `Bearer ${sessionToken}` },
        body: JSON.stringify(body),
      },
    ),

  removeMyArea: (sessionToken: string, subscriberId: string, aoiId: string) =>
    request<void>(
      `/subscribers/${encodeURIComponent(subscriberId)}/areas/${encodeURIComponent(aoiId)}`,
      {
        method: "DELETE",
        headers: { Authorization: `Bearer ${sessionToken}` },
      },
    ),

  /** The caller's own profile, using their session rather than the platform key. */
  me: (sessionToken: string) =>
    request<Account>("/iam/me", {
      headers: { Authorization: `Bearer ${sessionToken}` },
    }),

  /**
   * Consumes an emailed verification token. Single-use.
   *
   * Deliberately unauthenticated — the token *is* the proof. Requiring a session too
   * would break the common case where someone opens the link in their phone's mail app,
   * which is a different browser from the one they signed up in.
   */
  /**
   * Screen a password against Have I Been Pwned.
   *
   * Unauthenticated because it runs on the signup form, before an account exists. Safe
   * because it is a pure function of its input and reveals nothing an attacker could not
   * learn by querying HIBP directly.
   *
   * The password reaches OUR server, which sends only a 5-character SHA-1 prefix upstream —
   * never the password, never the full hash. See `backend/app/iam/breached.py`.
   */
  checkPassword: (password: string) =>
    request<{ breached: boolean; times_seen: number; checked: boolean }>(
      "/iam/password/check",
      { method: "POST", body: JSON.stringify({ password }) },
    ),

  verifyEmail: (token: string) =>
    request<Account>(`/iam/verify-email?token=${encodeURIComponent(token)}`, {
      method: "POST",
    }),

  /**
   * Re-sends the confirmation link to the caller's own address.
   *
   * Session-scoped, and the backend takes the address from the session rather than a
   * parameter — an unauthenticated resend that accepted an arbitrary email would be a
   * free email cannon pointed at anyone.
   */
  /**
   * Re-send the confirmation link.
   *
   * The two outcomes have different shapes — `{sent: true, expires_in_hours}` on success,
   * `{sent: false, detail}` when the address is already confirmed — so both fields are
   * optional here and the caller composes the message. Typing `detail` as required made
   * the success path render an empty string.
   */
  resendVerification: (sessionToken: string) =>
    request<{
      sent: boolean;
      detail?: string;
      expires_in_hours?: number;
    }>("/iam/resend-verification", {
      method: "POST",
      headers: { Authorization: `Bearer ${sessionToken}` },
    }),

  // ------------------------------------------------------------------------- //
  // Session state
  //
  // `sessionState` READS and `sessionActivity` WRITES, and the split is the design: if
  // reading refreshed the idle window, the dashboard's own polling would keep an
  // unattended tab signed in indefinitely.
  // ------------------------------------------------------------------------- //

  /** Idle state. Read-only — never extends the window. */
  sessionState: (sessionToken: string) =>
    request<SessionState>("/iam/session", {
      headers: { Authorization: `Bearer ${sessionToken}` },
    }),

  /** Report real user input. The only call that resets the idle window. */
  sessionActivity: (sessionToken: string) =>
    request<SessionState>("/iam/session/activity", {
      method: "POST",
      headers: { Authorization: `Bearer ${sessionToken}` },
    }),

  /** Deliberate "keep me signed in". Same refresh, but audited. */
  sessionExtend: (sessionToken: string) =>
    request<SessionState>("/iam/session/extend", {
      method: "POST",
      headers: { Authorization: `Bearer ${sessionToken}` },
    }),

  /** End a session, recording whether it was idle or deliberate. */
  sessionEnd: (sessionToken: string, reason: "idle" | "user") =>
    request<{ ended: boolean }>("/iam/session/end", {
      method: "POST",
      headers: { Authorization: `Bearer ${sessionToken}` },
      body: JSON.stringify({ reason }),
    }),

  /** Append a frontend action to the immutable audit log. */
  recordPortalEvent: (sessionToken: string, event: string, detail?: string) =>
    request<{ recorded: boolean }>("/iam/session/event", {
      method: "POST",
      headers: { Authorization: `Bearer ${sessionToken}` },
      body: JSON.stringify({ event, detail: detail ?? null }),
    }),

  // ------------------------------------------------------------------------- //
  // Portal — all scoped by the session, never by a parameter
  // ------------------------------------------------------------------------- //

  /**
   * One page of the caller's own audit log.
   *
   * Keyset-paginated over `(at, _id)`. `cursor` is opaque — it comes from the previous
   * response's `next_cursor` and must not be constructed by the caller.
   */
  auditPage: (
    sessionToken: string,
    opts: { cursor?: string; action?: string; pageSize?: number } = {},
  ) => {
    const q = new URLSearchParams();
    if (opts.cursor) q.set("cursor", opts.cursor);
    if (opts.action) q.set("action", opts.action);
    if (opts.pageSize) q.set("page_size", String(opts.pageSize));
    const qs = q.toString();

    return request<AuditPage>(`/iam/audit${qs ? `?${qs}` : ""}`, {
      headers: { Authorization: `Bearer ${sessionToken}` },
    });
  },

  /** Action counts over a window, for the activity widget. */
  auditActivity: (sessionToken: string, days = 30) =>
    request<AuditSummary>(`/iam/audit/activity?days=${days}`, {
      headers: { Authorization: `Bearer ${sessionToken}` },
    }),

  /** Whether TOTP is enrolled, and how many recovery codes remain. */
  totpState: (sessionToken: string) =>
    request<TotpState>("/iam/auth/totp", {
      headers: { Authorization: `Bearer ${sessionToken}` },
    }),

  /**
   * Devices this account has signed in from, for the Security page.
   *
   * Derived server-side from the audit log, so it is correct retroactively rather than only from
   * the moment a devices table started being written.
   */
  trustedDevices: (sessionToken: string) =>
    request<TrustedDeviceList>("/iam/security/devices", {
      headers: { Authorization: `Bearer ${sessionToken}` },
    }),

  /**
   * Start an in-session password change: emails a 6-character code to the registered address.
   *
   * Distinct from `requestPasswordReset`, which is the signed-OUT forgot-password path and stays a
   * single-use link. See the backend note on `passwordless.PASSWORD_CODE_LENGTH` for why a short
   * code is safe behind a session and would not be in front of one.
   */
  requestPasswordChangeCode: (sessionToken: string) =>
    request<PasswordChangeCodeSent>("/iam/password/change/request", {
      method: "POST",
      headers: { Authorization: `Bearer ${sessionToken}` },
    }),

  /** Confirm the code and set the new password. Returns a fresh session. */
  confirmPasswordChange: (
    sessionToken: string,
    body: { code: string; password: string },
  ) =>
    request<SessionToken>("/iam/password/change/confirm", {
      method: "POST",
      headers: { Authorization: `Bearer ${sessionToken}` },
      body: JSON.stringify(body),
    }),

  /**
   * Webhook endpoints this aggregator owns.
   *
   * Scoped server-side by `webhook_caller`: a portal session resolves to the signed-in
   * aggregator's organisation, so another tenant's subscription is never a candidate row. A
   * platform key sees all of them — that is the operations surface.
   */
  listWebhooks: (sessionToken: string) =>
    request<WebhookPublic[]>("/webhook/subscriptions", {
      headers: { Authorization: `Bearer ${sessionToken}` },
    }),

  /**
   * Register an endpoint. **Returns the signing secret once.**
   *
   * There is no reveal endpoint — the API stores a hash. A leaked secret could forge flood alerts
   * into a payout engine, which is why rotation is a hard cutover rather than a grace period.
   */
  createWebhook: (
    sessionToken: string,
    body: {
      name: string;
      url: string;
      events: string[];
      min_severity?: string | null;
      /**
       * Which workspace this endpoint serves. Omit for every workspace this account owns.
       *
       * Not merely cosmetic: an aggregator running a Bayelsa flood pilot and a Kebbi rice season
       * needs each programme's endpoint to receive only its own alerts. The API refuses a workspace
       * the caller does not own with a **404**, not a 403 — a 403 would confirm the id exists and
       * turn the endpoint into a workspace-enumeration oracle across tenants.
       */
      workspace_id?: string | null;
    },
  ) =>
    request<WebhookPublic & { secret: string }>("/webhook/subscriptions", {
      method: "POST",
      headers: { Authorization: `Bearer ${sessionToken}` },
      body: JSON.stringify(body),
    }),

  /**
   * Send a `shelter.test` payload now.
   *
   * The point is validating signature verification against real bytes from the real engine
   * *before* a live flood alert depends on it. The payload is fixed and obviously synthetic.
   */
  testWebhook: (sessionToken: string, id: string) =>
    request<{ delivered: boolean }>(
      `/webhook/subscriptions/${encodeURIComponent(id)}/test`,
      { method: "POST", headers: { Authorization: `Bearer ${sessionToken}` } },
    ),

  /** Delivery history and health for one endpoint. */
  webhookDeliveries: (sessionToken: string, id: string) =>
    request<{ deliveries: unknown[] }>(
      `/webhook/subscriptions/${encodeURIComponent(id)}/deliveries`,
      { headers: { Authorization: `Bearer ${sessionToken}` } },
    ),

  /** Remove an endpoint. It receives nothing further. */
  deleteWebhook: (sessionToken: string, id: string) =>
    request<void>(`/webhook/subscriptions/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${sessionToken}` },
    }),

  /** The caller's own API keys. Individuals get 403 — only commercial accounts mint keys. */
  listApiKeys: (sessionToken: string) =>
    request<ApiKeyPublic[]>("/iam/api-keys", {
      headers: { Authorization: `Bearer ${sessionToken}` },
    }),

  /**
   * Mint an API key for one workspace.
   *
   * **The plaintext is in this response and nowhere else** — not in the database, not in the
   * notification email. So the caller must show it once and say so; there is no "reveal" later.
   *
   * `workspace_id` is required by this client even though the API defaults it, because a key that
   * silently lands on the default workspace is the kind of mistake that is only discovered when it
   * returns the wrong customers.
   */
  createApiKey: (
    sessionToken: string,
    body: {
      name: string;
      workspace_id: string;
      scopes: string[];
      expires_in_days?: number | null;
    },
  ) =>
    request<ApiKeyPublic & { key: string }>("/iam/api-keys", {
      method: "POST",
      headers: { Authorization: `Bearer ${sessionToken}` },
      body: JSON.stringify(body),
    }),

  /** Revoke a key immediately. Irreversible — an integration using it starts failing at once. */
  revokeApiKey: (sessionToken: string, keyId: string) =>
    request<void>(`/iam/api-keys/${encodeURIComponent(keyId)}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${sessionToken}` },
    }),

  /**
   * Every monitored area in a workspace, with the customer it belongs to and where its alerts go.
   *
   * The one call that answers `Workspace > Customer > Area > Alerts`. Added because an aggregator
   * could see its customers and one customer's areas, but never the workspace's monitoring as a
   * whole — so a workspace holding active areas whose attribution was broken looked, correctly but
   * unhelpfully, like a workspace with no customers.
   */
  workspaceAreas: (sessionToken: string, workspaceId: string) =>
    request<WorkspaceAreaRow[]>(
      `/iam/workspaces/${encodeURIComponent(workspaceId)}/areas`,
      { headers: { Authorization: `Bearer ${sessionToken}` } },
    ),

  /**
   * Which organisations can see this subscriber's data.
   *
   * Takes no parameter by design: one aggregator learning that another also serves this
   * farmer is commercially sensitive, so this is available to the subscriber alone.
   */
  myAggregators: (sessionToken: string) =>
    request<{ aggregators: AggregatorMembership[] }>("/iam/me/aggregators", {
      headers: { Authorization: `Bearer ${sessionToken}` },
    }),

  /** Update language, preferred channel and alert threshold. */
  updatePreferences: (
    sessionToken: string,
    body: { language?: string; preferred_channel?: string },
  ) =>
    request<Account>("/iam/me/preferences", {
      method: "PATCH",
      headers: { Authorization: `Bearer ${sessionToken}` },
      body: JSON.stringify(body),
    }),
};

export const safeApi = {
  health: () => safe(api.health(), null),
  /**
   * `subscriberId` scopes the feed to one subscriber — the portal passes it so a
   * subscriber never receives another's advisories in their own view. Omitted, this is
   * the global operations feed that `/dashboard` shows.
   */
  listAlerts: (limit = 20, subscriberId?: string) =>
    safe(api.listAlerts(limit, subscriberId), [] as Alert[]),

  /**
   * The latest assessment for one area, or null.
   *
   * Null is a real state, not an error: an area added minutes ago has no assessment yet, and the
   * UI must say "first scan queued" rather than "unavailable" — the two mean different things to
   * someone waiting to see whether monitoring actually started.
   */
  latestAssessment: (aoiId: string) =>
    safe(api.latestAssessment(aoiId), null),
  listSubscribers: () => safe(api.listSubscribers(), [] as Subscriber[]),

  /**
   * One subscriber, or null.
   *
   * Null covers both "backend unreachable" and "not yours" — the API returns 404 for another
   * tenant's record on purpose, so the two are indistinguishable here by design.
   */
  getSubscriber: (id: string) => safe(api.getSubscriber(id), null),
};
