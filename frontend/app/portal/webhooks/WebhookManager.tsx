"use client";

import { useActionState, useState } from "react";
import { useFormStatus } from "react-dom";

import EmptyState from "@/components/EmptyState";
import Modal from "@/components/Modal";
import {
  SEVERITY_META,
  type WebhookPublic,
  type Workspace,
} from "@/lib/types";

import {
  createWebhook,
  deleteWebhook,
  sendTestDelivery,
  type WebhookState,
} from "./actions";

const INITIAL: WebhookState = { ok: false, message: "" };

/**
 * Webhook endpoints — the reference implementation of the portal's create pattern.
 *
 * ## The bug this fixes
 *
 * The page's only call-to-action linked to `/portal/webhooks/new`, a route that was never built, so
 * "+ Create" produced a **404**. It was missing for a reason rather than by oversight: every webhook
 * endpoint required `platform:operate`, so a form there would have returned 403 instead. Both halves
 * are now fixed — `webhook_caller` accepts a portal session with `integration:manage`, and the form
 * is this modal.
 *
 * ## The pattern, which the other portal pages now follow
 *
 *   1. **Empty state** when there is nothing — explain what the thing does, then one primary action.
 *   2. **A modal** for creation, opened by that action. Not an always-open card: a create form
 *      sitting open occupies the page before you have done anything, and on a page with four such
 *      cards the actual content is below the fold.
 *   3. **The list** when records exist, with the create action moved to a header button.
 *
 * ## Why the secret block is above the form and outside it
 *
 * `useActionState` keeps the result across renders, and the secret must survive the form resetting
 * so it can be copied. Rendering it above means it cannot be scrolled past on a phone — and it is
 * the one piece of information on this page that cannot be recovered if missed.
 */
const EVENTS: { value: string; label: string; help: string; preset: boolean }[] = [
  {
    value: "alert.created",
    label: "Alert dispatched",
    help: "An advisory was generated and sent for one of your customers' plots.",
    preset: true,
  },
  {
    value: "assessment.completed",
    label: "Assessment completed",
    help: "A satellite pass was processed — including quiet readings that produce no alert.",
    preset: false,
  },
];

const SEVERITIES = [
  { value: "", label: "Everything, including quiet readings" },
  { value: "advisory", label: "Advisory and up" },
  { value: "watch", label: "Watch and up" },
  { value: "warning", label: "Warning and up" },
  { value: "emergency", label: "Emergency only" },
];

function Saving({ children }: { children: string }) {
  const { pending } = useFormStatus();
  return (
    <button type="submit" className="btn btn--primary" disabled={pending}>
      {pending ? "Working…" : children}
    </button>
  );
}

