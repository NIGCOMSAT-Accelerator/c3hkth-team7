"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";

import type { AuthState } from "@/app/auth/actions";

/**
 * Submit button that knows when the form is in flight.
 *
 * A separate component because `useFormStatus` only reports the status of the *parent*
 * form — reading it in the same component that renders `<form>` returns `pending: false`
 * forever, which is a common and silent mistake.
 *
 * Disabling during submit is not cosmetic here: a double-submitted signup creates a
 * duplicate-email 409 that reads to the user as "it failed" when the first attempt
 * actually succeeded.
 */
export function SubmitButton({
  children,
  pendingLabel,
}: {
  children: React.ReactNode;
  pendingLabel?: string;
}) {
  const { pending } = useFormStatus();

  return (
    <button
      type="submit"
      className="btn btn--primary authform__submit"
      disabled={pending}
      // Announced to screen readers so a slow connection does not look like a dead
      // button.
      aria-busy={pending}
    >
      {pending ? (pendingLabel ?? "Working…") : children}
    </button>
  );
}

const EMPTY: AuthState = { ok: false, message: "" };

/**
 * Wraps a Server Action with its own error/success state.
 *
 * `useActionState` rather than a client-side fetch: the action runs on the server, so the
 * password and the session token never enter the browser bundle, and the form still works
 * with JavaScript disabled — which matters on the low-end Android this targets, where a
 * failed bundle load would otherwise mean no sign-in at all.
 */
export function AuthForm({
  action,
  children,
  submitLabel,
  pendingLabel,
  footer,
}: {
  action: (prev: AuthState, formData: FormData) => Promise<AuthState>;
  children: React.ReactNode | ((state: AuthState) => React.ReactNode);
  submitLabel: string;
  pendingLabel?: string;
  footer?: React.ReactNode;
}) {
  const [state, formAction] = useActionState(action, EMPTY);

  return (
    <form action={formAction} className="authform" noValidate>
      {state.message && (
        <div
          className="authform__message"
          data-tone={state.ok ? "ok" : "error"}
          // `assertive` for errors so the result is announced immediately rather than
          // waiting for the user to navigate back to it; `polite` for success, which is
          // usually followed by a redirect anyway.
          role={state.ok ? "status" : "alert"}
          aria-live={state.ok ? "polite" : "assertive"}
        >
          {state.message}
        </div>
      )}

      {typeof children === "function" ? children(state) : children}

      <SubmitButton pendingLabel={pendingLabel}>{submitLabel}</SubmitButton>

      {footer}
    </form>
  );
}

/** A labelled input. Extracted so every field on every auth page is built identically. */
export function Field({
  name,
  label,
  type = "text",
  autoComplete,
  required = true,
  hint,
  placeholder,
  defaultValue,
  inputMode,
}: {
  name: string;
  label: string;
  type?: string;
  autoComplete?: string;
  required?: boolean;
  hint?: string;
  placeholder?: string;
  defaultValue?: string;
  inputMode?: "text" | "email" | "tel" | "numeric";
}) {
  const id = `f-${name}`;
  const hintId = hint ? `${id}-hint` : undefined;

  return (
    <div className="authform__field">
      <label htmlFor={id} className="authform__label">
        {label}
        {!required && <span className="authform__optional">optional</span>}
      </label>
      <input
        id={id}
        name={name}
        type={type}
        required={required}
        // Correct autocomplete tokens matter more than usual here: a farmer on a shared
        // handset relies on the browser's password manager, and a wrong token means it
        // never offers to save the credential.
        autoComplete={autoComplete}
        inputMode={inputMode}
        placeholder={placeholder}
        defaultValue={defaultValue}
        aria-describedby={hintId}
        className="authform__input"
      />
      {hint && (
        <p id={hintId} className="authform__hint">
          {hint}
        </p>
      )}
    </div>
  );
}
