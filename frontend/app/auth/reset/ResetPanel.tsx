"use client";

import { confirmPasswordReset } from "@/app/auth/actions";
import { AuthForm } from "@/components/AuthForm";
import PasswordField from "@/components/PasswordField";

/**
 * Set a new password from a reset link.
 *
 * The token travels in a hidden field rather than being read from the URL by client code,
 * so it is submitted with the form and never handled in JavaScript that an injected
 * script could read.
 *
 * On success the action signs the user straight in. Ending at a login form would make
 * them type the password they just chose — which is exactly where a typo in the confirm
 * field surfaces as "my new password doesn't work".
 */
export default function ResetPanel({ token }: { token: string }) {
  return (
    <>
      <h1 className="authpanel__title">Choose a new password</h1>
      <p className="authpanel__lede">
        Once you save this, you will be signed in and any other reset links stop working.
      </p>

      <AuthForm
        action={confirmPasswordReset}
        submitLabel="Save and sign in"
        pendingLabel="Saving…"
      >
        <input type="hidden" name="token" value={token} />
        {/* Same live validation as signup. It matters more here: a reset token is
            single-use, so a rejected password after submit can leave the user holding a
            spent link and needing to request another. */}
        <PasswordField />
      </AuthForm>
    </>
  );
}
