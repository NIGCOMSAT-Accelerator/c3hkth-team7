import { api } from "@/lib/api";
import { getSessionToken, requirePermission } from "@/lib/session";

import CustomerManager from "./CustomerManager";
import WorkspaceMonitoring from "./WorkspaceMonitoring";

export const metadata = { title: "Workspace customers" };
export const dynamic = "force-dynamic";

/**
 * Workspace → Customers → Areas.
 *
 * ## Why an aggregator needs this in the portal at all
 *
 * The Partner API is the production path — a bank onboarding ten thousand farmers is not doing it
 * by hand. But nobody should write their first integration against an API they have never seen
 * succeed. Onboarding one customer here, watching the first assessment land, and *then* automating
 * turns a blind integration into a verification: this page and `POST /iam/customers` produce an
 * identical record.
 *
 * It is also the honest answer for a small cooperative with forty members, for whom a Partner API
 * integration is more engineering than the problem deserves.
 *
 * ## Scoping
 *
 * The workspace is in the path and `requirePermission` gates the page, but the real control is
 * `require_workspace_permission` on each route — it resolves the caller's role **on this
 * workspace**, so a member who is View-Only here cannot act by holding a wider role on another
 * project. Aggregator-only: an individual has no workspace and no customers.
 */
export default async function WorkspaceCustomersPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  await requirePermission("customers:view", `/portal/workspace/${id}/customers`);

  const token = await getSessionToken();

  // Each read degrades to empty rather than throwing: a downed backend should render an
  // explanation, not a 500 that loses the operator's place.
  const [workspaces, customers, monitoring] = await Promise.all([
    token ? api.listWorkspaces(token).catch(() => []) : Promise.resolve([]),
    token ? api.workspaceCustomers(token, id).catch(() => []) : Promise.resolve([]),
    // Null rather than [] on failure: "could not load" and "nothing monitored" are different
    // answers, and conflating them would tell an aggregator their monitoring had stopped.
    token
      ? api.workspaceAreas(token, id).catch(() => null)
      : Promise.resolve(null),
  ]);

  const workspace = workspaces.find((w) => w.id === id);

  // Areas per customer, fetched alongside so the page renders one complete picture rather than
  // making the operator click into each farmer to see whether monitoring is actually running.
  const areas = await Promise.all(
    customers.map(async (customer) => ({
      accountId: customer.account_id,
      areas: token
        ? await api
            .workspaceCustomerAreas(token, id, customer.account_id)
            .catch(() => [])
        : [],
    })),
  );

  return (
    <>
      <header className="pcard__head">
        <h1 className="portal__title">
          {workspace?.name ?? "Workspace"} — customers
        </h1>
        <p className="portal__lede">
          The farmers this project monitors, and the plots watched for each. Onboard one by hand
          here to prove the flow, then automate the rest with the Partner API — both produce the
          same record.
        </p>
      </header>

      {!workspace ? (
        <section className="pcard">
          <p className="authform__hint">
            This workspace is unavailable. Open <a href="/portal/workspace">Workspaces</a> to
            pick one.
          </p>
        </section>
      ) : (
        <CustomerManager
          workspaceId={id}
          workspaceName={workspace.name}
          customers={customers}
          areasByCustomer={Object.fromEntries(
            areas.map((entry) => [entry.accountId, entry.areas]),
          )}
        />
      )}

      {/* The workspace-wide view, AFTER the per-customer manager.
          
          It answers a different question — "what is this workspace monitoring?" rather than "what is
          this farmer's monitoring?" — and it is the only view that can see the aggregator's own plots
          or an area whose attribution is broken, because it does not iterate customers. */}
      {workspace && (
        <WorkspaceMonitoring rows={monitoring} />
      )}
    </>
  );
}
