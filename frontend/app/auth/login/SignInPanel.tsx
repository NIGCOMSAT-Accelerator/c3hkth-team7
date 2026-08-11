"use client";

import { useState } from "react";

import { requestMagicLink, signIn, verifyMfa } from "@/app/auth/actions";
import { AuthForm, Field } from "@/components/AuthForm";
import SecretField from "@/components/SecretField";

/**
 * Sign-in, with three routes to the same place.
 *
 * ## Why magic link is offered first
 *
 * The default tab is the emailed link, not the password. That is a product decision about
 * who this is for: a farmer on a shared handset, possibly with limited literacy in the
 * interface language, on a metered connection. A forgotten password there is a support
 * call; a link in an inbox they already check is not.
 *
 * Password sign-in stays a peer option rather than being hidden, because an aggregator's
 * operator signs in several times a day and wants their password manager to work.
 *
 * ## Why MFA is a state transition, not a separate page
 *
 * When the password is right but a second factor is required, the form swaps in place and
 * keeps the challenge token. A separate route would mean a refresh loses the challenge and
 * sends the user back through the password step for a mistyped digit.
 */

type Mode = "link" | "password";

export default function SignInPanel({
  justVerified = false,
  idleEnded = false,
  idleMinutes = 15,
  suspended = false,
}: {
  /** Set when the user just confirmed their address in this (session-less) browser. */
  justVerified?: boolean;
  /** Set when the previous session ended through the idle timeout rather than a sign-out. */
  idleEnded?: boolean;
  /** The configured idle window, so the message states the real number rather than a
   *  hardcoded one that a changed setting would make untrue. */
  idleMinutes?: number;
  suspended?: boolean;
} = {}) {
  const [mode, setMode] = useState<Mode>("link");
  // Held here rather than in the action's state so a failed code attempt does not
  // discard it — retyping the password because of one wrong digit is the fastest way to
  // make someone abandon 2FA.
  const [challenge, setChallenge] = useState<string | null>(null);

  if (challenge) {
    return (
      <>
        <h1 className="authpanel__title">Enter your code</h1>
        <p className="authpanel__lede">
          Open your authenticator app and enter the 6-digit code. If you have lost the
          device, use one of your recovery codes instead.
        </p>

        <AuthForm
          action={verifyMfa}
          submitLabel="Verify and sign in"
          pendingLabel="Verifying…"
          footer={
            <button
              type="button"
              className="authpanel__link"
              onClick={() => setChallenge(null)}
            >
              Start again
            </button>
          }
        >
          <input type="hidden" name="challenge" value={challenge} />
          <Field
            name="code"
            label="Authentication code"
            // `one-time-code` lets iOS and Android offer the code from the keyboard,
            // which removes an app-switch on the device generating it.
            autoComplete="one-time-code"
            inputMode="numeric"
            placeholder="123456"
            hint="6 digits, or a recovery code like A7K2-M9P4"
          />
        </AuthForm>
      </>
    );
  }

  return (
    <>
      {/* Confirmation succeeded, but in a browser holding no session — the usual outcome
          when the link is opened from a phone's mail app. Saying so turns "I clicked the
          link and got a login form" into "that worked, now sign in". */}
      {justVerified && (
        <div className="authform__message" data-tone="ok" role="status">
          Your email address is confirmed. Sign in to open your dashboard.
        </div>
      )}

      {/* Naming the reason matters. Without it an idle timeout is indistinguishable from a
          hijacked account or a bug, and "why am I suddenly logged out?" is alarming on a
          service people rely on for hazard warnings. */}
      {idleEnded && (
        <div className="authform__message" data-tone="ok" role="status">
          You were signed out after {idleMinutes} minutes of inactivity, to protect your
          account. <strong>Monitoring never stopped</strong> — SHELTER keeps watching every
          area on each satellite pass, and alerts keep being delivered, whether or not anyone
          is signed in. Sign in to see the detail.
        </div>
      )}

      {suspended && (
        <div className="authform__message" data-tone="error" role="alert">
          This account is suspended. Contact support to restore access.
        </div>
      )}

      <h1 className="authpanel__title">Sign in to SHELTER</h1>
      <p className="authpanel__lede">
        Your areas keep being monitored whether or not you are signed in — this is for
        seeing the detail and changing your preferences.
      </p>

      <div className="authtabs" role="tablist" aria-label="Sign-in method">
        <button
          type="button"
          role="tab"
          aria-selected={mode === "link"}
          className="authtabs__tab"
          onClick={() => setMode("link")}
        >
          Email me a link
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={mode === "password"}
          className="authtabs__tab"
          onClick={() => setMode("password")}
        >
          Use a password
        </button>
      </div>

      {mode === "link" ? (
        <AuthForm
          action={requestMagicLink}
          submitLabel="Send me a sign-in link"
          pendingLabel="Sending…"
        >
          {(state) =>
            state.emailSent ? (
              <p className="authpanel__lede" style={{ marginTop: 0 }}>
                Check your inbox. The link works once and expires in 15 minutes.
              </p>
            ) : (
              <Field
                name="email"
                label="Email address"
                type="email"
                autoComplete="email"
                inputMode="email"
                placeholder="you@example.com"
                hint="No password needed. We email you a link that signs you in."
              />
            )
          }
        </AuthForm>
      ) : (
        <AuthForm
          action={(prev, data) =>
            signIn(prev, data).then((next) => {
              if (next.mfaChallenge) setChallenge(next.mfaChallenge);
              return next;
            })
          }
          submitLabel="Sign in"
          pendingLabel="Signing in…"
          footer={
            <a className="authpanel__link" href="/auth/forgot">
              Forgot your password?
            </a>
          }
        >
          <Field
            name="email"
            label="Email address"
            type="email"
            autoComplete="email"
            inputMode="email"
          />
          {/* Padlock left, show/hide eye right. The eye is not cosmetic on a phone: a
              masked field gives no feedback, so a mistyped passphrase on a cramped keyboard
              reads as "wrong password" and sends the user to a reset they did not need. */}
          <SecretField autoComplete="current-password" />
        </AuthForm>
      )}

      <p className="authpanel__alt">
        New to SHELTER? <a href="/auth/signup">Create an account</a>
      </p>
    </>
  );
}