function Notice({ state }: { state: WebhookState }) {
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

const ICON = (
  <svg
    width="52"
    height="52"
    viewBox="0 0 20 20"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.2"
    strokeLinecap="round"
    aria-hidden="true"
  >
    <circle cx="10" cy="5.5" r="2.2" />
    <circle cx="5" cy="14.5" r="2.2" />
    <circle cx="15" cy="14.5" r="2.2" />
    <path d="M8.6 7.3 6.2 12.4M11.4 7.3l2.4 5.1M7.2 14.5h5.6" />
  </svg>
);

export default function WebhookManager({
  endpoints,
  workspaces,
  docsUrl,
}: {
  /** Null means the read failed — distinct from an empty list, which means none exist. */
  endpoints: WebhookPublic[] | null;
  /**
   * The account's workspaces, for scoping the endpoint.
   *
   * An aggregator running two programmes needs each one's endpoint to receive only its own alerts.
   * With one workspace the choice is trivial and the field still shows it, because "all projects"
   * meaning "the only project" is worth stating rather than leaving implicit.
   */
  workspaces: Workspace[];
  /**
   * Absolute URL of the webhook reference, passed in from the server page.
   *
   * NOT imported from `lib/links`: that module is `server-only` because it reads
   * `SHELTER_API_URL`, which must not reach the client bundle. Importing it here fails the build,
   * which is the marker doing its job — the API console is served by FastAPI on a different origin,
   * so the href has to be absolute and the origin is server knowledge.
   */
  docsUrl: string;
}) {
  const [open, setOpen] = useState(false);
  const [createState, create] = useActionState(createWebhook, INITIAL);
  const [testState, test] = useActionState(sendTestDelivery, INITIAL);
  const [removeState, remove] = useActionState(deleteWebhook, INITIAL);

  const form = (
    <form action={create} className="wsform">
      <label className="wsform__field">
        <span className="authform__label">Name</span>
        <input
          className="authform__input"
          name="name"
          placeholder="Loan-book alert receiver"
          autoComplete="off"
          required
        />
        <span className="authform__hint">
          Something you will recognise when a delivery starts failing.
        </span>
      </label>

      <label className="wsform__field">
        <span className="authform__label">Endpoint URL</span>
        <input
          className="authform__input"
          name="url"
          type="url"
          placeholder="https://you.example.com/shelter/alerts"
          autoComplete="off"
          required
        />
        <span className="authform__hint">
          Must be <strong>https</strong>. Deliveries carry plot locations and contact
          addresses, so an unencrypted endpoint is refused.
        </span>
      </label>

      <fieldset className="wsform__field scopeset">
        <legend className="authform__label">Which events</legend>
        {EVENTS.map((e) => (
          <label className="scopeopt" key={e.value}>
            <input
              type="checkbox"
              name="events"
              value={e.value}
              defaultChecked={e.preset}
            />
            <span>
              <strong>{e.label}</strong>
              <span className="scopeopt__help">{e.help}</span>
            </span>
          </label>
        ))}
      </fieldset>

      <label className="wsform__field">
        <span className="authform__label">Which workspace</span>
        <select className="authform__input" name="workspace_id" defaultValue="">
          {/* Empty FIRST and default: an endpoint that receives everything this account owns is
              the right starting point for a single-programme aggregator, and narrowing later is
              safe where widening a live integration is not. */}
          <option value="">All projects</option>
          {workspaces.map((w) => (
            <option key={w.id} value={w.id}>
              {w.name}
              {w.is_default ? " (default)" : ""}
            </option>
          ))}
        </select>
        <span className="authform__hint">
          Restricts this endpoint to one programme&rsquo;s customers. Leave as{" "}
          <strong>All projects</strong> unless you run more than one and they must not see each
          other&rsquo;s alerts.
        </span>
      </label>

      <label className="wsform__field">
        <span className="authform__label">Send from severity</span>
        <select className="authform__input" name="min_severity" defaultValue="">
          {SEVERITIES.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>
      </label>

      <div className="wsform__row">
        <Saving>Create endpoint</Saving>
        <button
          type="button"
          className="btn btn--ghost"
          onClick={() => setOpen(false)}
        >
          Cancel
        </button>
      </div>

      <Notice state={createState} />
    </form>
  );

  return (
    <>
      {/* The secret. Outside the modal so it survives closing, and above everything so it cannot
          be scrolled past — it is the one thing on this page that cannot be recovered. */}
      {createState.ok && createState.secret && (
        <div className="keyreveal" role="status" aria-live="polite">
          <p className="keyreveal__warn">
            Copy this signing secret now. It is shown once and we cannot recover it &mdash; if you
            lose it, delete the endpoint and create another.
          </p>
          <code className="keyreveal__secret">{createState.secret}</code>
          <p className="keyreveal__name">
            {createState.endpointName} &middot; verify every delivery with it. See the{" "}
            <a href={docsUrl} target="_blank" rel="noopener noreferrer">
              signature reference
            </a>
            .
          </p>
        </div>
      )}

      {endpoints === null ? (
        <section className="pcard">
          <p className="muted" style={{ margin: 0, fontSize: 14 }}>
            Endpoints are temporarily unavailable. Nothing has changed &mdash; this is a read
            failure, not a delivery failure.
          </p>
        </section>
      ) : endpoints.length === 0 ? (
        <EmptyState
          icon={ICON}
          title="No webhook endpoints yet"
          body={
            <>
              Create an endpoint to receive events as they happen instead of polling for them
              &mdash; every alert dispatched to one of your customers, and optionally every
              assessment. Deliveries are signed with HMAC, retried with backoff, and each attempt
              is logged so a delivery you did not receive is answerable rather than a mystery.
            </>
          }
          actionLabel="+ Create endpoint"
          onAction={() => setOpen(true)}
          learnMore={docsUrl}
        />
      ) : (
        <section className="pcard">
          <div className="pcard__head">
            <h2 className="pcard__title">Your endpoints</h2>
            <button
              type="button"
              className="btn btn--primary btn--small"
              onClick={() => setOpen(true)}
            >
              + Create endpoint
            </button>
          </div>

          {endpoints.map((endpoint) => (
            <div className="wsarea" key={endpoint.id}>
              <div className="wsarea__head">
                <span className="wsarea__name">{endpoint.name}</span>
                {/* An auto-disabled endpoint is the state someone came here to diagnose, so it is
                    called out with its reason rather than as a colour on a chip. */}
                {endpoint.active ? (
                  <span className="wsarea__owner">Active</span>
                ) : (
                  <span className="wsarea__owner" data-unlinked="true">
                    Disabled after {endpoint.failure_streak} consecutive failures
                  </span>
                )}
              </div>

              <dl className="wsarea__meta">
                <dt>URL</dt>
                <dd className="mono">{endpoint.url}</dd>

                <dt>Workspace</dt>
                <dd>
                  {/* Named, not shown as an id. An aggregator with two projects otherwise sees two
                      identically-described endpoints and cannot tell them apart. A workspace that
                      no longer exists falls back to its id rather than rendering blank. */}
                  {endpoint.workspace_id
                    ? (workspaces.find((w) => w.id === endpoint.workspace_id)?.name ??
                      endpoint.workspace_id)
                    : "All projects"}
                </dd>

                <dt>Events</dt>
                <dd>{endpoint.events.join(", ") || "none"}</dd>

                <dt>Severity</dt>
                <dd>
                  {endpoint.min_severity
                    ? `${SEVERITY_META[endpoint.min_severity]?.label ?? endpoint.min_severity} and up`
                    : "Everything"}
                </dd>

                {endpoint.last_error && (
                  <>
                    <dt>Last error</dt>
                    <dd>{endpoint.last_error}</dd>
                  </>
                )}
              </dl>

              <div className="wsform__row">
                <form action={test}>
                  <input type="hidden" name="subscription_id" value={endpoint.id} />
                  <button type="submit" className="btn btn--ghost btn--small">
                    Send test delivery
                  </button>
                </form>
                <form action={remove}>
                  <input type="hidden" name="subscription_id" value={endpoint.id} />
                  <button type="submit" className="linkbutton">
                    Remove
                  </button>
                </form>
              </div>
            </div>
          ))}

          <Notice state={testState} />
          <Notice state={removeState} />
        </section>
      )}

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title="Create a webhook endpoint"
        description="SHELTER will POST signed events here as they happen. The signing secret is shown once, at creation."
      >
        {form}
      </Modal>
    </>
  );
}
