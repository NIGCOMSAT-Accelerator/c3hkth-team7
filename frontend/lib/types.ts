/**
 * Mirrors `backend/app/models/schemas.py`.
 * These names are the wire contract — change them on both sides together.
 */

export type Severity =
  | "info"
  | "advisory"
  | "watch"
  | "warning"
  | "emergency";

export type HazardType =
  | "crop_waterlogging"
  | "crop_drought_stress"
  | "crop_vegetation_anomaly"
  | "flood_inundation"
  | "flood_forecast"
  | "malaria_risk";

export type Channel =
  | "whatsapp"
  | "telegram"
  | "signal"
  | "email"
  | "slack"
  | "webhook"
  | "nigcomsat_broadcast";

export type SubscriberKind =
  | "farmer"
  | "cooperative"
  | "government"
  | "emergency_responder"
  | "public_health"
  | "insurer";

export interface BBox {
  west: number;
  south: number;
  east: number;
  north: number;
}

export interface AreaOfInterest {
  id: string;
  name: string;
  bbox: BBox;
  country: string;
  admin1?: string | null;
  admin2?: string | null;
  crop?: string | null;
  hectares?: number | null;
  /** Who contacts the subscriber about this plot. Defaults to `direct`. */
  delivery_mode?: DeliveryMode;
}

export interface ForecastPoint {
  day: number;
  date: string;
  risk: number;
  rainfall_mm: number;
  note?: string | null;
}

export interface ExposureSummary {
  population: number;
  cropland_hectares: number;
  cropland_fraction: number;
  water_fraction: number;
  builtup_fraction: number;
  /** Share of the AOI below its own median elevation — where water collects. */
  lowland_fraction: number;
  settlements: number;
  health_facilities: number;
  /** Which datasets answered. Empty means "unknown", not "nothing there". */
  sources: string[];
}

export interface SoilProfile {
  clay_g_kg: number;
  sand_g_kg: number;
  /** "free" | "moderate" | "impeded" | "unknown" */
  drainage: string;
  available: boolean;
}

export interface HealthBaseline {
  malaria_pfpr: number;
  endemic: boolean;
  available: boolean;
}

/**
 * Measured soil water content from SMAP, m3/m3.
 *
 * Distinct from `SoilProfile`, which is texture — a permanent property. This is a state, and it is
 * the one an irrigation decision turns on.
 *
 * `available: false` means UNKNOWN, never dry. `volumetric` defaults to 0, so rendering it without
 * checking `available` would show a farmer "0.00 — bone dry" for a field nobody measured.
 * `irrigation_advice` is null in that case, which is the field to branch on.
 */
export interface SoilMoisture {
  volumetric: number;
  /**
   * Agronomic band, computed server-side: `unknown` | `very_dry` | `dry` | `adequate` | `wet` |
   * `saturated`.
   *
   * Sent rather than derived here on purpose — re-implementing the wilting-point and
   * field-capacity thresholds in TypeScript would put a second copy of an agronomic decision in a
   * language that cannot import the first.
   */
  status: string;
  /** `irrigate` | `hold` | `drain`, or null when unknown. Null means no instruction. */
  irrigation_advice: string | null;
  /** `YYYY-MM-DD` of the overpass. Never today — SMAP publishes ~2 days in arrears. */
  observed_date: string;
  /** How far the grid cell read was from the plot, degrees. Evidence the lookup was correct. */
  location_error_deg: number;
  available: boolean;
}

