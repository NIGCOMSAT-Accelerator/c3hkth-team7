"""Team membership and per-workspace RBAC.

These tests exist because the interesting property of this model is not "does a role map to
permissions" — `test_iam.py` covers that — but **does a role stop at its workspace**. A member
who is an Organization Owner on one project and View-Only on another must not be able to act
as an owner on the second, and every test below is a way that could silently fail.

Pure functions over edge dictionaries, so no Mongo is needed: `team.py` deliberately takes the
edges as an argument rather than fetching them, which is what makes the boundary testable
offline.
"""

from __future__ import annotations

from app.iam import team
from app.iam.models import ApiKeyScope
from app.iam.roles import Permission, Role, permissions_for, scopes_for


def _edge(workspace_id: str, role: Role, *, status: str = "active", permissions=None) -> dict:
    return {
        "organisation_id": "ORG1",
        "account_id": "ACC1",
        "workspace_id": workspace_id,
        "role": role.value,
        "permissions": permissions or [],
        "status": status,
    }


# --------------------------------------------------------------------------- #
# The boundary
# --------------------------------------------------------------------------- #


def test_a_role_does_not_leak_across_workspaces():
    """**The test this module exists for.**

    Owner on one project, View-Only on another. The union says they can manage a team; the
    per-workspace answer must say they cannot do it *here*. If `permissions_in` ever fell back
    to the union, every workspace boundary in the product would be decorative.
    """
    edges = [_edge("WS_PILOT", Role.OWNER), _edge("WS_SEASON", Role.VIEW_ONLY)]

    assert Permission.MANAGE_TEAM in team.permissions_in(edges, "WS_PILOT")
    assert Permission.MANAGE_TEAM not in team.permissions_in(edges, "WS_SEASON")
    assert Permission.MANAGE_WORKSPACE not in team.permissions_in(edges, "WS_SEASON")


def test_no_membership_on_a_workspace_grants_nothing():
    """Not even dashboard access. A member with no edge has no authority, and inheriting
    anything from another workspace is the failure mode this guards."""
    edges = [_edge("WS_PILOT", Role.OWNER)]
    assert team.permissions_in(edges, "WS_UNRELATED") == frozenset()


def test_a_revoked_edge_grants_nothing():
    """Revoked rather than deleted, so the audit log still resolves — which means status has
    to be honoured on read or a removed colleague keeps their access."""
    edges = [_edge("WS_PILOT", Role.OWNER, status=team.MemberStatus.REVOKED.value)]
    assert team.permissions_in(edges, "WS_PILOT") == frozenset()
    assert team.effective_permissions(edges) == frozenset()


def test_an_invited_but_unaccepted_edge_grants_nothing():
    """An invitation is not access. Until it is accepted the person must be able to do
    nothing at all, or sending one to a mistyped address would be a grant."""
    edges = [_edge("WS_PILOT", Role.OWNER, status=team.MemberStatus.INVITED.value)]
    assert team.effective_permissions(edges) == frozenset()


def test_the_union_is_the_union_and_nothing_more():
    """Drives the side-nav: a member who can manage integrations anywhere sees the section."""
    edges = [_edge("WS_A", Role.ENGINEERING), _edge("WS_B", Role.COMPLIANCE)]
    union = team.effective_permissions(edges)

    assert Permission.MANAGE_KEYS in union  # from Engineering
    assert Permission.MANAGE_COMPLIANCE in union  # from Compliance
    # Neither role holds these, so the union must not invent them.
    assert Permission.MANAGE_TEAM not in union
    assert Permission.MANAGE_BILLING not in union


# --------------------------------------------------------------------------- #
# Display
# --------------------------------------------------------------------------- #


def test_display_role_is_none_when_it_varies():
    """None is the honest answer, and the portal renders "varies by workspace".

    Picking one of the two would misrepresent the other — and if it picked the wider one it
    would tell a View-Only member they are an owner.
    """
    mixed = [_edge("WS_A", Role.OWNER), _edge("WS_B", Role.VIEW_ONLY)]
    assert team.display_role(mixed) is None

    same = [_edge("WS_A", Role.ENGINEERING), _edge("WS_B", Role.ENGINEERING)]
    assert team.display_role(same) == Role.ENGINEERING.value


