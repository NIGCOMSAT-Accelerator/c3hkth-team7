"use client";

import { useState } from "react";

import { resendVerification, signOut } from "@/app/auth/actions";

/**
 * The confirm-your-email panel.
 *
 * ## What makes this a waiting room rather than a dead end
 *
 * A bare "check your email" screen is where signups die: the user has no idea which
 * address it went to, no way to fix a typo, no way to resend, and nothing to do if it
 * never arrives. So this page carries four things:
 *
 *   1. **The address, shown back.** The most common failure is a typo, and it is invisible
 *      unless we display what was actually stored.
 *   2. **Resend**, with the result reported in place.
 *   3. **"I've confirmed it" ** — a plain link back to the dashboard. The gate re-checks
 *      server-side, so this cannot bypass anything; it just saves someone who verified in
 *      their mail app from wondering how to get back.
 *   4. **A way out** — wrong address, so sign out and start again. Without it, a mistyped
 *      address is a permanently stuck account.
 *
 * ## Why there is no polling
 *
 * The obvious move is to poll `/iam/me` every few seconds and redirect on success. Two
 * reasons not to: it spends the subscriber's data on a page whose whole job is to wait,
 * and the verification link opens in the mail app's browser, which is frequently a
 * *different* browser — so the tab that polls is often not the tab that would benefit.
 * A visible button the user controls is honest about what has to happen.
 */
export default function PendingPanel({
  email,
  firstName,
  isCommercial,
  justSignedUp,
  sendFailed,
  senderEmail,
  cameFromSignIn = false,
}: {
  email: string;
  firstName: string;
  isCommercial: boolean;
  justSignedUp: boolean;
  sendFailed: boolean;
  /**
   * The address SHELTER actually sends from, passed in from the server.
   *
   * Passed rather than hardcoded because this string was wrong: it read
   * `alerts@shelter.zerorate.io`, an address that does not exist, so anyone who
   * allow-listed it would still have had their warnings filtered — and the advice looked
   * authoritative enough that nobody would question it. The real sender is
   * `BREVO_SENDER_EMAIL` / `SMTP_FROM` in the backend's environment.
   *
   * Threading it through means there is one place to change when the sending domain moves,
   * instead of a copy in the UI that silently disagrees with what Brevo puts in the From
   * header.
   */
  senderEmail: string;
  /** True when they were routed here from a sign-in attempt rather than from signup. */
  cameFromSignIn?: boolean;
}) {
  const [state, setState] = useState<{ ok: boolean; message: string } | null>(
    // A failed send is surfaced immediately rather than waiting for the user to press
    // resend — they would otherwise sit watching an inbox nothing was sent to.
    sendFailed
      ? {
          ok: false,
          message:
            "We could not send the confirmation email just now. Please try again below.",
        }
      : null,
  );
  const [pending, setPending] = useState(false);

  async function onResend() {
    setPending(true);
    const result = await resendVerification();
    setState({ ok: result.ok, message: result.message });
    setPending(false);
  }

  return (
    <>
      {/* An icon, not just a heading: this page is reached in a slightly anxious moment
          ("did my signup work?"), and an envelope with a tick answers that before the
          text is read. Inline SVG, currentColor, so it themes and costs nothing. */}
      <div className="pendingmark" aria-hidden="true">
        <svg viewBox="0 0 48 48" width="46" height="46">
          <rect
            x="5"
            y="12"
            width="38"
            height="26"
            rx="4"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
          />
          <path
            d="M5 15l19 13 19-13"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
          />
          <circle cx="38" cy="34" r="9" fill="var(--surface)" />
          <circle cx="38" cy="34" r="8" fill="none" stroke="currentColor" strokeWidth="2" />
          <path
            d="M34.5 34l2.5 2.5 4.5-4.5"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </div>

      {/* Three arrival routes, three openings. A returning user told "Almost there!" as
          though they had just signed up would reasonably wonder whether their earlier
          signup was lost. */}
      <h1 className="authpanel__title">
        {justSignedUp
          ? `Almost there, ${firstName}`
          : cameFromSignIn
            ? `Welcome back, ${firstName}`
            : "Confirm your email to continue"}
      </h1>

      <p className="authpanel__lede">
        {justSignedUp
          ? "Your account is created. One last step before the dashboard opens."
          : cameFromSignIn
            ? "You are signed in, but your email address is still unconfirmed — so the " +
              "dashboard stays closed and we are holding all alerts."
            : "Your account is waiting on one step."}
      </p>

      {/*
        The address, shown back. This is the line that catches a typo, so it gets its own
        line and its own weight rather than being run into the sentence.

        The trailing space in the label is not cosmetic: these are two inline-level nodes,
        so without it the accessible name and anything copied to the clipboard read
        "confirmation link tolionel@freepass.africa". A screen reader announces exactly
        that string.
      */}
      <div className="pendingbox">
        <span className="pendingbox__label">We sent a confirmation link to </span>
        <strong className="pendingbox__email">{email}</strong>
      </div>

      {/*
        Why we ask, stated plainly. "Verify your email" with no reason reads as a hoop;
        naming the consequence — an alert that cannot reach you — makes it obviously in the
        user's own interest.
      */}
      <p className="authpanel__note">
        {isCommercial
          ? "Confirming your address is how we know API-key notices and service " +
            "correspondence will reach your team. It also confirms a real person is " +
            "behind the account."
          : "A hazard warning is only useful if it reaches you. Confirming your address " +
            "is how we know it will — and it tells us a real person is behind the " +
            "account, not an automated signup."}
      </p>

      {state && (
        // Same element and tones AuthForm uses, so a resend result looks identical to
        // every other form result in the flow rather than introducing a second style of
        // message. `assertive` for failures, `polite` for success — matching that
        // component's reasoning.
        <div
          className="authform__message"
          data-tone={state.ok ? "ok" : "error"}
          role={state.ok ? "status" : "alert"}
          aria-live={state.ok ? "polite" : "assertive"}
        >
          {state.message}
        </div>
      )}

      <div className="pendingactions">
        {/* Primary action is continuing, not resending: most people will have the email
            already, and the common path should be the prominent one. The gate re-checks
            server-side, so this is a convenience and never a bypass. */}
        <a href="/dashboard" className="btn btn--primary pendingactions__go">
          I&rsquo;ve confirmed — continue
        </a>

        <button
          type="button"
          onClick={onResend}
          disabled={pending}
          className="btn btn--ghost"
        >
          {pending ? "Sending…" : "Resend the email"}
        </button>
      </div>

      <div className="pendinghelp">
        <p className="authform__hint">
          Not arrived after a minute or two? Check your spam or promotions folder — and
          add <strong>{senderEmail}</strong> to your contacts so future alerts are not
          filtered.
        </p>
        {/*
          The escape hatch. Without it a mistyped address is an account nobody can rescue
          without support — and "wrong address" is the single most likely reason someone is
          stuck on this page.
        */}
        <form action={signOut}>
          <button type="submit" className="linkbutton">
            Wrong address? Sign out and start again
          </button>
        </form>
      </div>
    </>
  );
}
