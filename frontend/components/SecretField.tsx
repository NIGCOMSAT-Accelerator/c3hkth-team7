"use client";

import { useState } from "react";

/**
 * A password input with a padlock on the left and a show/hide eye on the right.
 *
 * ## Why the eye matters more than it looks
 *
 * This is not decoration on a phone. The target device is a low-end Android with a cramped
 * keyboard, often used outdoors, sometimes by someone who is not a confident typist. A
 * masked field gives no feedback at all, so a mistyped passphrase reads as "wrong password"
 * — and the user's next move is usually a password reset they did not need. Being able to
 * check what was typed removes an entire class of support contact.
 *
 * It defaults to hidden, so nothing is exposed to someone standing nearby unless the user
 * asks for it.
 *
 * ## Why the padlock is on the input and not just the label
 *
 * A phishing lookalike is trivial to build; a subscriber cannot audit TLS. The padlock is
 * not a security guarantee and must not be presented as one — what it does is make the
 * field's *purpose* legible at a glance to someone scanning a form in a second language.
 * That is a comprehension aid, so it is `aria-hidden`: a screen reader already has the
 * label, and "lock icon, Password" is noise.
 *
 * ## Type toggle, not a second field
 *
 * `type` switches between `password` and `text` on the same input. A separate visible field
 * would break password managers — they key on the input's identity, so swapping elements
 * makes autofill and save-prompt behaviour unreliable.
 */
export default function SecretField({
  name = "password",
  label = "Password",
  autoComplete = "current-password",
  hint,
  required = true,
}: {
  name?: string;
  label?: string;
  /**
   * `current-password` for sign-in, `new-password` for signup or reset.
   *
   * The distinction is what makes a password manager offer to *fill* rather than to
   * *generate* — getting it backwards is why some sign-in forms suggest a new password.
   */
  autoComplete?: "current-password" | "new-password";
  hint?: string;
  required?: boolean;
}) {
  const [shown, setShown] = useState(false);
  const id = `f-${name}`;
  const hintId = hint ? `${id}-hint` : undefined;

  return (
    <div className="authform__field">
      <label htmlFor={id} className="authform__label">
        {label}
        {!required && <span className="authform__optional">optional</span>}
      </label>

      <div className="secretfield">
        {/* Padlock. aria-hidden — the label already names the field. */}
        <svg
          className="secretfield__lock"
          width="15"
          height="15"
          viewBox="0 0 20 20"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <rect x="4.5" y="8.5" width="11" height="8" rx="1.8" />
          <path d="M7.2 8.5V6.4a2.8 2.8 0 0 1 5.6 0v2.1" />
          <path d="M10 11.6v1.8" />
        </svg>

        <input
          id={id}
          name={name}
          type={shown ? "text" : "password"}
          autoComplete={autoComplete}
          required={required}
          className="authform__input secretfield__input"
          aria-describedby={hintId}
        />

        <button
          type="button"
          onClick={() => setShown((v) => !v)}
          className="secretfield__eye"
          // A real label, not a title: `title` is invisible on touch, where this is used
          // most. `aria-pressed` communicates the toggle state, which an icon cannot.
          aria-label={shown ? "Hide password" : "Show password"}
          aria-pressed={shown}
          // Excluded from tab order deliberately. Tabbing from the password field should
          // reach the submit button — this is a mouse/touch affordance, and a keyboard user
          // who wants it can still reach it by Shift-Tab.
          tabIndex={-1}
        >
          {shown ? (
            // Eye with a slash: currently visible, click to hide.
            <svg
              width="17"
              height="17"
              viewBox="0 0 20 20"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              aria-hidden="true"
            >
              <path d="M3.5 3.5l13 13" />
              <path d="M7.4 7.6a3 3 0 0 0 4.1 4.2" />
              <path d="M6.1 6.2C4.6 7.2 3.3 8.6 2.5 10c1.6 2.8 4.3 4.7 7.5 4.7 1.2 0 2.3-.3 3.3-.7" />
              <path d="M15.4 13A11 11 0 0 0 17.5 10C15.9 7.2 13.2 5.3 10 5.3c-.7 0-1.3.1-1.9.2" />
            </svg>
          ) : (
            // Plain eye: currently hidden, click to show.
            <svg
              width="17"
              height="17"
              viewBox="0 0 20 20"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              aria-hidden="true"
            >
              <path d="M2.5 10S5.2 5.3 10 5.3 17.5 10 17.5 10 14.8 14.7 10 14.7 2.5 10 2.5 10z" />
              <circle cx="10" cy="10" r="2.4" />
            </svg>
          )}
        </button>
      </div>

      {hint && (
        <p id={hintId} className="authform__hint">
          {hint}
        </p>
      )}
    </div>
  );
}
