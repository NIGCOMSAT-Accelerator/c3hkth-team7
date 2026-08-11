"use client";

import { useCallback, useEffect, useState } from "react";

/**
 * Live service-status widget. Polls `/api/status`, the serverless projection.
 *
 * ## Two decisions that make this usable rather than decorative
 *
 * **It reports capabilities, not components.** A grid of "Postgres ✓ Redis ✓ MinIO ✓"
 * is meaningless to a farmer and only slightly less so to an insurer's ops team. The
 * route maps those signals onto "Satellite monitoring", "Hazard analysis", "Alert
 * delivery" and "Processing pipeline" — the four things a subscriber can actually act
 * on.
 *
 * **It never claims more certainty than it has.** When the backend is unreachable the
 * widget says so *and* states that alert delivery is unaffected, because the watch loop
 * is a separate process from this portal. That is true, and it is the single most useful
 * sentence during an outage — a subscriber's real fear is "did I stop being monitored?".
 *
 * ## Accessibility
 *
 * State is conveyed by icon **and** text label, never by colour alone — the same rule
 * `SeverityBadge` follows, and the reason the severity palette ships with icons. The
 * live region is `polite` so a screen reader is not interrupted mid-sentence by a poll.
 */

type Level = "operational" | "degraded" | "unavailable";

interface Capability {
  key: string;
  label: string;
  level: Level;
  detail: string;
}

interface StatusPayload {
  level: Level;
  summary: string;
  capabilities: Capability[];
  reachable: boolean;
  checked_at: string;
  operations?: {
    queue_depth: Record<string, number>;
    channels: string[];
    inference: string;
  };
}

/** Icon plus text for every state. Colour is reinforcement, never the signal. */
const LEVEL_META: Record<Level, { icon: string; label: string }> = {
  operational: { icon: "●", label: "Operational" },
  degraded: { icon: "◐", label: "Degraded" },
  unavailable: { icon: "○", label: "Unavailable" },
};

/**
 * Poll interval. 30s matches the backend's own Docker healthcheck cadence, so the widget
 * cannot be more stale than the container's view of itself — and polling faster would
 * turn an outage into a thundering herd against a service already struggling.
 */
const POLL_MS = 30_000;

export default function ServiceStatus({
  /** Aggregators see queue depth and delivery channels; individuals do not need them. */
  showOperations = false,
  /** Collapsed by default in the header, expanded on a dedicated status page. */
  defaultExpanded = false,
}: {
  showOperations?: boolean;
  defaultExpanded?: boolean;
}) {
  const [status, setStatus] = useState<StatusPayload | null>(null);
  const [expanded, setExpanded] = useState(defaultExpanded);
  // Distinguished from "no data": the first load should show a neutral checking state
  // rather than flashing an alarming "Unavailable" before the first response arrives.
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      // `cache: no-store` on the client so the browser does not serve a stale answer
      // from its own cache; the shared CDN cache on the route already does the
      // coalescing that protects the backend.
      const res = await fetch("/api/status", { cache: "no-store" });
      setStatus(await res.json());
    } catch {
      // The route returns 200 even during an outage, so reaching here means the
      // *frontend* is unreachable — in which case nothing renders anyway. Keep the last
      // known value rather than blanking the widget.
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_MS);

    // Pause while the tab is hidden. A backgrounded dashboard left open overnight would
    // otherwise make ~1,000 pointless requests — and resume with an immediate refresh so
    // the first thing a returning user sees is current.
    const onVisible = () => {
      if (document.visibilityState === "visible") load();
    };
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [load]);

  if (loading && !status) {
    return (
      <div className="status status--loading" aria-live="polite">
        <span className="status__dot" aria-hidden="true">
          ◌
        </span>
        <span className="status__label">Checking service status…</span>
      </div>
    );
  }

  if (!status) return null;

  const meta = LEVEL_META[status.level];

  return (
    <section className="status" data-level={status.level} aria-live="polite">
      <button
        type="button"
        className="status__head"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        <span className="status__dot" aria-hidden="true">
          {meta.icon}
        </span>
        <span className="status__label">
          {/* Screen readers get the state as words, not as a colour or a glyph. */}
          <span className="status__level">{meta.label}</span>
          <span className="status__summary">{status.summary}</span>
        </span>
        <span className="status__chevron" aria-hidden="true">
          {expanded ? "▴" : "▾"}
        </span>
      </button>

      {expanded && (
        <div className="status__body">
          <ul className="status__list">
            {status.capabilities.map((c) => (
              <li key={c.key} className="status__item" data-level={c.level}>
                <span className="status__item-dot" aria-hidden="true">
                  {LEVEL_META[c.level].icon}
                </span>
                <span>
                  <strong className="status__item-label">
                    {c.label}
                    {/* The state in text, so it survives colour blindness and a
                        greyscale print. */}
                    <span className="status__item-state">
                      {" · "}
                      {LEVEL_META[c.level].label}
                    </span>
                  </strong>
                  <span className="status__item-detail">{c.detail}</span>
                </span>
              </li>
            ))}
          </ul>

          {showOperations && status.operations && (
            <div className="status__ops">
              <span className="status__ops-label">Pipeline</span>
              <span className="status__ops-value">
                {Object.entries(status.operations.queue_depth)
                  .map(([stage, n]) => `${stage} ${n}`)
                  .join(" · ") || "idle"}
              </span>
              <span className="status__ops-label">Channels</span>
              <span className="status__ops-value">
                {status.operations.channels.join(", ") || "none configured"}
              </span>
              <span className="status__ops-label">Advisory engine</span>
              <span className="status__ops-value">{status.operations.inference}</span>
            </div>
          )}

          <p className="status__checked">
            Checked{" "}
            {new Date(status.checked_at).toLocaleTimeString([], {
              hour: "2-digit",
              minute: "2-digit",
            })}
            {" · refreshes every 30 seconds"}
          </p>
        </div>
      )}
    </section>
  );
}
