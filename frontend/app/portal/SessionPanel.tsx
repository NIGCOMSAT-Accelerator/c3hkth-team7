"use client";

import { useEffect, useState } from "react";

import { readSessionState } from "@/app/auth/session-actions";
import type { SessionState } from "@/lib/types";

/**
 * Who you are, where you appear to be, and what you are signed in from.
 *
 * Sits under the portal sidebar nav. Three jobs, in descending order of importance:
 *
 *   1. **Identity.** Avatar and full name, so someone on a shared handset can see at a
 *      glance whose session this is. The avatar is derived from the account id, so it is
 *      stable — a *different* emoji is the signal that this is not the expected account.
 *   2. **Location.** "Warrington, United Kingdom · From your IP address". The hedge is not
 *      decoration: GeoLite2 city accuracy in Sub-Saharan Africa is materially worse than in
 *      Europe, often resolving to a carrier's national gateway. Stating the source is what
 *      keeps an approximate answer honest.
 *   3. **Device.** "Mobile · Android 14 · Chrome 141", parsed from *this* request's
 *      user-agent rather than the one stored at login, so it describes the device in use.
 *
 * ## Why the data comes from `GET /iam/session` and not from the page's own account read
 *
 * Location and device are properties of the *request*, not of the account, so they cannot
 * be rendered from a stored profile. The session endpoint sees the live headers.
 *
 * Reading it is also deliberately free of side effects: `readSessionState` calls
 * `GET /iam/session`, which does **not** extend the idle window. If this panel refreshed
 * the session, an open portal tab would keep itself alive forever with nobody present —
 * the exact failure the read/write split in the backend exists to prevent.
 */
export default function SessionPanel({
  fallbackName,
  fallbackEmail,
  fallbackEmoji,
  fallbackColor,
}: {
  /** Server-rendered identity, so the panel is never blank on first paint. */
  fallbackName: string;
  fallbackEmail: string;
  fallbackEmoji: string;
  fallbackColor: string;
}) {
  const [state, setState] = useState<SessionState | null>(null);

  useEffect(() => {
    // One read on mount. No polling: SessionGuard already polls for the idle countdown,
    // and a second timer on the same page would double a subscriber's data cost for
    // information that does not change while they sit still.
    void readSessionState().then(setState);
  }, []);

  const name = state?.full_name || fallbackName;
  const email = state?.email || fallbackEmail;
  const emoji = state?.avatar_emoji || fallbackEmoji;
  const color = state?.avatar_color || fallbackColor;

  return (
    <div className="sesspanel">
      <div className="sesspanel__id">
        <span
          className="sesspanel__avatar"
          style={{ background: color }}
          aria-hidden="true"
        >
          {emoji}
        </span>
        <div className="sesspanel__who">
          <strong className="sesspanel__name">{name}</strong>
          <span className="sesspanel__mail">{email}</span>
        </div>
      </div>

      {/*
        Location. Rendered only once the session read returns — a placeholder city would be
        worse than none, since the whole point is that the subscriber recognises it or does
        not.
      */}
      {state?.location && (
        <div className="sesspanel__row">
          <svg
            className="sesspanel__icon"
            width="13"
            height="13"
            viewBox="0 0 20 20"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.6"
            aria-hidden="true"
          >
            <path d="M10 17.5s5.5-5 5.5-9a5.5 5.5 0 0 0-11 0c0 4 5.5 9 5.5 9z" />
            <circle cx="10" cy="8.2" r="2" />
          </svg>
          <div>
            <span className="sesspanel__value">{state.location}</span>
            {/*
              The hedge, always. `location_source === "ip_raw"` means the GeoLite2 database
              is not installed and this is a bare IP address, which needs a different
              sentence — telling someone an IP is "your location" would be nonsense.
            */}
            <span className="sesspanel__hint">
              {state.location_source === "ip_raw"
                ? "Your IP address"
                : "From your IP address — approximate"}
            </span>
            {/*
              Not a real settings control. It links to Security, where the activity log
              explains what to do if a location is unexpected — which is the actionable
              response. A literal "update location" field would invite someone to type a
              city, and a self-declared location proves nothing and protects nothing.
            */}
            <a href="/portal/security" className="sesspanel__link">
              Not you?
            </a>
          </div>
        </div>
      )}

      {state?.device && (
        <div className="sesspanel__row">
          <svg
            className="sesspanel__icon"
            width="13"
            height="13"
            viewBox="0 0 20 20"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.6"
            aria-hidden="true"
          >
            <rect x="6" y="2.5" width="8" height="15" rx="1.6" />
            <path d="M9 15.2h2" />
          </svg>
          <div>
            <span className="sesspanel__value">{state.device}</span>
            <span className="sesspanel__hint">This device</span>
          </div>
        </div>
      )}
    </div>
  );
}
