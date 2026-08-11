"""Who owns a monitored area, and therefore who is billed for it.

## The business model, stated once

SHELTER serves two audiences from one platform, and they are **distinct, not variations of
each other**:

| | B2C — individual | B2B — aggregator |
|---|---|---|
| Who | A farmer or household, signed up directly | A business entity: a bank, insurer, state agency, cooperative |
| Example | Someone registering their own plot | Central Bank of Nigeria's Farmer Anchor Scheme |
| How areas arrive | The portal's own subscribe flow | Their own systems, via the Partner API |
| Whose farmers | Their own single plot | **Their** customers, onboarded by them |
| Billing | Personal subscription | Billed as one commercial customer, separately |
| Aggregator link | **None, ever** | Is the aggregator |

The consequence that matters for this module: **an individual has no aggregator**. A direct
subscriber is not a degenerate case of an aggregator's customer — they are a different product
with a different price and a different relationship.

## Why this needed its own module

Both were previously `AccountKind.INDIVIDUAL`, distinguished only by whether a `memberships`
edge happened to exist. Ownership was therefore *derived* — "if an edge exists, an aggregator
owns this; otherwise the person does" — and a derived answer to a billing question is a
liability. It also left the many-to-many case ambiguous: `memberships` permits several
aggregators per subscriber, so "who is billed" had no single answer.

Under the model above the ambiguity disappears. An AOI belongs to exactly one of:

  * the **individual** who registered it, or
  * the **aggregator** who onboarded the farmer whose area it is.

Never both, never several. So attribution can be *recorded* rather than inferred.

## Why the join stays in Mongo and Postgres stays tenant-blind

Postgres holds no tenant column and gains none. That is the blast-radius separation CLAUDE.md
describes: the pipeline keeps running when Mongo is unavailable, because Scout, Analyst and
Oracle never ask who owns anything.

So this collection lives in Mongo and is keyed by `aoi_id`. Billing aggregates assessment
counts in Postgres by `aoi_id`, then resolves each one to its owner here. Two queries, no
coupling, and the risk layer stays unable to care.

## Recorded at creation, maintained through the lifecycle

An attribution row is written when an area is created and removed when it is deleted — from
whichever path created it. Writing it only at billing time would mean reconstructing history
from a `memberships` table that has since changed: a farmer who moved from one scheme to
another would have last month's assessments attributed to this month's aggregator.

`recorded_at` and the immutable audit of changes are what make a disputed invoice answerable.

## Why the workspace is recorded too, and why only for aggregators

An aggregator holds **several workspaces**, one per customer base — the Bayelsa pilot, the
Anchor Scheme, an insurer's portfolio. They are projects, and a project is the unit a partner
actually reconciles against: "what did the Kano rollout cost us this quarter" is the question
being asked, and rolling every area up to the aggregator alone cannot answer it.

Recorded at creation for the same reason ownership is. The workspace is knowable only from the
path that created the area — the API key presented, or the workspace route called — and by the
time an invoice is assembled that context is gone. Deriving it later from `memberships` would
attribute last quarter's areas to whichever project the customer sits in *today*, and a customer
moving between projects is a normal thing that happens.

**`workspace_id` is None for an individual, always.** Workspaces are an aggregator capability;
a B2C subscriber has no project structure and inventing one for them would put a concept in
their invoice that does not exist in their product.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum


class OwnerKind(str, Enum):
    """Which audience this area is billed to.

    Two values, deliberately closed. A third would mean a third product with a third price,
    which is a commercial decision rather than a schema one.
    """

    #: A direct B2C subscriber. `owner_id` is their own account id, and there is no aggregator.
    INDIVIDUAL = "individual"
    #: An aggregator's customer. `owner_id` is the AGGREGATOR's account id — the billable
    #: entity — and `subject_account_id` is the farmer whose land it is.
    AGGREGATOR = "aggregator"


def build_attribution(
    *,
    aoi_id: str,
    owner_kind: OwnerKind,
    owner_id: str,
    subscriber_id: str,
    subject_account_id: str | None = None,
    external_ref: str | None = None,
    workspace_id: str | None = None,
) -> dict:
    """One `(aoi_id → billable owner)` record.

    `owner_id` is always the party that pays: the individual themselves, or the aggregator.
    `subject_account_id` is the person the area belongs to, which differs from `owner_id` only
    in the aggregator case — the Anchor Scheme is billed, the farmer is monitored.

    Keeping both is what lets an invoice line say "1,204 areas across 890 farmers" rather than
    a single opaque count, and lets a farmer still ask "who sees my data?" and get an answer.

    `external_ref` is the aggregator's own identifier for this area or farmer — their loan id,
    member number, scheme reference. Copied here so a reconciliation against their system does
    not require a join back through `memberships`, which may have changed.

    `workspace_id` is the aggregator's project this area belongs to, so an invoice can break down
    per customer base rather than presenting one undifferentiated total. **Forced to None for an
    individual** — see the module docstring: workspaces are an aggregator capability, and a value
    here for a B2C subscriber would be a project that does not exist.
    """
    return {
        "aoi_id": aoi_id,
        "owner_kind": owner_kind.value,
        "owner_id": owner_id,
        "subject_account_id": subject_account_id or owner_id,
        "subscriber_id": subscriber_id,
        "external_ref": external_ref,
        # Normalised here rather than trusted from the caller. Every creation path would
        # otherwise need to remember the rule, and the one that forgot would put a workspace on
        # an individual's invoice — visible only to the person reading it.
        "workspace_id": (
            workspace_id if owner_kind is OwnerKind.AGGREGATOR else None
        ),
        "recorded_at": datetime.now(timezone.utc),
        # Set when monitoring stops. The row is KEPT rather than deleted: an invoice for a
        # period during which the area was monitored must remain answerable after the customer
        # removes it, and a deleted row would make a past charge unexplainable.
        "ended_at": None,
    }


def is_billable_at(record: dict, at: datetime) -> bool:
    """Whether this area was billable to its owner at a given moment.

    Used to price a period rather than a snapshot: an area created mid-month and removed the
    next should appear on one invoice and not the other. Comparing against `recorded_at` and
    `ended_at` is what makes that possible, and is the reason `ended_at` is a timestamp instead
    of a boolean flag.
    """
    recorded = record.get("recorded_at")
    if recorded is None:
        return False
    if recorded.tzinfo is None:
        recorded = recorded.replace(tzinfo=timezone.utc)
    if recorded > at:
        return False

    ended = record.get("ended_at")
    if ended is None:
        return True
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=timezone.utc)
    return ended > at