export interface RiskAssessment {
  id: string;
  aoi_id: string;
  aoi_name: string;
  hazard: HazardType;
  severity: Severity;
  score: number;
  confidence: number;
  forecast: ForecastPoint[];
  exposure: ExposureSummary;
  soil: SoilProfile;
  soil_moisture: SoilMoisture;
  /**
   * How this reading differs from the previous run and from the seasonal norm.
   *
   * Every field optional, and **absent means unknown rather than unchanged** — a first assessment
   * has nothing to compare against, and a plot with no fitted baseline has no norm.
   */
  change?: {
    previous_severity?: string | null;
    previous_score?: number | null;
    previous_assessed_at?: string | null;
    /** `up` | `down` | `steady`. Switch on this, not on the wording. */
    direction?: string | null;
    /** "wetter" | "drier" | "greener" | "browner" | "normal" */
    vs_seasonal?: string | null;
    vs_seasonal_z?: number | null;
  };
  /** When the inputs were observed and when the next look is due. */
  freshness?: {
    observed_at?: string | null;
    platform?: string | null;
    /** An ESTIMATE from the revisit cadence, not a schedule. */
    next_expected?: string | null;
    /** Names an absent leg — "No radar pass this cycle". */
    caveat?: string | null;
  };
  health: HealthBaseline;
  evidence: string[];
  cascade: HazardType[];
  /** Provenance — which upstream datasets contributed to this assessment. */
  data_sources: string[];
  lead_time_days: number;
  assessed_at: string;
}

/** Human labels for the provenance chips on the dashboard. */
export const SOURCE_LABEL: Record<string, string> = {
  "sentinel-1": "Sentinel-1 SAR",
  "sentinel-2": "Sentinel-2",
  "gfs-forecast": "NOAA GFS forecast",
  "climateserv-gefs": "GEFS forecast",
  "climateserv-chirps": "CHIRPS observed",
  "gpm-imerg": "GPM IMERG",
  era5: "ERA5 reanalysis",
  worldpop: "WorldPop",
  worldcover: "ESA WorldCover",
  "copernicus-dem": "Copernicus DEM",
  openstreetmap: "OpenStreetMap",
  soilgrids: "SoilGrids",
  "smap-l3": "NASA SMAP soil moisture",
  "malaria-atlas": "Malaria Atlas",
};

/**
 * The three explanation surfaces, stored with the alert.
 *
 * Empty strings when the alert predates this feature or no provider was configured — never null,
 * so a caller never has to guard before rendering. Each field carries its deterministic template
 * rather than nothing when generation was unavailable.
 */
export interface Explanations {
  /** What the crop is doing, in plain language. */
  crop: string;
  /** Why the risk score is what it is. */
  drivers: string;
  /** Irrigate or hold, with the reason. */
  irrigation: string;
}

export interface Advisory {
  headline: string;
  body: string;
  actions: string[];
  broadcast_text: string;
  language: string;
  generated_by: string;
  explanations: Explanations;
}

/**
 * Fahis's verdict on one alert — "were we right?".
 *
 * `null` on an alert means verification has not run yet, which is normal: it is scheduled days
 * after the assessment so the outcome has time to be reported. That is a DIFFERENT state from a
 * verdict of `unverified`, which is a finding — nothing was found either way.
 */
/**
 * One source behind a verdict, as shown to the subscriber.
 *
 * No snippet. What makes a verdict checkable is the reader's ability to open the link and judge
 * for themselves; an excerpt asks them to trust ours instead, and puts unattributed web prose
 * beside a measured advisory — the adjacency the grounding rule prevents.
 */
export interface CitedSource {
  url: string;
  title: string;
  /** `official` (government or agency), `media`, or `low`. Weight, shown so it can be judged. */
  tier: string;
  /** Publication date where the source stated one. Load-bearing: a 2019 article cannot
   *  corroborate a 2026 warning, and showing the date lets a reader catch that themselves. */
  published: string | null;
}

export interface VerdictSummary {
  verdict: "confirmed" | "partial" | "refuted" | "unverified" | "not_attempted";
  /** How sure Fahis is of the VERDICT, not of the original alert. */
  confidence: number;
  rationale: string;
  /** How many independent sources were cited. */
  source_count: number;
  /** The citations themselves, highest tier first. Empty on `unverified` with nothing found,
   *  and on an older backend that returned only the count. */
  sources?: CitedSource[];
  /** True for `confirmed` and `refuted` only — the two that count toward precision. */
  trainable: boolean;
  verified_at: string | null;
}

export interface DeliveryReceipt {
  channel: Channel;
  address: string;
  status: "pending" | "sent" | "failed" | "skipped";
  provider_message_id?: string | null;
  error?: string | null;
  attempted_at: string;
}

