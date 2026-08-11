"use client";

import { useActionState, useState } from "react";
import { useFormStatus } from "react-dom";

import EmptyState from "@/components/EmptyState";
import Modal from "@/components/Modal";

import type {
  PendingInvitation,
  RoleOption,
  TeamMember,
  Workspace,
} from "@/lib/types";

import {
  inviteMember,
  removeMember,
  resendInvitation,
  updateGrants,
  type TeamState,
} from "./actions";

const INITIAL: TeamState = { ok: false, message: "" };

/**
 * Team management — one role selector per workspace, per person.
 *
 * ## Why the grid is per workspace rather than per person
 *
 * A single role per colleague is the obvious design and the wrong one: an aggregator running a
 * flood pilot alongside a live rice season needs an engineer who can rotate keys on the pilot
 * and only read the season. One role would force them to grant the wider access everywhere,
 * which is how least privilege quietly stops being practised.
 *
 * So each row is a person, and each column a workspace. "No access" is a real option, and
 * choosing it revokes that edge on save.
 *
 * ## The role list is fetched, not hardcoded
 *
 * From `GET /iam/team/assignable-roles`, which is narrower than the full list: only an owner
 * may create another owner. Rendering every role would offer a choice the API refuses, and the
 * refusal would look like a bug.
 */

function Submitting({ children }: { children: string }) {
  const { pending } = useFormStatus();
  return (
    <button type="submit" className="btn btn--primary" disabled={pending}>
      {pending ? "Saving…" : children}
    </button>
  );
}

function Notice({ state }: { state: TeamState }) {
  if (!state.message) return null;
  return (
    <p
      className="authform__message"
      // `data-tone` rather than two class names: that is how every other form in this app
      // renders an outcome, and the tone styles are keyed off the attribute.
      data-tone={state.ok ? "ok" : "error"}
      role="status"
      aria-live="polite"
    >
      {state.message}
    </p>
  );
}

/** One role selector per workspace. `current` maps workspace id → role. */
function RoleGrid({
  workspaces,
  roles,
  current,
  idPrefix,
}: {
  workspaces: Workspace[];
  roles: RoleOption[];
  current: Record<string, string>;
  idPrefix: string;
}) {
  return (
    <div className="teamgrid">
      {workspaces.map((workspace) => {
        const id = `${idPrefix}-${workspace.id}`;
        return (
          <div className="teamgrid__cell" key={workspace.id}>
            <label className="authform__label" htmlFor={id}>
              {workspace.name}
            </label>
            <select
              id={id}
              name={`role-${workspace.id}`}
              className="authform__input"
              defaultValue={current[workspace.id] ?? ""}
            >
              {/* Empty is a real choice: it means no access to this workspace, and on save
                  the API revokes the edge. */}
              <option value="">No access</option>
              {roles.map((role) => (
                <option key={role.value} value={role.value} title={role.description}>
                  {role.label}
                </option>
              ))}
            </select>
          </div>
        );
      })}
    </div>
  );
}

const TEAM_ICON = (
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
    <circle cx="9" cy="8.5" r="3.2" />
    <path d="M3 20a6 6 0 0 1 12 0" />
    <path d="M17 5.8a3.2 3.2 0 0 1 0 5.4M20.5 20a6 6 0 0 0-2.2-4.4" opacity="0.6" />
  </svg>
);

