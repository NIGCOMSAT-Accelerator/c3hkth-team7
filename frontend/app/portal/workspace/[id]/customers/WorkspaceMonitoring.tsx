import { CHANNEL_LABEL, type WorkspaceAreaRow } from "@/lib/types";

/**
 * Every monitored area in a workspace, with the customer it belongs to and where its alerts go.
 *
 * ## The gap this fills
 *
 * `CustomerManager` above shows customers and, per customer, their plots. That answers "what is this
 * farmer's monitoring?" and cannot answer "what is this **workspace** monitoring?" — because it
 * iterates customers, so a plot belonging to the aggregator itself, or one whose attribution was
 * written wrong, appears nowhere at all.
 *
 * That was the reported bug. A test workspace held two active monitoring areas and displayed zero
 * customers, because both areas had been recorded as `owner_kind=individual, workspace_id=null` by
 * the activation path. The data was wrong *and* no view existed that would have shown it.
 *
 * So this renders the server-resolved chain — `Workspace > Customer > Area > Alert delivery` — from
 * one call, including the aggregator's own plots and including unattributed ones.
 *
 * ## An unattributed area is shown as broken, not hidden
 *
 * `customer_account_id === null` renders in the error colour with an explicit label. Hiding it, or
 * defaulting it to the aggregator, would make a broken link look like a valid one — which is exactly
 * how the original defect stayed invisible through several rounds of testing.
 *
 * ## Why the channels come from the server
 *
 * `channels` is already resolved through `Subscriber.channels_for(aoi_id)`, so a per-plot override
 * *replaces* the general binding rather than adding to it. Re-deriving that here would mean
 * duplicating the override rule, and a page that showed both rows would tell an aggregator their
 * farmer gets two emails when they get one.
 */
export default function WorkspaceMonitoring({
  rows,
}: {
  rows: WorkspaceAreaRow[] | null;
}) {
  if (rows === null) {
    return (
      <section className="pcard">
        <div className="pcard__head">
          <h2 className="pcard__title">Active monitoring</h2>
        </div>
        <p className="muted" style={{ margin: 0, fontSize: 14 }}>
          Temporarily unavailable.
        </p>
      </section>
    );
  }

  return (
    <section className="pcard">
      <div className="pcard__head">
        <h2 className="pcard__title">Active monitoring</h2>
        <p className="pcard__sub">
          Every plot this workspace watches, who it belongs to, and where its alerts are sent.
          Ordered by recent alert activity &mdash; what needs attention first, then what is simply
          being watched.
        </p>
      </div>

      {rows.length === 0 ? (
        <p className="authform__hint">
          No monitored areas in this workspace yet. Onboard a customer above, or create areas
          programmatically with a Partner API key from{" "}
          <a href="/portal/api-keys">API keys</a>.
        </p>
      ) : (
        rows.map((row) => {
          const unlinked = row.customer_account_id === null;
          return (
            <div className="wsarea" key={row.aoi_id}>
              <div className="wsarea__head">
                <span className="wsarea__name">{row.name}</span>
                <span className="wsarea__owner" data-unlinked={unlinked}>
                  {unlinked
                    ? "Not linked to a customer — this plot will not be billed or attributed"
                    : row.is_own_plot
                      ? "Your own plot"
                      : `${row.customer_name} · ${row.customer_email}`}
                </span>
              </div>

              <dl className="wsarea__meta">
                {row.hectares !== null && (
                  <>
                    <dt>Size</dt>
                    <dd>about {row.hectares} ha</dd>
                  </>
                )}
                {row.crop && (
                  <>
                    <dt>Crop</dt>
                    <dd>{row.crop}</dd>
                  </>
                )}

                <dt>Alerts go to</dt>
                <dd>
                  {/* `delivery_mode: webhook` means SHELTER contacts nobody directly and the
                      aggregator relays it. Stating that plainly matters more than listing the
                      channels, which in that mode are not used. */}
                  {row.delivery_mode === "webhook" ? (
                    <>
                      Your webhook only &mdash; SHELTER does not contact this subscriber directly
                    </>
                  ) : row.channels.length === 0 ? (
                    <span style={{ color: "#b42318", fontWeight: 600 }}>
                      Nobody &mdash; this plot is watched and no alert can be delivered
                    </span>
                  ) : (
                    row.channels
                      .map(
                        (c) =>
                          `${CHANNEL_LABEL[c.channel] ?? c.channel}: ${c.address} (from ${c.min_severity})`,
                      )
                      .join(" · ")
                  )}
                  {row.delivery_mode === "both" && " · plus your webhook"}
                </dd>

                <dt>Alert queue</dt>
                <dd>
                  {/* Zero alerts is not an error and not "no risk": on a new plot it means the
                      first scan has not completed. Saying which it is needs the assessment, which
                      `latest_severity` carries. */}
                  {row.alert_count === 0
                    ? row.latest_severity
                      ? `No alerts yet — latest reading ${row.latest_severity.toUpperCase()}`
                      : "First satellite pass queued"
                    : `${row.alert_count} sent · latest ${
                        row.latest_severity?.toUpperCase() ?? "—"
                      }`}
                </dd>

                {row.external_ref && (
                  <>
                    <dt>Your reference</dt>
                    <dd className="mono">{row.external_ref}</dd>
                  </>
                )}

                <dt>Area id</dt>
                <dd className="mono">{row.aoi_id}</dd>
              </dl>
            </div>
          );
        })
      )}
    </section>
  );
}
