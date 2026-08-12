"use server";

import { redirect } from "next/navigation";

import { api, ApiError } from "@/lib/api";
import { clearSession, getSessionToken, setSession } from "@/lib/session";
import type { Account, LoginChallenge, SessionToken } from "@/lib/types";

/**
 * Auth Server Actions.
 *
 * Every one runs on the server, so a password never enters the client bundle and the
 * session token is written straight to an httpOnly cookie without passing through
 * JavaScript the browser can read.
 *
 * ## The rule these all follow
 *
 * **A failure never says whether the account exists.** "Email or password is incorrect"
 * covers both a wrong password and an unknown address; magic-link and reset requests
 * return the same acknowledgement either way. Otherwise these forms are free
 * account-enumeration oracles over a list of farmers in named districts — which has
 * value to people other than us, and physical consequences for them.
 *
 * The backend enforces this too (identical response, matched timing). Repeating it here
 * means a future UI change cannot leak what the API is careful not to.
 */

export interface AuthState {
  ok: boolean;
  message: string;
  /** Set when the password was right but a second factor is needed. */
  mfaChallenge?: string;
  /** Set on signup so the UI can tell the user to check their inbox — or that we
   *  could not send, which they need to know before waiting for an email. */
  emailSent?: boolean;
}

const GENERIC_FAILURE =
  "Something went wrong on our side. Nothing was saved — please try again.";

const UNAVAILABLE =
  "We could not reach the sign-in service just now. Please try again in a moment.";

function isChallenge(
  value: SessionToken | LoginChallenge,
): value is LoginChallenge {
  return "mfa_required" in value && value.mfa_required === true;
}

/**
 * Where to send someone immediately after a session is issued.
 *
 * ## Why this exists rather than every path redirecting to `/dashboard`
 *
 * Five separate flows create a session — password sign-in, MFA completion, magic link,
 * password reset, and signup — and each chose its own destination. Four of them sent the
 * user to `/dashboard` and relied on `requireVerifiedAccount` to bounce an unverified
 * account onward to `/auth/pending`.
 *
 * That *worked*, but it was the wrong shape for three reasons:
 *
 *   1. **Two redirects instead of one.** On a slow rural connection that is a visible
 *      double navigation, and the intermediate page is one the user has no business
 *      seeing.
 *   2. **It relied on the gate never being forgotten.** A sixth session path added later
 *      would land an unverified account on the dashboard unless whoever wrote it
 *      remembered — the failure would be silent and only on unverified accounts, which
 *      are exactly the ones nobody tests with.
 *   3. **The reason was lost.** Arriving at `/auth/pending` via a bounce gives no signal
 *      about *why*, so the page could not say "you still need to confirm your address" as
 *      the first thing a returning user reads.
 *
 * Centralising it means the decision is made once, from the account the backend just
 * returned, and every caller gets it.
 *
 * **This is a convenience, not the control.** `requireVerifiedAccount` still gates every
 * protected page server-side, and the backend's `verified_account` dependency still
 * refuses anything that would send a message to an unconfirmed address. Someone who types
 * `/dashboard` directly is still redirected. This only removes the detour.
 */
function destinationFor(account: Account | undefined, fallback = "/dashboard"): string {
  // No account in the response — take the fallback. The gate on the destination will
  // sort it out; guessing "pending" here could strand a verified user.
  if (!account) return fallback;

  if (!account.email_verified) {
    // `?reason=signin` lets the page lead with the right sentence: someone who just tried
    // to sign in needs "you still need to confirm", not "almost there, welcome".
    return "/auth/pending?reason=signin";
  }
  return fallback;
}

/**
 * Joins the country dialling code and the local number into one E.164 string.
 *
 * `PhoneField` submits `phone_country` (`+234`) and `phone_local` (`803 123 4567`)
 * separately, so the join happens here rather than in the browser. That is what lets the
 * component ship no JavaScript: a hidden input mirrored by client code would submit an
 * empty number if the bundle failed to load, on exactly the low-end devices this is for.
 *
 * The leading zero is stripped because subscribers type their number the way they say it
 * ("oh-eight-oh-three…") and `+234` + `0803…` is not a valid number. Doing it here rather
 * than relying on the backend matters: the backend's own fallback prepends `+234` to any
 * bare local number, which is right for Nigeria and silently wrong for the other 53
 * countries — sending an explicit, complete E.164 string means that path is never taken.
 *
 * Returns null for an empty number, because phone is optional throughout — an email-only
 * subscriber is fully functional, and `+234` alone must not be stored as a number.
 */
