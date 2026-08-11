"""Team membership — a person, a workspace, and the role they hold *there*.

## The role belongs on the edge, not on the account

The first version of this stored one role per account, and `org_members` carried a unique
index on `account_id` to enforce it. That models "Amina is Engineering" — but the brief is
that a team member is invited and assigned to **a workspace or many workspaces, with the
assigned roles**. Those are different statements, and only the second one is useful to an
aggregator running several projects:

    Amina — Engineering on "Bayelsa flood pilot", View-Only on "Kebbi rice season"

With a role on the account, granting her the access she needs on one project silently grants
it on every other. So a membership is one document per **(account, workspace)** pair, and the
account-level unique index had to go — it was actively preventing the requirement.

## Effective permissions are the union; authority is per workspace

Two different questions, and conflating them is how this kind of model leaks:

  * **"Should the Team item appear in her side-nav?"** — union across her workspaces. If she
    can manage the team anywhere, the section is worth showing; the page itself then shows
    only the workspaces where she actually holds it.
  * **"May she rotate a key on THIS workspace?"** — the role on that one edge, and nothing
    else. `permissions_in()` never consults another workspace.

The union is deliberately confined to navigation and to org-wide routes that have no
workspace to speak of. Every workspace-scoped route resolves `permissions_in`, so being an
owner of one project grants nothing on another. `require_workspace_permission` is the guard
that holds that line.

## Invitations are single-use, and redeeming one creates the account

An invitation names the workspaces and roles up front and is hashed at rest, exactly like an
email verification token (`security.hash_token`) — a leaked database dump must not be a set
of usable invitations. Redeeming it creates the membership documents; until then the invited
person has no access at all, so an invitation sent to a mistyped address grants nothing to
whoever receives it.

**One email, one credential, no password in transit.** The first version required the invitee
to already hold a verified account, so the real journey was: read invite → discover you need
an account → sign up → verify that email → return to the link → accept. `redeem_team_invitation`
now creates the account itself with **no password hash**, and issues a session that can do
nothing but set one.

A generated one-time *password* was the obvious alternative and is the weaker one. A password
is valid at `POST /iam/login` — public and reachable by anyone, so it is an online guessing
target for its whole 14-day life — and it must be short enough to type, perhaps 50-60 bits
against this token's 256. It also survives being forwarded in a reply-all. The token is usable
only by whoever holds the URL, and `find_one_and_delete` destroys it on first redemption.

**An invitation cannot grant more than its sender holds.** Checked at creation against the
sender's own `permissions_in` for each named workspace, because otherwise an Engineering
member could invite themselves back as an Organization Owner — a one-hop privilege
escalation that no amount of nav hiding would catch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum

from app.iam import security
from app.iam.roles import Permission, Role, permissions_for

#: How long an invitation stays usable.
#:
#: Longer than email verification (48h) because accepting one needs a colleague to create an
#: account first, which may wait for a working day. Not indefinite: an invitation is standing
#: authorisation to join an organisation, and one found in a mailbox a year later should be
#: dead rather than live.
INVITE_TTL_HOURS = 14 * 24


class MemberStatus(str, Enum):
    """Where a membership is in its life.

    `REVOKED` is kept rather than deleted so the audit log's `target_id` still resolves to
    something — "who removed Amina, and when" is unanswerable if the row vanishes.
    """

    #: Invited, not yet accepted. No access.
    INVITED = "invited"
    ACTIVE = "active"
    REVOKED = "revoked"


def build_member_document(
    *,
    organisation_id: str,
    account_id: str,
    workspace_id: str,
    role: Role | str,
    permissions: list[str] | None = None,
    invited_by: str | None = None,
) -> dict:
    """One (account, workspace) membership.

    `permissions` is stored only for `Role.CUSTOM` and ignored otherwise — see
    `permissions_for`, which will not merge a stray list into a named role. Storing it anyway
    would leave a document whose fields imply an access level it does not have.
    """
    resolved = Role(role) if not isinstance(role, Role) else role
    return {
        "organisation_id": organisation_id,
        "account_id": account_id,
        "workspace_id": workspace_id,
        "role": resolved.value,
        "permissions": list(permissions or []) if resolved is Role.CUSTOM else [],
        "status": MemberStatus.ACTIVE.value,
        "invited_by": invited_by,
        "created_at": datetime.now(timezone.utc),
    }


def build_invitation(
    *,
    organisation_id: str,
    email: str,
    grants: list[dict],
    invited_by: str,
    first_name: str = "",
    last_name: str = "",
    organisation_name: str = "",
) -> tuple[dict, str]:
    """An invitation document and its single-use plaintext token.

    Returns `(document, plaintext)` — the plaintext is emailed and never stored, so this is
    the only moment it exists. `grants` is a list of `{workspace_id, role, permissions}`.

    The email is lowercased because that is how accounts are keyed; an invitation to
    `Amina@Coop.NG` must match the account created as `amina@coop.ng`, or accepting it would
    silently do nothing.
    """
    plaintext = security.new_verification_token()
    now = datetime.now(timezone.utc)
    return (
        {
            "organisation_id": organisation_id,
            "email": email.strip().lower(),
            # Carried on the invitation because redeeming it CREATES the account, and an
            # account created with empty names shows an inviting organisation a blank row in
            # its own team list. The invitee corrects them later; this is a sensible start,
            # not a claim about who they are.
            "first_name": first_name.strip(),
            "last_name": last_name.strip(),
            "organisation_name": organisation_name.strip(),
            "token_hash": security.hash_token(plaintext),
            "grants": grants,
            "invited_by": invited_by,
            "status": MemberStatus.INVITED.value,
            "created_at": now,
            "expires_at": now + timedelta(hours=INVITE_TTL_HOURS),
        },
        plaintext,
    )


def effective_permissions(edges: list[dict]) -> frozenset[Permission]:
    """Union of permissions across every active membership.

    Drives the side-nav and org-wide routes only. A workspace-scoped route must use
    `permissions_in`, or holding a permission on one project would grant it on all of them —
    which is the exact failure the per-workspace model exists to prevent.
    """
    out: set[Permission] = set()
    for edge in edges:
        if edge.get("status") != MemberStatus.ACTIVE.value:
            continue
        out |= permissions_for(edge.get("role", ""), edge.get("permissions"))
    return frozenset(out)


def permissions_in(edges: list[dict], workspace_id: str) -> frozenset[Permission]:
    """Permissions on one workspace. Empty if there is no active membership on it.

    Empty rather than falling back to the union — a member with no edge to a workspace has no
    authority over it, and inheriting anything here would make every workspace boundary
    decorative.
    """
    for edge in edges:
        if (
            edge.get("workspace_id") == workspace_id
            and edge.get("status") == MemberStatus.ACTIVE.value
        ):
            return permissions_for(edge.get("role", ""), edge.get("permissions"))
    return frozenset()


def display_role(edges: list[dict]) -> str | None:
    """A single role name for the profile line, or None if it varies.

    None when a member holds different roles on different workspaces, because there is no
    honest single answer — the portal then says "varies by workspace" and links to the team
    page rather than picking one and misrepresenting the other.
    """
    active = {
        edge.get("role")
        for edge in edges
        if edge.get("status") == MemberStatus.ACTIVE.value
    }
    return active.pop() if len(active) == 1 else None


def grantable_roles(granter: frozenset[Permission]) -> list[Role]:
    """Which roles this member may assign on a workspace they administer.

    `MANAGE_TEAM` is required to invite at all; an owner may create another owner, and
    anyone else may not. Without that last rule, a member with `team:manage` but not
    `billing:manage` could mint an Organization Owner and inherit billing through them.
    """
    if Permission.MANAGE_TEAM not in granter:
        return []
    if Permission.MANAGE_BILLING in granter:
        return list(Role)
    return [role for role in Role if role is not Role.OWNER]
