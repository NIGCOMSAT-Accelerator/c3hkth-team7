import { api } from "@/lib/api";
import { ROLE_GUIDE } from "@/lib/roles";
import { getAccount, getSessionToken, requirePermission } from "@/lib/session";

import TeamManager from "./TeamManager";

export const metadata = { title: "Team" };
export const dynamic = "force-dynamic";

/**
 * Team management — invite colleagues and set what each may do, per workspace.
 *
 * Aggregator-only, gated on `team:manage`.
 *
 * ## Roles are shown with their permissions, not just their names
 *
 * "Operations" tells an administrator nothing on its own. The consequence of the choice —
 * which sections that person reaches, and which API scopes they may put on a key — is what
 * they are actually deciding, so it is on the page rather than in documentation.
 *
 * The list comes from the backend's own table (`GET /iam/roles`), so a role added there
 * appears here without a frontend change and the two cannot disagree about what a role means.
 *
 * ## Access is per workspace
 *
 * The invite and edit forms render one role selector per workspace. That is not a UI
 * flourish — `require_workspace_permission` resolves authority on the named workspace alone,
 * so a colleague who is an owner on one project genuinely cannot act as one on another.
 */
export default async function TeamPage() {
  await requirePermission("team:manage", "/portal/team");

  const token = await getSessionToken();

  // Every read degrades to empty rather than throwing: the roles reference below is useful
  // even when the team list is unavailable, and a 500 would take both away.
  const [roles, account, members, invitations, workspaces, assignable] =
    await Promise.all([
      ROLE_GUIDE(),
      getAccount(),
      token ? api.listTeam(token).catch(() => []) : Promise.resolve([]),
      token ? api.listInvitations(token).catch(() => []) : Promise.resolve([]),
      token ? api.listWorkspaces(token).catch(() => []) : Promise.resolve([]),
      token ? api.assignableRoles(token).catch(() => []) : Promise.resolve([]),
    ]);

  return (
    <>
      <header className="pcard__head">
        <h1 className="portal__title">Team</h1>
        <p className="portal__lede">
          Invite colleagues and choose what each of them can do, on each workspace.
        </p>
      </header>

      {workspaces.length === 0 ? (
        <section className="pcard">
          <p className="authform__hint">
            Create a workspace first — access is granted per workspace, so there is nothing to
            assign yet. Open <a href="/portal/workspace">Workspaces</a> to make one.
          </p>
        </section>
      ) : (
        <TeamManager
          members={members}
          invitations={invitations}
          workspaces={workspaces}
          roles={assignable}
          selfAccountId={account?.id ?? null}
        />
      )}

      <section className="pcard">
        <h2 className="pcard__title">Roles</h2>
        <p className="pcard__sub">
          What each role can reach. Every role can view the dashboard.
        </p>

        <ul className="rolelist">
          {roles.map((role) => (
            <li key={role.value} className="rolelist__row">
              <div className="rolelist__head">
                <strong>{role.label}</strong>
                {role.value === "custom" && (
                  <span className="chip chip--quiet">choose per person</span>
                )}
              </div>
              <p className="rolelist__desc">{role.description}</p>
              {role.permissions.length > 0 && (
                <div className="pcard__chips">
                  {role.permissions.map((p) => (
                    <span key={p} className="chip chip--quiet">
                      {p.replace(":", " · ")}
                    </span>
                  ))}
                </div>
              )}
              {role.scopes.length > 0 && (
                <p className="rolelist__scopes">
                  API scopes: <span className="mono">{role.scopes.join(", ")}</span>
                </p>
              )}
            </li>
          ))}
        </ul>
      </section>
    </>
  );
}