/**
 * One measured dimension of a plot's situation — a report-card module.
 *
 * **Derived by the backend, never here.** The thresholds that turn 0.31 into "a substantial part of
 * your plot is under water" live in `backend/app/dispatch/tracks.py` and nowhere else. A TypeScript
 * copy is how the alert email and this page would come to describe one plot differently — the exact
 * drift `card_fields` and the shared email layout were each written to end.
 *
 * So this interface carries no logic: render `reading` and `meaning` as given, key icons off `key`,
 * and preserve the server's order. `backend/tests/test_tracks.py` asserts no threshold constants
 * appear in this directory.
 */
export interface AssessmentTrack {
  /** `flood` | `crop` | `soil_water` | `rainfall` | `malaria`. Switch on this, not on `label`. */
  key: string;
  label: string;
  /** Already formatted with its unit — "31%", "Saturated", "126 mm expected". */
  reading: string;
  meaning: string;
  /** Ordering only. Already applied server-side; kept so the order survives a re-sort. */
  weight: number;
  sources: string[];
  /** `[label, value]` rows, revealed when the module is opened. */
  detail: [string, string][];
}

export interface Alert {
  id: string;
  subscriber_id: string;
  assessment: RiskAssessment;
  advisory: Advisory;
  /**
   * The per-track modules, most relevant first.
   *
   * **Empty is a real state, not an error** — a fully clouded cycle with no radar pass measured
   * nothing, and the honest rendering is "we could not look this cycle" rather than five modules
   * reading zero. Optional because an older cached payload predates the field.
   */
  tracks?: AssessmentTrack[];
  receipts: DeliveryReceipt[];
  /** Fahis's verdict, once it has run. Null until then. */
  verdict?: VerdictSummary | null;
  created_at: string;
}

export type DeliveryMode = "direct" | "webhook" | "both";

export interface ChannelBinding {
  channel: Channel;
  address: string;
  enabled: boolean;
  min_severity: Severity;
  /**
   * Which plot this applies to. **Null means every plot.**
   *
   * A binding naming an area OVERRIDES the general ones for that area rather than adding to
   * them — otherwise adding an SMS override would leave the general email firing too, and the
   * subscriber would get two alerts about one hazard with no way to silence the first.
   */
  aoi_id?: string | null;
  /**
   * Only deliver at or above this risk score, 0–1. **Null means no score filter.**
   *
   * The continuous companion to `min_severity`. The severity ladder has five steps, so every
   * subscriber inside the 0.20-wide band between WATCH (0.40) and WARNING (0.60) is treated
   * identically — and that is the band they most disagree about.
   *
   * Filters **delivery only**: the assessment is computed and stored regardless, and stays visible
   * in the portal. Raising this opts out of being messaged, not out of being watched.
   */
  min_score?: number | null;
}

export interface Subscriber {
  id: string;
  name: string;
  kind: SubscriberKind;
  language: string;
  areas: AreaOfInterest[];
  channels: ChannelBinding[];
  active: boolean;
  created_at: string;
}

export interface HealthResponse {
  status: string;
  service: string;
  environment: string;
  redis: string;
  /** Optional because these fields postdate some deployed backends. Absence must read
   *  as "unknown", never as "down" — treating a missing field as a failure would make
   *  a version skew look like an outage. */
  postgres?: {
    status: string;
    extensions?: Record<string, boolean>;
    pending_migrations?: string[];
  };
  cache?: { status: string };
  /** Which email transport is actually resolved, not merely configured. */
  notifications?: {
    configured: string;
    resolved: string;
    operational: boolean;
  };
  channels_configured: Channel[];
  /** Which path generates advisories, resolved server-side.
   *  `provider` is "openai" | "anthropic" | "template"; `label` is the model
   *  name, or "template" when generation is off. Render `label`. */
  advisory_generator: {
    provider: string;
    configured: string;
    model: string | null;
    label: string;
  };
  models: Record<string, string>;
  scheduler: {
    enabled: boolean;
    running: boolean;
    interval_seconds: number;
    last_cycle: string | null;
  };
  queue_depth: Record<string, number>;
}

/** Display metadata. Severity always carries an icon and a label so identity
 *  is never conveyed by color alone. */