# --------------------------------------------------------------------------- #
# Escalation
# --------------------------------------------------------------------------- #


def test_only_an_owner_may_create_another_owner():
    """Otherwise a member with `team:manage` but not `billing:manage` could mint an
    Organization Owner and inherit billing authority through them — one hop, no nav involved.
    """
    owner = permissions_for(Role.OWNER)
    assert Role.OWNER in team.grantable_roles(owner)

    # A CUSTOM role holding team management but not billing.
    delegate = permissions_for(
        Role.CUSTOM, [Permission.MANAGE_TEAM.value, Permission.VIEW_CUSTOMERS.value]
    )
    grantable = team.grantable_roles(delegate)
    assert Role.OWNER not in grantable
    assert Role.ENGINEERING in grantable


def test_without_team_management_no_role_is_assignable():
    """Operations is deliberately absent from this list.

    It holds `MANAGE_TEAM` so it can invite colleagues and resend an invitation that lapsed —
    day-to-day platform work that should not queue behind the one Organization Owner. Its
    ceiling is asserted in `test_operations_can_invite_but_not_create_an_owner`.
    """
    for role in (Role.ENGINEERING, Role.COMPLIANCE, Role.VIEW_ONLY):
        assert team.grantable_roles(permissions_for(role)) == []


def test_operations_can_invite_but_not_create_an_owner():
    """**The ceiling that makes granting Operations `team:manage` safe.**

    Without it the grant would be a widening: an Operations member could mint an Organization
    Owner and reach billing and workspace activation through them — the two things Operations
    is specifically not trusted with. `grantable_roles` refuses `OWNER` to anyone lacking
    `MANAGE_BILLING`, which Operations does not hold.
    """
    ops = permissions_for(Role.OPERATIONS)

    assert Permission.MANAGE_TEAM in ops
    assert Permission.MANAGE_BILLING not in ops
    assert Permission.MANAGE_WORKSPACE not in ops

    grantable = team.grantable_roles(ops)
    assert Role.OWNER not in grantable, (
        "Operations must not be able to create an Organization Owner — that is a one-hop "
        "route to the billing and workspace authority it is denied directly"
    )
    assert Role.ENGINEERING in grantable and Role.VIEW_ONLY in grantable


# --------------------------------------------------------------------------- #
# Custom roles reach the API, not just the nav
# --------------------------------------------------------------------------- #


def test_a_custom_role_extends_to_api_scopes():
    """The user's brief: "side nav permission (Read/Write) that is extended to the API
    Endpoints or Scopes as well". A custom read-only grant must yield a read-only key.
    """
    read_only = permissions_for(Role.CUSTOM, [Permission.VIEW_CUSTOMERS.value])
    assert scopes_for(read_only) == frozenset({ApiKeyScope.READ})

    read_write = permissions_for(Role.CUSTOM, [Permission.MANAGE_CUSTOMERS.value])
    assert ApiKeyScope.WRITE in scopes_for(read_write)


def test_view_only_cannot_reach_a_write_scope():
    """The enforcement half of the same rule, checked at the scope layer that key minting
    consults. A View-Only member who could mint `customers:write` would make the hidden nav
    item the only thing standing between them and the API."""
    assert ApiKeyScope.WRITE not in scopes_for(permissions_for(Role.VIEW_ONLY))


def test_a_custom_role_with_nothing_still_sees_the_dashboard():
    """Otherwise the person cannot log in to discover they have no access, and the state is
    indistinguishable from a broken account."""
    assert permissions_for(Role.CUSTOM, []) == frozenset({Permission.VIEW_DASHBOARD})


def test_a_stored_custom_list_is_ignored_for_a_named_role():
    """A hand-edited document must not escalate. `permissions_for` ignores `custom` unless the
    role is CUSTOM — merging it would be an invisible grant with no audit trail."""
    resolved = permissions_for(Role.VIEW_ONLY, [Permission.MANAGE_BILLING.value])
    assert Permission.MANAGE_BILLING not in resolved


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #


