"use client";

import { useEffect, useRef, useState } from "react";

import { screenPassword, type BreachResult } from "@/app/auth/password-actions";

/**
 * Password entry with live validation, a strength read-out, and a confirm-match check.
 *
 * ## The problem this solves
 *
 * Without it, a subscriber submits the form, waits for a round trip, and gets a raw
 * Pydantic error back:
 *
 * ```
 * {"detail":[{"type":"string_too_short","loc":["body","password"],
 *   "msg":"String should have at least 12 characters","input":"Password@123", …}]}
 * ```
 *
 * Three separate failures in one response. It leaks the field path and validator name; it
 * costs a network round trip to learn something knowable at keystroke time; and it echoes
 * the password back — so anyone's proxy log, error tracker or screenshot now holds it.
 * On a metered rural connection the round trip is also the expensive part.
 *
 * ## The rules are mirrored from the backend deliberately, and must stay in step
 *
 * `backend/app/iam/models.py` is the authority:
 *
 *   * `Password = Annotated[str, StringConstraints(min_length=12, max_length=200)]`
 *   * `_not_obvious` rejects any password *containing* one of
 *     `password`, `shelter`, `123456`, `qwerty`, `letmein`, `welcome` (case-insensitive).
 *
 * Client-side validation is a **convenience, never a control** — the server still rejects
 * anything invalid, which is what makes a bypassed browser harmless. But the two lists
 * must agree, or the form will accept something the API then refuses, which is a worse
 * experience than no client validation at all. If a rule changes there, change it here.
 *
 * ## Why the strength meter is length-first and not a complexity score
 *
 * The backend deliberately has no complexity policy, and its comment says why: requiring
 * a symbol and a digit pushes people to `Passw0rd!` plus a written note, which is weaker
 * in practice. This component follows that reasoning rather than contradicting it — it
 * rewards length and distinct words, and never demands a character class. A form that
 * nags for a capital letter while the API does not is inventing a rule.
 */

/** Mirrors `min_length=12` on the backend's `Password` type. */
const MIN_LENGTH = 12;

/** Mirrors `max_length=200`. */
const MAX_LENGTH = 200;

/**
 * Mirrors `_not_obvious` in `backend/app/iam/models.py`.
 *
 * Substring matching, case-insensitive — exactly as the backend does it, so
 * `Password@123` and `MyShelter2026` both fail here as they would there.
 */
const BANNED_SUBSTRINGS = [
  "password",
  "shelter",
  "123456",
  "qwerty",
  "letmein",
  "welcome",
];

interface Check {
  label: string;
  pass: boolean;
}

/** The two hard requirements, evaluated live. Order matches how they are explained. */
function evaluate(value: string): { checks: Check[]; banned: string | null } {
  const lowered = value.toLowerCase();
  const banned = BANNED_SUBSTRINGS.find((b) => lowered.includes(b)) ?? null;

  return {
    checks: [
      { label: `At least ${MIN_LENGTH} characters`, pass: value.length >= MIN_LENGTH },
      {
        // Named rather than vague ("password is too common"), because a subscriber who
        // cannot see *which* word is the problem will keep editing around it.
        label: banned
          ? `Remove “${banned}” — it is too easily guessed`
          : "No easily-guessed words",
        pass: value.length > 0 && !banned,
      },
    ],
    banned,
  };
}

/**
 * A coarse strength read-out: length and distinct-word count only.
 *
 * Not an entropy estimate — a real one needs a dictionary and would be a large download
 * for a hint. Four bands are enough to steer someone from "too short" toward a passphrase,
 * which is the only advice here that actually raises security.
 */
function strength(value: string, valid: boolean): { label: string; level: 0 | 1 | 2 | 3 } {
  if (!valid) return { label: "Too short", level: 0 };

  const words = value.trim().split(/\s+/).filter((w) => w.length > 2).length;
  if (value.length >= 20 || words >= 3) return { label: "Strong", level: 3 };
  if (value.length >= 16 || words >= 2) return { label: "Good", level: 2 };
  return { label: "Acceptable", level: 1 };
}

