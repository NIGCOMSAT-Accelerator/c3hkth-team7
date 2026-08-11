"use client";

import { useActionState } from "react";
import { useFormStatus } from "react-dom";

import { revokeKey, type KeyState } from "./actions";

const INITIAL: KeyState = { ok: false, message: "" };

/**
 * Revoke one key, inline beside the key it removes.
 *
 * ## Why this is per-row rather than a separate card
 *
 * Revocation used to live in its own "Revoke a key" section below the list, which meant scrolling
 * between the key you were reading — its scopes, when it was last used — and the button that removes
 * it. That is the wrong place for an irreversible action: the decision depends on exactly the
 * information you had to scroll away from.
 *
 * ## Why the page stays a server component
 *
 * The key list is server-rendered and should be: it is a straightforward read with no interaction.
 * Only the button needs client state for the pending label and the result message, so only the
 * button is a client component. Making the whole page client to add one form would move the API
 * token's usage into a client boundary, which `lib/api.ts` being `server-only` exists to prevent.
 *
 * ## No confirm dialog, deliberately
 *
 * A `confirm()` on a button whose label is already "Revoke" trains people to click through
 * confirmations. The protection that matters is that the consequence is stated *before* the click —
 * the surrounding copy says it is immediate and irreversible — and that rotation exists as the
 * non-destructive alternative.
 */
export default function RevokeKey({ keyId }: { keyId: string }) {
  const [state, revoke] = useActionState(revokeKey, INITIAL);
  const { pending } = useFormStatus();

  return (
    <form action={revoke} className="keylist__revoke">
      <input type="hidden" name="key_id" value={keyId} />
      <button type="submit" className="linkbutton" disabled={pending}>
        {pending ? "Revoking…" : "Revoke"}
      </button>
      {state.message && (
        <span
          className="keylist__revokemsg"
          data-tone={state.ok ? "ok" : "error"}
          role="status"
          aria-live="polite"
        >
          {state.message}
        </span>
      )}
    </form>
  );
}
