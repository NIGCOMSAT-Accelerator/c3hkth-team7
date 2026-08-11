"use client";

import { useActionState, useState } from "react";
import { useFormStatus } from "react-dom";

import PasswordField from "@/components/PasswordField";

import {
  confirmPasswordChange,
  requestPasswordChangeCode,
  type PasswordChangeState,
} from "./actions";

const INITIAL: PasswordChangeState = { ok: false, message: "" };

/**
 * Change your password from inside the portal, confirmed by a code emailed to you.
 *
 * ## Why a code and not just "type the old one"
 *
 * A password alone is phishable, and a stolen session cookie needs no password at all. Requiring a
 * code sent to the registered address means changing the credential also requires control of the
 * mailbox — so someone who has borrowed an unlocked laptop cannot quietly lock the owner out of
 * their own account.
 *
 * The code goes to the address on the account and never to one typed here, which is what makes it
 * proof rather than a formality.
 *
 * ## Why the flow is two explicit steps
 *
 * Asking for the code, the new password and the confirmation all at once looks shorter and is
 * worse: the reader has to leave for their inbox mid-form, and on a phone that means switching
 * apps with a half-filled form behind them. Sending first, then showing a form built around the
 * code, matches what they are actually doing.
 *
 * The current password keeps working throughout, and that is stated at every step — someone who
 * started this by mistake otherwise assumes they are mid-change and locked out.
 */

function Pending({ children, idle }: { children: string; idle: string }) {
  const { pending } = useFormStatus();
  return (
    <button type="submit" className="btn btn--primary" disabled={pending}>
      {pending ? children : idle}
    </button>
  );
}

function Notice({ state }: { state: PasswordChangeState }) {
  if (!state.message) return null;
  return (
    <p
      className="authform__message"
      data-tone={state.ok ? "ok" : "error"}
      role="status"
      aria-live="polite"
    >
      {state.message}
    </p>
  );
}

export default function PasswordChange() {
  const [requestState, request] = useActionState(requestPasswordChangeCode, INITIAL);
  const [confirmState, confirm] = useActionState(confirmPasswordChange, INITIAL);
  const [open, setOpen] = useState(false);

  // A code has been sent, so show the form that uses it. Driven by the action's own result rather
  // than by local state, so a page re-render mid-flow does not lose the step.
  const codeSent = requestState.ok;
  // Finished. The panel collapses back to a button, because leaving a spent form on screen invites
  // a second attempt with a code that no longer exists.
  const done = confirmState.ok;

  if (done) {
    return (
      <div className="pwchange">
        <p className="authform__message" data-tone="ok" role="status">
          {confirmState.message}
        </p>
        <button
          type="button"
          className="btn btn--ghost btn--small"
          onClick={() => {
            setOpen(false);
            // Full reload rather than a client reset: the session cookie was reissued server-side
            // when the password changed, and the page should be rendered against the new one.
            window.location.reload();
          }}
        >
          Done
        </button>
      </div>
    );
  }

  if (!open) {
    return (
      <div className="pwchange">
        <button
          type="button"
          className="btn btn--ghost btn--small"
          onClick={() => setOpen(true)}
        >
          Change password
        </button>
        <span className="pwchange__hint">
          Confirmed by a 6-character code sent to your registered email.
        </span>
      </div>
    );
  }

  return (
    <div className="pwchange pwchange--open">
      {!codeSent ? (
        <form action={request} className="wsform">
          <p className="pwchange__lede">
            We will email a 6-character code to your registered address. Your current password
            keeps working until you finish, so you will not be locked out.
          </p>
          <div className="wsform__row">
            <Pending idle="Email me a code">Sending…</Pending>
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => setOpen(false)}
            >
              Cancel
            </button>
          </div>
          <Notice state={requestState} />
        </form>
      ) : (
        <form action={confirm} className="wsform">
          <p className="pwchange__lede">
            {requestState.message}
            {requestState.sentTo && (
              <>
                {" "}
                Sent to <strong>{requestState.sentTo}</strong>.
              </>
            )}
          </p>

          <label className="authform__label" htmlFor="pw-code">
            Confirmation code
          </label>
          <input
            id="pw-code"
            name="code"
            className="authform__input authform__input--code"
            // `characters` rather than `words`: the code is alphanumeric, and word capitalisation
            // on a phone keyboard would fight a user typing it.
            autoCapitalize="characters"
            autoComplete="one-time-code"
            spellCheck={false}
            placeholder="4K7P2M"
            maxLength={12}
            required
            autoFocus
          />
          <p className="authform__hint">
            Six characters, not case-sensitive. It expires in{" "}
            {requestState.expiresInMinutes ?? 10} minutes.
          </p>

          {/* The shared field, so the strength meter, live breach screening and confirm-match
              behave exactly as they do at signup. A weaker check here would be the obvious way in:
              an attacker who can reach this form would simply set a weak password instead of
              guessing a strong one. It supplies its own `password` and `confirm` inputs. */}
          <PasswordField hint="Three unrelated words is strong and easy to remember. Your other sessions are not signed out, so keep this device to hand." />

          <div className="wsform__row">
            <Pending idle="Set new password">Saving…</Pending>
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => setOpen(false)}
            >
              Cancel
            </button>
          </div>

          <Notice state={confirmState} />

          <p className="authform__hint">
            Did not arrive? Check spam, then{" "}
            {/* A nested form is invalid HTML, so re-requesting is a formaction on this button
                rather than a second form. */}
            <button type="submit" formAction={request} className="linkbtn">
              send a new code
            </button>
            . Requesting one cancels the previous code.
          </p>
        </form>
      )}
    </div>
  );
}