def test_a_named_role_never_stores_a_permission_list():
    """Storing one would leave a document whose fields imply access it does not have — the
    kind of discrepancy that makes an audit unanswerable."""
    document = team.build_member_document(
        organisation_id="ORG1",
        account_id="ACC1",
        workspace_id="WS_A",
        role=Role.OPERATIONS,
        permissions=[Permission.MANAGE_BILLING.value],
    )
    assert document["permissions"] == []


def test_a_custom_role_keeps_its_permission_list():
    document = team.build_member_document(
        organisation_id="ORG1",
        account_id="ACC1",
        workspace_id="WS_A",
        role=Role.CUSTOM,
        permissions=[Permission.VIEW_CUSTOMERS.value],
    )
    assert document["permissions"] == [Permission.VIEW_CUSTOMERS.value]


def test_an_invitation_stores_only_a_hash():
    """A leaked database dump must not be a set of usable invitations — the same reason email
    verification tokens are hashed."""
    document, plaintext = team.build_invitation(
        organisation_id="ORG1",
        email="Amina@Coop.NG",
        grants=[{"workspace_id": "WS_A", "role": Role.ENGINEERING.value}],
        invited_by="ACC1",
    )

    assert plaintext
    assert plaintext not in str(document)
    assert document["token_hash"] != plaintext
    # Lowercased, because accounts are keyed that way: an invitation to `Amina@Coop.NG` must
    # match the account created as `amina@coop.ng` or accepting it silently does nothing.
    assert document["email"] == "amina@coop.ng"


def test_an_invitation_expires():
    document, _ = team.build_invitation(
        organisation_id="ORG1",
        email="a@b.ng",
        grants=[{"workspace_id": "WS_A", "role": Role.VIEW_ONLY.value}],
        invited_by="ACC1",
    )
    assert document["expires_at"] > document["created_at"]
    assert document["status"] == team.MemberStatus.INVITED.value


# --------------------------------------------------------------------------- #
# Structural
# --------------------------------------------------------------------------- #


def test_workspace_scoped_routes_use_the_workspace_guard():
    """A workspace-scoped route guarded by `require_permission` would check the UNION, which
    is exactly the leak `test_a_role_does_not_leak_across_workspaces` forbids at the model
    layer. Asserted structurally because it is a mistake that reads as correct.
    """
    import pathlib
    import re

    source = pathlib.Path("app/api/routes/iam.py").read_text()

    # Each `{workspace_id}` route and the guard in its signature.
    for match in re.finditer(
        r'@router\.(get|post|patch|put|delete)\(\s*\n?\s*"(/workspaces/\{workspace_id\}[^"]*)"',
        source,
    ):
        start = match.end()
        signature = source[start : source.index(") ->", start)]
        assert "require_workspace_permission" in signature, (
            f"{match.group(2)} addresses one workspace but is guarded by the union — "
            "a member with the permission on any workspace would pass."
        )


def test_key_minting_resolves_scopes_per_workspace():
    """**The subtlest half of the boundary.**

    Key minting must resolve the caller's role on the workspace the key names, not the union.
    Using the union would leave the boundary holding on every route except the one that
    produces a lasting credential — an owner of one project could mint a write key naming a
    project where they are View-Only, then use it there.

    Asserted structurally because the failure is invisible: `member_permissions` and
    `member_permissions_in` have the same shape and the wrong one still returns a plausible
    set.
    """
    import pathlib

    source = pathlib.Path("app/api/routes/iam.py").read_text()
    start = source.index('@router.post("/api-keys"')
    body = source[start : source.index("@router.", start + 10)]

    assert "member_permissions_in" in body, (
        "key minting must resolve scopes from the role on the named workspace"
    )
    assert "workspace_id=workspace_id" in body, (
        "the minted key must record which workspace it is scoped to"
    )


def test_the_account_level_unique_index_is_dropped():
    """A `unique` index on `account_id` alone would reject a second workspace membership.

    It existed in the first version of this model and is present in any already-deployed Atlas
    database, so `ensure_indexes` has to drop it explicitly — `create_index` will not replace
    an index of different keys. Without the drop, inviting someone to a second workspace fails
    with a duplicate-key error that reads like a bug rather than a stale index.
    """
    import pathlib

    source = pathlib.Path("app/iam/store.py").read_text()
    assert 'drop_index("org_member_unique")' in source
    # And the replacement must be on the pair.
    assert '[("account_id", 1), ("workspace_id", 1)]' in source


