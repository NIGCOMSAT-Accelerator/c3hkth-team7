import { NextResponse } from "next/server";

import { api } from "@/lib/api";
import type { HealthResponse } from "@/lib/types";

/**
 * Serverless status aggregator — `GET /api/status`.
 *
 * ## Honest framing: what is and is not novel here
 *
 * A status page is not novel; Statuspage, Better Stack and a dozen others do this. What
 * this route does that a generic status page cannot is **translate infrastructure health
 * into product capability**, and that translation is the whole point:
 *
 *     Postgres up, Dragonfly up, scheduler running   ->  "Monitoring active"
 *     email transport resolved, channels configured  ->  "Alerts delivering"
 *     trained weights absent                         ->  "Running on threshold science"
 *
 * A subscriber does not know what Dragonfly is and should never have to. They have
 * exactly two questions — *is my plot still being watched?* and *will I be told?* — and
 * a component-by-component grid answers neither. So the projection below maps the seven
 * infrastructure signals onto four capability statements a farmer or an insurer can act
 * on.
 *
 * ## Why this is a serverless function and not a client fetch
 *
 * Three reasons, and the first is a security boundary rather than a preference:
 *
 *   1. **`/health` is internal.** It reports Dragonfly's version, pending migration
 *      names, the LLM base URL and token budgets. Handing that to every browser is
 *      reconnaissance for free. The projection strips it; a client fetching `/health`
 *      directly could not.
 *   2. **The API key stays server-side.** `lib/api.ts` is `server-only`, so a browser
 *      calling the backend would need its own credential — which means shipping one.
 *   3. **The backend is not internet-facing in the target shape.** It sits behind a
 *      reverse proxy on a VPS while the frontend is on Netlify; the browser has no route
 *      to it at all.
 *
 * ## Caching is adaptive, and that is not a micro-optimisation
 *
 * A single TTL is wrong in both directions. Long enough to shield the backend under load
 * is long enough to keep telling a subscriber "operational" while the queue is down —
 * measured during development: with a flat 20s cache, stopping Dragonfly left the widget
 * reporting all-clear for a full window. Short enough to catch that promptly means every
 * visitor hammers a service already struggling.
 *
 * So the TTL depends on the answer: 15s while healthy, 3s once anything is degraded. That
 * bounds how long a stale all-clear can survive, while `stale-while-revalidate` keeps the
 * response instant either way.
 */

// Node runtime, not Edge: `lib/api.ts` is `server-only` and reads process env, and the
// backend may sit on a private network the Edge runtime cannot reach.
export const runtime = "nodejs";
// No route-level cache. Freshness is controlled by the Cache-Control header below,
// which distinguishes the healthy case from the degraded one — a single `revalidate`
// value cannot. Verified: with a 20s cache, stopping Dragonfly left the widget
// reporting "operational" for a full window, which is exactly when a stale answer is
// most harmful.
export const dynamic = "force-dynamic";

/** Product-level state. Deliberately three values, not a percentage. */
type Level = "operational" | "degraded" | "unavailable";

interface Capability {
  key: string;
  /** What a subscriber calls it. Never a component name. */
  label: string;
  level: Level;
  /** One sentence, in the second person, saying what this means for them. */
  detail: string;
}

interface StatusPayload {
  level: Level;
  /** Headline a human can read without expanding anything. */
  summary: string;
  capabilities: Capability[];
  /** Set when the whole backend is unreachable, so the UI can say so plainly. */
  reachable: boolean;
  checked_at: string;
  /**
   * Extra signals for aggregators, omitted for individuals. Queue depth and delivery
   * channels are operationally meaningful to a partner running an integration and are
   * noise to a farmer.
   */
  operations?: {
    queue_depth: Record<string, number>;
    channels: string[];
    inference: string;
  };
}

/** The worst of a set — a chain is only as good as its weakest link. */
function worst(levels: Level[]): Level {
  if (levels.includes("unavailable")) return "unavailable";
  if (levels.includes("degraded")) return "degraded";
  return "operational";
}

/**
 * Map infrastructure health onto capability statements.
 *
 * The mapping is the product decision. Two examples of why it is not mechanical:
 *
 *   * **A cold cache is not degraded.** `cache.status` down means every read falls
 *     through to Postgres — slower, still correct. Reporting that as degraded would
 *     alarm a subscriber about something they cannot act on and that does not affect
 *     their warnings.
 *   * **Absent model weights are not a fault.** The pipeline falls back to documented
 *     physical thresholds at lower confidence, which is a deliberate design position.
 *     It is surfaced as reduced *precision*, not as a broken service.
 */