export const SEVERITY_META: Record<
  Severity,
  { label: string; icon: string; rank: number }
> = {
  info: { label: "Info", icon: "●", rank: 0 },
  advisory: { label: "Advisory", icon: "◆", rank: 1 },
  watch: { label: "Watch", icon: "▲", rank: 2 },
  warning: { label: "Warning", icon: "▲", rank: 3 },
  emergency: { label: "Emergency", icon: "■", rank: 4 },
};

export const HAZARD_LABEL: Record<HazardType, string> = {
  crop_waterlogging: "Waterlogged cropland",
  crop_drought_stress: "Crop drought stress",
  crop_vegetation_anomaly: "Crop condition change",
  flood_inundation: "Flooding detected",
  flood_forecast: "Flooding expected",
  malaria_risk: "Raised malaria risk",
};

export const CHANNEL_LABEL: Record<Channel, string> = {
  whatsapp: "WhatsApp",
  telegram: "Telegram",
  signal: "Signal",
  email: "Email",
  slack: "Slack",
  webhook: "Webhook",
  nigcomsat_broadcast: "NIGCOMSAT-1R broadcast",
};

// --------------------------------------------------------------------------- //
// IAM — accounts, sessions and API keys
//
// Mirrors `backend/app/iam/models.py`. Kept hand-written rather than generated from
// openapi.json because these are the types the UI reasons about most, and a generated
// file would be regenerated over any local clarification. `make openapi-client` exists
// for partners who want a full generated client.
// --------------------------------------------------------------------------- //

/**
 * Individual vs commercial is a security boundary, not a pricing tier.
 * Individuals are portal-only and hold no API key; commercial accounts hold scoped
 * keys restricted to their own customers. `service` is a machine principal and never
 * appears in the portal.
 */
export type AccountKind = "individual" | "commercial" | "service";

export type AccountStatus =
  | "pending_verification"
  | "active"
  | "suspended"
  | "disabled";

export interface Account {
  /** 10-character alphanumeric, e.g. `A7K2M9P4QX`. Public-facing and quotable. */
  id: string;
  kind: AccountKind;
  status: AccountStatus;
  email: string;
  first_name: string;
  last_name: string;
  phone: string | null;
  organisation: string | null;
  sector: string | null;
  language: string;
  preferred_channel: Channel;
  /** Null until a plot is bound — signup and activation are separate steps. */
  subscriber_id: string | null;
  email_verified: boolean;
  created_at: string;
  last_login_at: string | null;
  /**
   * Chosen avatar, or null to use the one derived from `id`.
   *
   * The backend validates a chosen emoji against its own curated list and falls back to the
   * derived avatar for anything else — so this is safe to render as text.
   */
  avatar_emoji: string | null;
  avatar_color: string | null;
}

/**
 * Idle-session state, as reported by `GET /iam/session`.
 *
 * `seconds_remaining` is computed server-side deliberately. A browser counting down from
 * its own stored timestamp drifts, and is simply wrong after the machine sleeps — which is
 * the main case an idle timeout exists to catch.
 */
export interface SessionState {
  account_id: string;
  /** Idle seconds left before the session is refused. */
  seconds_remaining: number;
  /** The full window, so the UI never hardcodes "15 minutes". */
  idle_window_seconds: number;
  /** When to start warning. Server-owned so the two ends cannot disagree. */
  warning_at_seconds: number;
  /** False when the cache is unavailable and the window is not being enforced. */
  tracked: boolean;

  /** Identity, so the sidebar needs no second request. */
  full_name: string;
  email: string;
  avatar_emoji: string;
  avatar_color: string;

  /**
   * "Mobile · Android 14 · Chrome 141", parsed from THIS request's user-agent rather than
   * the one stored at login — so it describes the device actually in use.
   */
  device: string;

  /**
   * Approximate location, or the raw IP when the GeoLite2 database is not installed.
   * `location_source` distinguishes the two so the UI can attach the right caveat:
   * "From your IP address — approximate" vs "Your IP address".
   */
  location: string | null;
  location_source: "ip" | "ip_raw" | null;
}

// --------------------------------------------------------------------------- //
// Portal — audit, security, tenancy
// --------------------------------------------------------------------------- //