export default function TeamManager({
  members,
  invitations,
  workspaces,
  roles,
  selfAccountId,
}: {
  members: TeamMember[];
  invitations: PendingInvitation[];
  workspaces: Workspace[];
  roles: RoleOption[];
  /** So the current user's own row can explain why it cannot be removed here. */
  selfAccountId: string | null;
}) {
  const [inviteState, invite] = useActionState(inviteMember, INITIAL);
  const [grantState, setGrants] = useActionState(updateGrants, INITIAL);
  const [removeState, remove] = useActionState(removeMember, INITIAL);
  const [resendState, resend] = useActionState(resendInvitation, INITIAL);
  const [inviteOpen, setInviteOpen] = useState(false);

  return (
    <>
      {/*
        Members and pending invitations first; the invite form lives in a modal.

        It used to be a permanently-open card at the top, which meant the colleagues an
        administrator came to check sat below a form they were not using. With nobody invited yet the
        empty state carries the same explanation the card's subtitle did, so nothing is lost.
      */}
      {members.length === 0 && invitations.length === 0 ? (
        <EmptyState
          icon={TEAM_ICON}
          title="No colleagues yet"
          body={
            <>
              Invite someone to help run this organisation. You choose their role{" "}
              <strong>per workspace</strong>, so a colleague can administer one project and be
              view-only on another. They receive a single-use link valid for 14 days that signs them
              in and asks them to set their own password &mdash; no temporary password is ever sent,
              and the link stops working once used.
            </>
          }
          actionLabel="+ Invite a colleague"
          onAction={() => setInviteOpen(true)}
        />
      ) : (
        <div className="pcard__head pcard__head--bare">
          <button
            type="button"
            className="btn btn--primary btn--small"
            onClick={() => setInviteOpen(true)}
          >
            + Invite a colleague
          </button>
        </div>
      )}

      {invitations.length > 0 && (
        <section className="pcard">
          <h2 className="pcard__title">Pending invitations</h2>
          <p className="pcard__sub">
            Sent but not yet accepted. Inviting the same address again replaces the earlier
            invitation rather than adding a second one.
          </p>
          <ul className="rolelist">
            {invitations.map((invitation) => (
              <li key={invitation.email} className="rolelist__row">
                <div className="rolelist__head">
                  <strong>{invitation.email}</strong>
                  {/*
                    `expired` comes from the server, not from comparing dates here — a skewed
                    laptop clock would otherwise offer "Resend" on a live invitation, or hide
                    it on a lapsed one.
                  */}
                  <span className={invitation.expired ? "chip chip--bad" : "chip chip--quiet"}>
                    {invitation.expired ? "expired" : "expires"}{" "}
                    {invitation.expires_at
                      ? new Date(invitation.expires_at).toLocaleDateString()
                      : "—"}
                  </span>
                </div>
                <p className="rolelist__desc">
                  {invitation.grants
                    .map((grant) => {
                      const workspace = workspaces.find(
                        (w) => w.id === grant.workspace_id,
                      );
                      return `${workspace?.name ?? grant.workspace_id}: ${grant.role.replace(/_/g, " ")}`;
                    })
                    .join(" · ")}
                </p>
                {/*
                  Offered on live invitations too, not only expired ones: the commonest reason
                  a colleague has not joined is that the email never arrived, and making them
                  wait 14 days for the button to appear would be absurd. Resending supersedes
                  the earlier link either way.
                */}
                <form action={resend} className="teaminvite__resend">
                  <input type="hidden" name="email" value={invitation.email} />
                  <button type="submit" className="btn btn--ghost btn--small">
                    {invitation.expired ? "Send a new link" : "Resend"}
                  </button>
                  <span className="wsform__dangerHint">
                    {invitation.expired
                      ? "Their link has expired. A new one is valid for another 14 days."
                      : "Sends a fresh link. The earlier one stops working."}
                  </span>
                </form>
              </li>
            ))}
          </ul>

          <Notice state={resendState} />
        </section>
      )}

      <section className="pcard">
        <div className="pcard__head">
          <h2 className="pcard__title">Members</h2>
          <p className="pcard__sub">
            Access is granted per workspace, so a role held on one project does not apply to
            another.
          </p>
        </div>

        {members.length === 0 ? (
          <p className="authform__hint">
            You are the only member. Invite a colleague above.
          </p>
        ) : (
          members.map((member) => {
            const current: Record<string, string> = {};
            for (const grant of member.grants) {
              if (grant.status === "active" && grant.workspace_id && grant.role) {
                current[grant.workspace_id] = grant.role;
              }
            }
            const isSelf = member.account_id === selfAccountId;

            return (
              <div className="teamrow" key={member.account_id}>
                <div className="teamrow__head">
                  <strong>{member.full_name || member.email || member.account_id}</strong>
                  {member.email && (
                    <span className="teamrow__email">{member.email}</span>
                  )}
                  {isSelf && <span className="chip chip--quiet">you</span>}
                </div>

                <form action={setGrants} className="wsform">
                  <input type="hidden" name="account_id" value={member.account_id} />
                  <RoleGrid
                    workspaces={workspaces}
                    roles={roles}
                    current={current}
                    idPrefix={member.account_id}
                  />
                  <div className="wsform__row">
                    <Submitting>Save access</Submitting>
                  </div>
                </form>

                {/*
                  No remove button on your own row. The API refuses it with a 409 — an
                  organisation whose last owner removed themselves cannot be administered by
                  anyone, and the recovery needs an operator with database access. Absent
                  rather than disabled, since it will never become available to them.
                */}
                {!isSelf && (
                  <form action={remove} className="wsform__danger">
                    <input type="hidden" name="account_id" value={member.account_id} />
                    <input
                      type="hidden"
                      name="name"
                      value={member.full_name || member.email || ""}
                    />
                    <button type="submit" className="btn btn--ghost">
                      Remove from team
                    </button>
                    <span className="wsform__dangerHint">
                      Removes their access to every workspace. Their account, areas and alert
                      history are untouched.
                    </span>
                  </form>
                )}
              </div>
            );
          })
        )}

        <Notice state={grantState} />
        <Notice state={removeState} />
      </section>
      <Modal
        open={inviteOpen}
        onClose={() => setInviteOpen(false)}
        title="Invite a colleague"
        description="They receive a single-use link valid for 14 days. Choose their role per workspace."
      >
        <form action={invite} className="wsform">
          <label className="authform__label" htmlFor="invite-email">
            Work email address
          </label>
          <input
            id="invite-email"
            name="email"
            type="email"
            className="authform__input"
            placeholder="colleague@yourorganisation.ng"
            required
          />

          <RoleGrid
            workspaces={workspaces}
            roles={roles}
            current={{}}
            idPrefix="invite"
          />

          <div className="wsform__row">
            <Submitting>Send invitation</Submitting>
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => setInviteOpen(false)}
            >
              Cancel
            </button>
          </div>
        </form>

        <Notice state={inviteState} />
        <Notice state={inviteState} />
      </Modal>

    </>
  );
}
