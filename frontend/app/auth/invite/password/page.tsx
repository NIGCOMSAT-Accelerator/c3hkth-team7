import { redirect } from "next/navigation";

import { AuthForm } from "@/components/AuthForm";
import PasswordField from "@/components/PasswordField";
import { getSessionToken } from "@/lib/session";

import { setFirstPassword } from "../actions";

export const metadata = { title: "Choose your password" };
export const dynamic = "force-dynamic";

/**
 * Choose the first password, holding a `SCOPE_SET_PASSWORD` session.
 *
 * ## This page is a convenience, not the control
 *
 * The backend refuses that scoped session on every other route — `current_account` raises 403
 * and only `password_setup_session` opts out. So a member who navigates away cannot reach
 * anything; this page simply means they see a form instead of a wall of refusals.
 *
 * The cookie check below is likewise courtesy: without it someone arriving here directly gets
 * a form that fails on submit, which reads as broken rather than as "your setup expired".
 */
export default async function FirstPasswordPage() {
  const session = await getSessionToken();
  if (!session) {
    // No setup session — they arrived here directly, or their 15 minutes lapsed. Sent to
    // sign-in rather than to the invite page, which needs a token they no longer have.
    redirect("/auth/login?reason=setup-expired");
  }

  return (
    <main className="authpanel">
      <h1 className="authpanel__title">Choose your password</h1>
      <p className="authpanel__lede">
        Your invitation has been used and cannot be used again. Set a password now and you will
        be signed in — from then on this is how you reach SHELTER.
      </p>

      <AuthForm
        action={setFirstPassword}
        submitLabel="Save and continue"
        pendingLabel="Saving…"
      >
        {/* Same live validation and breach screening as signup. It matters more here: the
            invitation is already spent, so a password rejected after submit would leave them
            holding nothing and needing a fresh invitation. */}
        <PasswordField hint="Three unrelated words is strong and easy to remember. You can enable two-factor authentication once you are in." />
      </AuthForm>

      <p className="authform__hint">
        You have 30 minutes to finish. If it lapses, open your invitation link again — it stays
        valid for 14 days from when it was sent.
      </p>
    </main>
  );
}