export default function PasswordField({
  hint = "Three unrelated words is strong and easy to remember — for example “market rain bicycle”.",
}: {
  hint?: string;
}) {
  const [value, setValue] = useState("");
  // Breach screening state. `null` = not yet checked for the current value.
  const [breach, setBreach] = useState<BreachResult | null>(null);
  const [screening, setScreening] = useState(false);
  // Guards against a slow response for an old value overwriting a newer one — without it,
  // deleting a character could re-show a warning for the password you just changed.
  const latest = useRef("");
  const [confirm, setConfirm] = useState("");
  const [shown, setShown] = useState(false);
  // Requirements stay hidden until the field has been touched. Showing two red crosses
  // beside an untouched field reads as "you have already done something wrong".
  const [touched, setTouched] = useState(false);

  /**
   * Debounced breach screening.
   *
   * 600ms after typing stops, not per keystroke. Three reasons: it is a network round trip
   * on a metered connection, HIBP is a shared resource that should not be hammered, and a
   * warning that flickers while someone is mid-word is noise rather than information.
   *
   * Only runs once the local rules pass. Screening a 4-character fragment wastes a request
   * to tell the user something the requirement list already says.
   */
  useEffect(() => {
    latest.current = value;
    const { checks: local } = evaluate(value);

    if (!local.every((c) => c.pass)) {
      setBreach(null);
      setScreening(false);
      return;
    }

    setScreening(true);
    const timer = window.setTimeout(async () => {
      const result = await screenPassword(value);
      // Discard a stale answer: the field may have changed while this was in flight.
      if (latest.current !== value) return;
      setBreach(result);
      setScreening(false);
    }, 600);

    return () => {
      window.clearTimeout(timer);
      setScreening(false);
    };
  }, [value]);

  const { checks } = evaluate(value);
  const allPass = checks.every((c) => c.pass);
  const isBreached = breach?.breached === true;
  const meter = strength(value, allPass);

  const confirmTouched = confirm.length > 0;
  const matches = confirm === value;

  return (
    <>
      <div className="authform__field">
        <label htmlFor="f-password" className="authform__label">
          Password
        </label>

        <div className="pwfield">
          <input
            id="f-password"
            name="password"
            // `type` toggles rather than a separate field, so the browser's password
            // manager still recognises and offers to save it.
            type={shown ? "text" : "password"}
            autoComplete="new-password"
            required
            minLength={MIN_LENGTH}
            maxLength={MAX_LENGTH}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onBlur={() => setTouched(true)}
            className="authform__input pwfield__input"
            aria-describedby="f-password-reqs"
            // Announces validity to screen readers without relying on the colour of a
            // tick, which a colourblind or blind user cannot use.
            aria-invalid={touched && value.length > 0 && !allPass}
          />
          <button
            type="button"
            className="pwfield__toggle"
            onClick={() => setShown((s) => !s)}
            // A visible label, not just an icon: "eye" alone is ambiguous about whether
            // it means "currently hidden" or "click to hide".
            aria-label={shown ? "Hide password" : "Show password"}
          >
            {shown ? "Hide" : "Show"}
          </button>
        </div>

        {/*
          A strength bar, shown only once the hard requirements pass. Displaying "Weak"
          while someone is still typing character four is noise — the requirement list
          below already says what is missing.
        */}
        {/*
          The breach warning. Rendered above the strength bar because it overrides it: a
          password can be 24 characters of three unrelated words and still be in a breach
          corpus, and showing "Strong" beside "exposed" would be contradictory.

          The wording states WHAT was compared — part of a hash, not the password — so the
          message cannot be read as "we have your password on file".
        */}
        {isBreached && (
          <div className="authform__message pwbreach" data-tone="error" role="alert">
            <strong>Please choose a different password.</strong>
            <span>
              We compared part of a hash of your password with data from{" "}
              <a
                href="https://haveibeenpwned.com/Passwords"
                target="_blank"
                rel="noopener noreferrer"
              >
                Have I Been Pwned
              </a>
              , and it appears the password you entered may have been exposed on another
              website
              {breach && breach.timesSeen > 0
                ? ` — it appears ${breach.timesSeen.toLocaleString()} times in known breaches`
                : ""}
              . For the best security, we want you to use a unique password.
            </span>
          </div>
        )}

        {value.length > 0 && allPass && !isBreached && (
          <div className="pwmeter" aria-hidden="true">
            <span className={`pwmeter__bar pwmeter__bar--${meter.level}`} />
            <span className="pwmeter__label">{meter.label}</span>
          </div>
        )}

        {/*
          `aria-live="polite"` so a screen reader announces a requirement flipping to met
          as the user types, rather than only on submit. Polite, not assertive: assertive
          interrupts mid-word on every keystroke.
        */}
        <ul
          id="f-password-reqs"
          className="pwreqs"
          aria-live="polite"
          hidden={!touched && value.length === 0}
        >
          {/* The screening row. Shown as a live third requirement so the user understands
              a check is happening rather than wondering why the form paused. It is
              deliberately NOT rendered as passed when `checked` is false — claiming a
              password is clean on the strength of a check that did not run would be a
              false reassurance. */}
          {allPass && (
            <li
              className={`pwreqs__item${
                breach?.checked && !breach.breached ? " pwreqs__item--ok" : ""
              }`}
            >
              <span className="pwreqs__mark" aria-hidden="true">
                {screening ? "…" : breach?.checked && !breach.breached ? "✓" : "○"}
              </span>
              {screening
                ? "Checking against known breaches…"
                : breach?.breached
                  ? "Found in a known data breach"
                  : breach?.checked
                    ? "Not in any known data breach"
                    : "Breach check unavailable — your password is still accepted"}
            </li>
          )}
          {checks.map((c) => (
            <li
              key={c.label}
              className={`pwreqs__item${c.pass ? " pwreqs__item--ok" : ""}`}
            >
              {/* A glyph as well as colour — colour alone fails the same accessibility
                  rule the severity badges follow elsewhere in this codebase. */}
              <span className="pwreqs__mark" aria-hidden="true">
                {c.pass ? "✓" : "○"}
              </span>
              {c.label}
            </li>
          ))}
        </ul>

        <p className="authform__hint">{hint}</p>
      </div>

      <div className="authform__field">
        <label htmlFor="f-confirm" className="authform__label">
          Confirm password
        </label>
        <input
          id="f-confirm"
          name="confirm"
          type={shown ? "text" : "password"}
          autoComplete="new-password"
          required
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          className="authform__input"
          aria-describedby="f-confirm-state"
          aria-invalid={confirmTouched && !matches}
        />
        {/*
          Mismatch is reported live rather than on submit. Caught late, the user has to
          retype both fields; caught here, they fix one character. This is also the failure
          that produces "my new password doesn't work" after a reset.
        */}
        <p
          id="f-confirm-state"
          className={`authform__hint${confirmTouched && !matches ? " authform__hint--warn" : ""}`}
          aria-live="polite"
        >
          {confirmTouched
            ? matches
              ? "✓ Passwords match"
              : "Passwords do not match yet"
            : "Type it once more to be sure."}
        </p>
      </div>
    </>
  );
}