/** One immutable audit entry. Shape from `backend/app/iam/audit.py:build_event`. */
export interface AuditEntry {
  action: string;
  outcome: "success" | "failure" | "denied";
  /** "self" | "aggregator" | "operator" | "system" — who acted. */
  actor_kind: string;
  actor_id: string | null;
  target_id: string | null;
  detail: string | null;
  ip: string | null;
  user_agent: string | null;
  at: string;

  /**
   * Derived on read, not stored.
   *
   * The log keeps the raw `user_agent` because that is what makes it evidence; the label is
   * computed per request so an entry written before the parser learned a browser string
   * still reads correctly once it does.
   */
  agent?: {
    device: string;
    os: string;
    browser: string;
    summary: string;
    /** True for a script. Surfaced so an integration's call is not read as a human login. */
    is_bot: boolean;
  };

  /** Null when the GeoLite2 database is not installed — the UI falls back to the IP. */
  location?: {
    label: string;
    city: string | null;
    country: string | null;
    country_code: string | null;
    /** "city" | "country" | "none" — how firmly the UI may state it. */
    confidence: string;
  } | null;
}

/**
 * A keyset-paginated page of audit entries.
 *
 * **No `total`, deliberately.** `count_documents` on a growing collection is an unindexed
 * scan that gets slower exactly as the log becomes more valuable, so the API does not
 * offer one and the UI must not imply one.
 */
export interface AuditPage {
  entries: AuditEntry[];
  /** Opaque. Pass back as `?cursor=` for the next page. */
  next_cursor: string | null;
  has_more: boolean;
  page_size: number;
}

/**
 * Action counts over a window, from `GET /iam/audit/activity`.
 *
 * The shape is a LIST of `{action, outcome, count}` rather than a map — the same action can
 * appear more than once with different outcomes (a successful sign-in and a failed one are
 * separate rows), which a map keyed on action alone could not represent.
 */
export interface AuditSummary {
  window_days: number;
  by_action: { action: string; outcome: string; count: number }[];
}

export interface TotpState {
  enabled: boolean;
  /** Down to one, the user should regenerate before they lock themselves out. */
  recovery_codes_remaining: number;
  enrolled_at?: string | null;
}

export interface AggregatorMembership {
  aggregator_id: string;
  organisation: string | null;
  role: string | null;
  joined_at: string | null;
}

/**
 * One device this account has signed in from.
 *
 * Derived server-side from the audit log rather than a stored devices table, so the history is
 * correct for accounts that existed before the feature.
 */
export interface TrustedDevice {
  /** Whose sign-in this was. Present on every row because a shared handset means a device does not
   *  identify a person on its own. */
  email: string;
  /** Device and OS together, e.g. "Mobile · Android 14". */
  device: string;
  browser: string;
  user_agent: string | null;
  ip: string | null;
  /** Approximate, from an offline IP database. Falls back to the raw IP when unresolvable —
   *  "could not name this place" and "no address" are different states. */
  location: string | null;
  last_login: string | null;
  first_seen: string | null;
  sign_ins: number;
  /** True for the device reading the page, so one row can be marked "this device". */
  is_current: boolean;
}

export interface TrustedDeviceList {
  devices: TrustedDevice[];
  /** Rendered above the table verbatim, so policy and data cannot drift apart. */
  notice: string;
}

/** Response to starting an in-session password change. */
export interface PasswordChangeCodeSent {
  detail: string;
  expires_in_minutes: number;
  /** Masked, e.g. `a****@example.com` — enough to know which mailbox to open, not enough to
   *  harvest from a screenshot. */
  sent_to: string;
}

/**
 * One monitored area in a workspace, with the customer it belongs to and how they are reached.
 *
 * The wire shape of `GET /iam/workspaces/{id}/areas` — the chain
 * `Workspace > Customer > Area > Alerts` resolved server-side.
 *
 * **`customer_account_id: null` means the area carries no attribution**, not that it belongs to
 * nobody in particular. That is a broken link and it renders as one, deliberately: an area whose
 * attribution was written wrong is exactly what made this bug invisible, so hiding it behind a
 * fallback would make a broken link look valid.
 */
