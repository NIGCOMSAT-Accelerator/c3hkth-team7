import SeverityBadge from "@/components/SeverityBadge";
import { safeApi } from "@/lib/api";
import { recordPortalEvent } from "@/app/auth/session-actions";
import { getAccount } from "@/lib/session";
import { CHANNEL_LABEL, HAZARD_LABEL, SEVERITY_META, type Alert } from "@/lib/types";

export const metadata = { title: "Portal" };
export const dynamic = "force-dynamic";

/**
 * Portal overview — the subscriber's own state, in one screen.
 *
 * ## Scoped by `subscriber_id`, and what it means when that is null
 *
 * `account.subscriber_id` is null until a plot is bound (signup and activation are separate
 * steps by design — an account is useful before someone has chosen their field). So the
 * empty state here is not an error, it is the normal state of a fresh account, and it says
 * what to do about it.
 *
 * Alerts are fetched with the subscriber id so this page never shows another subscriber's
 * advisories. The gate is in `portal/layout.tsx`; this is the data scoping.
 */
export default async function PortalOverviewPage({
  searchParams,
}: {
  searchParams: Promise<{ denied?: string }>;
}) {
  // `?denied=` is set by `requirePermission` when a member reaches a section their role does
  // not allow. Explained rather than dropped: landing silently on the overview reads as a
  // broken link, and the actionable information is WHICH permission is missing and who can
  // grant it.
  const { denied } = await searchParams;
  // The layout has already gated, so a null account here is impossible in practice —
  // but the type is nullable and a non-null assertion would be a lie. Handled below.
  const account = await getAccount();

  // Audited. Non-throwing by design — `recordPortalEvent` swallows failures, so a log
  // write can never stop a subscriber seeing their own data. "Who opened this, and when"
  // is the question an incident review asks and cannot reconstruct afterwards.
  await recordPortalEvent("portal.viewed", "portal overview");

  const alerts = account?.subscriber_id
    ? await safeApi.listAlerts(10, account.subscriber_id)
    : [];

  const highest = alerts.reduce<Alert | undefined>((best, a) => {
    if (!best) return a;
    return SEVERITY_META[a.assessment.severity].rank >
      SEVERITY_META[best.assessment.severity].rank
      ? a
      : best;
  }, undefined);

  const latest = alerts[0];
  const delivered = alerts.reduce(
    (n, a) => n + a.receipts.filter((r) => r.status === "sent").length,
    0,
  );
  const failed = alerts.reduce(
    (n, a) => n + a.receipts.filter((r) => r.status === "failed").length,
    0,
  );

  return (
    <>
      {denied && (
        <div className="authform__message" data-tone="error" role="status">
          Your role does not include <strong>{denied.replace(":", " · ")}</strong>, so that
          section is not available to you. An Organization Owner can change your role or grant
          the permission from <strong>Team</strong>.
        </div>
      )}

      <header className="pcard__head">
        <h1 className="portal__title">
          {account?.first_name ? `Hello, ${account.first_name}` : "Your portal"}
        </h1>
        <p className="portal__lede">
          {account?.subscriber_id
            ? "Your areas are watched on every satellite pass. You are contacted only when something needs action."
            : "Your account is ready. One more step to start monitoring."}
        </p>
      </header>

      {/* Not activated yet — the single most useful thing this page can say. */}
      {!account?.subscriber_id && (
        <section className="pcard pcard--prompt">
          <h2 className="pcard__title">Choose the area to watch</h2>
          <p className="pcard__sub">
            Nothing is being monitored yet. Tell us where your plot is and the watch loop
            scans it on the next pass — usually within a few hours.
          </p>
          <a href="/subscribe" className="btn btn--primary">
            Set up monitoring
          </a>
        </section>
      )}

      {account?.subscriber_id && (
        <>
          <section className="grid grid--tiles portal__tiles">
            <div className="tile">
              <p className="tile__label">Current highest risk</p>
              <div className="tile__value">
                {highest ? (
                  <SeverityBadge severity={highest.assessment.severity} />
                ) : (
                  <span className="muted" style={{ fontSize: 17 }}>
                    Nothing active
                  </span>
                )}
              </div>
              <p className="tile__note">
                {highest
                  ? highest.assessment.aoi_name
                  : "No advisory has met the alert threshold"}
              </p>
            </div>

            <div className="tile">
              <p className="tile__label">Alerts received</p>
              <div className="tile__value">{alerts.length}</div>
              <p className="tile__note">
                <a href="/portal/alerts">See all</a>
              </p>
            </div>

            <div className="tile">
              <p className="tile__label">Messages delivered</p>
              <div className="tile__value">{delivered}</div>
              {/*
                Failures are surfaced, not hidden. A subscriber whose WhatsApp number is
                wrong needs to know their warnings are not arriving — that is the whole
                product failing quietly, and it is fixable in Settings.
              */}
              <p className="tile__note">
                {failed > 0 ? (
                  <span className="pcard__warn">
                    {failed} failed — <a href="/portal/settings">check your channels</a>
                  </span>
                ) : (
                  "All delivered"
                )}
              </p>
            </div>

            <div className="tile">
              <p className="tile__label">Monitoring</p>
              <div className="tile__value" style={{ fontSize: 17 }}>
                Active
              </div>
              <p className="tile__note">Every Sentinel pass, around the clock</p>
            </div>
          </section>

          <section className="pcard">
            <div className="pcard__head">
              <h2 className="pcard__title">Most recent advisory</h2>
              {latest && <SeverityBadge severity={latest.assessment.severity} />}
            </div>

            {latest ? (
              <>
                <p className="pcard__sub">
                  {HAZARD_LABEL[latest.assessment.hazard]} ·{" "}
                  {latest.assessment.aoi_name} · confidence{" "}
                  {(latest.assessment.confidence * 100).toFixed(0)}%
                </p>
                <p className="pcard__headline">{latest.advisory.headline}</p>
                <p className="pcard__body">{latest.advisory.body}</p>

                {latest.advisory.actions.length > 0 && (
                  <>
                    <p className="pcard__minihead">What to do</p>
                    <ul className="evidence">
                      {latest.advisory.actions.map((a, i) => (
                        <li key={i}>{a}</li>
                      ))}
                    </ul>
                  </>
                )}

                <div className="pcard__chips">
                  {latest.receipts.map((r, i) => (
                    <span
                      key={i}
                      className={`chip${r.status === "failed" ? " chip--bad" : ""}`}
                    >
                      {CHANNEL_LABEL[r.channel] ?? r.channel} · {r.status}
                    </span>
                  ))}
                </div>

                <p className="pcard__foot">
                  <a href="/portal/alerts">All alerts</a> ·{" "}
                  <a href="/dashboard">Live map</a>
                </p>
              </>
            ) : (
              <p className="muted" style={{ margin: 0, fontSize: 14 }}>
                Nothing yet. Silence means no hazard crossed the alert threshold for your
                area — assessments still run on every pass, and you can see them on the{" "}
                <a href="/dashboard">live map</a>.
              </p>
            )}
          </section>
        </>
      )}

      {/*
        No quick-link tiles for Settings / Security / Activity.

        They restated the side navigation one screen below it, which is duplicated effort for the
        reader as much as for us: a second route to the same three pages adds a decision without
        adding a destination. The overview's job is this subscriber's own state — risk, alerts,
        delivery — and navigation is the nav's job on every viewport, including the mobile strip.
      */}
    </>
  );
}
