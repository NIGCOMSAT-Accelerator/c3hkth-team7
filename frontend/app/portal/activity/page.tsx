import { safePortal } from "@/lib/portal";
import { ACTION_LABEL, ACTOR_LABEL } from "@/lib/types";

export const metadata = { title: "Activity log" };
export const dynamic = "force-dynamic";

/**
 * The subscriber's own audit trail.
 *
 * ## Why a subscriber sees this at all, rather than only operators
 *
 * The backend docstring for `GET /iam/audit` puts it exactly right: it answers *"what
 * happened to my account?"*, **including actions an aggregator took**. That last part is why
 * this page matters here rather than only in an admin console — a farmer served by a
 * cooperative should be able to see that the cooperative read their data or triggered a
 * scan, without having to ask the cooperative.
 *
 * ## Pagination is a cursor, not a page number
 *
 * `GET /iam/audit` is keyset-paginated over `(at, _id)` and deliberately returns **no
 * total** — `count_documents` on a growing collection is an unindexed scan that gets slower
 * exactly as the log becomes more valuable. So this renders "Load more" with an opaque
 * cursor rather than numbered pages, and does not claim a count it cannot cheaply know.
 *
 * The cursor lives in the URL, so the back button works and a reloaded page shows the same
 * entries. A client-side accumulating list would lose position on refresh.
 */
export default async function ActivityPage({
  searchParams,
}: {
  searchParams: Promise<{ cursor?: string; action?: string }>;
}) {
  const { cursor, action } = await searchParams;

  const [page, summary] = await Promise.all([
    safePortal.auditPage({ cursor, action }),
    safePortal.auditActivity(30),
  ]);

  const entries = page?.entries ?? [];

  return (
    <>
      <header className="pcard__head">
        <h1 className="portal__title">Activity log</h1>
        <p className="portal__lede">
          Everything that has happened on your account — by you, by SHELTER, and by any
          organisation serving you. This record is append-only: entries are never edited or
          removed.
        </p>
      </header>

      {/* Counts over 30 days, from the backend's own summary endpoint — cheaper than
          counting client-side and it covers entries beyond the first page.

          `by_action` is a list, not a map: the same action appears once per outcome, so a
          failed sign-in is a separate row from a successful one. Counts are merged by
          action here because the chips filter by action alone. */}
      {summary && (summary.by_action?.length ?? 0) > 0 && (
        <section className="pcard">
          <h2 className="pcard__title">Last {summary.window_days} days</h2>
          <div className="pcard__chips">
            {Object.entries(
              summary.by_action.reduce<Record<string, number>>((acc, row) => {
                acc[row.action] = (acc[row.action] ?? 0) + row.count;
                return acc;
              }, {}),
            )
              .sort((a, b) => b[1] - a[1])
              .map(([act, count]) => (
                <a
                  key={act}
                  href={`/portal/activity?action=${encodeURIComponent(act)}`}
                  className={`chip${action === act ? " chip--on" : ""}`}
                >
                  {ACTION_LABEL[act] ?? act} · {count}
                </a>
              ))}
            {action && (
              <a href="/portal/activity" className="chip chip--clear">
                Clear filter
              </a>
            )}
          </div>
        </section>
      )}

      <section className="pcard">
        <div className="pcard__head">
          <h2 className="pcard__title">
            {action ? `Filtered: ${ACTION_LABEL[action] ?? action}` : "All events"}
          </h2>
        </div>

        {page === null ? (
          <p className="muted" style={{ margin: 0, fontSize: 14 }}>
            The activity log is temporarily unavailable. Your account is unaffected.
          </p>
        ) : entries.length === 0 ? (
          <p className="muted" style={{ margin: 0, fontSize: 14 }}>
            {action
              ? "No events of that type."
              : "No activity recorded yet."}
          </p>
        ) : (
          <ol className="auditlist">
            {entries.map((e, i) => (
              <li key={`${e.at}-${i}`} className="auditrow">
                <div className="auditrow__main">
                  <span className="auditrow__action">
                    {ACTION_LABEL[e.action] ?? e.action}
                  </span>
                  {/* Outcome only when it is not a success. A green "success" tag on
                      every row is noise that makes the failures harder to spot. */}
                  {e.outcome !== "success" && (
                    <span className={`auditrow__outcome auditrow__outcome--${e.outcome}`}>
                      {e.outcome}
                    </span>
                  )}
                  {/* Who did it. Shown only when it was NOT the account holder — "you did
                      this" on every row is redundant, but "your cooperative did this" is
                      the single most important thing on the page. */}
                  {e.actor_kind !== "self" && (
                    <span className="auditrow__actor">
                      by {ACTOR_LABEL[e.actor_kind] ?? e.actor_kind}
                    </span>
                  )}
                </div>

                {e.detail && <p className="auditrow__detail">{e.detail}</p>}

                <div className="auditrow__meta">
                  <time dateTime={e.at}>{formatWhen(e.at)}</time>

                  {/*
                    Device before location, and both before the raw IP. This is the ordering
                    a person actually reasons with: "was this my phone?" is answerable, "was
                    this 102.89.x.x?" is not.

                    `agent.summary` is derived server-side from the stored user-agent, so a
                    row written before the parser knew a browser still reads correctly once
                    it does.
                  */}
                  {e.agent && (
                    <span title={e.user_agent ?? undefined}>
                      {e.agent.summary}
                      {e.agent.is_bot && " (automated)"}
                    </span>
                  )}

                  {e.location ? (
                    <span title={e.ip ?? undefined}>
                      {e.location.label}
                      {/* The hedge travels with the value. A city named without it reads as
                          fact, and GeoLite2 in this region often resolves to a carrier
                          gateway rather than the subscriber's town. */}
                      {e.location.confidence === "city" && " (approx.)"}
                    </span>
                  ) : (
                    // No database, or an unresolvable range. The IP is still the most
                    // useful thing available — a subscriber can recognise "that is not my
                    // network" even without a city name.
                    e.ip && <span className="mono">{e.ip}</span>
                  )}
                </div>
              </li>
            ))}
          </ol>
        )}

        {page?.has_more && page.next_cursor && (
          <p className="pcard__foot">
            <a
              className="btn btn--ghost"
              href={`/portal/activity?cursor=${encodeURIComponent(page.next_cursor)}${
                action ? `&action=${encodeURIComponent(action)}` : ""
              }`}
            >
              Load older entries
            </a>
          </p>
        )}
      </section>
    </>
  );
}

function formatWhen(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
