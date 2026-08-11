import { getAccount } from "@/lib/session";
import { safeApi } from "@/lib/api";
import { safePortal } from "@/lib/portal";
import { CHANNEL_LABEL } from "@/lib/types";

import AlertDelivery from "./AlertDelivery";
import PasswordChange from "./PasswordChange";
import PreferencesForm from "./PreferencesForm";

export const metadata = { title: "Settings" };
export const dynamic = "force-dynamic";

/**
 * Delivery preferences, and who can see this account.
 *
 * ## Why the tenancy list lives here rather than on its own page
 *
 * "Which organisations can see my data" is a settings question, and `GET /iam/me/aggregators`
 * is available to the subscriber alone — one aggregator learning that another also serves
 * this farmer is commercially sensitive. Putting it beside language and channels is what
 * makes it discoverable; a separate page nobody visits is a transparency control in name
 * only.
 */
export default async function SettingsPage() {
  const [account, memberships] = await Promise.all([
    getAccount(),
    safePortal.aggregators(),
  ]);

  const orgs = memberships?.aggregators ?? [];

  // The subscription, for the delivery editor. Fetched by id rather than from a list — the list
  // endpoint is scoped now, but a single read means another tenant's record is never loaded into
  // this process even momentarily.
  //
  // Null is a real state: an account exists before a plot is bound, and the editor is simply not
  // rendered until there is a subscription to configure.
  const subscription = account?.subscriber_id
    ? await safeApi.getSubscriber(account.subscriber_id)
    : null;

  // Whether to offer "we relay it ourselves".
  //
  // Only meaningful for a commercial account: `webhook` mode needs an aggregator behind the plot,
  // and the backend refuses it otherwise — so showing the control to an individual would offer a
  // setting that can only fail.
  const canRelay = account?.kind === "commercial";

  return (
    <>
      <header className="pcard__head">
        <h1 className="portal__title">Settings</h1>
        <p className="portal__lede">
          How and where SHELTER reaches you. Changes take effect on the next advisory.
        </p>
      </header>

      {account && (
        <PreferencesForm
          language={account.language}
          preferredChannel={account.preferred_channel}
        />
      )}

      {/* Where alerts actually go. Above "Your details" because it is the reason someone opens
          this page — a farmer whose alerts are not arriving needs it first, not after their
          name and email. */}
      {subscription && (
        <AlertDelivery
          subscriberId={subscription.id}
          bindings={subscription.channels}
          areas={subscription.areas}
          canRelay={canRelay}
        />
      )}

      <section className="pcard">
        <h2 className="pcard__title">Your details</h2>
        <dl className="deflist">
          <div>
            <dt>Name</dt>
            <dd>{`${account?.first_name ?? ""} ${account?.last_name ?? ""}`.trim() || "—"}</dd>
          </div>
          <div>
            <dt>Email</dt>
            <dd>{account?.email ?? "—"}</dd>
          </div>
          <div>
            <dt>Phone</dt>
            {/* Absent phone is stated, not blank: an email-only subscriber is fully
                supported, and a blank cell reads as a loading failure. */}
            <dd>{account?.phone ?? <span className="muted">Not provided</span>}</dd>
          </div>
          <div>
            <dt>Account ID</dt>
            <dd className="mono">{account?.id ?? "—"}</dd>
          </div>
          {account?.organisation && (
            <div>
              <dt>Organisation</dt>
              <dd>{account.organisation}</dd>
            </div>
          )}
          <div>
            <dt>Default channel</dt>
            <dd>{CHANNEL_LABEL[account?.preferred_channel ?? "email"] ?? "Email"}</dd>
          </div>
          <div>
            <dt>Password</dt>
            <dd>
              {/*
                Changing a password belongs beside the details it protects, not on a page of its
                own — this is where someone comes when they want to alter their account, and a
                credential change is the commonest such alteration.

                Confirmed by a code emailed to the registered address, so altering the credential
                also requires control of the mailbox. A password field alone would let anyone who
                borrowed an unlocked session lock the owner out.
              */}
              <PasswordChange />
            </dd>
          </div>
        </dl>
      </section>

      {/*
        Who can see this account's data — and the two audiences have DIFFERENT answers.

        ## Why the old copy was wrong

        It read "These organisations can see your areas and assessments. You may be served by more
        than one", shown to everybody. Both halves misdescribe the product as it now works:

          * An **individual is B2C**. A direct subscriber has a personal subscription and no
            aggregator association, ever — they are not a degenerate case of an aggregator's
            customer. So "you may be served by more than one" invited a worry about data sharing
            that cannot happen, and the empty state read as a coincidence rather than as the
            guarantee it actually is.
          * A **commercial account is the aggregator**. Showing it a list of organisations with
            access to "your data" is a category error: it holds its own customers' areas, and the
            question it needs answered is who on ITS team can reach them — which is Team, not this.

        So the section is now branched on the audience, and each branch states the guarantee that
        applies to it. `app/iam/attribution.py` is the authority on the model.
      */}
      {account?.kind === "commercial" ? (
        <section className="pcard">
          <h2 className="pcard__title">Access to your customers&rsquo; data</h2>
          <p className="pcard__sub">
            You are a commercial account, so the areas under your organisation belong to your own
            customers. Nobody outside your organisation can see them — SHELTER does not share a
            customer base between accounts.
          </p>
          <p className="authform__hint">
            Access <em>inside</em> your organisation is controlled per workspace, so a team member
            on one project cannot see another&rsquo;s customers. Manage that in{" "}
            <a href="/portal/team">Team</a>, and see the projects themselves in{" "}
            <a href="/portal/workspace">Workspaces</a>.
          </p>
        </section>
      ) : (
        <section className="pcard">
          <h2 className="pcard__title">Who can see your data</h2>

          {memberships === null ? (
            <p className="muted" style={{ margin: 0, fontSize: 14 }}>
              Temporarily unavailable.
            </p>
          ) : orgs.length === 0 ? (
            <>
              {/*
                The empty state is the NORMAL state for an individual subscriber, and it is a
                guarantee rather than an absence. Worth saying plainly: a farmer wondering whether
                their bank can see their plot deserves a direct answer, not a blank list.
              */}
              <p className="pcard__sub">
                <strong>Only you.</strong> Your subscription is personal — it is not linked to any
                bank, cooperative, insurer or agency, and no organisation can see your plots or
                your assessments.
              </p>
              <p className="authform__hint">
                Organisations that monitor land on behalf of their own members — a state scheme, a
                lender, a cooperative — hold those areas under their own account. Yours is separate
                by design, and joining one would be your choice and would show here.
              </p>
            </>
          ) : (
            <>
              <p className="pcard__sub">
                Your account was set up by the organisation below, so it can see the plots it
                registered for you and the assessments on them. You can remove that access.
              </p>
              <ul className="orglist">
                {orgs.map((o) => (
                  <li key={o.aggregator_id} className="orglist__row">
                    <div>
                      <strong>{o.organisation ?? "Unnamed organisation"}</strong>
                      <span className="mono orglist__id">{o.aggregator_id}</span>
                    </div>
                    <span className="muted">
                      {o.role ?? "member"}
                      {o.joined_at ? ` · since ${o.joined_at.slice(0, 10)}` : ""}
                    </span>
                  </li>
                ))}
              </ul>
              <p className="authform__hint">
                To remove an organisation&rsquo;s access, contact hi@freepass.africa. Doing it from
                here is coming next — the removal is permanent and needs a confirmation step we
                would rather build properly than approximate.
              </p>
            </>
          )}
        </section>
      )}
    </>
  );
}
