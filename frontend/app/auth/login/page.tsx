import type { Metadata } from "next";


import SignInPanel from "./SignInPanel";

export const metadata: Metadata = {
  title: "Sign in",
  description:
    "Sign in to SHELTER — satellite-enabled and AI-powered early warning for flood, crop and health risk.",
  // A sign-in page has no value in search results and appearing there invites
  // credential-phishing lookalikes to rank beside it.
  robots: { index: false, follow: false },
};

/**
 * The "just verified" notice arrives as `?verified=1` from `/auth/verify`.
 *
 * It is sent when someone confirmed their address in a browser holding no session — the normal
 * case, because a link opened from a phone's mail app is usually a different browser from the one
 * they signed up in. Without an acknowledgement here a successful confirmation would look like a
 * failure: they clicked a link and got a login form.
 *
 * It was a one-shot httpOnly cookie, to keep the notice out of browser history and Referer
 * headers. That could not work: writing a cookie during a page render throws
 * ("Cookies can only be modified in a Server Action or Route Handler"), and `takeFlash`
 * DELETES on read, which is equally a modification. A query parameter carries it instead —
 * `verified` names no account and holds no token, so there is nothing here worth hiding, and a
 * reload without the parameter simply does not show the notice, which is the one-shot behaviour
 * the cookie was for.
 *
 * `?reason=suspended` comes from the dashboard gate; `?ended=idle` from the session guard.
 */
export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{
    reason?: string;
    next?: string;
    ended?: string;
    verified?: string;
  }>;
}) {
  const params = await searchParams;

  // `?verified=1` from /auth/verify, NOT a cookie.
  //
  // This called `takeFlash()`, which reads the cookie and then DELETES it — and a delete is
  // a modification, so it throws the same "Cookies can only be modified in a Server Action
  // or Route Handler" that broke the verify page. It never fired only because the cookie was
  // never successfully set; fixing the writer would have moved the 500 to this page.
  //
  // The notice is not sensitive — it names no account and carries no token — so a query
  // parameter is the right carrier. It is also self-clearing: a reload without the parameter
  // simply does not show it, which is the one-shot behaviour the cookie was for.
  const justVerified = params.verified === "1";

  return (
    <SignInPanel
      // The real configured value, not a hardcoded 15. `IAM_IDLE_TIMEOUT_MINUTES` is a
      // setting, so a deployment that lengthens it would otherwise have this page state a
      // number that is simply untrue — and the whole point of naming the reason is to be
      // believed.
      idleMinutes={Number(process.env.IAM_IDLE_TIMEOUT_MINUTES ?? 15) || 15}
      justVerified={justVerified}
      idleEnded={params.ended === "idle"}
      suspended={params.reason === "suspended"}
    />
  );
}
