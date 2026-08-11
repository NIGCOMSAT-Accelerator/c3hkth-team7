"""Roles, permissions, and what each account kind may reach.

## One definition, enforced on both sides

A role decides two things that must never disagree:

  * which **side-nav sections** a person sees in the portal;
  * which **API scopes** a key minted under that role may carry.

Those are the same question asked twice, so they resolve from one table. The alternative —
a permission list in the frontend and a scope list in the backend — is how a UI ends up
hiding a button while the endpoint behind it stays reachable, which is not access control at
all.

**Hiding a nav item is not enforcement.** `permissions_for` drives the navigation, and
`require_permission` guards the route. A caller who skips the UI hits the same table.

## Why six roles rather than a permission matrix

Named roles are what an aggregator's administrator can reason about when adding a colleague.
A raw permission grid is more expressive and, in practice, produces either everyone-as-owner
or a misconfigured grant nobody can audit. `CUSTOM` exists for the cases the five named roles
genuinely do not cover, and it carries an explicit permission set rather than being a sixth
preset.

## Individual accounts have no roles at all

An individual is one person managing their own plots — there is no team to divide access
among, no workspace, and no API key. So `Permission` applies only to commercial accounts, and
the individual portal is defined by absence rather than by a role that grants everything.
A `VIEW_ONLY` individual would be a nonsense: they would be unable to change their own
delivery channel.
"""

from __future__ import annotations

from enum import Enum

from app.iam.models import ApiKeyScope


class Permission(str, Enum):
    """One capability. The unit both the nav and the API guard against.

    Deliberately coarse — a permission per portal *section*, not per button. A finer grid
    would be more expressive and much harder for an administrator to reason about, and the
    scopes it maps onto are themselves section-shaped.
    """

    #: Read assessments, alerts and the dashboard. Every role has this; without it there is
    #: nothing to log in for.
    VIEW_DASHBOARD = "dashboard:view"

    #: Onboard and edit customers, bind areas.
    MANAGE_CUSTOMERS = "customers:manage"
    #: Read customers without changing them.
    VIEW_CUSTOMERS = "customers:view"

    #: Create, rotate and revoke API keys. Separate from `MANAGE_INTEGRATION` because a key
    #: is a credential: an engineer needs to *use* the API, which is not the same as being
    #: able to mint new access to it.
    MANAGE_KEYS = "keys:manage"
    #: Webhooks, and reading the developer reference.
    MANAGE_INTEGRATION = "integration:manage"

    #: Create workspaces and activate intelligence tracks on them. A commercial decision —
    #: activating Public Health Intelligence changes what the organisation is buying.
    MANAGE_WORKSPACE = "workspace:manage"
    #: Invite colleagues and set their roles.
    MANAGE_TEAM = "team:manage"
    #: Change organisation name, sector, billing contact.
    MANAGE_BILLING = "billing:manage"

    #: Fahis verification review, and blacklisting a source or an area.
    MANAGE_COMPLIANCE = "compliance:manage"
    #: Trigger an immediate assessment. Costs real satellite catalogue quota, so it is a
    #: permission rather than something every viewer can do.
    TRIGGER_SCAN = "scan:trigger"


class Role(str, Enum):
    """The named roles an administrator picks from.

    Values are stored on the membership document, so renaming one is a migration. The labels
    below are what the portal shows.
    """

    OWNER = "organization_owner"
    OPERATIONS = "operations"
    ENGINEERING = "engineering"
    COMPLIANCE = "compliance"
    VIEW_ONLY = "view_only"
    CUSTOM = "custom"


#: What the portal calls each role, and who it is for.
#:
#: Written in the second person because it is read while choosing a role for a colleague —
#: "best for people who…" is a more useful prompt than a permission list they would have to
#: interpret.
ROLE_LABELS: dict[Role, tuple[str, str]] = {
    Role.OWNER: (
        "Organization Owner",
        "Best for business owners and company administrators. Full access, including "
        "billing, team and workspace settings.",
    ),
    Role.OPERATIONS: (
        "Operations",
        "Best for people who need full access to the SHELTER platform, but do not need to "
        "change business settings.",
    ),
    Role.ENGINEERING: (
        "Engineering",
        "Best for people integrating the SHELTER Partner API with your workspace and "
        "intelligence tracks.",
    ),
    Role.COMPLIANCE: (
        "Compliance",
        "Manage verification, review and blacklist operations.",
    ),
    Role.VIEW_ONLY: (
        "View-Only",
        "Best for people who need to view the dashboard, but do not need to make any "
        "updates.",
    ),
    Role.CUSTOM: (
        "Custom",
        "Choose exactly which sections and API scopes this person may use.",
    ),
}


P = Permission

