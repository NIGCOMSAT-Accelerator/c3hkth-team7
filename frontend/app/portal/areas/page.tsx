import { safeApi } from "@/lib/api";
import { getAccount } from "@/lib/session";
import { CHANNEL_LABEL } from "@/lib/types";

import AreaManager from "./AreaManager";

export const metadata = { title: "Monitored areas" };
export const dynamic = "force-dynamic";

/**
 * The plots being watched.
 *
 * ## What is editable here, and the one thing that is not
 *
 * **Add a plot** and **rename or re-crop** one. Both are safe: a new area starts its own clean
 * history from its first satellite pass, and a rename is an in-place edit that keeps the `aoi_id`,
 * so every past assessment stays attached and stays meaningful.
 *
 * **Geometry is not editable.** The API supports it, for correcting a mis-dropped pin — but for
 * the case a subscriber actually has ("this is a different field") it is the wrong answer: past
 * assessments measured the old footprint, so one timeline would mix readings of two pieces of
 * ground. Adding a plot is strictly better and there is no limit, so that is what is offered.
 */
export default async function AreasPage() {
  const account = await getAccount();

  // **One record, fetched by id.**
  //
  // This used to call `listSubscribers()` — the operations endpoint, which returned EVERY
  // subscriber on the platform — and then pick this account's out with `.find()`. Nothing
  // other than the account's own plots was ever rendered, so it was not a visible leak, but
  // it pulled every farmer's name, contact address and plot coordinates into this server
  // render to display one of them.
  //
  // Two reasons that is not good enough. A client-side `.find()` is scoping that fails
  // silently: widen the list and the filter still "works" while the data in memory grows. And
  // the leak that WAS visible on `/dashboard` had exactly this shape — an unscoped platform
  // read on a page that only needed one account's data.
  //
  // `getSubscriber` is scoped server-side and 404s for anyone else's record, so an individual
  // sees precisely the plots they activated themselves, and an aggregator sees only customers
  // its workspace serves.
  const mine = account?.subscriber_id
    ? await safeApi.getSubscriber(account.subscriber_id)
    : null;

  // The latest assessment per plot, fetched alongside.
  //
  // Without this the page could not distinguish a plot scanned twenty times from one that has
  // never been looked at — they rendered identically, which is the opposite of what someone
  // checking "is my monitoring actually working?" needs to see. `safeApi` returns null rather
  // than throwing, and null is a real state: "first scan queued", not "unavailable".
  const assessments = mine
    ? Object.fromEntries(
        await Promise.all(
          mine.areas.map(async (area) => [
            area.id,
            await safeApi.latestAssessment(area.id),
          ]),
        ),
      )
    : {};

  return (
    <>
      <header className="pcard__head">
        <h1 className="portal__title">Monitored areas</h1>
        <p className="portal__lede">
          The plots SHELTER watches for you, on every satellite pass.
        </p>
      </header>

      {!account?.subscriber_id ? (
        <section className="pcard pcard--prompt">
          <h2 className="pcard__title">Nothing is being monitored</h2>
          <p className="pcard__sub">
            Tell us where your plot is and the watch loop scans it on the next pass.
          </p>
          <a href="/subscribe" className="btn btn--primary">
            Set up monitoring
          </a>
        </section>
      ) : !mine ? (
        <section className="pcard">
          <h2 className="pcard__title">Areas temporarily unavailable</h2>
          <p className="pcard__sub">
            Your monitoring is unaffected — this is a display problem, not a gap in
            coverage.
          </p>
        </section>
      ) : (
        <>
          <AreaManager
            areas={mine.areas}
            assessments={assessments}
            canRemove={mine.areas.length > 1}
          />

          <section className="pcard">
            <h2 className="pcard__title">Delivery channels</h2>
            <div className="pcard__chips">
              {mine.channels.map((c, i) => (
                <span key={i} className="chip">
                  {CHANNEL_LABEL[c.channel] ?? c.channel}
                </span>
              ))}
            </div>
            <p className="authform__hint">
              Change your default channel in <a href="/portal/settings">Settings</a>.
            </p>
          </section>

        </>
      )}
    </>
  );
}
