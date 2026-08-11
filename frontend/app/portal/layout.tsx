import SessionGuard from "@/components/SessionGuard";
import { getAccess, requireVerifiedAccount } from "@/lib/session";

import PortalNav from "./PortalNav";
import SessionPanel from "./SessionPanel";

export const dynamic = "force-dynamic";

/**
 * The subscriber's own portal — everything scoped to *their* account.
 *
 * ## Why this is separate from `/dashboard`
 *
 * `/dashboard` is a global operations view: every alert the service dispatched, across all
 * subscribers. Useful to an operator, and it is what the frontend-journey review calls out
 * as "global, not personal". This is the personal half — my alerts, my settings, my
 * security, my audit trail.
 *
 * Keeping them apart matters because they answer different questions and have different
 * blast radii. Merging them would mean either showing a farmer everyone else's plots, or
 * stripping the operations view of the breadth that makes it useful.
 *
 * ## One gate, one guard, for every child route
 *
 * `requireVerifiedAccount` runs here rather than in each page. A layout cannot be bypassed
 * by a child route, so adding a page under `/portal/` gets the gate automatically — the
 * failure mode of a per-page check is a new page shipped without one, which is silent and
 * only affects unverified or signed-out users.
 *
 * `SessionGuard` also mounts once here instead of per page, so the idle countdown survives
 * client-side navigation between portal pages instead of restarting on each.
 */
export default async function PortalLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const account = await requireVerifiedAccount("/portal");
  // Permissions drive which organisation sections appear. Read once here rather than per
  // page: the nav needs them, and a second fetch per navigation would be wasted.
  const access = await getAccess();

  return (
    <div className="portal">
      <SessionGuard />

      <div className="shell portal__grid">
        <div className="portal__side">
          <PortalNav
            isCommercial={account.kind === "commercial"}
            permissions={access?.permissions ?? []}
          />
          {/* Identity, location and device. Server-rendered fallbacks so the panel is
              never blank on first paint; the live location and device arrive from
              GET /iam/session, which reads request headers the page cannot see. */}
          <SessionPanel
            // Composed here rather than read from the account: `full_name` is a Python
            // @property on the backend model, so it is computed and never serialised into
            // the JSON response.
            fallbackName={
              `${account.first_name} ${account.last_name}`.trim() || account.email
            }
            fallbackEmail={account.email}
            fallbackEmoji={account.avatar_emoji ?? "🌾"}
            fallbackColor={account.avatar_color ?? "#6a0dad"}
          />
        </div>
        <div className="portal__main">{children}</div>
      </div>
    </div>
  );
}