export interface WorkspaceAreaRow {
  aoi_id: string;
  name: string;
  hectares: number | null;
  crop: string | null;
  delivery_mode: DeliveryMode;

  customer_account_id: string | null;
  customer_name: string | null;
  customer_email: string | null;
  subscriber_id: string | null;
  /** The aggregator's own reference for this customer, when they supplied one. */
  external_ref: string | null;
  /** True when the plot is the aggregator's own rather than an onboarded customer's. */
  is_own_plot: boolean;

  /** Where alerts for THIS plot go — per-plot overrides already applied. */
  channels: ChannelBinding[];
  alert_count: number;
  last_alert_at: string | null;
  /** Null when no scan has completed yet — "queued", not "no risk". */
  latest_severity: string | null;
}

/**
 * A registered webhook endpoint, as the API returns it.
 *
 * **No `secret`.** It is returned once by `POST /webhook/subscriptions` and stored as a hash;
 * this shape is what every subsequent read gives you. `failure_streak` and `last_error` are the
 * fields that answer "why am I not receiving anything" — the engine auto-disables an endpoint
 * after `WEBHOOK_MAX_CONSECUTIVE_FAILURES`, and `active: false` with a `last_error` is what that
 * looks like.
 */
export interface WebhookPublic {
  id: string;
  name: string;
  url: string;
  events: string[];
  /** Null means every severity. */
  min_severity: Severity | null;
  /** Empty means every area this account owns. */
  aoi_ids: string[];
  active: boolean;
  failure_streak: number;
  last_error: string | null;
  /**
   * Which workspace this endpoint serves. **Null means every workspace this account owns.**
   *
   * Read back so the list can name it: without it an aggregator with two projects sees two
   * identically-described endpoints and cannot tell which programme each belongs to.
   */
  workspace_id: string | null;
}

export interface ApiKeyPublic {
  id: string;
  name: string;
  /** First characters only — the secret is shown once, at creation. */
  prefix: string;
  scopes: string[];
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  revoked: boolean;
}

/**
 * Human labels for audit actions.
 *
 * The API returns dotted machine tokens (`auth.session.idle_timeout`). Rendering those
 * raw would make the page unreadable for the person it is for — a farmer checking whether
 * someone else signed in. Unmapped actions fall back to the raw token rather than being
 * hidden, so a new backend action is visible-but-ugly instead of silently missing.
 */
export const ACTION_LABEL: Record<string, string> = {
  "account.created": "Account created",
  "account.verified": "Email address confirmed",
  "account.suspended": "Account suspended",
  "account.reactivated": "Account reactivated",
  "account.preferences.updated": "Preferences updated",
  "auth.login.succeeded": "Signed in",
  "auth.login.failed": "Failed sign-in attempt",
  "auth.login.locked": "Account temporarily locked",
  "auth.password.changed": "Password changed",
  "auth.logout": "Signed out",
  "auth.session.idle_timeout": "Signed out after inactivity",
  "auth.session.extended": "Session extended",
  "portal.dashboard.viewed": "Dashboard opened",
  "subscription.activated": "Monitoring activated",
  "subscription.area.added": "Area added",
  "subscription.channel.updated": "Delivery channel updated",
  "apikey.created": "API key created",
  "apikey.rotated": "API key rotated",
  "apikey.revoked": "API key revoked",
  "apikey.rejected": "API key rejected",
  "apikey.scope_denied": "API key denied a scope",
  "tenancy.membership.attached": "Added to an organisation",
  "tenancy.membership.detached": "Removed from an organisation",
  "tenancy.membership.revoked_by_subscriber": "You revoked an organisation's access",
  "aggregator.customer.onboarded": "Onboarded by an organisation",
  "aggregator.customer.read": "Your data was read by an organisation",
  "aggregator.customer.scan_triggered": "An organisation triggered a scan",
};

/** Who performed an action. "self" is never displayed — see the activity page. */
export const ACTOR_LABEL: Record<string, string> = {
  self: "you",
  aggregator: "an organisation serving you",
  operator: "a SHELTER operator",
  system: "SHELTER automatically",
};

