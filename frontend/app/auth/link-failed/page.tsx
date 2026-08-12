import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "That link did not work",
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

/**
 * Where `GET /auth/verify` sends someone when their emailed link could not be used.
 *
 * ## Why this is a separate page from the handler
 *
 * `app/auth/verify/route.ts` is a Route Handler, so it returns a `Response` rather than HTML. It
 * needs somewhere to send a failure, and a redirect has the useful side effect of **dropping the
 * token from the address bar** — so a user who then hits reload, or shares the URL asking for help,
 * is not carrying a credential around.
 *
 * ## Three reasons, three messages, and why one message was not enough
 *
 * The old page said the same sentence for every failure: *"That sign-in link is invalid, already
 * used, or has expired. Request a new one."* That wording is what made the magic-link bug so hard
 * to report — it was displayed for a token the backend had **just accepted**, and it told the user
 * to do the one thing that could not help.
 *
 * Separating the reasons matters beyond that specific bug:
 *
 *   * `spent` — genuinely used or expired. "Request a new one" is correct advice.
 *   * `unavailable` — the API was unreachable or returned 5xx. The link may be perfectly good, and
 *     telling someone to request a replacement burns a working link for nothing.
 *   * `missing` — no token in the URL, usually a mail client that truncated it or a copy-paste that
 *     lost the query string. Retyping the address will not help; the link has to be re-opened.
 *
 * No reason code is echoed into the page beyond selecting a message, and no token is ever accepted
 * here — this page performs no redemption at all, which is what keeps it safe to link to.
 */
export default async function LinkFailedPage({
  searchParams,
}: {
  searchParams: Promise<{ reason?: string }>;
}) {
  const { reason } = await searchParams;

  const { title, message, actionHref, actionLabel } = copyFor(reason);

  return (
    <>
      <h1 className="authpanel__title">{title}</h1>
      <p className="authpanel__lede">{message}</p>
      <p className="authpanel__alt">
        <a href={actionHref}>{actionLabel}</a>
      </p>
    </>
  );
}

function copyFor(reason: string | undefined): {
  title: string;
  message: string;
  actionHref: string;
  actionLabel: string;
} {
  if (reason === "unavailable") {
    // Deliberately does NOT say the link is bad, because it probably is not. Sending someone to
    // request a replacement here would spend a good link and hit the same outage.
    return {
      title: "We could not check that link",
      message:
        "Your link is likely fine — we could not reach the service to check it. Wait a moment " +
        "and open the link from your email again.",
      actionHref: "/api/status",
      actionLabel: "Check service status",
    };
  }

  if (reason === "missing") {
    return {
      title: "That link is incomplete",
      message:
        "The address is missing its confirmation code. Some mail apps shorten long links — open " +
        "the message again and tap the link itself rather than copying the address.",
      actionHref: "/auth/login",
      actionLabel: "Request a new sign-in link",
    };
  }

  return {
    title: "That link has already been used",
    // "Already been used" leads, because it is by far the commonest true cause: these links are
    // single-use, and mail clients and security scanners routinely fetch a URL before the human
    // taps it. Expiry is mentioned second rather than first.
    message:
      "Sign-in links work once and then expire. If you have already signed in on this device you " +
      "are all set; otherwise request a fresh link and open it straight away.",
    actionHref: "/auth/login",
    actionLabel: "Request a new sign-in link",
  };
}
