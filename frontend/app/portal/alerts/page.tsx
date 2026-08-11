import { safeApi } from "@/lib/api";
import AlertCard from "@/components/AlertCard";
import TrackModules from "@/components/TrackModules";
import VerdictPanel from "@/components/VerdictPanel";
import { advisoryByline } from "@/lib/intelligence";
import { getAccount } from "@/lib/session";
import { CHANNEL_LABEL, SOURCE_LABEL } from "@/lib/types";

export const metadata = { title: "My alerts" };
export const dynamic = "force-dynamic";

/**
 * Every advisory sent to this subscriber, newest first.
 *
 * Scoped by `subscriber_id` in the query, so another subscriber's advisory is never a
 * candidate result — the gate in the layout controls access, this controls the data.
 *
 * Each entry carries the evidence the advisory was allowed to cite. That is deliberate and
 * matches the honesty discipline in `advisory/generator.py`: the model receives only
 * `RiskAssessment.evidence` and is instructed to add no numbers, so showing the same list
 * lets a subscriber check the reasoning rather than take the headline on trust.
 */
export default async function MyAlertsPage() {
  const account = await getAccount();
  const alerts = account?.subscriber_id
    ? await safeApi.listAlerts(50, account.subscriber_id)
    : [];

  return (
    <>
      <header className="pcard__head">
        <h1 className="portal__title">My alerts</h1>
        <p className="portal__lede">
          Every advisory sent to you, with the measurements behind it.
        </p>
      </header>

      {!account?.subscriber_id ? (
        <section className="pcard pcard--prompt">
          <h2 className="pcard__title">No area is being monitored yet</h2>
          <p className="pcard__sub">
            Alerts appear here once you tell us which plot to watch.
          </p>
          <a href="/subscribe" className="btn btn--primary">
            Set up monitoring
          </a>
        </section>
      ) : alerts.length === 0 ? (
        <section className="pcard">
          <h2 className="pcard__title">No alerts yet</h2>
          {/*
            Silence is explained rather than left ambiguous. "No alerts" could mean the
            service is broken; saying that assessments still run and nothing crossed the
            threshold is what makes an empty page reassuring instead of worrying.
          */}
          <p className="pcard__sub">
            Your area is assessed on every satellite pass. Nothing has crossed the alert
            threshold, which is the outcome you want — you are contacted only when there is
            something to act on. Current assessments are on the{" "}
            <a href="/dashboard">live map</a>.
          </p>
        </section>
      ) : (
        <div className="alertfeed">
          {alerts.map((alert, index) => (
            <AlertCard
              key={alert.id}
              alert={alert}
              // Newest-first, so the leading alert is the one someone came to read. Opening it by
              // default means the common case needs no interaction; the rest stay collapsed.
              defaultOpen={index === 0}
              when={formatWhen(alert.created_at)}
            >
              {/*
                Severity, headline, plot and time are in the collapsed row already, so the expanded
                body starts with what the row could not carry: the confidence figure and the prose.
                Repeating the headline here would make every open card start by telling you what you
                just clicked.
              */}
              <p className="pcard__sub">
                Confidence {(alert.assessment.confidence * 100).toFixed(0)}%
              </p>

              <p className="pcard__body">{alert.advisory.body}</p>

              {alert.advisory.actions.length > 0 && (
                <>
                  <p className="pcard__minihead">What to do</p>
                  <ul className="evidence">
                    {alert.advisory.actions.map((a, i) => (
                      <li key={i}>{a}</li>
                    ))}
                  </ul>
                </>
              )}

              {/*
                The per-track modules, in the SAME position as in the alert email: after the
                action, before the narration. A subscriber who reads the email and then opens the
                portal must find the same figures in the same order, or one of the two surfaces
                reads as incomplete. Renders nothing when nothing was measured.
              */}
              <TrackModules tracks={alert.tracks} />

              {/*
                The three explanation surfaces, between the actions and the raw evidence.
                
                That position is the reading order the alert is built for: what to do, then what
                it means, then the measurements it rests on. Each row is omitted individually when
                empty, so an alert generated without a provider — or one predating this feature —
                shows fewer rows rather than empty headings.
              */}
              {(alert.advisory.explanations?.crop ||
                alert.advisory.explanations?.drivers ||
                alert.advisory.explanations?.irrigation) && (
                <>
                  <p className="pcard__minihead">What this means for you</p>
                  <dl className="explains">
                    {alert.advisory.explanations.crop && (
                      <div>
                        <dt>Your crop</dt>
                        <dd>{alert.advisory.explanations.crop}</dd>
                      </div>
                    )}
                    {alert.advisory.explanations.drivers && (
                      <div>
                        <dt>Why</dt>
                        <dd>{alert.advisory.explanations.drivers}</dd>
                      </div>
                    )}
                    {alert.advisory.explanations.irrigation && (
                      <div>
                        <dt>Watering</dt>
                        <dd>{alert.advisory.explanations.irrigation}</dd>
                      </div>
                    )}
                  </dl>
                </>
              )}

              {/*
                Fahis's verdict, beside the alert it judges.
                
                "Were we right?" is only meaningful next to the warning it answers — a separate
                accuracy page can show the aggregate, but a subscriber looking at one alert wants
                to know about THAT one.
                
                Absent when verification has not run yet, which is normal: it is scheduled days
                after the assessment. Rendering "unknown" there would read as a failure.
              */}
              {alert.verdict && <VerdictPanel verdict={alert.verdict} />}

              {alert.assessment.evidence.length > 0 && (
                <>
                  <p className="pcard__minihead">Why this was sent</p>
                  <ul className="evidence">
                    {alert.assessment.evidence.map((e, i) => (
                      <li key={i}>{e}</li>
                    ))}
                  </ul>
                </>
              )}

              <div className="pcard__chips">
                {alert.receipts.map((r, i) => (
                  <span
                    key={i}
                    title={r.error ?? undefined}
                    className={`chip${r.status === "failed" ? " chip--bad" : ""}`}
                  >
                    {CHANNEL_LABEL[r.channel] ?? r.channel} · {r.status}
                  </span>
                ))}
                {alert.assessment.data_sources.map((s) => (
                  <span key={s} className="chip chip--quiet">
                    {SOURCE_LABEL[s] ?? s}
                  </span>
                ))}
                {/*
                  Who wrote it — the capability, never the model name. The sources are already
                  chips above, so this carries the author only. See `advisoryByline` for why the
                  resolved model is not published to subscribers.
                */}
                <span className="chip chip--quiet">
                  {advisoryByline(alert.advisory.generated_by).author}
                </span>
              </div>
            </AlertCard>
          ))}
        </div>
      )}
    </>
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