function joinPhone(formData: FormData): string | null {
  const local = String(formData.get("phone_local") ?? "").replace(/[^\d]/g, "");
  if (!local) return null;

  const dial = String(formData.get("phone_country") ?? "").trim() || "+234";
  return `${dial}${local.replace(/^0+/, "")}`;
}

/** Maps an ApiError onto copy a subscriber can act on, never a stack trace. */
function describe(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    if (error.status === 503) return UNAVAILABLE;
    if (error.status === 429) return error.message;
    // 4xx messages from the backend are written for end users (they name the missing
    // scope, the expired link, the failed validation), so they are surfaced as-is.
    if (error.status < 500) return error.message;
  }
  return fallback;
}

// --------------------------------------------------------------------------- //
// Sign in
// --------------------------------------------------------------------------- //

export async function signIn(
  _prev: AuthState,
  formData: FormData,
): Promise<AuthState> {
  const email = String(formData.get("email") ?? "").trim();
  const password = String(formData.get("password") ?? "");

  if (!email || !password) {
    return { ok: false, message: "Enter your email address and password." };
  }

  let result: SessionToken | LoginChallenge;
  try {
    result = await api.login(email, password);
  } catch (error) {
    return {
      ok: false,
      message: describe(error, "Email or password is incorrect."),
    };
  }

  if (isChallenge(result)) {
    // Deliberately NOT a session yet. The challenge token cannot read account data, so
    // an intercepted one is useless without the code.
    return {
      ok: true,
      message: result.detail,
      mfaChallenge: result.challenge_token,
    };
  }

  await setSession(result.access_token, result.expires_in);
  // `redirect` throws internally, so nothing after it runs — which is why the session is
  // set first.
  //
  // An unverified account goes straight to /auth/pending rather than bouncing off the
  // dashboard gate. The backend sends nothing to an unconfirmed address, so signing in
  // without finishing that step leaves the account inert — the page that can resend the
  // link is the only useful destination.
  redirect(destinationFor(result.account));
}

export async function verifyMfa(
  _prev: AuthState,
  formData: FormData,
): Promise<AuthState> {
  const challenge = String(formData.get("challenge") ?? "");
  const code = String(formData.get("code") ?? "").trim();

  if (!challenge) {
    return {
      ok: false,
      message: "That sign-in attempt expired. Please start again.",
    };
  }

  let session;
  try {
    session = await api.verifyMfa(challenge, code);
    await setSession(session.access_token, session.expires_in);
  } catch (error) {
    return {
      ok: false,
      // The challenge token is preserved so a mistyped code does not force the user
      // back through the password step.
      mfaChallenge: challenge,
      message: describe(
        error,
        "That code did not match. Check your device clock, or use a recovery code.",
      ),
    };
  }

  redirect(destinationFor(session.account));
}

// --------------------------------------------------------------------------- //
// Passwordless
// --------------------------------------------------------------------------- //

export async function requestMagicLink(
  _prev: AuthState,
  formData: FormData,
): Promise<AuthState> {
  const email = String(formData.get("email") ?? "").trim();
  if (!email) return { ok: false, message: "Enter your email address." };

  try {
    const result = await api.requestMagicLink(email, "/dashboard");
    return { ok: true, message: result.detail, emailSent: true };
  } catch (error) {
    return { ok: false, message: describe(error, GENERIC_FAILURE) };
  }
}

/**
 * Redeems the token from an emailed link.
 *
 * Called from the `/auth/verify` page rather than a form, because the user arrives by
 * clicking — there is nothing to submit. Redirects to the `next` path the backend
 * returns, which it has already sanitised against open redirects.
 */
