"""Who is asking, and which subscribers they may see.

## Why this is one module rather than a check per route

Two separate incidents, one cause. `GET /alerts` and `GET /subscribers` were both unauthenticated
reads whose only scoping was an optional query parameter, so both returned the whole platform to a
bare curl:

    /alerts       -> another farmer's advisories, plot names, delivery receipts
    /subscribers  -> full name, email address, and every plot's exact bounding box

The second was found only because the first was reported. That is the argument for a shared
resolver: a per-route check is a thing to forget, and the routes that forgot it were the ones nobody
looked at. `tests/test_tenancy.py` enumerates every read route on both routers and asserts each one
depends on this.

## The three callers, resolved in this order

  1. **Portal session** — the account's own subscription, plus its customers if commercial.
     **Checked FIRST, deliberately.** `frontend/lib/api.ts` attaches the platform key to every
     request including a subscriber's own, so checking the key first made any browser request
     resolve as unrestricted — a subscriber could change another's alert delivery by editing the id
     in the URL. A session is the more specific credential; the key is transport.
  2. **Platform key with `platform:read`** — unrestricted, when it arrives ALONE. The operations
     dashboard and machine-to-machine calls. The only path to a global read.
  3. **Aggregator key with `customers:read`** — the subscribers belonging to that key's workspace,
     resolved through the membership edge so another aggregator's customer is never a candidate row.
     Same boundary as `list_customers`.

**No credential is not a fourth case.** It is a 401. A subscriber record names a farmer, their phone
number and where their field is; there is no anonymous read of that.

## The invariant that the original bug broke

`permitted_subscriber_ids is None` means unrestricted. An **empty set means nothing**. Those must
never be conflated — treating a falsy scope as "unfiltered" is exactly how `subscriber_id=None` came
to mean "every subscriber", and it is why a brand-new aggregator with no customers saw an unrelated
individual's data.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request, Security, status

from app.api.security_schemes import (
    aggregator_api_key,
    platform_api_key,
    portal_session,
)
from app.iam import store as iam_store
from app.iam.models import AccountKind, ApiKeyScope
from app.iam.security import read_session
from app.logging_config import get_logger

log = get_logger(__name__)

#: Ceiling on the customer lookup. An aggregator with more than this many customers gets a
#: truncated audience rather than a slow request; `list_customers` is the paginated surface for
#: enumerating them. Logged when it bites, because a silent truncation of a SCOPE reads as
#: "these are all your customers" when it is not.
MAX_TENANT_CUSTOMERS = 500


@dataclass(frozen=True)
class Audience:
    """Which subscribers this caller may read.

    `permitted_subscriber_ids is None` -> unrestricted (platform only). Anything else is an
    explicit allow-list, and empty means nothing.
    """

    permitted_subscriber_ids: frozenset[str] | None
    #: For logs only. Never returned to the caller — it names an account id.
    label: str

    @property
    def unrestricted(self) -> bool:
        return self.permitted_subscriber_ids is None

    def may_see(self, subscriber_id: str | None) -> bool:
        """Whether this caller may read one subscriber."""
        if self.unrestricted:
            return True
        if not subscriber_id:
            return False
        return subscriber_id in (self.permitted_subscriber_ids or frozenset())

    def narrow(self, requested: str | None) -> list[str] | None:
        """The subscriber ids to actually query.

        `None` means "no filter" and is reachable only for an unrestricted caller. A requested id
        that this caller may not see collapses to their own permitted set rather than raising — a
        403 would confirm the id exists, which is the enumeration leak `get_customer` and
        `authenticate` both avoid.
        """
        if self.unrestricted:
            return None if requested is None else [requested]
        permitted = self.permitted_subscriber_ids or frozenset()
        if requested and requested in permitted:
            return [requested]
        return sorted(permitted)


async def resolve_audience(
    request: Request,
    session: object | None = Security(portal_session),
    aggregator_key: str | None = Security(aggregator_api_key),
    platform_key: str | None = Security(platform_api_key),
) -> Audience:
    """FastAPI dependency: the audience for this request. 401 if unidentifiable."""
    if not iam_store.available():
        # IAM unconfigured. The pipeline runs without it, but then no caller can be attributed —
        # so the safe answer is "nothing", never "everything".
        log.warning("audience requested with no IAM store; refusing the read")
        return Audience(permitted_subscriber_ids=frozenset(), label="iam-unavailable")

    client_ip = request.client.host if request.client else None

    # --- 0. a PORTAL SESSION always wins ------------------------------------
    #
    # **Order matters, and getting it wrong is a write vulnerability.**
    #
    # `frontend/lib/api.ts` attaches the platform service key to EVERY request, including ones
    # made on behalf of a signed-in subscriber. So if the platform key were checked first, a
    # browser request carrying both a session and that key would resolve as *unrestricted* — and
    # a subscriber could change another subscriber's alert delivery simply by editing the
    # subscriber id in the URL. Caught while testing `PUT /channels`: the write succeeded and
    # overwrote a real address.
    #
    # A session is the more SPECIFIC credential: it names a person, where the platform key names
    # only "the portal". So when both are present the session is what the request is about, and
    # the key is merely transport. Checking it first is what makes that true.
    #
    # The platform key still reaches the unrestricted branch when it arrives ALONE, which is the
    # operations-dashboard and machine-to-machine case it exists for.
    session_token = getattr(session, "credentials", None)
    if session_token:
        claims = read_session(session_token)
        if claims is not None:
            account = await iam_store.get_account(claims.get("sub", ""))
            if account is not None:
                return await _account_audience(account)

    # --- 1. platform key ---------------------------------------------------
    if platform_key:
        resolved = await iam_store.resolve_api_key(platform_key.strip(), ip=client_ip)
        if resolved is not None:
            _account, scopes, _workspace = resolved
            if ApiKeyScope.PLATFORM_READ in scopes:
                return Audience(permitted_subscriber_ids=None, label="platform")

    # --- 2. aggregator key -------------------------------------------------
    if aggregator_key:
        resolved = await iam_store.resolve_api_key(aggregator_key.strip(), ip=client_ip)
        if resolved is not None:
            account, scopes, workspace_id = resolved
            if account.kind is AccountKind.COMMERCIAL and ApiKeyScope.READ in scopes:
                return await _tenant_audience(account.id, workspace_id)

    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "This data is private. Send a portal session, an aggregator API key, or a "
        "platform key with `platform:read`.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _account_audience(account) -> Audience:
    """What one signed-in account may see.

    A commercial account signed into the PORTAL sees its customers, exactly as its API key would —
    otherwise an aggregator's own dashboard would show nothing while its integration showed
    everything, and the two surfaces would disagree about what the account is entitled to. Plus its
    own subscription, if it holds one.
    """
    if account.kind is AccountKind.COMMERCIAL:
        tenant = await _tenant_audience(account.id, None)
        own = frozenset({account.subscriber_id}) if account.subscriber_id else frozenset()
        return Audience(
            permitted_subscriber_ids=(tenant.permitted_subscriber_ids or frozenset()) | own,
            label=f"portal-commercial:{account.id}",
        )
    return Audience(
        permitted_subscriber_ids=frozenset(
            {account.subscriber_id} if account.subscriber_id else set()
        ),
        label=f"account:{account.id}",
    )


async def _tenant_audience(account_id: str, workspace_id: str | None) -> Audience:
    """The subscriber ids an aggregator serves, via the membership edge.

    The membership query establishes the boundary *before* any subscriber is read, so another
    aggregator's customer is never a candidate — a bug in later filtering could not leak one.
    """
    customers = await iam_store.list_tenant_accounts(
        account_id, limit=MAX_TENANT_CUSTOMERS, workspace_id=workspace_id
    )
    if len(customers) >= MAX_TENANT_CUSTOMERS:
        log.warning(
            "tenant audience truncated; some customers are not in this scope",
            extra={"account_id": account_id, "cap": MAX_TENANT_CUSTOMERS},
        )
    return Audience(
        permitted_subscriber_ids=frozenset(
            c.subscriber_id for c in customers if c.subscriber_id
        ),
        label=f"aggregator:{account_id}",
    )
