import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { redeemMagicLink, redeemVerification } from "@/app/auth/actions";
import { getAccount, setFlash } from "@/lib/session";

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
      // A one-shot httpOnly cookie rather than a query parameter: nothing about the
      // session ends up in browser history, Referer headers or CDN logs. See setFlash.
      await setFlash("verified");
      const account = await getAccount();
      redirect(account ? "/dashboard" : "/auth/login");
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
