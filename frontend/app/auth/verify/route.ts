import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { api, ApiError } from "@/lib/api";
import { SESSION_COOKIE, sessionCookieOptions } from "@/lib/session";

/**
 * Emailed-link landing — `GET /auth/verify`.
 *
 * ## The bug this exists to fix
 *
 * A Route Handler, not a page, and that distinction **is** the fix.
 *
 * This route was `app/auth/verify/page.tsx`, a Server Component. It redeemed the token during
 * render and then called `setSession()`, which calls `cookies().set()`. That throws,
 * unconditionally, every time:
 *
 *     Error: Cookies can only be modified in a Server Action or Route Handler.
 *
 * Not a race, not an expiry, not environment-dependent. Read it in Next's own source
 * (`server/web/spec-extension/adapters/request-cookies.js`): during a render, `cookies()` returns a
 * proxy sealed by `RequestCookiesAdapter.seal`, and `set`/`delete`/`clear` are replaced with a
 * function whose only behaviour is `throw new ReadonlyRequestCookiesError()`. There is no
 * conditional. A page render cannot write a cookie, because headers may already have streamed and
 * there is no response left to attach `Set-Cookie` to.
 *
 * ## Why it presented as "that link has expired"
 *
 * This is the part worth understanding, because the reported symptom pointed away from the cause.
 * The order of operations was:
 *
 *   1. `api.redeemMagicLink(token)` succeeds. **The backend deletes the token** — redemption is
 *      single-use and atomic (`store.redeem_single_use_token`), by design.
 *   2. `setSession()` throws `ReadonlyRequestCookiesError`.
 *   3. `redeemMagicLink`'s `catch` swallows it. `describe()` only maps `ApiError` to a message, so
 *      any other error falls through to the generic string — which for this action is *"That
 *      sign-in link is invalid, already used, or has expired."*
 *
 * So the user saw an expiry message for a token that had **just been accepted**, and a retry could
 * not work because step 1 had genuinely consumed it. Verified against production: POSTing the
 * judge's token to `/iam/auth/magic-link/verify` returns 400 "invalid, already used, or has
 * expired" — correct, because the first click spent it. The backend was never at fault.
 *
 * That is also why "it happened within 60 seconds" was the most useful detail in the report: it
 * ruled out expiry and left only something that fails on the success path.
 *
 * ## Why a redirect rather than rendering an error here
 *
 * A Route Handler returns a `Response`, so it can do the one thing the page could not: set the
 * session cookie **and** send the user onward in the same response. Failures redirect to
 * `/auth/link-failed` with a reason code rather than rendering HTML, keeping this file to the two
 * jobs a handler is good at — exchange a token, write a cookie.
 *
 * ## The token never reaches the browser as JavaScript
 *
 * Unchanged from the page version, and worth keeping: redemption happens server-side, and the
 * redirect drops the query string, so the single-use token stays out of client JS. It is still in
 * the URL the user clicked — that is unavoidable for an emailed link — but it is spent by the time
 * this responds.
 */
export const dynamic = "force-dynamic";

/**
 * The absolute URL to redirect to, built from the PUBLIC origin.
 *
 * ## Why `request.url` cannot be the base, verified in production
 *
 * `NextResponse.redirect` requires an absolute URL, and the obvious base — `request.url` — is the
 * address the *container* was reached on, not the one the browser used. On the VPS the Next server
 * binds `HOSTNAME=0.0.0.0` and `PORT=3100` (Dokploy's own UI occupies 3000), so Traefik proxies to
 * it and `request.url` is `http://0.0.0.0:3100/...`. The emitted header was:
 *
 *     location: https://0.0.0.0:3100/auth/link-failed?reason=spent
 *
 * ...which no browser can resolve. The redemption itself had already SUCCEEDED — the first click
 * reached `/dashboard`, on the wrong host — so this presented as the magic link still being broken
 * when in fact only the redirect target was.
 *
 * It passed local testing precisely because `request.url` is correct there: bound and public origin
 * are the same when nothing proxies. That is what makes this class of bug worth a named helper
 * rather than an inline `new URL`.
 *
 * ## The resolution order, and why each rung exists
 *
 *   1. `x-forwarded-host` + `x-forwarded-proto` — what the proxy states the client asked for. Most
 *      accurate, and Traefik sets both.
 *   2. `NEXT_PUBLIC_SITE_URL` — the configured public origin, for a deployment whose proxy does not
 *      forward those headers.
 *   3. `request.nextUrl.origin` — correct with no proxy in front (local `npm start`, `next dev`).
 *
 * This mirrors `lib/links.ts`, which exists for the *same class of bug* one layer over: an internal
 * Docker service name leaking into an `href` a browser had to resolve. Two occurrences of "an
 * internal address escaped into something client-facing" is the pattern worth naming.
 */
