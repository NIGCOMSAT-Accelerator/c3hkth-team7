"use client";

import { useActionState, useState } from "react";
import { useFormStatus } from "react-dom";

import EmptyState from "@/components/EmptyState";
import Modal from "@/components/Modal";
import type { Workspace } from "@/lib/types";

import { createKey, type KeyState } from "./actions";

const INITIAL: KeyState = { ok: false, message: "" };

/**
 * Mint a Partner API key, in the portal.
 *
 * ## Why this had to exist
 *
 * `POST /iam/api-keys` has always worked. There was no way to call it from the portal, so this page
 * documented a curl command instead — which means the one credential an aggregator needs in order to
 * use the Partner API at all could only be obtained by hand-crafting an HTTP request with a session
 * token they had no way to read. In practice that made the Partner API unreachable for the people it
 * is for.
 *
 * ## The scopes are checkboxes, not a preset
 *
 * A "full access" button would be the obvious shortcut and the wrong one: least privilege is the
 * whole reason scopes exist, and a key embedded in a partner's batch importer should not also be
 * able to delete their customers. The read scope is pre-ticked because a key without it can
 * authenticate and see nothing.
 *
 * The API refuses any scope above the caller's role **on the named workspace**, so a member who is
 * View-Only there cannot mint a write key by holding Owner elsewhere. This form does not try to
 * predict that — it shows the options and surfaces the API's own refusal, which explains itself
 * better than a greyed-out checkbox.
 *
 * ## An empty state, then a modal — not an always-open form
 *
 * The first version of this rendered the create form permanently. On a page that also lists existing
 * keys and explains the scope model, that is three stacked cards before you have done anything, and
 * the list an aggregator came to check is below the fold.
 *
 * So: an empty state with one action when there are no keys, a header button once there are, and the
 * form in a modal either way. Same pattern as the Webhooks page.
 *
 * ## The secret renders once and says so
 *
 * There is no reveal endpoint; the API stores a hash. So the plaintext is shown in a copyable block
 * with the warning attached, and it disappears on the next action. An aggregator who believes they
 * can retrieve it later will not copy it now, which is the failure this wording exists to prevent.
 */
const SCOPES: { value: string; label: string; help: string; preset: boolean }[] = [
  {
    value: "customers:read",
    label: "Read customers",
    help: "List your customers, their monitored areas and their alerts.",
    preset: true,
  },
  {
    value: "customers:write",
    label: "Onboard and update customers",
    help: "Create customers and add monitored areas for them. Needed to onboard via the API.",
    preset: false,
  },
  {
    value: "scan:trigger",
    label: "Trigger a scan",
    help: "Request an immediate satellite assessment for one of your customers' areas.",
    preset: false,
  },
  {
    value: "webhooks:manage",
    label: "Manage webhooks",
    help: "Register the endpoint that receives alerts. Required if you relay alerts yourself.",
    preset: false,
  },
];

function Saving({ children }: { children: string }) {
  const { pending } = useFormStatus();
  return (
    <button type="submit" className="btn btn--primary" disabled={pending}>
      {pending ? "Working…" : children}
    </button>
  );
}

const ICON = (
  <svg
    width="52"
    height="52"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.3"
    strokeLinecap="round"
    aria-hidden="true"
  >
    <circle cx="8" cy="12" r="4" />
    <path d="M12 12h9M18 12v4M15 12v3" />
  </svg>
);

