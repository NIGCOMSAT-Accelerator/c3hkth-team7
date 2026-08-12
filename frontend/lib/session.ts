import "server-only";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { api, ApiError } from "./api";
import type { Account, MyAccess } from "./types";

/**
 * Portal session, held in an httpOnly cookie.
 *
 * ## Why a cookie and not localStorage
 *
 * The backend returns a JWT. The obvious move is to keep it in `localStorage` and attach
 * it from client code — and that is the wrong move here for three reasons:
 *
 *   1. **XSS becomes account takeover.** Anything readable by JavaScript is readable by
 *      injected JavaScript. `httpOnly` means a script cannot read the token at all, so a
 *      single sanitisation slip does not hand over a subscriber's account.
 *   2. **This app is server-rendered.** `/dashboard` renders on the server, which means
 *      it needs the session *during* the render. A token in `localStorage` is not
 *      available then, so the page would have to render empty and hydrate — a flash of
 *      unauthenticated content on every navigation.
 *   3. **The token never enters the JS bundle.** Actions read it server-side and call the
 *      API from there, so it is not in any payload the browser can inspect.
 *
 * `sameSite: "lax"` rather than `strict`: `strict` would drop the cookie on the
 * cross-site navigation that a magic link *is* — the user clicks from their mail client,
 * and a strict cookie would not be sent, so they would land signed out having just
 * proven who they are.
 */

const COOKIE = "shelter_session";

/**
 * The cookie's name and options, exported so a **Route Handler** can write the same cookie.
 *
 * ## Why this is shared rather than duplicated
 *
 * `setSession` below cannot be used from a Route Handler: it goes through `cookies()`, which is the
 * request-scoped store, whereas a handler must set the cookie on the `Response` it returns
 * (`response.cookies.set`). Two different APIs, same cookie.
 *
 * `app/auth/verify/route.ts` is the caller, and it exists because a page render **cannot** write a
 * cookie at all — that was the magic-link bug. Given two write sites, the options have to have one
 * definition: a handler that set `sameSite: "strict"` or forgot `httpOnly` would produce a session
 * that behaves subtly differently from every other sign-in path, and nothing would fail loudly.
 *
 * `sameSite: "lax"` is load-bearing here specifically. A magic link IS a cross-site navigation —
 * the user clicks from their mail client — and `strict` would withhold the cookie on exactly that
 * request, landing them signed out having just proven who they are.
 */
export const SESSION_COOKIE = COOKIE;

export function sessionCookieOptions(expiresIn?: number) {
  return {
    httpOnly: true,
    // Secure everywhere except local http development, where the browser would silently drop the
    // cookie and sign-in would appear to do nothing.
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax" as const,
    path: "/",
    maxAge: expiresIn ?? MAX_AGE,
  };
}

/**
 * Cookie lifetime, in seconds. Matched to the backend's `IAM_SESSION_MINUTES` (12h).
 *
 * Deliberately not longer than the token: a cookie outliving its JWT means the user
 * appears signed in, every request 401s, and the UI has to guess why. Expiring together
 * turns that into a clean redirect to sign-in.
 */
const MAX_AGE = 12 * 60 * 60;

export async function setSession(token: string, expiresIn?: number): Promise<void> {
  const store = await cookies();
  store.set(COOKIE, token, sessionCookieOptions(expiresIn));
}

export async function clearSession(): Promise<void> {
  const store = await cookies();
  store.delete(COOKIE);
}

export async function getSessionToken(): Promise<string | null> {
  const store = await cookies();
  return store.get(COOKIE)?.value ?? null;
}

/**
 * The signed-in account, or null.
 *
 * Verified against the backend on every call rather than decoded locally. Decoding the
 * JWT client-side would be faster and would also mean a suspended account keeps working
 * until its token expires — the backend checks status on every `/iam/me`, so suspension
 * takes effect immediately.
 *
 * A 401 clears the cookie, so an expired session does not leave the user in a loop where
 * the UI thinks they are signed in and every action fails.
 */
export async function getAccount(): Promise<Account | null> {
  const token = await getSessionToken();
  if (!token) return null;

  try {
    return await api.me(token);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      await clearSession();
    }
    // Any other failure (backend down) is NOT treated as signed out — clearing the
    // session over a transient outage would log everyone out on a restart.
    return null;
  }
}

/**
 * Whether this session is the scoped one issued by a team invitation.
 *
 * The backend refuses a `SCOPE_SET_PASSWORD` session on every route except the password one,
 * with a 403 and an `X-Password-Setup-Required` header. Without recognising that, the gate
 * below reads the 403 as "not signed in" and sends the member to `/auth/login` — where they
 * have no password to sign in with, stranding them mid-onboarding with a spent invitation.
 *
 * Detected by calling `/iam/me`, which is the same call the gate already makes, so this costs
 * nothing extra. The session is NOT cleared: it is valid and needed by the password form.
 */
export async function needsPasswordSetup(): Promise<boolean> {
  const token = await getSessionToken();
  if (!token) return false;

  try {
    await api.me(token);
    return false;
  } catch (error) {
    return error instanceof ApiError && error.status === 403;
  }
}

