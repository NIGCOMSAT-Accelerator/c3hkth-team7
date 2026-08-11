"""Auth dependencies — the three guards, and what each one is for.

| Guard | Credential | Who passes | Used by |
|---|---|---|---|
| `current_account` | `Authorization: Bearer <jwt>` | any logged-in account | portal self-service |
| `current_aggregator` | `X-SHELTER-API-Key: shltky…` | commercial accounts only | the partner REST API |
| `require_api_key` (existing) | `X-SHELTER-Key: <bootstrap>` | the frontend server | machine-to-machine |

**The separation is the security model, not organisation.** A farmer's portal session
is a JWT with `aud=shelter:portal`; `current_aggregator` does not read the
`Authorization` header at all, so a stolen farmer session cannot reach the partner
API. And `current_account` does not accept API keys, so a leaked aggregator key cannot
be used to browse the portal as a human and change another account's settings.

**Scopes are checked per route, not per key.** `require_scope` returns a dependency,
so a route declares the permission it needs and the key's grant is compared against it
at call time. A key created before a scope existed therefore lacks it, which is the
correct default — a new capability must be re-granted deliberately.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials

from app.api.security_schemes import (
    aggregator_api_key,
    platform_api_key,
    portal_session,
)
from app.iam import identifiers, idle, security, store
from app.iam.models import (
    PLATFORM_SCOPES,
    Account,
    AccountKind,
    AccountStatus,
    ApiKeyScope,
)
from app.iam.roles import ROLE_LABELS, Permission, Role
from app.logging_config import get_logger

log = get_logger(__name__)

_UNAVAILABLE = (
    "The IAM service is not configured (MONGO_URL is unset), so accounts and API "
    "keys are unavailable. The satellite pipeline is unaffected."
)


def _require_store() -> None:
    if not store.available():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE)


# --------------------------------------------------------------------------- #
# Portal sessions
# --------------------------------------------------------------------------- #


async def current_account(
    # Declared as a security scheme so the internal console offers an Authorize button for a
    # portal session. `HTTPBearer` yields credentials rather than the raw header, so the
    # value is reassembled below to keep the existing parsing intact.
    credentials: HTTPAuthorizationCredentials | None = Security(portal_session),
) -> Account:
    """The logged-in account behind a portal session token.

    401 on anything wrong — expired, malformed, wrong audience, or an account that has
    since been suspended. **The message never distinguishes those cases**: telling a
    caller "this token is valid but your account is suspended" confirms the account
    exists, which is the enumeration leak `authenticate` is careful to avoid.
    """
    _require_store()

    # Rebuilt into the header form the rest of this function already parses, rather than
    # rewriting that ladder — the validation order (signature, audience, expiry, status,
    # idle) is deliberate and worth leaving untouched.
    authorization = (
        f"{credentials.scheme} {credentials.credentials}" if credentials else None
    )

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing session. Send `Authorization: Bearer <token>` from POST /iam/login.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    claims = security.read_session(authorization.split(" ", 1)[1].strip())
    if claims is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Session is invalid or has expired. Log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    account = await store.get_account(claims.get("sub", ""))
    if account is None or account.status in (
        AccountStatus.SUSPENDED,
        AccountStatus.DISABLED,
    ):
        # Deliberately the same message as an invalid token.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Session is invalid or has expired. Log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Idle enforcement, server-side. The JWT's `exp` is an absolute ceiling and cannot
    # express "unattended for 15 minutes", so a stolen token would otherwise stay usable
    # for the rest of its 12 hours.
    #
    # A DISTINCT reason string, unlike every other failure here. That is a considered
    # exception to the no-enumeration rule: reaching this branch requires a validly
    # signed, unexpired token for a live account, so the caller has already proved they
    # held a real session and learns nothing new. In exchange the frontend can tell
    # "you were idle" from "your credentials are wrong" and say so — without it, a
    # timeout looks identical to a hijacked account, which is alarming and wrong.
    jti = claims.get("jti")
    if jti:
        state = await idle.check(str(jti))
        if state.expired:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Session ended after a period of inactivity. Sign in again.",
                headers={"WWW-Authenticate": "Bearer", "X-Session-Ended": "idle"},
            )

    # A scoped session may reach only the route that satisfies its scope.
    #
    # `SCOPE_SET_PASSWORD` is issued when a team invitation is redeemed, so the invited
    # colleague lands signed in with no password yet. It must not be usable for anything else:
    # otherwise a 15-minute window would exist in which a hijacked invite session could read
    # the organisation's customers, and the "no temporary password" design would have bought
    # nothing.
    #
    # **Enforced here rather than by a frontend redirect.** A redirect only governs navigation;
    # a caller with the token can POST to any endpoint directly. This is the check that makes
    # the restriction real, and the route that clears it opts out explicitly via
    # `password_setup_session`.
    #
    # 403 rather than 401: the session is perfectly valid, it simply cannot do this. A 401
    # would make the frontend clear the cookie and bounce them to sign-in — where they have no
    # password to sign in with, stranding them mid-onboarding.
    if claims.get("scope") == security.SCOPE_SET_PASSWORD:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Finish setting your password before using the rest of SHELTER.",
            headers={"X-Password-Setup-Required": "1"},
        )

    return account


async def password_setup_session(
    credentials: HTTPAuthorizationCredentials | None = Security(portal_session),
) -> Account:
    """The one dependency that accepts a `SCOPE_SET_PASSWORD` session.

    Deliberately does NOT depend on `current_account`, which refuses that scope — this is the
    single opt-out, so the set of routes reachable with an invite session is exactly the set of
    routes naming this dependency. A test asserts there is only one.

    Also accepts a full session, so a signed-in member changing their password later uses the
    same route rather than a parallel one that could drift.
    """
    _require_store()

    authorization = (
        f"{credentials.scheme} {credentials.credentials}" if credentials else None
    )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing session.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    claims = security.read_session(authorization.split(" ", 1)[1].strip())
    if claims is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Session is invalid or has expired. Use your invitation link again, or ask for a "
            "new one.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    account = await store.get_account(claims.get("sub", ""))
    if account is None or account.status in (
        AccountStatus.SUSPENDED,
        AccountStatus.DISABLED,
    ):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Session is invalid or has expired. Log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return account


class Session:
    """A validated portal session: the account, plus the token's own identity.

    `current_account` returns only the account, which is right for almost every route —
    but the session-state endpoints need the `jti` to read and refresh the idle window,
    and it must be the jti of *this* token rather than any session for the account. A
    second dependency is cheaper and clearer than threading the raw header into a route
    and re-decoding it there, which would duplicate the validation ladder above.
    """

    __slots__ = ("account", "jti")

    def __init__(self, account: Account, jti: str) -> None:
        self.account = account
        self.jti = jti


async def current_session(
    # Same scheme as `current_account`, so the session endpoints do not show a stray
    # `authorization` header parameter alongside the Authorize button.
    credentials: HTTPAuthorizationCredentials | None = Security(portal_session),
    account: Account = Depends(current_account),
) -> Session:
    """The session behind this request, including its `jti`.

    Depends on `current_account`, so every check above — signature, audience, expiry,
    account status, idle window — has already passed. This only recovers the claim; it
    re-decodes rather than caching because `read_session` is a local HMAC verification
    with no I/O, so the second call costs microseconds and keeps the two paths from
    disagreeing about what is valid.
    """
    token = credentials.credentials if credentials else ""
    claims = security.read_session(token) or {}
    return Session(account, str(claims.get("jti", "")))


async def verified_account(account: Account = Depends(current_account)) -> Account:
    """A logged-in account that has confirmed its email address.

    Required for anything that causes an outbound message. Delivering alerts to an
    unconfirmed address is how a warning service becomes a spam vector — and worse,
    how someone else's address gets subscribed to alerts they never asked for.
    """
    if not account.email_verified:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Confirm your email address first. We re-send the link from "
            "POST /iam/resend-verification.",
        )
    return account


# --------------------------------------------------------------------------- #
# Aggregator API keys
# --------------------------------------------------------------------------- #


class Aggregator:
    """A resolved commercial account plus the scopes its key carries.

    Carries both because every scoped read needs the account id (to filter
    the membership edge) and the route needs the scopes. Passing only the account would mean
    a second lookup, and passing only the scopes would lose the tenant boundary.
    """

    __slots__ = ("account", "scopes", "workspace_id")

    def __init__(
        self,
        account: Account,
        scopes: list[ApiKeyScope],
        workspace_id: str | None = None,
    ) -> None:
        self.account = account
        self.scopes = scopes
        #: The workspace the presented key belongs to, or None for a key minted before
        #: workspaces existed. Customers created through this key are scoped to it, so an
        #: aggregator running several projects keeps their customer bases separate.
        self.workspace_id = workspace_id

    def has(self, scope: ApiKeyScope) -> bool:
        return scope in self.scopes


async def current_aggregator(
    # `Security(...)` rather than `Header(...)`, so this appears in the OpenAPI document as a
    # SECURITY SCHEME rather than as a plain header parameter. That is what gives both
    # consoles an Authorize button and a padlock on the operation, and what makes
    # `openapi-generator` emit credential handling instead of leaving it to the partner.
    x_shelter_api_key: str | None = Security(aggregator_api_key),
) -> Aggregator:
    """Resolve an aggregator from its API key.

    **Note the header is distinct from `X-SHELTER-Key`.** That one is the bootstrap
    key the Next.js server uses for its own machine-to-machine calls; this one
    identifies a *tenant*. Sharing one header would make it impossible to tell
    "trusted frontend" from "one specific partner", and every scoped query depends on
    that distinction.

    Individuals cannot reach here even in principle: `resolve_api_key` only ever
    returns commercial accounts, because only they can mint keys. The explicit check
    below is belt-and-braces — a defence that survives a future change to key minting.
    """
    _require_store()

    if not x_shelter_api_key:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing API key. Send `X-SHELTER-API-Key: shltky…`. Create one in "
            "the portal, or via POST /iam/api-keys with a portal session.",
        )

    resolved = await store.resolve_api_key(x_shelter_api_key.strip())
    if resolved is None:
        # One message for every failure: unknown, revoked, expired, or belonging to a
        # suspended account. Distinguishing them would let a caller probe which keys
        # exist.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "API key is invalid, revoked or expired.",
        )

    account, scopes, workspace_id = resolved

    if account.kind is not AccountKind.COMMERCIAL:
        log.error(
            "non-commercial account holds an API key",
            extra={"account_id": account.id, "kind": account.kind.value},
        )
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "API access is available to commercial accounts only.",
        )

    return Aggregator(account, scopes, workspace_id)


def require_scope(scope: ApiKeyScope):
    """Dependency factory asserting one scope.

    A factory rather than a parameterised guard so the requirement is visible in the
    route declaration and in the generated OpenAPI, instead of buried in a body check
    a reader has to find.
    """

    async def guard(aggregator: Aggregator = Depends(current_aggregator)) -> Aggregator:
        if not aggregator.has(scope):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"This API key lacks the `{scope.value}` scope. Mint a new key with "
                f"it, or use one that has it — scopes cannot be added to an existing "
                f"key, deliberately: widening a key in place would silently grant "
                f"every system already holding it.",
            )
        return aggregator

    return guard


async def owned_account(account_id: str, aggregator: Aggregator) -> Account:
    """Fetch a customer this tenant may see, or 404.

    **404, not 403, when the account belongs to another tenant.** A 403 would confirm
    the id exists, letting one aggregator enumerate another's customer ids by
    iterating.

    Authorisation is the `memberships` edge, not a field on the account: the same
    person may be served by several aggregators, so "is this mine?" is a relationship
    question. `is_member` filters on `(account_id, aggregator_id, status=active)`
    inside the query.
    """
    # Shape-validate before the lookup. These ids appear in URL paths, so a typo
    # would otherwise become a query and return 404 — which reads as "your record is
    # gone" rather than "that is not an id". `normalise` is forgiving about case and
    # separators, because a partner may quote an id from a printed slip.
    canonical = identifiers.normalise(account_id) or account_id
    if not identifiers.is_valid(canonical) and not identifiers.looks_like_legacy(canonical):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{account_id!r} is not a valid subscriber id. Ids are 10 characters, "
            f"letters and digits, e.g. A7K2M9P4QX.",
        )

    account = await store.get_account(canonical)
    # Scoped to the presented key's workspace: a key for one project must not reach another
    # project's customer by quoting their id. 404 either way, so the boundary reveals nothing.
    if account is None or not await store.is_member(
        canonical, aggregator.account.id, workspace_id=aggregator.workspace_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such customer")
    return account


class KeyHolder:
    """Whoever presented a valid `X-SHELTER-API-Key` — aggregator or platform service.

    ## Why a combined identity rather than two separate guards

    Some endpoints are legitimately used by both audiences. Place resolution is the clearest
    case: a partner's importer calls it to turn a village name into a monitorable area, and
    the SHELTER portal calls it for exactly the same reason on a farmer's behalf.

    `current_aggregator` cannot serve that: it rejects platform service accounts, because
    `resolve_api_key` only returns commercial accounts and the portal's key belongs to a
    service account with `platform:*` scopes. Requiring it would break the portal's own
    signup flow.

    So this resolves either, and records WHICH — because the distinction still matters for
    the audit trail. "An aggregator resolved 400 places" and "the portal resolved one" are
    different facts, and collapsing them would make usage attribution impossible.
    """

    __slots__ = ("account", "scopes", "is_platform")

    def __init__(
        self, account: Account | None, scopes: list[ApiKeyScope], *, is_platform: bool
    ) -> None:
        #: None for a platform service account that is not a tenant. A tenant-scoped query
        #: must therefore check this rather than assume an account exists.
        self.account = account
        self.scopes = scopes
        self.is_platform = is_platform

    def has(self, scope: ApiKeyScope) -> bool:
        return scope in self.scopes

    @property
    def label(self) -> str:
        """For logs and audit detail. Never the key itself, and never a hash of it."""
        if self.account is not None:
            return f"aggregator:{self.account.id}"
        return "platform:service-account"


async def current_key_holder(
    # Both schemes declared, because this guard genuinely accepts either. In the consoles
    # that renders as two padlocks on the operation — which is the honest signal: a partner
    # key works here, and so does the portal's service key.
    #
    # `Security` returns the first scheme's value; the second is declared for documentation
    # and resolves from the same header, so a caller sending one key satisfies both.
    aggregator_key: str | None = Security(aggregator_api_key),
    platform_key: str | None = Security(platform_api_key),
) -> KeyHolder:
    """Require a valid API key — aggregator OR platform service. No scope check.

    Used by endpoints that are pure functions over open data and cost us an upstream call,
    where the point of gating is **attribution and rate control**, not authorisation. Place
    resolution is the example: there is nothing tenant-specific to protect in "where is
    Argungu?", but an ungated endpoint that proxies a rate-limited third party is one anyone
    can exhaust on our behalf, and it produces no record of who consumed the service.

    Routes needing a specific capability should keep using `require_scope`, which is the
    least-privilege mechanism. This is deliberately weaker and says so.
    """
    _require_store()

    # Both schemes read the same header, so whichever resolved is the key that was sent.
    x_shelter_api_key = aggregator_key or platform_key

    if not x_shelter_api_key:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing API key. Send `X-SHELTER-API-Key: shltky…`. Aggregators create one in "
            "the portal; platform services use a service-account key.",
        )

    resolved = await store.resolve_api_key(x_shelter_api_key.strip())
    if resolved is None:
        # One message for unknown, revoked, expired and suspended-owner alike —
        # distinguishing them would let a caller probe which keys exist.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "API key is invalid, revoked or expired."
        )

    # The workspace is unused on this path: `current_key_holder` guards surfaces shared by the
    # portal's service key and a partner key (`/places/*`), where scoping is about attribution
    # rather than about which customer base is being read.
    account, scopes, _workspace_id = resolved
    is_platform = any(scope in PLATFORM_SCOPES for scope in scopes)

    # A platform key belongs to a service account, not a tenant, so no account is carried
    # forward — nothing downstream should be able to treat the portal as a customer of itself.
    return KeyHolder(
        None if is_platform else account, scopes, is_platform=is_platform
    )


def require_permission(permission: Permission):
    """Dependency factory: this route needs this permission.

    ## Why this exists alongside `require_scope`

    They guard different credentials for the same capability:

      * `require_scope` checks an **API key's** scopes — a machine calling the partner API.
      * `require_permission` checks a **person's** role in the portal — a human clicking.

    Both resolve from `roles.ROLE_PERMISSIONS`, so a View-Only member is refused in the portal
    and cannot mint a key that would let them do it through the API either. Two guards, one
    table: that is what makes "side-nav permission extended to the API scopes" true rather
    than a UI convention.

    ## Hiding the nav item is not the control

    The portal hides sections a role cannot use, which is courtesy. This is the enforcement —
    a caller who bookmarks a URL, or posts directly, hits the same check.

    403 rather than 404: unlike the tenant-isolation case, there is nothing to conceal here.
    The member is authenticated and their own organisation's structure is not a secret, so a
    precise "your role does not allow this" is more useful than a false "not found".
    """

    async def guard(account: Account = Depends(current_account)) -> Account:
        # An individual has no team, no workspace and no roles — the concept does not apply.
        # Refused rather than granted: these routes manage organisation resources that an
        # individual account does not have, so allowing one through would mean inventing an
        # implicit workspace for them.
        if account.kind is not AccountKind.COMMERCIAL:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "This is an organisation feature. Individual accounts manage their own "
                "areas and alerts from the portal.",
            )

        granted = await store.member_permissions(account.id)
        if permission not in granted:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"{await _role_name(account.id)} cannot "
                f"{permission.value.replace(':', ' ')}. Ask an Organization Owner to change "
                f"your role or grant this permission.",
            )
        return account

    return guard


async def _role_name(account_id: str) -> str:
    """How to name the caller's role in a refusal.

    `member_role` returns a *string* (or None when it varies across workspaces), so it must be
    converted to a `Role` before indexing `ROLE_LABELS` — which is keyed by the enum. Passing
    the raw string silently missed every time and produced "your role cannot …" for everyone,
    losing the one detail that tells the reader what to ask for.
    """
    role = await store.member_role(account_id)
    if role is None:
        # Genuinely varies across workspaces, so no single name is honest. The workspace-scoped
        # guard has its own wording; this is the org-wide case.
        return "Your role"
    try:
        return ROLE_LABELS[Role(role)][0]
    except (ValueError, KeyError):
        return "Your role"


def require_workspace_permission(permission: Permission):
    """This route needs this permission **on the workspace it names**.

    ## Why this exists alongside `require_permission`

    `require_permission` checks the union across every workspace, which is right for the
    side-nav and for org-wide routes. It is wrong for anything addressing one workspace: a
    member who is an Organization Owner on "Kebbi rice season" and View-Only on "Bayelsa flood
    pilot" would pass a union check and then rotate a key on Bayelsa.

    So this resolves `member_permissions_in` for the workspace in the path and nothing else.
    That is the guard that makes a workspace boundary real rather than decorative.

    404, not 403, when the workspace is not theirs — matching the workspace CRUD routes. A 403
    would confirm the id exists in some other organisation, which is an enumeration oracle.
    """

    async def guard(
        workspace_id: str, account: Account = Depends(current_account)
    ) -> Account:
        if account.kind is not AccountKind.COMMERCIAL:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "This is an organisation feature. Individual accounts manage their own "
                "areas and alerts from the portal.",
            )

        organisation = await store.organisation_for(account.id)
        known = {w["id"] for w in await store.list_workspaces(organisation)}
        if workspace_id not in known:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such workspace.")

        granted = await store.member_permissions_in(account.id, workspace_id)
        if permission not in granted:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Your role on this workspace cannot "
                f"{permission.value.replace(':', ' ')}. Access is granted per workspace, so "
                f"a role you hold on another one does not apply here.",
            )
        return account

    return guard