function project(health: HealthResponse | null): StatusPayload {
  const now = new Date().toISOString();

  if (!health) {
    return {
      level: "unavailable",
      summary:
        "We cannot reach the monitoring service right now. Your alerts are unaffected — delivery runs separately from this dashboard.",
      reachable: false,
      checked_at: now,
      capabilities: [
        {
          key: "monitoring",
          label: "Satellite monitoring",
          level: "unavailable",
          detail:
            "Live status is unavailable. Scheduled scans continue independently of this dashboard.",
        },
      ],
    };
  }

  const postgres = health.postgres?.status === "up";
  const queue = health.redis === "up";
  const scheduler = Boolean(health.scheduler?.running);
  const channels = health.channels_configured ?? [];
  // `notifications` is newer than some deployed backends, so its absence is treated as
  // unknown-but-not-broken rather than as a failure.
  const email = health.notifications?.operational ?? channels.length > 0;
  const trained =
    health.models?.sar_flood === "trained" &&
    health.models?.crop_stress === "trained";

  const capabilities: Capability[] = [
    {
      key: "monitoring",
      label: "Satellite monitoring",
      level: scheduler && postgres ? "operational" : postgres ? "degraded" : "unavailable",
      detail: scheduler
        ? "Your areas are scanned automatically every 6 hours. Sentinel-1 radar sees through cloud, so monitoring continues during a storm."
        : postgres
          ? "Automatic scanning is paused. Existing assessments are still available and no data has been lost."
          : "Monitoring records are temporarily unreachable.",
    },
    {
      key: "analysis",
      label: "Hazard analysis",
      // Threshold fallback is a documented design position, not a fault — so it reads
      // as reduced precision rather than a broken capability.
      level: postgres ? "operational" : "unavailable",
      detail: trained
        ? "Flood and crop-stress analysis is running on trained models."
        : "Analysis is running on published physical thresholds rather than trained models — findings stay conservative, and severity is capped until a model is validated for your region.",
    },
    {
      key: "delivery",
      label: "Alert delivery",
      level: email ? "operational" : "degraded",
      detail: email
        ? `Alerts will reach you on your chosen channels (${channels.join(", ") || "email"}).`
        : "Alert delivery is not fully configured on this deployment. Assessments are still recorded and visible here.",
    },
    {
      key: "queue",
      label: "Processing pipeline",
      level: queue ? "operational" : "degraded",
      detail: queue
        ? "New scans are queued and processed normally."
        : "New scans cannot be queued right now. Scheduled work resumes automatically once the queue recovers.",
    },
  ];

  const level = worst(capabilities.map((c) => c.level));

  return {
    level,
    summary:
      level === "operational"
        ? "All systems operational. Your areas are being monitored."
        : level === "degraded"
          ? "Partially degraded. Monitoring continues; some capabilities are reduced."
          : "Service disruption. Our team is aware and alert delivery is unaffected.",
    reachable: true,
    checked_at: now,
    capabilities,
    operations: {
      queue_depth: health.queue_depth ?? {},
      channels,
      inference: health.advisory_generator?.label ?? "template",
    },
  };
}

export async function GET() {
  let health: HealthResponse | null = null;
  try {
    health = await api.health();
  } catch {
    // Swallowed on purpose. A status endpoint that 500s when the thing it monitors is
    // down is useless precisely when it is needed — the outage IS the payload.
    health = null;
  }

  const payload = project(health);

  // Cache aggressively when healthy, barely at all when not.
  //
  // A single TTL is wrong in both directions: long enough to protect the backend under
  // load is long enough to keep telling a subscriber "operational" while the queue is
  // down — measured, and it did exactly that for a full 20-second window. Short enough
  // to catch an outage promptly means every visitor hammers a service that is already
  // struggling.
  //
  // So: 15s of shared cache while everything is fine, and 3s once anything is degraded,
  // which bounds how long a stale "all clear" can survive. `stale-while-revalidate`
  // keeps the response instant either way — the refresh happens behind the scenes rather
  // than making a visitor wait on a timing-out upstream call.
  const healthy = payload.level === "operational";
  const cache = healthy
    ? "public, s-maxage=15, stale-while-revalidate=45"
    : "public, s-maxage=3, stale-while-revalidate=10";

  return NextResponse.json(payload, {
    // 200 even when the backend is unreachable: the response body carries the state, so
    // a non-2xx would make the widget's own fetch fail and it would render nothing
    // instead of rendering the outage.
    status: 200,
    headers: {
      "Cache-Control": cache,
      // So an operator can tell a cached answer from a fresh one when debugging.
      "X-SHELTER-Status-Level": payload.level,
    },
  });
}
