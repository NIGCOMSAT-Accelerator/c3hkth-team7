"use client";

import { usePathname } from "next/navigation";

/**
 * Portal sidebar.
 *
 * ## Why a client component when the rest of the portal is server-rendered
 *
 * Only to read `usePathname()` for the active state. That genuinely needs the client: the
 * layout renders once and children navigate underneath it, so a server-computed active
 * item would be stale after the first client-side navigation — the highlight would stick to
 * whichever page was loaded first.
 *
 * It ships no data and no account fields, only the `isCommercial` flag that decides whether
 * the API-keys item exists.
 *
 * ## Below 900px this is a horizontal scroller, not a hamburger
 *
 * A hamburger hides navigation behind a tap and needs JS to open. A scrolling row keeps
 * every destination visible and reachable with a thumb, and it degrades to a plain row of
 * links with no script at all. On a 360px screen the first three items are visible and the
 * rest scroll — which is how a farmer reaches "My alerts" in one gesture.
 */

interface Item {
  href: string;
  label: string;
  /** One line explaining the section. Shown as a title attribute, not inline — the
   *  sidebar must stay scannable, but a first-time visitor benefits from the hint. */
  hint: string;
  icon: React.ReactNode;
}

// 18px stroke icons, currentColor, drawn inline. An icon font or sprite sheet would be a
// separate request for decoration on a metered connection.
const ICONS = {
  overview: (
    <path d="M3 10.5 10 4l7 6.5V17a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z" />
  ),
  alerts: (
    <>
      <path d="M10 3.5 2.5 16.5h15z" />
      <path d="M10 8.5v3.5" />
      <path d="M10 14.4v.2" />
    </>
  ),
  areas: (
    <>
      <path d="M10 17s5.5-5 5.5-9a5.5 5.5 0 0 0-11 0c0 4 5.5 9 5.5 9z" />
      <circle cx="10" cy="8" r="2" />
    </>
  ),
  settings: (
    <>
      <circle cx="10" cy="10" r="2.6" />
      <path d="M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M15.3 4.7l-1.4 1.4M6.1 13.9l-1.4 1.4" />
    </>
  ),
  security: (
    <>
      <path d="M10 2.5 4 5v5c0 4 6 7.5 6 7.5S16 14 16 10V5z" />
      <path d="M7.6 10l1.7 1.7 3.2-3.4" />
    </>
  ),
  activity: (
    <>
      <path d="M2.5 10.5h3l2-5 3 9 2.5-4h4.5" />
    </>
  ),
  workspace: (
    <>
      <path d="M3 7.5h14v9.5H3z" />
      <path d="M7.5 7.5V5.5a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v2" />
    </>
  ),
  webhook: (
    <>
      <circle cx="10" cy="5.5" r="2.2" />
      <circle cx="5" cy="14.5" r="2.2" />
      <circle cx="15" cy="14.5" r="2.2" />
      <path d="M8.6 7.3 6.2 12.4M11.4 7.3l2.4 5.1M7.2 14.5h5.6" />
    </>
  ),
  team: (
    <>
      <circle cx="7.5" cy="7.5" r="2.6" />
      <path d="M2.8 16.5a4.7 4.7 0 0 1 9.4 0" />
      <path d="M13 5.4a2.6 2.6 0 0 1 0 4.5M14.4 16.5a4.7 4.7 0 0 0-1.6-3.5" />
    </>
  ),
  compliance: (
    <>
      <path d="M10 2.5 4 5v5c0 4 6 7.5 6 7.5S16 14 16 10V5z" />
      <path d="M7.6 10l1.7 1.7 3.2-3.4" />
    </>
  ),
  keys: (
    <>
      <circle cx="7" cy="10" r="3.2" />
      <path d="M10.2 10H17M14.5 10v3M12.5 10v2.2" />
    </>
  ),
};

