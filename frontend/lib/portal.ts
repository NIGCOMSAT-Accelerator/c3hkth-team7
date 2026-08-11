import "server-only";

import { api } from "./api";
import { getSessionToken } from "./session";
import type { AuditPage, AuditSummary, TotpState } from "./types";

/**
 * Session-scoped reads for the portal.
 *
 * ## Why a separate module from `lib/api.ts`
 *
 * `api.*` takes an explicit session token, which is right for a transport layer — it makes
 * the credential visible at every call site and keeps the client testable. But every portal
 * page would then start with the same three lines of "read the cookie, bail if absent", and
 * the failure mode of forgetting the bail is a call made with `undefined` as the token.
 *
 * These wrappers read the cookie themselves and return `null` when there is no session, so
 * a page can render a degraded state instead of throwing. The gate in `portal/layout.tsx`
 * means that null is nearly unreachable in practice — this is the belt to its braces.
 *
 * ## Everything here is scoped by the session, never by a parameter
 *
 * No function takes an account id. The backend derives identity from the bearer token on
 * every one of these routes, so there is no argument with which a page could ask for
 * someone else's audit log. That is the same reasoning as chat's `subscriber_id` being
 * closed over rather than exposed as a tool argument.
 */

/** Never throws. A portal section that cannot load renders an explanation, not a 500. */
async function safe<T>(fn: (token: string) => Promise<T>): Promise<T | null> {
  const token = await getSessionToken();
  if (!token) return null;

  try {
    return await fn(token);
  } catch {
    return null;
  }
}

export const safePortal = {
  /**
   * One page of the caller's own audit log.
   *
   * Keyset-paginated: `cursor` is opaque and comes from the previous response. There is
   * deliberately no page number and no total — see the backend docstring on `GET /iam/audit`.
   */
  auditPage: (opts: { cursor?: string; action?: string } = {}) =>
    safe((t) => api.auditPage(t, opts)),

  /** Action counts over a window, for the activity widget. */
  auditActivity: (days = 30) => safe((t) => api.auditActivity(t, days)),

  /** Whether TOTP is enrolled. Drives the Security page. */
  totpState: () => safe((t) => api.totpState(t)),

  /** The caller's own API keys. Commercial accounts only; individuals get a 403. */
  apiKeys: () => safe((t) => api.listApiKeys(t)),

  /** Aggregators serving this subscriber — the multi-tenancy view. */
  aggregators: () => safe((t) => api.myAggregators(t)),

  /** Devices this account has signed in from. Drives the Security page's trusted-device table. */
  trustedDevices: () => safe((t) => api.trustedDevices(t)),

  /** The caller's workspaces. Needed by the API-key form: a key belongs to exactly one. */
  workspaces: () => safe((t) => api.listWorkspaces(t)),

  /**
   * Every monitored area in one workspace, with its customer and where its alerts go.
   *
   * The `Workspace > Customer > Area > Alerts` chain in one call. `safe` returns null rather than
   * throwing, and null is a real state here — a workspace whose areas cannot be read should degrade
   * to "could not load" rather than 500 the page.
   */
  workspaceAreas: (workspaceId: string) =>
    safe((t) => api.workspaceAreas(t, workspaceId)),
};

export type { AuditPage, AuditSummary, TotpState };
