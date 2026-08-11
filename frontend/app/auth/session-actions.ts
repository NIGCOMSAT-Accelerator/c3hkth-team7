"use server";

import { api } from "@/lib/api";
import { clearSession, getSessionToken } from "@/lib/session";
import type { SessionState } from "@/lib/types";

/**
 * Session-state Server Actions, called by `SessionGuard`.
 *
 * ## Why these are Server Actions and not `fetch` from the browser
 *
 * The session token is an httpOnly cookie. Client code cannot read it — which is the point
 * — so it cannot attach `Authorization: Bearer` to a request of its own. Routing through
 * Server Actions means the token is read server-side and never enters the JS bundle, and
 * the browser only ever sees the resulting numbers.
 *
 * A convenient alternative would be a Route Handler that proxies these calls. It would
 * work, but it would also be an unauthenticated-looking endpoint that refreshes a session
 * — reachable by any page in any tab. Server Actions are POST-only with an origin check,
 * which is the narrower surface.
 *
 * ## Every one returns `null` for "session is gone"
 *
 * Rather than throwing. The caller's response to an expired session is the same in all
 * cases — explain and redirect — and a thrown error inside a `useEffect` in a client
 * component becomes an unhandled rejection that the user sees as nothing at all.
 */

/**
 * Read the idle window. **Does not extend it.**
 *
 * This is the property the whole design rests on: the guard polls this once a minute to
 * drive its countdown, and if reading refreshed the window the timeout could never fire.
 */
export async function readSessionState(): Promise<SessionState | null> {
  const token = await getSessionToken();
  if (!token) return null;

  try {
    return await api.sessionState(token);
  } catch {
    // Any failure is treated as "no usable session". A network blip resolves on the next
    // poll; a 401 means genuinely expired. Distinguishing them here would let a transient
    // outage keep a dead session on screen.
    return null;
  }
}

/** Report real user input and reset the window. The only call that extends a session. */
export async function reportActivity(): Promise<SessionState | null> {
  const token = await getSessionToken();
  if (!token) return null;

  try {
    return await api.sessionActivity(token);
  } catch {
    return null;
  }
}

/** The warning modal's "I'm still here" — same refresh, but audited on the backend. */
export async function extendSession(): Promise<SessionState | null> {
  const token = await getSessionToken();
  if (!token) return null;

  try {
    return await api.sessionExtend(token);
  } catch {
    return null;
  }
}

/**
 * End the session, recording why, and clear the cookie.
 *
 * The cookie is cleared **even if the backend call fails**. A cookie that outlives its
 * server-side session leaves the user apparently signed in while every request 401s, and
 * the UI has to guess why — clearing locally turns that into a clean redirect.
 */
export async function endSession(reason: "idle" | "user"): Promise<void> {
  const token = await getSessionToken();

  if (token) {
    try {
      await api.sessionEnd(token, reason);
    } catch {
      // Best effort. The audit entry is worth having but must not block a sign-out —
      // refusing to log someone out because the API was briefly unreachable would be a
      // worse failure than a gap in the log.
    }
  }

  await clearSession();
}

/**
 * Record a frontend action in the immutable audit log.
 *
 * Fire-and-forget from the caller's perspective: the backend's `record_audit` never raises
 * and never fails the action it describes, and the same reasoning applies here. A page must
 * not fail to render because its audit entry could not be written.
 *
 * The event name is validated against a server-side allow-list. That matters — letting the
 * browser name its own audit action would let any session holder write arbitrary entries
 * into a log that is meant to be evidence.
 */
export async function recordPortalEvent(
  event: string,
  detail?: string,
): Promise<void> {
  const token = await getSessionToken();
  if (!token) return;

  try {
    await api.recordPortalEvent(token, event, detail);
  } catch {
    // Swallowed deliberately — see above.
  }
}
