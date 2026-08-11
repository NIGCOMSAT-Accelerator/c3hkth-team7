"use server";

import { redirect } from "next/navigation";

import { api } from "@/lib/api";
import { getSessionToken, setSession } from "@/lib/session";

export interface InviteState {
  ok: boolean;
  message: string;
}

/**
 * Redeem an invitation and set the first password.
 *
 * ## Why redemption is a POST from a form, not part of the page render
 *
 * The token is single-use. Consuming it during a GET would let a mail scanner, a link
 * preview, or a corporate URL-rewriting proxy burn the invitation before the person ever
 * clicked — and the failure is unrecoverable without a fresh invitation, because the token is
 * already gone.
 *
 * So the page renders a button, and pressing it is what redeems.
 */
export async function redeemInvitation(
  _prev: InviteState,
  formData: FormData,
): Promise<InviteState> {
  const token = String(formData.get("token") ?? "").trim();
  if (!token) {
    return { ok: false, message: "This invitation link is incomplete." };
  }

  try {
    const redeemed = await api.redeemInvitation(token);
    // The scoped session goes in the same httpOnly cookie as any other, so the token never
    // touches client JavaScript. Its own expiry (15 minutes) governs the cookie, so an
    // abandoned tab does not leave a usable credential behind for the full 12 hours.
    await setSession(redeemed.access_token, redeemed.expires_in);
  } catch (error) {
    if (error instanceof Error && error.message === "NEXT_REDIRECT") throw error;
    // One message for every failure. Whether the token expired, was already used, or never
    // existed is information about invitation state that a holder of a random token should
    // not be able to learn.
    return {
      ok: false,
      message:
        "This invitation is not valid. It may have expired or already been used — ask your " +
        "colleague to send a new one.",
    };
  }

  redirect("/auth/invite/password");
}

export async function setFirstPassword(
  _prev: InviteState,
  formData: FormData,
): Promise<InviteState> {
  const password = String(formData.get("password") ?? "");
  const confirm = String(formData.get("confirm") ?? "");

  // Checked server-side as well as in the browser: client validation is a convenience and can
  // be bypassed, and a mismatch that reached the API would set a password the user did not
  // intend — with no old password to fall back on, since this account has never had one.
  if (password !== confirm) {
    return { ok: false, message: "Those passwords do not match." };
  }
  if (password.length < 12) {
    return {
      ok: false,
      message: "Use at least 12 characters. Three unrelated words is strong and easy.",
    };
  }

  const scoped = await getSessionToken();
  if (!scoped) {
    return {
      ok: false,
      message:
        "Your setup session has expired. Open your invitation link again to continue.",
    };
  }

  try {
    // Returns a NEW, unscoped session. Replacing the cookie is what retires the scoped one —
    // it has a different `jti`, so anything holding the old token can no longer act.
    const session = await api.setFirstPassword(scoped, password);
    await setSession(session.access_token, session.expires_in);
  } catch (error) {
    if (error instanceof Error && error.message === "NEXT_REDIRECT") throw error;
    return {
      ok: false,
      message:
        error instanceof Error
          ? error.message
          : "Could not set your password. Nothing was changed.",
    };
  }

  // Straight into the portal, signed in. Ending at a login form would make them type the
  // password they chose ten seconds ago — which is exactly where a typo in the confirmation
  // field surfaces as "my new password doesn't work".
  redirect("/portal");
}
