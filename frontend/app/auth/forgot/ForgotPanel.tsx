"use client";

import { requestPasswordReset } from "@/app/auth/actions";
import { AuthForm, Field } from "@/components/AuthForm";

/**
 * Forgot-password request.
 *
 * The success message is identical whether or not the address is registered — the
 * backend enforces this, and repeating it here means a future UI change cannot leak what
 * the API is careful not to. Confirming an address exists would turn this form into an
 * enumeration oracle over a list of farmers in named districts.
 *
 * The magic-link alternative is offered prominently: for most subscribers it is the
 * better answer to "I forgot my password", because it needs no new password at all.
 */
export default function ForgotPanel() {
  return (
    <>
      <h1 className="authpanel__title">Reset your password</h1>
      <p className="authpanel__lede">
        Enter your email address and we will send you a link to choose a new password.
        Your current password keeps working until you finish.
      </p>

      <AuthForm
        action={requestPasswordReset}
        submitLabel="Send reset link"
        pendingLabel="Sending…"
        footer={
          <p className="authpanel__alt" style={{ marginTop: 4 }}>
            Would rather not set a password?{" "}
            <a href="/auth/login">Sign in with an emailed link instead</a>.
          </p>
        }
      >
        {(state) =>
          state.emailSent ? (
            <p className="authpanel__lede" style={{ marginTop: 0 }}>
              Check your inbox. The link works once and expires in 1 hour.
            </p>
          ) : (
            <Field
              name="email"
              label="Email address"
              type="email"
              autoComplete="email"
              inputMode="email"
            />
          )
        }
      </AuthForm>

      <p className="authpanel__alt">
        <a href="/auth/login">Back to sign in</a>
      </p>
    </>
  );
}
