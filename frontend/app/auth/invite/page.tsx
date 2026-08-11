import { AuthForm } from "@/components/AuthForm";

import { redeemInvitation } from "./actions";

export const metadata = { title: "Team invitation" };
export const dynamic = "force-dynamic";

/**
 * Accept a team invitation. One link, one credential, no temporary password.
 *
 * ## Why the token is not redeemed during this render
 *
 * It is single-use. Consuming it on a GET would let a mail scanner, a link preview, or a
 * corporate URL-rewriting proxy burn the invitation before the person ever clicked — and
 * that failure is unrecoverable, because the token is already gone. So this page renders a
 * button, and the POST is what redeems.
 *
 * ## Why there is no sign-in step
 *
 * The earlier version required an account first, so the real journey was: read invite →
 * discover you need an account → sign up → verify that email → come back → accept. Redeeming
 * now *creates* the account, with no password hash, and hands back a session scoped to setting
 * one. The invitation email already proved the address, so asking them to verify it again
 * would be proving the same fact twice.
 */
export default async function InvitePage({
  searchParams,
}: {
  searchParams: Promise<{ token?: string }>;
}) {
  const { token } = await searchParams;

  if (!token) {
    return (
      <main className="authpanel">
        <h1 className="authpanel__title">Invitation link incomplete</h1>
        <p className="authpanel__lede">
          This link is missing its token. Open the link from your invitation email again, or
          ask your colleague to resend it.
        </p>
      </main>
    );
  }

  return (
    <main className="authpanel">
      <h1 className="authpanel__title">You have been invited to SHELTER</h1>
      <p className="authpanel__lede">
        Accepting takes you straight to choosing your own password — there is no temporary
        password to type, and this link works only once.
      </p>

      <AuthForm
        action={redeemInvitation}
        submitLabel="Accept and choose my password"
        pendingLabel="Opening…"
      >
        {/* Hidden field rather than read from the URL by client code, so the token is
            submitted with the form and never handled in JavaScript an injected script
            could read. */}
        <input type="hidden" name="token" value={token} />
      </AuthForm>

      <p className="authform__hint">
        Invitations are valid for 14 days. If yours has expired, ask the colleague who invited
        you to send another — the new one replaces the old.
      </p>
    </main>
  );
}