// `redeemMagicLink` USED TO LIVE HERE, and its removal is the fix for a production bug.
//
// It called `setSession()`, which calls `cookies().set()`. That is legal in a Server Action — but
// this one was invoked from `app/auth/verify/page.tsx` **during a render**, where `cookies()` is a
// sealed proxy whose `set` throws unconditionally. So every successful sign-in raised
// `ReadonlyRequestCookiesError`, the `catch` here swallowed it, and `describe()` reported the
// generic *"That sign-in link is invalid, already used, or has expired."* — for a token the backend
// had just accepted and, being single-use, already deleted. Retrying could not work.
//
// The `"use server"` marker at the top of this file makes every export a Server Action, which is
// what made the mistake invisible: the function genuinely was one, it was just called from the one
// context that cannot write a cookie.
//
// It now lives in `app/auth/verify/route.ts`, a Route Handler, which owns its `Response` and can
// therefore set `Set-Cookie`. Deliberately NOT left here as an unused export: a helper that looks
// available and breaks in the caller a reader is most likely to try is worse than no helper.

// --------------------------------------------------------------------------- //
// Password reset
// --------------------------------------------------------------------------- //

export async function requestPasswordReset(
  _prev: AuthState,
  formData: FormData,
): Promise<AuthState> {
  const email = String(formData.get("email") ?? "").trim();
  if (!email) return { ok: false, message: "Enter your email address." };

  try {
    const result = await api.requestPasswordReset(email);
    return { ok: true, message: result.detail, emailSent: true };
  } catch (error) {
    return { ok: false, message: describe(error, GENERIC_FAILURE) };
  }
}

export async function confirmPasswordReset(
  _prev: AuthState,
  formData: FormData,
): Promise<AuthState> {
  const token = String(formData.get("token") ?? "");
  const password = String(formData.get("password") ?? "");
  const confirm = String(formData.get("confirm") ?? "");

  if (password !== confirm) {
    // Checked here as well as in the browser: client-side validation is a convenience
    // and can be bypassed, and a mismatch that reached the API would set a password the
    // user did not intend.
    return { ok: false, message: "Those passwords do not match." };
  }
  if (password.length < 12) {
    return {
      ok: false,
      message: "Use at least 12 characters. Three unrelated words is strong and easy.",
    };
  }

  let session;
  try {
    session = await api.confirmPasswordReset(token, password);
    await setSession(session.access_token, session.expires_in);
  } catch (error) {
    if (error instanceof Error && error.message === "NEXT_REDIRECT") throw error;
    return { ok: false, message: describe(error, GENERIC_FAILURE) };
  }

  // Signed in immediately. Ending at a login form would make the user type the password
  // they just chose, which is where a typo in the confirm field surfaces as "my new
  // password doesn't work".
  redirect(destinationFor(session.account));
}

// --------------------------------------------------------------------------- //
// Sign up
// --------------------------------------------------------------------------- //

export async function signUpIndividual(
  _prev: AuthState,
  formData: FormData,
): Promise<AuthState> {
  const password = String(formData.get("password") ?? "");
  const confirm = String(formData.get("confirm") ?? "");
  if (password !== confirm) {
    return { ok: false, message: "Those passwords do not match." };
  }

  let response;
  try {
    response = await api.signupIndividual({
      first_name: String(formData.get("first_name") ?? "").trim(),
      last_name: String(formData.get("last_name") ?? "").trim(),
      email: String(formData.get("email") ?? "").trim(),
      phone: joinPhone(formData),
      password,
      language: String(formData.get("language") ?? "en"),
    });
  } catch (error) {
    return { ok: false, message: describe(error, GENERIC_FAILURE) };
  }

  // A session is issued before email verification so the flow can continue straight to
  // choosing a plot. Verification gates *alert delivery*, not navigation — blocking on
  // an inbox round trip is where signup funnels die, and an unverified account cannot
  // cause anything to be sent to a third party.
  if (response.session) {
    await setSession(response.session.access_token, response.session.expires_in);
  }

  // Straight to the pending page in both cases — including when sending failed.
  //
  // The session is already set, so /auth/pending can resend to this address, which is the
  // one thing that recovers a failed send. Returning a message here instead would leave
  // the user on the signup form reading about an email they cannot request again. The
  // `sent=0` flag lets the page lead with "we could not send it" rather than "check your
  // inbox", which would send someone to wait for mail that never left.
  redirect(
    response.verification_email_sent
      ? "/auth/pending?welcome=1"
      : "/auth/pending?welcome=1&sent=0",
  );
}

