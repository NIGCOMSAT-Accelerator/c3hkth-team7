import { api } from "@/lib/api";
import { WEBHOOK_DOCS_URL } from "@/lib/links";
import { getSessionToken, requirePermission } from "@/lib/session";

import WebhookManager from "./WebhookManager";

export const metadata = { title: "Webhooks" };
export const dynamic = "force-dynamic";

/**
 * Webhook endpoints — receive SHELTER events in your own systems.
 *
 * Aggregator-only, permission-gated on `integration:manage`. `requirePermission` redirects; the
 * backend's own check on each webhook route is what actually protects them. Hiding the nav item is
 * courtesy, not access control.
 *
 * ## What changed here, and why the page was previously a stub
 *
 * This page listed nothing and its "+ Create" button pointed at `/portal/webhooks/new` — a route
 * that was never built, so the only call-to-action produced a **404**.
 *
 * The stub was honest about half the reason: `GET /webhook/subscriptions` was platform-scoped, so a
 * portal session could not read subscriptions and a screen showing none while they existed would be
 * worse than one saying so. The other half was that creation had the same problem — a form here
 * would have returned 403.
 *
 * Both are fixed at the API: `webhook_caller` resolves a portal session carrying
 * `integration:manage` to that aggregator's organisation, and every per-subscription route proves
 * ownership with a 404 (not 403) for another tenant's id. So this page now reads and writes real
 * subscriptions.
 */
export default async function WebhooksPage() {
  await requirePermission("integration:manage", "/portal/webhooks");

  const token = await getSessionToken();

  // Null on failure, NOT an empty array. "Could not load" and "you have none" call for different
  // screens — the second offers a create button, the first must not imply your endpoints are gone.
  const [endpoints, workspaces] = await Promise.all([
    token ? api.listWebhooks(token).catch(() => null) : Promise.resolve(null),
    // `[]` on failure, not null: the create form degrades to "All projects" only, which still
    // works. An endpoint scoped to every project is a usable outcome; no form at all is not.
    token ? api.listWorkspaces(token).catch(() => []) : Promise.resolve([]),
  ]);

  return (
    <>
      <header className="pcard__head">
        <h1 className="portal__title">Webhooks</h1>
        <p className="portal__lede">
          Receive alerts and assessments in your own systems as they happen, instead of
          polling for them.
        </p>
      </header>

      <WebhookManager
        endpoints={endpoints}
        workspaces={workspaces}
        docsUrl={WEBHOOK_DOCS_URL}
      />

      <section className="pcard">
        <h2 className="pcard__title">What you will receive</h2>
        <ul className="evidence">
          <li>
            <strong>Signed.</strong> Every delivery carries an HMAC signature computed with
            your endpoint&rsquo;s own secret, so you can verify it came from us.
          </li>
          <li>
            <strong>At-least-once.</strong> A failed delivery is retried with backoff, which
            means a duplicate is possible — key your handler on the event id.
          </li>
          <li>
            <strong>Logged.</strong> Every attempt, response code and body is recorded, so a
            delivery you did not receive is answerable rather than a mystery.
          </li>
        </ul>
      </section>
    </>
  );
}
