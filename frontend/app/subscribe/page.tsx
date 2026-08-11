import SessionGuard from "@/components/SessionGuard";
import { requireVerifiedAccount } from "@/lib/session";

import SubscribeForm from "./SubscribeForm";

export const metadata = { title: "Get alerts" };

export const dynamic = "force-dynamic";

/**
 * Gated on a confirmed address, same as the dashboard.
 *
 * This page binds a plot and a delivery channel — the point at which the service starts
 * committing to contact someone. Allowing it before confirmation would create exactly the
 * state verification exists to prevent: a monitored area whose warnings have nowhere to
 * go. It is also the page that could otherwise be used to point alerts at an address the
 * signer-up does not own.
 */
export default async function SubscribePage() {
  await requireVerifiedAccount("/subscribe");

  return (
    <>
      <SessionGuard />
      <SubscribePageBody />
    </>
  );
}

function SubscribePageBody() {
  return (
    <div className="shell" style={{ padding: "44px 24px 0", maxWidth: 780 }}>
      <header style={{ marginBottom: 26 }}>
        <h1 style={{ fontSize: 30, marginBottom: 8 }}>
          Activate alerts for your area
        </h1>
        <p style={{ margin: 0, color: "var(--text-secondary)", fontSize: 15 }}>
          Tell us where to watch and how to reach you. From then on the watch
          loop runs on its own — every Sentinel pass, around the clock — and
          contacts you only when something needs action.
        </p>
      </header>

      <SubscribeForm />

      <section className="card" style={{ margin: "28px 0 56px" }}>
        <h2 className="card__title">What you&rsquo;ll receive</h2>
        <p className="card__sub">And what we deliberately won&rsquo;t send</p>
        <ul className="evidence">
          <li>
            Alerts at <strong>advisory</strong> level and above by default.
            Routine findings are recorded on the dashboard but not sent — a
            system that pings every week gets muted, and then the one that
            mattered is muted too.
          </li>
          <li>
            No repeat of the same hazard at the same severity within 18 hours.
            An escalation always gets through.
          </li>
          <li>
            Every alert states the evidence behind it, so you can judge it
            rather than just trust it.
          </li>
          <li>
            At <strong>warning</strong> level and above, or if every other
            channel fails, the alert also goes out over NIGCOMSAT-1R broadcast —
            which needs no internet at your end.
          </li>
        </ul>
      </section>
    </div>
  );
}
