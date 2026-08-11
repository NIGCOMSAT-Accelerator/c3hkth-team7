import AlertCard from "@/components/AlertCard";
import RiskTimeline from "@/components/RiskTimeline";

import IntelligenceTracks from "@/components/IntelligenceTracks";
import { advisoryByline } from "@/lib/intelligence";
import ServiceStatus from "@/components/ServiceStatus";
import SeverityBadge from "@/components/SeverityBadge";
import { safeApi } from "@/lib/api";
import { recordPortalEvent } from "@/app/auth/session-actions";
import SessionGuard from "@/components/SessionGuard";
import { requireVerifiedAccount } from "@/lib/session";
import {
  CHANNEL_LABEL,
  HAZARD_LABEL,
  SEVERITY_META,
  SOURCE_LABEL,
  type Alert,
} from "@/lib/types";

export const dynamic = "force-dynamic";

export const metadata = { title: "Dashboard" };

/**
 * Gated. Redirects to sign-in when there is no session.
 *
 * Checked server-side before any data is fetched, so an unauthenticated visitor never
 * receives assessment or subscriber data in the HTML — a client-side guard would render
 * the page first and hide it afterwards, which means the data was already sent.
 */
export default async function DashboardPage() {
  // Gate BEFORE fetching, and on verification as well as session. A client-side guard
  // would render the page and hide it afterwards — by which point the assessment and
  // subscriber data has already been sent to the browser.
  //
  // `requireVerifiedAccount` handles all three failure cases: no session -> sign-in,
  // suspended -> sign-in, unverified -> /auth/pending. It redirects rather than
  // returning, so nothing below runs for an ineligible visitor.
  const account = await requireVerifiedAccount("/dashboard");

  // Audit the view. Awaited but non-throwing: `recordPortalEvent` swallows failures, so a
  // log write can never stop a subscriber seeing their own hazard data. "Who looked at
  // this, and when" is the question an incident review actually asks, and it cannot be
  // reconstructed afterwards from anything else.
  await recordPortalEvent("dashboard.viewed", `account ${account.id}`);

  // **Scoped to this account's own subscriber.** This page previously called
  // `listAlerts(20)` with no subscriber id, which is the platform-wide feed — so any
  // verified account, individual or aggregator, saw every other subscriber's advisories,
  // plot names and delivery receipts. Reported from a fresh aggregator account that landed
  // here on sign-in (`/dashboard` is the post-login destination) and found a stranger's
  // alerts waiting.
  //
  // The backend now refuses an unscoped read without `platform:read`, but the frontend
  // holds exactly that key on every call — so the server-side guard alone would not have
  // fixed this page. Both halves are needed: the API stops anonymous reads, and this passes
  // the id so a platform credential is not used to widen a subscriber's view.
  //
  // An aggregator sees their customers' alerts through `/portal/workspace`, which resolves
  // them through the membership edge. Not here: this page has one account's context, and
  // guessing which customers to show from a page with no workspace selected is how the
  // wrong tenant's data ends up on screen in the first place.
  const [health, alerts] = await Promise.all([
    safeApi.health(),
    account.subscriber_id
      ? safeApi.listAlerts(20, account.subscriber_id)
      : Promise.resolve([]),
  ]);

  const latest = alerts[0];
  const highest = alerts.reduce<Alert | undefined>((best, a) => {
    if (!best) return a;
    return SEVERITY_META[a.assessment.severity].rank >
      SEVERITY_META[best.assessment.severity].rank
      ? a
      : best;
  }, undefined);

  const peopleCovered = alerts.reduce(
    (sum, a) => sum + a.assessment.exposure.population,
    0,
  );
  const delivered = alerts.reduce(
    (sum, a) => sum + a.receipts.filter((r) => r.status === "sent").length,
    0,
  );

  return (
    <div className="shell" style={{ padding: "44px 24px 0" }}>
      {/* Idle-session management. Renders nothing until the countdown warning is due. */}
      <SessionGuard />

      <header style={{ marginBottom: 26 }}>
        <h1 style={{ fontSize: 30, marginBottom: 6 }}>Operations dashboard</h1>
        <p className="muted" style={{ margin: 0, fontSize: 14 }}>
          Live view of the watch loop. Assessments refresh on every Sentinel
          pass; alerts appear here whether or not they were dispatched.
        </p>
      </header>

      {/*
        Live status, replacing the previous static notice.

        The static version only appeared when the backend was *entirely* unreachable, so
        the common real case — the queue down while reads still work, or email not
        configured — rendered as a fully healthy page. This polls a serverless projection
        and reports capability-level state continuously, which is the difference between
        a banner and actual observability.

        It also answers the question a subscriber actually has during an outage: not
        "which component failed" but "am I still being watched, and will I be told".
      */}
      <div style={{ marginBottom: 24 }}>
        <ServiceStatus defaultExpanded={!health} />
      </div>

      {/* Stat tiles — hero numbers, no plot, so no hover layer. */}
      <section className="grid grid--tiles" style={{ marginBottom: 28 }}>
        <div className="tile">
          <p className="tile__label">Highest active severity</p>
          <div className="tile__value">
            {highest ? (
              <SeverityBadge severity={highest.assessment.severity} />
            ) : (
              <span className="muted" style={{ fontSize: 18 }}>
                None
              </span>
            )}
          </div>
          <p className="tile__note">
            {highest ? highest.assessment.aoi_name : "No alerts in the feed yet"}
          </p>
        </div>

        <div className="tile">
          <p className="tile__label">Alerts in feed</p>
          <div className="tile__value">{alerts.length}</div>
          <p className="tile__note">Most recent {alerts.length} dispatched</p>
        </div>

        <div className="tile">
          <p className="tile__label">People in footprints</p>
          <div className="tile__value">{peopleCovered.toLocaleString()}</div>
          <p className="tile__note">Summed across alerted areas</p>
        </div>

        <div className="tile">
          <p className="tile__label">Messages delivered</p>
          <div className="tile__value">{delivered}</div>
          <p className="tile__note">
            {health
              ? `${health.channels_configured.length} channel${health.channels_configured.length === 1 ? "" : "s"} configured`
              : "Channel status unknown"}
          </p>
        </div>
      </section>

      <div className="grid grid--split" style={{ marginBottom: 28 }}>
        <section className="card">
          {latest ? (
            <>
              <div
                style={{
                  display: "flex",
                  alignItems: "flex-start",
                  justifyContent: "space-between",
                  gap: 14,
                  marginBottom: 4,
                }}
              >
                <div>
                  <h2 className="card__title">
                    {latest.assessment.aoi_name} — 7-day outlook
                  </h2>
                  <p className="card__sub">
                    {HAZARD_LABEL[latest.assessment.hazard]} · assessed{" "}
                    {formatWhen(latest.assessment.assessed_at)} · confidence{" "}
                    {(latest.assessment.confidence * 100).toFixed(0)}%
                  </p>
                </div>
                <SeverityBadge severity={latest.assessment.severity} />
              </div>

              <RiskTimeline
                forecast={latest.assessment.forecast}
                rainfallAvailable={latest.assessment.forecast.some(
                  (p) => p.rainfall_mm > 0,
                )}
              />
            </>
          ) : (
            <>
              <h2 className="card__title">7-day outlook</h2>
              <p className="card__sub">
                No assessment yet. Register an area on the{" "}
                <a href="/subscribe">activation page</a> and the watch loop will
                scan it immediately.
              </p>
            </>
          )}
        </section>

        <section className="card">
          <h2 className="card__title">Why this was sent</h2>
          <p className="card__sub">
            The evidence the advisory was allowed to cite
          </p>
          {latest ? (
            <>
              <ul className="evidence">
                {latest.assessment.evidence.map((e, i) => (
                  <li key={i}>{e}</li>
                ))}
              </ul>

              {latest.assessment.cascade.length > 0 && (
                <>
                  <p
                    style={{
                      margin: "18px 0 8px",
                      fontSize: 13,
                      fontWeight: 650,
                    }}
                  >
                    Expected to trigger next
                  </p>
                  <ul className="evidence">
                    {latest.assessment.cascade.map((h) => (
                      <li key={h}>{HAZARD_LABEL[h]}</li>
                    ))}
                  </ul>
                </>
              )}

              {latest.assessment.data_sources.length > 0 && (
                <>
                  <p
                    style={{ margin: "18px 0 8px", fontSize: 13, fontWeight: 650 }}
                  >
                    Data sources
                  </p>
                  <div
                    style={{ display: "flex", flexWrap: "wrap", gap: 6 }}
                  >
                    {latest.assessment.data_sources.map((s) => (
                      <span
                        key={s}
                        style={{
                          fontSize: 11,
                          padding: "3px 8px",
                          borderRadius: 999,
                          border: "1px solid var(--hairline-strong)",
                          color: "var(--text-muted)",
                        }}
                      >
                        {SOURCE_LABEL[s] ?? s}
                      </span>
                    ))}
                  </div>
                  <p
                    className="muted"
                    style={{ fontSize: 11, margin: "10px 0 0" }}
                  >
                    Sources absent from this list did not answer — the
                    assessment treats them as unknown rather than as zero.
                  </p>
                </>
              )}
            </>
          ) : (
            <p className="muted" style={{ fontSize: 14, margin: 0 }}>
              Nothing assessed yet.
            </p>
          )}
        </section>
      </div>

      <section className="card" style={{ marginBottom: 48 }}>
        <h2 className="card__title">Recent alerts</h2>
        <p className="card__sub">Newest first</p>

        {alerts.length === 0 ? (
          <p className="muted" style={{ fontSize: 14, margin: 0 }}>
            No alerts dispatched yet.
          </p>
        ) : (
          <div style={{ display: "grid", gap: 14 }}>
            {alerts.map((alert, index) => (
              <AlertCard
                key={alert.id}
                alert={alert}
                // Newest-first, so the leading alert is the one an operator came to read.
                defaultOpen={index === 0}
                when={formatWhen(alert.created_at)}
              >
                {/*
                  The `<article>` wrapper and the severity/headline/time header are gone: AlertCard
                  supplies both, and the collapsed row already carries all three. Repeating them
                  here would make every opened card begin by restating what was just clicked.
                */}
                <p
                  style={{
                    margin: "0 0 12px",
                    fontSize: 14,
                    color: "var(--text-secondary)",
                  }}
                >
                  {alert.advisory.body}
                </p>

                {alert.advisory.actions.length > 0 && (
                  <ul className="evidence" style={{ marginBottom: 12 }}>
                    {alert.advisory.actions.map((a, i) => (
                      <li key={i}>{a}</li>
                    ))}
                  </ul>
                )}

                <div
                  style={{
                    display: "flex",
                    gap: 8,
                    flexWrap: "wrap",
                    fontSize: 12,
                  }}
                >
                  {alert.receipts.map((r, i) => (
                    <span
                      key={i}
                      title={r.error ?? undefined}
                      style={{
                        padding: "3px 9px",
                        borderRadius: 999,
                        border: "1px solid var(--hairline-strong)",
                        color:
                          r.status === "sent"
                            ? "var(--viz-rain)"
                            : r.status === "failed"
                              ? "var(--sev-emergency)"
                              : "var(--text-muted)",
                      }}
                    >
                      {CHANNEL_LABEL[r.channel] ?? r.channel} · {r.status}
                    </span>
                  ))}
                  {/*
                    The capability and the provenance, never the model name.
                    
                    This rendered `generated_by` directly, so every alert published our LLM vendor
                    and version — a supplier relationship a subscriber cannot act on and a
                    competitor can. "Herald-AI" and "System ML" describe what the reader is
                    actually getting; the exact model stays on /health for operators.
                    
                    The data sources are added because provenance is the argument: an advisory that
                    names Sentinel-1 is measured, and for Copernicus and OpenStreetMap the credit is
                    a licence obligation rather than a courtesy.
                  */}
                  <span className="muted" style={{ padding: "3px 0" }}>
                    {(() => {
                      const { author, sources } = advisoryByline(
                        alert.advisory.generated_by,
                        alert.assessment.data_sources,
                      );
                      return sources
                        ? `advisory by ${author} · from ${sources}`
                        : `advisory by ${author}`;
                    })()}
                  </span>
                </div>
              </AlertCard>
            ))}
          </div>
        )}
      </section>

      {/*
        Roadmap at the foot, compact. A subscriber came here for their own alerts, so
        this sits below them — but seeing what is next is how a cooperative decides
        whether to onboard now. Same component as the landing page, so the roadmap cannot
        say one thing to a prospect and another to a subscriber.
      */}
      <section className="shell" style={{ paddingBottom: 72 }}>
        <h2
          style={{
            fontSize: 13,
            letterSpacing: "0.12em",
            textTransform: "uppercase",
            color: "var(--text-muted)",
            marginBottom: 8,
          }}
        >
          Intelligence tracks
        </h2>
        <p className="muted" style={{ fontSize: 14, margin: "0 0 20px", maxWidth: "70ch" }}>
          Your account is on Agricultural Intelligence. Environmental and Public Health
          Intelligence run on the same satellite pipeline and arrive in the next phase.
        </p>
        <IntelligenceTracks compact />
      </section>

    </div>
  );
}

function formatWhen(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-GB", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}