/**
 * One place-search result. Mirrors `PlaceResult` in `app/api/routes/places.py`.
 *
 * `bbox` is `[west, south, east, north]` — NOT Nominatim's own `[south, north, west, east]`.
 * The backend converts it, because the two orders are a classic way to end up framing a
 * different continent.
 */
export interface PlaceResult {
  label: string;
  lat: number;
  lon: number;
  bbox: number[] | null;
  country: string | null;
  admin1: string | null;
  admin2: string | null;
  kind: string | null;

  /**
   * The feature's real outline as a closed `[[lon, lat], …]` ring — the market's perimeter, the
   * compound's walls, the LGA's boundary.
   *
   * **Null is normal, not an error.** Streets are lines and most Nigerian villages are single
   * nodes, so neither has an outline; `bbox` still frames them. Draw this when present: a
   * rectangle over someone's district proves nothing, whereas their own compound outlined on the
   * map is unambiguous confirmation that we found the right place.
   *
   * For display only. Submitting it as an AOI is legitimate only when
   * `monitoring.outline_is_monitorable` — a building footprint is below the measurement floor and
   * a state boundary is above the ceiling, and the write path refuses both.
   */
  ring: number[][] | null;
  /** True area of `ring` in hectares. Null when there is no ring. */
  ring_hectares: number | null;
  /** What the pipeline can actually do with `ring`. Present whenever a ring is. */
  monitoring: MonitoringNote | null;
}

/**
 * Whether a resolved outline can be monitored as-is, in words fit to display verbatim.
 *
 * Mirrors `MonitoringNote` in `app/api/routes/places.py`. **Do not compose your own sentence from
 * these fields** — `note` already says it correctly, and the reason matters: a building resolves to
 * a fraction of a hectare, well under the measurement floor, so the honest statement is "we located
 * it exactly, and we monitor the half-hectare around it". A frontend that rendered
 * `outline_is_monitorable: false` as "too small" would turn a precise answer into a refusal.
 *
 * No figures are repeated here on purpose. `tests/test_tracks.py` greps this file for numeric
 * cut-offs, because a TypeScript copy of a threshold is how the email and the portal come to
 * describe one plot differently — and it caught this comment, which is the guard working.
 */
export interface MonitoringNote {
  outline_is_monitorable: boolean;
  monitored_hectares: number;
  /** Render verbatim. */
  note: string;
  /** True when the monitored area is larger than what they outlined — a building, mostly. */
  enlarged: boolean;
}

/**
 * One type-ahead row from `GET /places/suggest`.
 *
 * Carries **no geometry**, deliberately: selecting one is expected to call `searchPlaces` with the
 * label, which is where the outline comes from. A partially typed string must not be able to
 * become a monitored area.
 */
export interface PlaceSuggestion {
  /** The name the eye lands on — "Argungu". */
  label: string;
  /** The context that disambiguates it — "Kebbi, Nigeria". Nigeria has several Kajolas. */
  detail: string;
  lat: number;
  lon: number;
  kind: string | null;
  country: string | null;
}

/** What `POST /places/preview` reports about a candidate area. */
export interface AoiPreview {
  monitorable: boolean;
  /** Written for the subscriber. Render it verbatim. */
  reason: string | null;
  bbox: BBox | null;
  /** The ring as it would be STORED — closed and counter-clockwise. */
  ring: number[][] | null;
  hectares: number | null;
  envelope_hectares: number | null;
  /** Above ~1.5, drawing the outline measurably changed the reading. */
  envelope_ratio: number | null;
  approx_pixels: number | null;
}

/**
 * A ready-to-submit area from `POST /places/resolve`.
 *
 * `area` goes verbatim into `POST /iam/activate`. Nothing in the UI computes a bounding box.
 */
export interface ResolvedArea {
  area: {
    name: string;
    bbox: BBox;
    geometry?: number[][] | null;
    country?: string | null;
    admin1?: string | null;
    admin2?: string | null;
    hectares?: number | null;
  };
  resolved_place: string | null;
  /** "about 7 football pitches" — the confirmation a person can actually perform. */
  size_description: string;
  hectares: number;
  /** True when the size was guessed. The UI invites a correction. */
  size_is_estimate: boolean;
  country: string | null;
  admin1: string | null;
  admin2: string | null;
  monitoring_cadence: string;
  attribution: string;
}

