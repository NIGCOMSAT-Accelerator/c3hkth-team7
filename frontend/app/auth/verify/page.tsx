import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { redeemMagicLink, redeemVerification } from "@/app/auth/actions";
import { getAccount } from "@/lib/session";

export const metadata: Metadata = {
  title: "Confirming your link",
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

/**
 * Landing page for both kinds of emailed link.
 *
 * ## Two token types, one route, and why `purpose` is explicit
 *
 * A magic link signs someone in; a confirmation link marks their address verified. Both
 * arrive here as `?token=…` and the two are indistinguishable by shape. Guessing wrong
 * matters because **both are single-use** — attempting a magic-link redemption on a
 * verification token would burn it, and the user's only recovery would be to request
 * another. So the sender states the kind:
 *
 *   * `?purpose=email` — from `mailer._verification_url`. Confirms the address.
 *   * no `purpose`     — a magic link, from `passwordless.magic_link_url`. Signs in.
 *
 * The default is the magic link because those were sent before `purpose` existed and are
 * still in inboxes now.
 *
 * ## Redemption happens server-side during the render
 *
 * The token is exchanged before any HTML reaches the browser, so it never enters client
 * JavaScript. On success this page is not displayed at all — the redirect happens first.
 * Only the failure path renders, which is why the copy is entirely about recovery.
 */
export default async function VerifyPage({
  searchParams,
}: {
  searchParams: Promise<{ token?: string; purpose?: string }>;
}) {
  const { token, purpose } = await searchParams;

  if (!token) {
    return (
      <Failure message="This page needs the link from your email. Request a new one below." />
    );
  }

  // ---- Email confirmation -------------------------------------------------- //
  if (purpose === "email") {
    const result = await redeemVerification(token);

    if (result.ok) {
      // Where they land depends on whether this browser holds a session. Clicking from a
      // phone's mail app usually opens a *different* browser from the one they signed up
      // in, so there is often no cookie here — and dropping such a user at the dashboard
      // would bounce them to sign-in with no explanation of what just succeeded.
      const account = await getAccount();

      // ## The flash is a QUERY PARAMETER, not a cookie, and it has to be
      //
      // This was `await setFlash("verified")`, and it threw on every successful
      // confirmation:
      //
      //     Error: Cookies can only be modified in a Server Action or Route Handler
      //
      // ...which Next.js renders as "A server error occurred. Reload to try again." So a
      // brand-new subscriber received a welcome email, clicked the link, saw a hard 500 —
      // and their address WAS confirmed, because the API call succeeds before this line.
      // The one-time token was therefore already spent, so reloading could not help and
      // requesting a new link produced the same crash. Signup looked broken at the exact
      // moment it had worked.
      //
      // `cookies().set()` is unavailable during Server Component rendering by design:
      // headers may already have been streamed, so there is no response left to attach a
      // `Set-Cookie` to. A page render cannot write one, no matter how it is wrapped.
      //
      // The honest fix is to carry the notice in the redirect itself. A cookie was chosen
      // to keep it out of browser history and Referer headers, which was a real concern —
      // but `verified` is not sensitive: it names no account, carries no token, and is
      // already implied by the URL the user just clicked. The token in *this* page's URL is
      // the thing worth protecting, and it stays behind on the redirect.
      const destination = account ? "/dashboard" : "/auth/login";
      redirect(`${destination}?verified=1`);
    }

    return (
      <Failure
        message={result.message}
        // Signed in but the link failed (expired, already used) — send them back to the
        // page that can issue a fresh one, rather than to a sign-in form they do not need.
        actionHref="/auth/pending"
        actionLabel="Send me a new confirmation link"
      />
    );
  }

  // ---- Magic-link sign-in -------------------------------------------------- //
  const result = await redeemMagicLink(token);
  return <Failure message={result.message} />;
}

function Failure({
  message,
  actionHref = "/auth/login",
  actionLabel = "Request a new sign-in link",
}: {
  message: string;
  actionHref?: string;
  actionLabel?: string;
}) {
  return (
    <>
      <h1 className="authpanel__title">That link did not work</h1>
      <p className="authpanel__lede">{message}</p>
      <p className="authpanel__alt">
        <a href={actionHref}>{actionLabel}</a>
      </p>
    </>
  );
}
