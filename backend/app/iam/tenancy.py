"""Multi-tenancy — a subscriber may belong to many aggregators.

## The modelling decision

A farmer is one person with one email address, and may be served by several
aggregators at once: their cooperative, their insurer, and the state extension
service. All three legitimately need to see that farmer's alerts; none of them owns the
farmer.

`managed_by: str` could not express this. It made the *first* aggregator to register
someone the permanent owner, and a second aggregator attempting to onboard them got a
409 — which reads as "that address is taken" when the truth is "you may both serve
them". So membership is a **join collection**, not a field:

```
accounts        one document per person or organisation   (identity)
memberships     one document per (account, aggregator)    (relationship)
```

## Why a join collection rather than an array on the account

An array of aggregator ids on the account document is the obvious Mongo shortcut and
is wrong here for three reasons:

1. **The account document is read on every authenticated request.** An unbounded array
   makes authentication progressively slower for the most-connected farmers.
2. **The edge carries its own data.** Each aggregator knows the farmer by their own
   internal reference, joined them on a different date, and may be suspended
   independently. That belongs on the relationship, not duplicated per element inside
   the identity.
3. **Both directions need indexing.** "which aggregators serve this farmer" and "which
   farmers does this aggregator serve" are both hot queries; an array indexes one well
   and the other poorly.

## Statelessness

Nothing here caches tenant context in the process. Every scoped read resolves the
membership from Atlas on the request, keyed by the API key's account id. That is what
makes the API horizontally scalable — any replica can serve any tenant's request with
no affinity, and revoking a membership takes effect on the next call rather than when
some cache expires.

## The isolation property, stated precisely

An aggregator can read exactly the accounts for which a `memberships` document pairs
them. Enforced by putting `aggregator_id` **inside** the query — never by filtering
results afterwards — so another tenant's customer is never a candidate row. Same
reasoning as the session filter in chat retrieval and `list_managed_accounts`.

**Identity is shared; the relationship is private.** Aggregator A can see that the
farmer exists and is theirs. A cannot see that B also serves them, cannot see B's
internal reference, and cannot remove B's membership.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from app.logging_config import get_logger

log = get_logger(__name__)


class MembershipStatus(str, Enum):
    ACTIVE = "active"
    #: The aggregator detached this customer. The edge is retained rather than deleted
    #: so re-attaching restores the same external reference and the history of who
    #: served whom survives — which matters for an insurance dispute.
    DETACHED = "detached"
    #: The subscriber revoked this aggregator's access from the portal. Distinct from
    #: DETACHED because only the subscriber may undo it: an aggregator must not be
    #: able to re-attach itself after the person removed it.
    REVOKED_BY_SUBSCRIBER = "revoked_by_subscriber"


class MembershipRole(str, Enum):
    """What an aggregator does for this subscriber.

    Recorded because it is the honest answer to "why does this organisation see my
    data?", which is the question a farmer asks and a regulator asks on their behalf.
    Not used for authorisation — scopes on the API key do that.
    """

    #: Onboarded them and manages their areas.
    MANAGER = "manager"
    #: Receives their alerts but does not manage them, e.g. an insurer.
    OBSERVER = "observer"


@dataclass(frozen=True)
class Membership:
    """One (account, aggregator) edge."""

    id: str
    account_id: str
    aggregator_id: str
    status: MembershipStatus
    role: MembershipRole
    #: The aggregator's own identifier for this person — their member number, policy
    #: number, loan id. **Lives on the edge, not the account**, because two
    #: aggregators know the same farmer by different references and neither's should
    #: overwrite the other's.
    external_ref: str | None
    #: Which aggregator's action created the identity. Informational only; it confers
    #: no privilege, which is the whole point of the change away from `managed_by`.
    onboarded_by_this_tenant: bool
    joined_at: datetime
    detached_at: datetime | None = None


def new_membership_id() -> str:
    return f"mem_{uuid.uuid4().hex[:20]}"


def build_membership_document(  # noqa: PLR0913 — one field per stored column
    account_id: str,
    aggregator_id: str,
    *,
    role: MembershipRole = MembershipRole.MANAGER,
    external_ref: str | None = None,
    onboarded_by_this_tenant: bool = False,
    workspace_id: str | None = None,
) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": new_membership_id(),
        "account_id": account_id,
        "aggregator_id": aggregator_id,
        "status": MembershipStatus.ACTIVE.value,
        "role": role.value,
        "external_ref": (external_ref or "").strip() or None,
        # Which of the aggregator's projects this customer belongs to.
        #
        # An aggregator running the Bayelsa flood pilot and the Kebbi rice season keeps two
        # customer bases, each reached with that workspace's own key. None for a customer
        # onboarded before workspaces existed, which reads as "all projects" rather than none —
        # excluding them from every list would hide real customers.
        "workspace_id": workspace_id,
        "onboarded_by_this_tenant": onboarded_by_this_tenant,
        "joined_at": now,
        "detached_at": None,
    }


def to_membership(doc: dict | None) -> Membership | None:
    if doc is None:
        return None
    try:
        return Membership(
            id=doc["id"],
            account_id=doc["account_id"],
            aggregator_id=doc["aggregator_id"],
            status=MembershipStatus(doc.get("status", "active")),
            role=MembershipRole(doc.get("role", "manager")),
            external_ref=doc.get("external_ref"),
            onboarded_by_this_tenant=bool(doc.get("onboarded_by_this_tenant", False)),
            joined_at=doc["joined_at"],
            detached_at=doc.get("detached_at"),
        )
    except Exception as exc:
        log.warning(
            "membership document failed validation",
            extra={"membership_id": doc.get("id", "<missing>"), "error": str(exc)[:200]},
        )
        return None


def active_filter(aggregator_id: str, *, workspace_id: str | None = None) -> dict:
    """Mongo filter for one tenant's live memberships, optionally within one workspace.

    A single helper so every scoped query uses the identical predicate. Three separate
    hand-written filters is how one code path forgets the status check and keeps
    serving a detached customer.

    ## The workspace clause is STRICT — an unscoped membership matches nothing

    An aggregator running several projects reaches each with that workspace's own key, and a
    customer onboarded under the Bayelsa key must not appear when the Kebbi key lists customers.

    An earlier version also matched `workspace_id: None`, to protect customers onboarded before
    workspaces existed. That tolerance is **removed**: no such rows exist (verified — zero
    memberships in the database at the time of the change), so it protected nothing while leaving
    a permanent hole in the boundary. Any membership written from now on carries a workspace,
    because `create_customer` takes it from the presenting key.

    The strict form is also the safer failure: a membership somehow lacking a workspace becomes
    invisible to a scoped key rather than visible to *every* one of them. An aggregator noticing a
    missing customer files a support request; a customer leaking across project boundaries is a
    confidentiality breach nobody reports.
    """
    query: dict = {
        "aggregator_id": aggregator_id,
        "status": MembershipStatus.ACTIVE.value,
    }
    if workspace_id is not None:
        query["workspace_id"] = workspace_id
    return query


def scoped_account_filter(aggregator_id: str, account_ids: list[str]) -> dict:
    """Filter accounts to those this tenant may see.

    The id list comes from `memberships`, so the tenant boundary is established before
    the accounts collection is touched at all.
    """
    return {"id": {"$in": account_ids}}