/**
 * What the signed-in member may do, from `GET /iam/me/access`.
 *
 * Fetched rather than derived: the role→permission table lives in
 * `backend/app/iam/roles.py` because that is also what `require_permission` enforces.
 * Reimplementing it in TypeScript is how a UI ends up showing a button the API refuses.
 *
 * `role` is **null for an individual** — there is no team to divide access among, and
 * returning a role would imply organisation sections might apply.
 */
export interface MyAccess {
  kind: AccountKind;
  role: string | null;
  role_label: string | null;
  permissions: string[];
  /** Scopes this member may put on a key they mint. Empty for an individual. */
  grantable_scopes: string[];
}

/**
 * One intelligence track, from `GET /iam/tracks`.
 *
 * `deliverable` is the field that matters: it is false when the risk model has no primary
 * hazard for the track, so activating it changes nothing. Fetched rather than hardcoded here
 * because it is derived from `OracleAgent._classify` — a copy in TypeScript would drift the
 * moment the risk model gains a hazard, and the drift would read as a working switch.
 */
export interface Track {
  value: string;
  label: string;
  summary: string;
  notes: string;
  deliverable: boolean;
  hazards: string[];
}

export interface Workspace {
  id: string;
  name: string;
  tracks: string[];
  created_at: string | null;
  is_default: boolean;
  /** Activated tracks that produce nothing yet. Labelled per row rather than globally. */
  undeliverable_tracks: string[];
}

/** One workspace and the role a colleague holds on it. */
export interface WorkspaceGrant {
  workspace_id: string;
  role: string;
  role_label?: string;
  permissions?: string[];
  status?: string;
}

export interface TeamMember {
  account_id: string;
  email: string | null;
  full_name: string | null;
  grants: WorkspaceGrant[];
}

/** One customer inside a workspace, from the portal's session-scoped route. */
export interface WorkspaceCustomer {
  account_id: string;
  email: string;
  full_name: string;
  external_ref: string | null;
  subscriber_id: string | null;
  areas: number;
}

export interface RoleOption {
  value: string;
  label: string;
  description: string;
  permissions: string[];
  scopes: string[];
}

export interface PendingInvitation {
  email: string;
  grants: WorkspaceGrant[];
  created_at: string | null;
  expires_at: string | null;
  /**
   * Whether the 14 days have elapsed. Computed server-side.
   *
   * Not derived in the browser: comparing `expires_at` against the visitor's clock would offer
   * "Resend" on a live invitation, or hide it on a lapsed one, whenever a laptop's time is
   * skewed.
   */
  expired: boolean;
}

export interface SessionToken {
  access_token: string;
  token_type: string;
  /** Seconds. Used to schedule a silent refresh before the session dies. */
  expires_in: number;
  account: Account;
}

/**
 * Returned by login when the password was correct but a second factor is required.
 *
 * A distinct shape rather than a 401, because the two mean different things: a 401 says
 * "those credentials are wrong", this says "they were right, now prove the second
 * factor". Discriminate on `mfa_required`.
 */
export interface LoginChallenge {
  mfa_required: true;
  /** Short-lived and single-purpose. NOT a session — it cannot read account data. */
  challenge_token: string;
  methods: string[];
  detail: string;
}

export interface SignupResponse {
  account: Account;
  session: SessionToken | null;
  /**
   * False when SMTP/Brevo is unavailable. Surfaced in the UI rather than swallowed:
   * an account whose confirmation email never arrived cannot activate, and the user
   * needs to know to use "resend" rather than assuming their inbox is slow.
   */
  verification_email_sent: boolean;
  next_step: string;
}

export interface IndividualSignupRequest {
  first_name: string;
  last_name: string;
  email: string;
  phone?: string | null;
  password: string;
  language?: string;
  preferred_channel?: Channel;
}

export interface CommercialSignupRequest {
  organisation: string;
  sector?: string;
  contact_first_name: string;
  contact_last_name: string;
  email: string;
  phone?: string | null;
  password: string;
}