/**
 * The gate for any page that shows real data: signed in **and** email confirmed.
 *
 * ## Why verification gates the dashboard rather than only alert delivery
 *
 * A signup with a mistyped or throwaway address produces an account that can never be
 * contacted — which for this product means a hazard warning that silently goes nowhere.
 * Confirming the address before the dashboard opens makes the one contact detail the
 * service depends on provably real, at the only moment the user is motivated to fix it.
 *
 * It also raises the cost of automated signups. Not a CAPTCHA — a mailbox round trip is
 * simply a step a script has to bother with, and unlike a CAPTCHA it does not tax the
 * legitimate user with a puzzle, which matters on a low-end phone over a metered link.
 *
 * ## The three outcomes are distinct on purpose
 *
 *   * `null` session          → `/auth/login`, because there is nobody to talk to.
 *   * session, unverified     → `/auth/pending`, which explains the state and can resend.
 *   * session, verified       → the page renders.
 *
 * Collapsing the middle case into the first would sign the user out — losing the session
 * they just earned, and with it the ability to resend to their own address, since
 * `resend-verification` is session-scoped by design.
 *
 * A `suspended` account is sent to sign-in rather than to pending: no self-service step
 * clears a suspension, and offering a "resend" button that cannot help is worse than a
 * plain sign-in page.
 */
/**
 * One-shot UI notices, carried in a short-lived httpOnly cookie instead of a query string.
 *
 * ## Why not `?verified=1`
 *
 * A URL parameter is the leakiest place to put anything. It lands in browser history (which
 * survives sign-out and is readable by any extension), in the `Referer` header sent to
 * every external asset on the page, in proxy and CDN access logs retained for months, and
 * in every screenshot and shared link.
 *
 * `?verified=1` happened to be harmless — the gate re-reads `email_verified` from the
 * backend on every request, so forging it granted nothing. But it was still a piece of
 * account state written into all of those places, and a clean URL is strictly better.
 *
 * **This is deliberately NOT a truncated session token.** Putting the last N characters of
 * the JWT in the URL would place real HMAC signature material into exactly the surfaces
 * listed above — trading a harmless flag for genuine credential leakage. The flash cookie
 * carries no token material at all, and nothing here is trusted for authorisation: it only
 * decides which sentence to render.
 *
 * 60 seconds because the redirect that sets it is immediate. Anything longer and a notice
 * could resurface on an unrelated visit minutes later.
 */
const FLASH_COOKIE = "shelter_flash";

export type FlashNotice = "verified" | "idle" | "signed-out";

export async function setFlash(notice: FlashNotice): Promise<void> {
  const store = await cookies();
  store.set(FLASH_COOKIE, notice, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: 60,
  });
}

/**
 * Reads and immediately clears the notice.
 *
 * Consuming on read is what makes it one-shot: a reload must not re-announce "your email
 * is confirmed", which would read as though it had happened twice.
 */
export async function takeFlash(): Promise<FlashNotice | null> {
  const store = await cookies();
  const value = store.get(FLASH_COOKIE)?.value;
  if (!value) return null;

  store.delete(FLASH_COOKIE);
  return value === "verified" || value === "idle" || value === "signed-out"
    ? value
    : null;
}

/**
 * The signed-in member's permissions, or an empty-ish default.
 *
 * Never throws: a portal that cannot read permissions should render the individual view
 * (dashboard only) rather than 500. Failing closed here is the safe direction — worst case a
 * member sees fewer sections than they are entitled to, and `require_permission` on the
 * backend is what actually protects anything.
 */
export async function getAccess(): Promise<MyAccess | null> {
  const token = await getSessionToken();
  if (!token) return null;

  try {
    return await api.myAccess(token);
  } catch {
    return null;
  }
}

/**
 * Gate a page on one permission. Redirects rather than rendering a 403 page.
 *
 * ## This is convenience, not the control
 *
 * The authority is `require_permission` on each backend route. This exists so a member who
 * bookmarks `/portal/team` without `team:manage` lands somewhere useful instead of on a
 * screen whose every action fails — but removing it would not grant them anything.
 *
 * An INDIVIDUAL is redirected too: these are organisation sections, and an individual has no
 * team, workspace or keys. The backend refuses them for the same reason.
 */
export async function requirePermission(
  permission: string,
  next: string,
): Promise<MyAccess> {
  const access = await getAccess();

  if (!access) {
    redirect(`/auth/login?next=${encodeURIComponent(next)}`);
  }
  if (!access.permissions.includes(permission)) {
    // Back to the portal overview, which every role can see. Not to /auth/login — they are
    // signed in perfectly well, and bouncing them to a login form for a permission problem
    // reads as a broken session.
    redirect("/portal?denied=" + encodeURIComponent(permission));
  }
  return access;
}

export async function requireVerifiedAccount(next: string): Promise<Account> {
  const account = await getAccount();

  if (!account) {
    // An invited member who has not chosen a password yet holds a valid session that the
    // backend refuses everywhere else. Sending them to sign-in would be a dead end — they
    // have no password — so they go to the form that completes their onboarding.
    if (await needsPasswordSetup()) {
      redirect("/auth/invite/password");
    }
    redirect(`/auth/login?next=${encodeURIComponent(next)}`);
  }
  if (account.status === "suspended") {
    redirect("/auth/login?reason=suspended");
  }
  if (!account.email_verified) {
    redirect("/auth/pending");
  }
  return account;
}