#: Role → permissions. The single source of truth for both nav and API enforcement.
#:
#: `OWNER` is spelled out rather than given a wildcard. A wildcard silently grants every
#: permission added later, which is the wrong default for the role that can change billing
#: and remove colleagues — a new capability should be granted deliberately, even to an owner.
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.OWNER: frozenset(
        {
            P.VIEW_DASHBOARD,
            P.VIEW_CUSTOMERS,
            P.MANAGE_CUSTOMERS,
            P.MANAGE_KEYS,
            P.MANAGE_INTEGRATION,
            P.MANAGE_WORKSPACE,
            P.MANAGE_TEAM,
            P.MANAGE_BILLING,
            P.MANAGE_COMPLIANCE,
            P.TRIGGER_SCAN,
        }
    ),
    # Everything operational, nothing commercial. Explicitly NOT `MANAGE_BILLING` or
    # `MANAGE_WORKSPACE` — those change what the organisation is buying, which is the owner's
    # decision.
    #
    # **`MANAGE_TEAM` is granted**, which it was not initially. Operations runs the platform
    # day to day, and day-to-day includes a colleague whose invitation expired unaccepted over
    # a holiday. Routing every such resend through an Organization Owner makes onboarding wait
    # on the one person least likely to be watching a queue.
    #
    # The escalation ceiling is what makes this safe rather than a widening: `grantable_roles`
    # still refuses `OWNER` to anyone without `MANAGE_BILLING`, so Operations can invite and
    # resend but cannot create an owner — and therefore cannot reach billing or workspace
    # activation through someone they minted.
    Role.OPERATIONS: frozenset(
        {
            P.VIEW_DASHBOARD,
            P.VIEW_CUSTOMERS,
            P.MANAGE_CUSTOMERS,
            P.MANAGE_INTEGRATION,
            P.MANAGE_TEAM,
            P.TRIGGER_SCAN,
        }
    ),
    # Integration work. Holds `MANAGE_KEYS` because rotating a leaked key at 2am is exactly
    # an engineer's job, and requiring an owner for it means the leak lives longer.
    Role.ENGINEERING: frozenset(
        {
            P.VIEW_DASHBOARD,
            P.VIEW_CUSTOMERS,
            P.MANAGE_KEYS,
            P.MANAGE_INTEGRATION,
            P.TRIGGER_SCAN,
        }
    ),
    Role.COMPLIANCE: frozenset(
        {P.VIEW_DASHBOARD, P.VIEW_CUSTOMERS, P.MANAGE_COMPLIANCE}
    ),
    Role.VIEW_ONLY: frozenset({P.VIEW_DASHBOARD, P.VIEW_CUSTOMERS}),
    # Empty by design: a CUSTOM membership carries its own permission list, and falling back
    # to a default set would silently grant something nobody chose.
    Role.CUSTOM: frozenset(),
}


#: Permission → the API scopes it authorises a key to carry.
#:
#: This is what makes "side-nav permission extended to the API endpoints" true rather than
#: aspirational: a member without `MANAGE_CUSTOMERS` cannot mint a key with
#: `customers:write`, so they cannot do through the API what the portal will not show them.
PERMISSION_SCOPES: dict[Permission, frozenset[ApiKeyScope]] = {
    P.VIEW_CUSTOMERS: frozenset({ApiKeyScope.READ}),
    P.MANAGE_CUSTOMERS: frozenset({ApiKeyScope.READ, ApiKeyScope.WRITE}),
    P.MANAGE_INTEGRATION: frozenset({ApiKeyScope.WEBHOOKS}),
    P.TRIGGER_SCAN: frozenset({ApiKeyScope.SCAN}),
    # The rest are portal-only concerns with no API surface. Listed explicitly rather than
    # omitted, so a reader can see the mapping is complete rather than partial.
    P.VIEW_DASHBOARD: frozenset(),
    P.MANAGE_KEYS: frozenset(),
    P.MANAGE_WORKSPACE: frozenset(),
    P.MANAGE_TEAM: frozenset(),
    P.MANAGE_BILLING: frozenset(),
    P.MANAGE_COMPLIANCE: frozenset(),
}


def permissions_for(
    role: Role | str, custom: list[str] | None = None
) -> frozenset[Permission]:
    """The permissions a membership grants.

    `custom` is only consulted for `Role.CUSTOM`. Ignored for a named role rather than
    merged: a stored custom list on an `OPERATIONS` membership would be an invisible
    privilege escalation, and silently honouring it is worse than ignoring it.

    An unrecognised role resolves to the empty set — fail closed. A membership carrying a
    role this build does not know about (a downgrade, a hand-edited document) must not be
    treated as an owner.
    """
    try:
        resolved = Role(role)
    except ValueError:
        return frozenset()

    if resolved is Role.CUSTOM:
        out: set[Permission] = set()
        for name in custom or []:
            try:
                out.add(Permission(name))
            except ValueError:
                # An unknown permission name is dropped, not fatal. A stale name from an
                # older build should degrade that person's access, never break their login.
                continue
        # Every role can see the dashboard; a custom role with nothing at all could not use
        # the portal to discover it had no access.
        out.add(P.VIEW_DASHBOARD)
        return frozenset(out)

    return ROLE_PERMISSIONS.get(resolved, frozenset())


def scopes_for(permissions: frozenset[Permission]) -> frozenset[ApiKeyScope]:
    """The API scopes a member with these permissions may put on a key.

    Used when minting: a requested scope outside this set is refused. That is the enforcement
    half of "permissions extend to the API" — without it, a View-Only member could mint a
    `customers:write` key and do through the API exactly what the portal denies them.
    """
    out: set[ApiKeyScope] = set()
    for permission in permissions:
        out |= PERMISSION_SCOPES.get(permission, frozenset())
    return frozenset(out)
