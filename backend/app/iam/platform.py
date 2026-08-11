"""Platform authentication — what replaces the single shared `X-SHELTER-Key`.

## What was wrong with the shared key

`API_KEY` was one static string granting **29 endpoints** across seven routers. Six
distinct problems, each independently serious for a SaaS platform:

| Problem | Consequence |
|---|---|
| **No attribution** | Every write logged identically. After an incident, "who registered these 400 subscribers?" was unanswerable. |
| **No scoping** | The frontend needs `/subscribers`, `/risk/assess`, `/health` — three endpoints. The key also granted NIGCOMSAT broadcast, verification sweeps and webhook administration. |
| **Shared blast radius** | One value in Netlify's environment, the compose file, every developer's `.env`, and CI. A leak from any of them is a leak of all of them. |
| **Rotation is an outage** | Changing it breaks every consumer simultaneously, so in practice nobody rotates it. |
| **No revocation** | Nothing can be disabled without disabling everything. |
| **No expiry** | It lives until someone remembers it exists. |

## What replaces it

A **service account** — `AccountKind.SERVICE` — holding scoped API keys with the
full lifecycle already built for aggregators: show-once, SHA-256 at rest, rotation
with a grace window, immediate revocation, optional expiry, and an immutable audit
trail. The frontend gets its own key with `FRONTEND_SCOPES` and *cannot* broadcast.

A service account is deliberately not a login: no password, no portal session, no
human owner. Its only credential is a key, so there is nothing to phish.

## Migration, and why it is not a hard cutover

`require_platform_scope` accepts **either** credential during transition:

1. `X-SHELTER-API-Key` resolved to a service account holding the scope — preferred.
2. The legacy `X-SHELTER-Key` matching `API_KEY` — accepted, logged as deprecated,
   and **refused outright in production once IAM is configured**.

A hard cutover would strand any deployment whose frontend still holds the old key,
and the failure would be a silent 401 on subscriber registration — i.e. signups
failing in front of real users. Accepting both, while making the legacy path noisy
and then fatal in production, converts that outage into a log line and a preflight
error.

`IAM_LEGACY_SHARED_KEY_ENABLED=false` disables the fallback immediately for operators who
have already migrated.
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, Security, status

from app.api.security_schemes import legacy_shared_key, platform_api_key
from app.config import settings
from app.iam import store
from app.iam.audit import AuditAction, AuditOutcome
from app.iam.models import Account, AccountKind, ApiKeyScope
from app.logging_config import get_logger

log = get_logger(__name__)


class Principal:
    """Who is making this request, and what they may do.

    Replaces `require_api_key`'s `None` return. That guard proved *a* valid caller
    existed and told the handler nothing else — which is why nothing could be
    attributed or scoped. A handler now receives an identity.
    """

    __slots__ = ("account", "scopes", "legacy")

    def __init__(
        self,
        account: Account | None,
        scopes: list[ApiKeyScope],
        *,
        legacy: bool = False,
    ) -> None:
        self.account = account
        self.scopes = scopes
        #: True when authenticated by the deprecated shared key. Handlers can record
        #: it, and `/health` counts it, so the migration has a visible finish line.
        self.legacy = legacy

    @property
    def id(self) -> str:
        """Stable identifier for the audit log.

        `"legacy-shared-key"` is deliberately a real, searchable value rather than
        None: an audit entry attributed to nobody is the thing being fixed, so the
        unattributable case is named explicitly.
        """
        return self.account.id if self.account else "legacy-shared-key"

    @property
    def label(self) -> str:
        if self.account:
            return self.account.organisation or self.account.full_name or self.account.id
        return "legacy shared key"

    def has(self, scope: ApiKeyScope) -> bool:
        return scope in self.scopes


def _legacy_allowed() -> bool:
    """Whether the deprecated shared key may still authenticate.

    Refused in production once IAM is configured, because at that point a service
    account is available and the shared key is a strictly worse credential that
    someone forgot to remove. Local and staging keep it so a developer with no Atlas
    connection is not locked out of their own API.
    """
    if not settings.iam_legacy_shared_key_enabled:
        return False
    if settings.is_production and store.available():
        return False
    return True


def require_platform_scope(scope: ApiKeyScope):
    """Dependency factory: this route needs this platform scope.

    A factory so the requirement appears in the route declaration rather than as a check
    buried in a handler body that a reader has to find.

    It also appears in the generated OpenAPI — but only because both credentials below are
    declared with `Security(...)` rather than `Header(...)`. An earlier version used plain
    headers, and this docstring claimed OpenAPI visibility it did not have: a header
    parameter is just a parameter, so the operation showed no padlock, the consoles offered
    no Authorize button, and a generated client could not authenticate.

    Returns a `Principal`, so handlers that care about attribution can record who
    acted — the audit gap the shared key created.
    """

    async def guard(
        request: Request,
        x_shelter_api_key: str | None = Security(platform_api_key),
        x_shelter_key: str | None = Security(legacy_shared_key),
    ) -> Principal:
        # --- 1. The scoped path -------------------------------------------------
        if x_shelter_api_key and store.available():
            resolved = await store.resolve_api_key(
                x_shelter_api_key.strip(),
                ip=request.client.host if request.client else None,
            )
            if resolved is None:
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED,
                    "API key is invalid, revoked or expired.",
                )
            account, scopes, _workspace_id = resolved

            if scope not in scopes:
                # Audited: repeated denials mean either a misconfiguration to help
                # them fix, or a key being probed for reach it does not have.
                await store.record_audit(
                    account_id=account.id,
                    action=AuditAction.KEY_SCOPE_DENIED,
                    outcome=AuditOutcome.DENIED,
                    detail=f"{scope.value} on {request.url.path}",
                    ip=request.client.host if request.client else None,
                )
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    f"This key lacks the `{scope.value}` scope. Scopes cannot be added "
                    f"to an existing key — mint a new one, deliberately, so widening "
                    f"authority is always an explicit act.",
                )

            return Principal(account, scopes)

        # --- 2. The deprecated shared key --------------------------------------
        if x_shelter_key and settings.api_key:
            if not hmac.compare_digest(x_shelter_key, settings.api_key):
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED, "Missing or invalid credentials."
                )

            if not _legacy_allowed():
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    "The shared X-SHELTER-Key is no longer accepted here. It granted "
                    "every write endpoint at once with no attribution and no way to "
                    "revoke one consumer. Create a service account and send a scoped "
                    "key as X-SHELTER-API-Key instead: "
                    "`POST /iam/service-accounts`.",
                )

            log.warning(
                "DEPRECATED: request authenticated with the shared X-SHELTER-Key",
                extra={
                    "path": request.url.path,
                    "scope": scope.value,
                    "ip": request.client.host if request.client else None,
                    "remedy": "issue a scoped service-account key (POST /iam/service-accounts)",
                },
            )
            # The shared key historically granted everything, so preserving behaviour
            # means granting the requested scope. That is precisely why it is
            # deprecated — but silently narrowing it would break live deployments
            # mid-request rather than at a checkpoint they can see.
            return Principal(None, [scope], legacy=True)

        # --- 3. No credential ---------------------------------------------------
        if not settings.api_key and not store.available():
            # Development with nothing configured. `require_api_key` was a no-op in
            # this state too; keeping that keeps local work frictionless, and
            # preflight makes it a hard error in production.
            return Principal(None, list(ApiKeyScope), legacy=True)

        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing credentials. Send `X-SHELTER-API-Key: shltky…` from a service "
            "account with the required scope.",
        )

    return guard


async def provision_service_account(
    name: str,
    scopes: list[ApiKeyScope],
    *,
    email: str,
    expires_in_days: int | None = None,
) -> tuple[str, str] | None:
    """Create a service account and its first key. `(account_id, plaintext_key)`.

    Used by `POST /iam/service-accounts` and by the bootstrap CLI, so a fresh
    deployment can provision the frontend's key without a portal login — which would
    otherwise be a chicken-and-egg problem, since the portal needs the key to work.

    The account has **no password**: `authenticate` cannot succeed against it, so
    there is no login to brute-force and no credential to phish. Its only
    authentication path is the key.
    """
    if not store.available():
        return None

    invalid = [s for s in scopes if s not in _platform_scope_set()]
    if invalid:
        log.error(
            "refusing to provision a service account with tenant scopes",
            extra={"invalid": [s.value for s in invalid]},
        )
        return None

    account, _ = await store.create_account(
        kind=AccountKind.SERVICE,
        email=email,
        first_name=name,
        last_name="(service)",
        password=None,
        organisation=name,
    )
    if account is None:
        return None

    # Service accounts skip email verification: there is no mailbox and no human to
    # confirm. Activated directly so the key works immediately.
    from app.iam.models import AccountStatus

    await store.set_status(account.id, AccountStatus.ACTIVE)

    minted = await store.create_api_key(
        account.id, f"{name} key", scopes, expires_in_days=expires_in_days
    )
    if minted is None:
        return None

    key, _public = minted
    log.info(
        "service account provisioned",
        extra={
            "account_id": account.id,
            "name": name,
            "scopes": [s.value for s in scopes],
        },
    )
    return account.id, key


def _platform_scope_set() -> frozenset[ApiKeyScope]:
    from app.iam.models import PLATFORM_SCOPES

    return PLATFORM_SCOPES