# --------------------------------------------------------------------------- #
# The invitation credential
# --------------------------------------------------------------------------- #


def test_the_invitation_carries_a_token_not_a_password():
    """**The security decision at the centre of this flow.**

    A generated one-time password was the obvious design. It is weaker in four specific ways,
    all of which follow from it being valid at `POST /iam/login`:

      * that endpoint is public, so the credential is an online guessing target for its whole
        14-day life, where a token is only usable by whoever holds the URL;
      * it must be short enough to type — perhaps 50-60 bits against this token's 256;
      * it survives being forwarded in a reply-all;
      * both the link and the password would open the account, widening the surface.

    So no password is generated anywhere in the invitation path. Asserted structurally because
    a well-meaning "make onboarding easier" change would reintroduce one.
    """
    import pathlib

    store_source = pathlib.Path("app/iam/store.py").read_text()
    start = store_source.index("async def redeem_team_invitation(")
    body = store_source[start : store_source.index("\nasync def ", start + 10)]

    # The account is created with no password hash at all.
    assert "password=None" in body, (
        "an invited account must be created with no password hash — otherwise a generated "
        "credential exists somewhere"
    )
    for forbidden in ("hash_password", "token_urlsafe", "choice(", "randbelow"):
        assert forbidden not in body, (
            f"redeem_team_invitation must not generate a credential ({forbidden} found)"
        )


def test_the_invite_token_lives_long_but_the_session_does_not():
    """14 days is defensible for a single-use 256-bit token in one mailbox.

    It would not be defensible for the *session* it redeems into: that is a bearer credential
    in a browser, so it gets 15 minutes and a scope.
    """
    from app.iam import passwordless

    assert passwordless.TEAM_INVITE_TTL_MINUTES == 14 * 24 * 60
    # The token TTL must be selected by purpose, not fall through to the magic-link default.
    minted = passwordless.mint_token(passwordless.TokenPurpose.TEAM_INVITE)
    lifetime = minted.expires_at - __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    )
    assert lifetime.days >= 13, f"invite token expires too soon: {lifetime}"


def test_an_unknown_purpose_gets_the_shortest_ttl():
    """Fail short, not long. A new purpose that forgets to declare a TTL must not silently
    inherit 14 days — a too-short token produces a support request, a standing credential
    nobody chose to issue is a security problem."""
    from app.iam import passwordless

    minted = passwordless.mint_token(passwordless.TokenPurpose.MAGIC_LINK)
    lifetime = minted.expires_at - __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    )
    assert lifetime.total_seconds() <= passwordless.MAGIC_LINK_TTL_MINUTES * 60 + 5


# --------------------------------------------------------------------------- #
# The scoped session
# --------------------------------------------------------------------------- #


def test_a_scoped_session_carries_its_restriction_in_the_token():
    """In the JWT, not in a database row.

    A scope looked up per request could fail open on a cache miss, and a restriction that
    fails open is no restriction. Signed into the token, it travels with the credential.
    """
    from app.iam import security

    token, _ = security.issue_session(
        "ACC1", "commercial", minutes=15, scope=security.SCOPE_SET_PASSWORD
    )
    claims = security.read_session(token)
    assert claims is not None
    assert claims["scope"] == security.SCOPE_SET_PASSWORD

    # A normal session must carry no scope at all — an absent claim is what "unrestricted"
    # means, so a stray empty string would be a third state nothing checks for.
    plain, _ = security.issue_session("ACC1", "commercial")
    assert "scope" not in (security.read_session(plain) or {})


def test_current_account_refuses_a_password_setup_session():
    """**Enforced server-side, not by a frontend redirect.**

    A redirect governs navigation only; a caller holding the token can POST to any endpoint
    directly. Without this check the 15-minute window would be a session that could read the
    organisation's customers, and the whole "no temporary password" design would buy nothing.
    """
    import pathlib

    source = pathlib.Path("app/iam/deps.py").read_text()
    start = source.index("async def current_account(")
    body = source[start : source.index("\nclass Session", start)]

    assert "SCOPE_SET_PASSWORD" in body, (
        "current_account must refuse a set-password session, or the scope is advisory only"
    )