function Icon({ children }: { children: React.ReactNode }) {
  return (
    <svg
      className="pnav__icon"
      width="18"
      height="18"
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

export default function PortalNav({
  isCommercial,
  permissions,
}: {
  isCommercial: boolean;
  /**
   * The signed-in member's permissions, resolved server-side from their role.
   *
   * Passed as plain strings rather than as a role name: the nav should not have to know that
   * "Operations" implies `integration:manage`, and duplicating that table here is exactly how
   * a UI ends up disagreeing with the API about what someone may do. One table, in
   * `backend/app/iam/roles.py`.
   */
  permissions: string[];
}) {
  const pathname = usePathname();

  const items: Item[] = [
    {
      href: "/portal",
      label: "Overview",
      hint: "Your monitored areas, latest risk and delivery health",
      icon: <Icon>{ICONS.overview}</Icon>,
    },
    {
      href: "/portal/alerts",
      label: "My alerts",
      hint: "Every advisory sent to you, and what it was based on",
      icon: <Icon>{ICONS.alerts}</Icon>,
    },
    {
      href: "/portal/areas",
      label: "Monitored areas",
      hint: "The plots being watched on every satellite pass",
      icon: <Icon>{ICONS.areas}</Icon>,
    },
    {
      href: "/portal/settings",
      label: "Settings",
      hint: "Language, delivery channels and alert threshold",
      icon: <Icon>{ICONS.settings}</Icon>,
    },
    {
      href: "/portal/security",
      label: "Security",
      hint: "Two-factor authentication and active session",
      icon: <Icon>{ICONS.security}</Icon>,
    },
    {
      href: "/portal/activity",
      label: "Activity log",
      hint: "Everything that happened on your account",
      icon: <Icon>{ICONS.activity}</Icon>,
    },
  ];

  // Organisation sections — commercial accounts only.
  //
  // An individual has no team to divide access among, no workspace, and no API key, so these
  // are absent rather than disabled. A greyed-out "Team management" on a farmer's portal
  // would advertise a feature that will never apply to them.
  //
  // These are also PERMISSION-gated, not just kind-gated: a View-Only member of an
  // organisation sees the dashboard and customers but not Workspace, Team or API keys. The
  // hiding is courtesy — `require_permission` on the backend is the control.
  if (isCommercial) {
    if (permissions.includes("workspace:manage")) {
      items.push({
        href: "/portal/workspace",
        label: "Workspace",
        hint: "Activate intelligence tracks for your organisation",
        icon: <Icon>{ICONS.workspace}</Icon>,
      });
    }
    if (permissions.includes("keys:manage")) {
      items.push({
        href: "/portal/api-keys",
        label: "API keys",
        hint: "Create, rotate and revoke keys for your integration",
        icon: <Icon>{ICONS.keys}</Icon>,
      });
    }
    if (permissions.includes("integration:manage")) {
      items.push({
        href: "/portal/webhooks",
        label: "Webhooks",
        hint: "Receive events in your own systems",
        icon: <Icon>{ICONS.webhook}</Icon>,
      });
    }
    if (permissions.includes("team:manage")) {
      items.push({
        href: "/portal/team",
        label: "Team",
        hint: "Invite colleagues and set what they can do",
        icon: <Icon>{ICONS.team}</Icon>,
      });
    }
    if (permissions.includes("compliance:manage")) {
      items.push({
        href: "/portal/compliance",
        label: "Compliance",
        hint: "Verification review and blacklist operations",
        icon: <Icon>{ICONS.compliance}</Icon>,
      });
    }
  }

  return (
    <nav className="pnav" aria-label="Portal sections">
      <ul className="pnav__list">
        {items.map((item) => {
          // Exact match for the index, prefix match for the rest. Without the exact check
          // on "/portal" every child route would light up the Overview item as well.
          const active =
            item.href === "/portal"
              ? pathname === "/portal"
              : pathname.startsWith(item.href);

          return (
            <li key={item.href}>
              <a
                href={item.href}
                title={item.hint}
                className={`pnav__item${active ? " pnav__item--active" : ""}`}
                // Announced to screen readers, which cannot see the highlight. `page` is
                // the correct token here — this marks the current page, not a step.
                aria-current={active ? "page" : undefined}
              >
                {item.icon}
                <span>{item.label}</span>
              </a>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