export default function KeyMinter({
  workspaces,
  keys,
  docsUrl,
}: {
  workspaces: Workspace[];
  keys: { id: string; name: string; prefix: string; revoked: boolean }[];
  /** Absolute API-reference URL. A prop, not an import — `lib/links` is `server-only`. */
  docsUrl: string;
}) {
  const [open, setOpen] = useState(false);
  const [state, mint] = useActionState(createKey, INITIAL);

  const live = keys.filter((k) => !k.revoked);

  const form =
    workspaces.length === 0 ? (
      <p className="authform__hint">
        No workspace yet. One is created automatically with your account &mdash; close this and
        visit <a href="/portal/workspace">Workspaces</a>.
      </p>
    ) : (
      <form action={mint} className="wsform">
        <label className="wsform__field">
          <span className="authform__label">Name</span>
          <input
            className="authform__input"
            name="name"
            placeholder="Loan-book importer"
            autoComplete="off"
            required
          />
          <span className="authform__hint">
            Something you will recognise in six months, when deciding whether to rotate it.
          </span>
        </label>

        <label className="wsform__field">
          <span className="authform__label">Workspace</span>
          <select className="authform__input" name="workspace_id" required>
            {workspaces.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
                {w.is_default ? " (default)" : ""}
              </option>
            ))}
          </select>
          <span className="authform__hint">
            A key reaches one workspace only, so it can never return another project&rsquo;s
            customers.
          </span>
        </label>

        <fieldset className="wsform__field scopeset">
          <legend className="authform__label">What this key may do</legend>
          {SCOPES.map((sc) => (
            <label className="scopeopt" key={sc.value}>
              <input
                type="checkbox"
                name="scopes"
                value={sc.value}
                defaultChecked={sc.preset}
              />
              <span>
                <strong>{sc.label}</strong>
                <span className="scopeopt__help">{sc.help}</span>
              </span>
            </label>
          ))}
          <span className="authform__hint">
            Grant only what the integration needs. Your role on the chosen workspace bounds what
            can be granted &mdash; a wider role on another project does not apply here.
          </span>
        </fieldset>

        <label className="wsform__field">
          <span className="authform__label">Expires after (days)</span>
          <input
            className="authform__input"
            name="expires_in_days"
            type="number"
            min={1}
            placeholder="Leave blank for no expiry"
            autoComplete="off"
          />
        </label>

        <div className="wsform__row">
          <Saving>Create key</Saving>
          <button type="button" className="btn btn--ghost" onClick={() => setOpen(false)}>
            Cancel
          </button>
        </div>

        {state.message && (
          <p
            className="authform__message"
            data-tone={state.ok ? "ok" : "error"}
            role="status"
            aria-live="polite"
          >
            {state.message}
          </p>
        )}
      </form>
    );

  return (
    <>
      {/* The secret, outside the modal so it survives closing, and first so it cannot be scrolled
          past. It is the one thing on this page that cannot be recovered. */}
      {state.ok && state.secret && (
        <div className="keyreveal" role="status" aria-live="polite">
          <p className="keyreveal__warn">
            Copy this now. It is shown once and we cannot recover it &mdash; if you lose it, revoke
            the key and create another.
          </p>
          <code className="keyreveal__secret">{state.secret}</code>
          <p className="keyreveal__name">
            {state.keyName} &middot; send it as{" "}
            <span className="mono">X-SHELTER-API-Key</span>
          </p>
        </div>
      )}

      {live.length === 0 ? (
        <EmptyState
          icon={ICON}
          title="No API keys yet"
          body={
            <>
              A key lets you onboard customers and create monitored areas programmatically, instead
              of using this portal. Keys are created explicitly rather than at signup, so an account
              that never integrates is not left holding a live credential nobody is watching. Each
              key reaches <strong>one workspace</strong> and carries only the permissions you grant.
            </>
          }
          actionLabel="+ Create API key"
          onAction={() => setOpen(true)}
          learnMore={docsUrl}
          learnMoreLabel="API reference"
        />
      ) : (
        /*
          With keys present, this is just the create affordance — the page's own table below is the
          richer list (scope chips, last used, expiry) and duplicating it here would put two
          different renderings of the same records on one screen.

          Revocation lives beside each key in that table for the same reason: a separate "Revoke a
          key" card meant scrolling between the key you were reading and the button that removes it.
        */
        <div className="pcard__head pcard__head--bare">
          <button
            type="button"
            className="btn btn--primary btn--small"
            onClick={() => setOpen(true)}
          >
            + Create API key
          </button>
        </div>
      )}

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title="Create an API key"
        description="For driving SHELTER from your own systems. The secret is shown once, at creation."
      >
        {form}
      </Modal>
    </>
  );
}