def test_only_one_dependency_accepts_the_scoped_session():
    """The set of routes reachable with an invite session must be exactly one.

    `password_setup_session` is the single opt-out from `current_account`'s refusal. A second
    dependency accepting that scope would widen what a hijacked invite session can do, and
    would do it invisibly.
    """
    import pathlib

    source = pathlib.Path("app/iam/deps.py").read_text()
    # One definition, and it is the named one.
    assert source.count("async def password_setup_session(") == 1

    routes = pathlib.Path("app/api/routes/iam.py").read_text()
    assert routes.count("Depends(password_setup_session)") == 1, (
        "exactly one route may accept a set-password session; found "
        f"{routes.count('Depends(password_setup_session)')}"
    )


def test_setting_the_first_password_issues_a_new_session():
    """Rotation is what retires the scoped token.

    Returning the same session with a widened scope would leave a hijacked invite session
    valid afterwards — a new `jti` is what actually invalidates it.
    """
    import pathlib

    source = pathlib.Path("app/api/routes/iam.py").read_text()
    start = source.index("async def set_first_password(")
    body = source[start : source.index("\n@router.", start)]

    assert "security.issue_session(account.id, account.kind.value)" in body, (
        "must mint a fresh unscoped session rather than reusing the scoped one"
    )
    assert "_reject_if_breached" in body, "the chosen password must be screened against HIBP"


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #


def test_activation_and_welcome_are_separate_emails():
    """The security notice must not be buried in the welcome prose.

    "Was this activation me?" needs answering in the first minute, and it carries the device
    block. Merged under three paragraphs about satellites is how a real compromise goes
    unnoticed — so they are two senders, and only the security one takes a context.
    """
    import inspect

    from app.iam import mailer

    activated = inspect.signature(mailer.send_profile_activated).parameters
    welcome = inspect.signature(mailer.send_team_welcome).parameters

    assert "context" in activated, "the activation notice must carry device and location"
    assert "context" not in welcome, (
        "the welcome email carries no security claim, so a device table there would train "
        "the reader to skim past the ones that matter"
    )


def test_security_notifications_carry_device_and_location():
    """Every notification about an account action a user should be able to validate.

    The recipient's check is "was that my device, in my city?" — which is impossible if the
    email does not say. `send_team_invitation` carries the INVITER's context, since the
    recipient has no session yet.
    """
    import inspect

    from app.iam import mailer

    for sender in (
        mailer.send_team_invitation,
        mailer.send_profile_activated,
        mailer.send_password_reset,
        mailer.send_magic_link,
    ):
        assert "context" in inspect.signature(sender).parameters, (
            f"{sender.__name__} must accept a RequestContext so the reader can validate the "
            "device, IP and location the action came from"
        )


def test_the_welcome_email_claims_nothing_the_pipeline_cannot_do():
    """A welcome email is the worst place to create an expectation.

    Public Health Intelligence is not deliverable — `_classify` never returns `malaria_risk` as
    a primary hazard — so the story must not promise malaria alerting. It may describe the four
    agents, Fahis, and the radar-through-cloud property, all of which are built.
    """
    import inspect

    from app.iam import mailer

    body = inspect.getsource(mailer.send_team_welcome).lower()
    assert "malaria" not in body, (
        "the welcome email must not promise malaria alerting — that track is not deliverable"
    )
    for built in ("scout", "analyst", "oracle", "herald", "fahis", "radar"):
        assert built in body, f"the story should describe {built}, which is built"


def test_invited_members_cannot_use_a_magic_link():
    """One credential path per account, so an audit has one answer to "how did they get in?".

    Password RESET must stay available, or a forgotten password becomes an administrator
    ticket — so the restriction is on MAGIC_LINK specifically.
    """
    import pathlib

    source = pathlib.Path("app/iam/store.py").read_text()
    start = source.index("async def issue_single_use_token(")
    body = source[start : source.index("\nasync def ", start + 10)]

    assert "_invited_member" in body
    assert "TokenPurpose.MAGIC_LINK" in body, (
        "the refusal must be scoped to magic links, not applied to password reset too"
    )


# --------------------------------------------------------------------------- #
# Resending an invitation
# --------------------------------------------------------------------------- #