function publicUrl(request: NextRequest, path: string): URL {
  const forwardedHost = request.headers.get("x-forwarded-host");
  if (forwardedHost) {
    // `x-forwarded-host` can be a comma-separated chain when several proxies are in play; the
    // left-most entry is what the client actually asked for.
    const host = forwardedHost.split(",")[0]!.trim();
    const proto = (request.headers.get("x-forwarded-proto") ?? "https")
      .split(",")[0]!
      .trim();
    return new URL(path, `${proto}://${host}`);
  }

  const configured = process.env.NEXT_PUBLIC_SITE_URL;
  if (configured) return new URL(path, configured);

  return new URL(path, request.nextUrl.origin);
}

/**
 * Where a failure lands. One page, a reason code, no token.
 *
 * `reason` drives the wording rather than being echoed: the message a user needs for "you already
 * used this" is different from "we could not reach the service", and the previous single generic
 * sentence sent people to request a replacement link when the service was simply down.
 */
function failed(request: NextRequest, reason: string): NextResponse {
  return NextResponse.redirect(publicUrl(request, `/auth/link-failed?reason=${reason}`));
}

export async function GET(request: NextRequest): Promise<NextResponse> {
  const params = request.nextUrl.searchParams;
  const token = params.get("token");
  const purpose = params.get("purpose");

  if (!token) return failed(request, "missing");

  // ---- Email confirmation ------------------------------------------------- //
  //
  // `purpose=email` is set by `mailer._verification_url`; a magic link carries no `purpose`. The
  // two token kinds are indistinguishable by shape and BOTH are single-use, so guessing wrong
  // would burn a token the user then cannot replace. The default is the magic link because those
  // were sent before `purpose` existed and are still in inboxes.
  if (purpose === "email") {
    try {
      await api.verifyEmail(token);
    } catch (error) {
      return failed(
        request,
        error instanceof ApiError && error.status >= 500 ? "unavailable" : "spent",
      );
    }

    // No cookie to write on this path — confirming an address does not sign anyone in. Clicking
    // from a phone's mail app usually opens a different browser from the one they signed up in, so
    // there is often no session here; `/auth/login?verified=1` states what succeeded instead of
    // bouncing them off the dashboard with no explanation.
    //
    // Left as a query parameter rather than a cookie deliberately, even though this handler COULD
    // now set one: `verified` names no account and carries no token, and the URL the user just
    // clicked already implies it.
    return NextResponse.redirect(publicUrl(request, "/auth/login?verified=1"));
  }

  // ---- Magic-link sign-in ------------------------------------------------- //
  let session;
  try {
    session = await api.redeemMagicLink(token);
  } catch (error) {
    // A 5xx or an unreachable backend is NOT a spent link, and saying so matters: "request a new
    // one" is useless advice when the service is down, and it costs the user their remaining
    // link for nothing.
    return failed(
      request,
      error instanceof ApiError && error.status >= 500 ? "unavailable" : "spent",
    );
  }

  // `session.next` is the backend-sanitised path from the link, so it cannot be an absolute URL
  // or a protocol-relative `//evil.example` — that check belongs upstream and is done there.
  //
  // The verification gate is still applied: a magic link proves the person controls the mailbox it
  // was sent to, which is not the same as the account's own address being confirmed.
  const fallback = session.next || "/dashboard";
  const destination = session.account?.email_verified === false
    ? "/auth/pending?reason=signin"
    : fallback;

  const response = NextResponse.redirect(publicUrl(request, destination));
  // **The line the page could not run.** A handler owns its response, so `Set-Cookie` is available.
  response.cookies.set(
    SESSION_COOKIE,
    session.access_token,
    sessionCookieOptions(session.expires_in),
  );
  return response;
}