export async function signUpCommercial(
  _prev: AuthState,
  formData: FormData,
): Promise<AuthState> {
  const password = String(formData.get("password") ?? "");
  const confirm = String(formData.get("confirm") ?? "");
  if (password !== confirm) {
    return { ok: false, message: "Those passwords do not match." };
  }

  let response;
  try {
    response = await api.signupCommercial({
      organisation: String(formData.get("organisation") ?? "").trim(),
      sector: String(formData.get("sector") ?? "cooperative"),
      contact_first_name: String(formData.get("first_name") ?? "").trim(),
      contact_last_name: String(formData.get("last_name") ?? "").trim(),
      email: String(formData.get("email") ?? "").trim(),
      phone: joinPhone(formData),
      password,
    });
  } catch (error) {
    return { ok: false, message: describe(error, GENERIC_FAILURE) };
  }

  if (response.session) {
    await setSession(response.session.access_token, response.session.expires_in);
  }

  // Same gate for aggregators. Arguably more important: an aggregator's address is where
  // API-key notices and billing correspondence go, so an unconfirmed one is a support
  // problem waiting to happen rather than just an undeliverable alert.
  redirect(
    response.verification_email_sent
      ? "/auth/pending?welcome=1&type=commercial"
      : "/auth/pending?welcome=1&type=commercial&sent=0",
  );
}

export async function signOut(): Promise<void> {
  await clearSession();
  redirect("/auth/login");
}

// --------------------------------------------------------------------------- //
// Email verification
// --------------------------------------------------------------------------- //

/**
 * Re-sends the confirmation email to the signed-in account's own address.
 *
 * Requires a session because the backend takes the address from the session rather than
 * from a parameter — which is what stops this being an open relay for spraying mail at
 * arbitrary addresses.
 */
export async function resendVerification(): Promise<AuthState> {
  const token = await getSessionToken();
  if (!token) {
    return {
      ok: false,
      message: "Your session expired. Please sign in again to resend the email.",
    };
  }

  try {
    const result = await api.resendVerification(token);

    // The two branches have DIFFERENT shapes, which is why this is not just
    // `message: result.detail`:
    //
    //   success          -> {"sent": true, "expires_in_hours": 48}     — no `detail`
    //   already verified -> {"sent": false, "detail": "This address…"} — no hours
    //
    // Reading `detail` unconditionally rendered an empty message on the success path,
    // so a subscriber who pressed Resend saw the button finish and nothing else — and
    // pressed it again. The sentence is composed here instead, and the hours come from
    // the response rather than being hardcoded, so the copy cannot disagree with
    // `IAM_VERIFICATION_TTL_HOURS`.
    if (result.sent) {
      const hours = result.expires_in_hours ?? 48;
      return {
        ok: true,
        emailSent: true,
        message: `Sent. Check your inbox for a new confirmation link — it works once and expires in ${hours} hours.`,
      };
    }

    return {
      ok: false,
      emailSent: false,
      // "Already confirmed" is not really a failure, but it is not a send either. The
      // backend's own sentence is used, since it is written for an end user.
      message: result.detail ?? "That address is already confirmed — you can continue.",
    };
  } catch (error) {
    return { ok: false, message: describe(error, GENERIC_FAILURE) };
  }
}

/**
 * Consumes a verification token from an emailed link.
 *
 * Called from `/auth/verify` on arrival rather than behind a button. The click in the
 * mail client *is* the consent; asking for a second click confirms nothing and loses
 * people who assume the link did not work.
 *
 * Returns rather than redirecting, so the page can distinguish "verified, welcome" from
 * "that link expired, here is how to get another" — a bare redirect to the dashboard on
 * failure would leave someone bounced to /auth/pending with no idea why.
 */
// `redeemVerification` was removed alongside `redeemMagicLink`, same reason.
//
// It did not write a cookie itself, so it was not broken — but its only caller was the page render
// that was, and leaving one half of a pair here would invite the next reader to reach for the same
// shape. Email confirmation now happens in `app/auth/verify/route.ts` beside the sign-in path, so
// both token kinds are handled in one place with one set of rules about which is which.