def test_resend_reissues_rather_than_extending():
    """**A new token, not a longer expiry on the old one.**

    Extending `expires_at` would leave the original link live — and that link has sat in a
    mailbox for two weeks and may have been forwarded. `create_invitation` supersedes any
    outstanding invitation to the same address, so reissuing destroys the old hash and only the
    newly emailed link works.
    """
    import pathlib

    source = pathlib.Path("app/iam/store.py").read_text()
    start = source.index("async def resend_invitation(")
    body = source[start : source.index("\nasync def ", start + 10)]

    assert "create_invitation(" in body, "a resend must mint a fresh token"
    for forbidden in ("$set", "expires_at\":", "update_one"):
        assert forbidden not in body, (
            f"resend must not extend the existing invitation in place ({forbidden} found) — "
            "the original link would stay valid"
        )


def test_resend_carries_the_original_grants():
    """A resend is "send that again", so the workspaces and roles must not be re-derived.

    Re-deriving them from the resender's current permissions would let an Operations member
    silently narrow an invitation an Owner had issued — changing what a colleague was offered
    without anyone choosing to.
    """
    import pathlib

    source = pathlib.Path("app/iam/store.py").read_text()
    start = source.index("async def resend_invitation(")
    body = source[start : source.index("\nasync def ", start + 10)]

    assert 'existing.get("grants", [])' in body, (
        "the reissued invitation must carry the original grants verbatim"
    )


def test_an_expired_invitation_is_still_findable():
    """The whole point of a resend.

    `find_invitation` must NOT filter on `expires_at` — an expired invitation is exactly what
    is being resent, and hiding it would make "their link lapsed" indistinguishable from "you
    never invited them".
    """
    import pathlib

    source = pathlib.Path("app/iam/store.py").read_text()
    start = source.index("async def find_invitation(")
    body = source[start : source.index("\nasync def ", start + 10)]
    # The QUERY, not the docstring — which explains precisely why the filter is absent.
    query = body[body.index("find_one(") :]

    assert "expires_at" not in query, (
        "find_invitation must include expired invitations — they are what a resend is for"
    )


def test_expiry_is_computed_on_the_server():
    """Not compared against the browser's clock.

    A UI comparing dates locally would offer "Resend" on a live invitation, or hide it on a
    lapsed one, whenever a laptop's clock is skewed. `list_invitations` returns `expired`.
    """
    import pathlib

    source = pathlib.Path("app/iam/store.py").read_text()
    start = source.index("async def list_invitations(")
    # Next top-level definition of either kind, or end of file — `list_invitations` is
    # currently the last function in the module.
    ends = [i for i in (source.find("\nasync def ", start + 10),
                        source.find("\ndef ", start + 10)) if i != -1]
    body = source[start : min(ends)] if ends else source[start:]

    assert '"expired"' in body


def test_resend_is_gated_per_workspace():
    """Holding team management on one project must not permit a resend into another."""
    import pathlib

    source = pathlib.Path("app/api/routes/iam.py").read_text()
    start = source.index("async def resend_team_invitation(")
    body = source[start : source.index("\n@router.", start)]

    assert "member_permissions_in" in body, (
        "resend must check authority on each workspace the invitation names"
    )
    assert "MEMBER_INVITE_RESENT" in body, "a resend must be audited distinctly"


def test_a_resend_is_audited_distinctly_from_an_invitation():
    """Several resends to one address is a deliverability signal.

    An entry that looked like a fresh invitation would hide it — the pattern only shows if the
    two actions are countable separately.
    """
    from app.iam.audit import AuditAction

    assert AuditAction.MEMBER_INVITE_RESENT.value != AuditAction.MEMBER_INVITED.value


def test_the_resend_response_never_carries_the_token():
    """An invitation is a credential; the only place it should exist is the mailbox.

    Returning it would put it in the portal's network log and in any error-reporting tool
    watching responses — even for an Organization Owner.
    """
    import pathlib

    source = pathlib.Path("app/api/routes/iam.py").read_text()
    start = source.index("async def resend_team_invitation(")
    body = source[start : source.index("\n@router.", start)]
    returned = body[body.index("return {") :]

    assert "plaintext" not in returned, "the resend response must not include the token"
