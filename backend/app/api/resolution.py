"""Short-lived tokens that carry a resolved area from a dry-run to the write that commits it.

## Why a token rather than trusting the client to send the area back

The partner flow is two calls by design:

    POST /iam/customers/{id}/areas/resolve   address  -> geometry + resolution_token
    POST /iam/customers/{id}/areas           token    -> monitored area, scan queued

The obvious alternative is to let the caller post the resolved `area` object straight back —
which is what the portal does today, and it is fine there because a human has just looked at the
shape on a map. It is **not** fine for a server-to-server integration, for one reason:

**Address resolution is the one input whose errors are silent.** `check_monitorable` catches a bad
size; `validate_ring` catches a bad shape. Nothing catches a *plausible* coordinate in the wrong
place. This platform has already registered a Nigerian farm in **England** through exactly that
path — the geocoder found nothing, browser geolocation filled the gap, and every number
downstream looked reasonable. A token means the geometry the pipeline monitors is byte-for-byte
the geometry we resolved and showed the partner, rather than something reconstructed by their code
from fields they may have mapped wrongly.

It also makes the confirmation step **unskippable** without making it burdensome: one extra field
on a call they are already making.

## Why the cache and not Postgres

These live for minutes and are read exactly once. A table would need a row, an index, a migration
and a sweeper for the ones nobody redeems — to hold data whose entire value expires before a
scheduled job would notice. Dragonfly's TTL is the right primitive, and `cache.set_text` makes the
TTL a required argument so one cannot be stored without an expiry.

**Consequence, stated rather than hidden:** an unreachable cache means resolve still answers but
the token cannot be redeemed. That is the correct failure direction — the alternative is accepting
an unverified area — and `resolve` says so in its response rather than letting the partner
discover it on the write.

## Single use

Consumed on redemption (`GETDEL` semantics via read-then-delete). Not for replay-attack reasons —
the token authorises nothing on its own, and the caller is already authenticated by their API key
— but because a token that could be redeemed twice invites a retry loop that creates duplicate
areas for one plot. `repository.add_area` raises `DuplicateAreaError`, so the second would fail
anyway; failing at the token is the clearer error.
"""

from __future__ import annotations

import json
import secrets

from app.logging_config import describe, get_logger
from app.store import cache

log = get_logger(__name__)

#: Cache namespace. Distinct from `geo:places`, which holds upstream geocoder payloads shared
#: across every caller — these are per-caller and per-customer and must never be conflated.
_PREFIX = "area-resolution"

#: How long a resolved area may be held before it is committed, seconds.
#:
#: Ten minutes. Long enough for a partner to show the geometry to an operator, or for a batch
#: importer to resolve a page of rows and then commit them; short enough that a resolution cannot
#: sit around while the underlying place data changes beneath it.
#:
#: Deliberately NOT hours: the token pins a geometry, and a geometry pinned overnight could be
#: committed against a customer whose subscription was cancelled in between.
TTL_SECONDS = 600

#: `shltres_` so a value found in a log or a bug report is identifiable at a glance, the same
#: reasoning behind the `shltky_` API-key prefix.
_TOKEN_PREFIX = "shltres_"


def _cache_key(token: str) -> str:
    return cache.key(_PREFIX, token)


async def issue(*, area: dict, account_id: str, aggregator_id: str) -> str | None:
    """Store a resolved area and return its token. None when the cache is unavailable.

    `account_id` and `aggregator_id` are stored **with** the area, not merely used to build the
    key, because the redeeming route re-checks them. A token minted for one customer must not be
    usable against another even by an aggregator who legitimately holds both — otherwise a
    spreadsheet with a shifted column could quietly attach every plot to the wrong farmer, and
    every downstream number would look correct.
    """
    token = _TOKEN_PREFIX + secrets.token_urlsafe(24)
    payload = json.dumps(
        {"area": area, "account_id": account_id, "aggregator_id": aggregator_id}
    )

    try:
        await cache.set_text(_cache_key(token), payload, ttl_seconds=TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 — resolve must still answer with the geometry
        log.warning("could not store a resolution token", extra={"error": describe(exc)})
        return None

    return token


async def redeem(token: str, *, account_id: str, aggregator_id: str) -> dict | None:
    """The area this token was issued for, or None.

    None covers every failure the caller must treat identically — unknown, expired, already used,
    or issued for a different customer or aggregator. They are deliberately not distinguished in
    the return value: telling a caller "that token belongs to another customer" confirms the
    token existed, and the recovery is the same in every case (resolve again).

    The route turns this into a 422 naming the recovery, not a 403.
    """
    if not token or not token.startswith(_TOKEN_PREFIX):
        return None

    key = _cache_key(token)
    try:
        raw = await cache.get_text(key)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read a resolution token", extra={"error": describe(exc)})
        return None

    if not raw:
        return None

    # Delete BEFORE returning, so a concurrent second redemption of the same token cannot also
    # succeed. Read-then-delete is not atomic, so a genuine race could still let two through —
    # `repository.add_area` raises `DuplicateAreaError` on the second, which is the backstop.
    # Worth being explicit that this is defence in depth rather than a lock.
    try:
        await cache.delete(key)
    except Exception:  # noqa: BLE001 — a token that outlives its use is a lesser fault
        log.debug("could not delete a redeemed resolution token", exc_info=True)

    try:
        stored = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None

    if (
        stored.get("account_id") != account_id
        or stored.get("aggregator_id") != aggregator_id
    ):
        log.warning(
            "resolution token presented for a different customer or aggregator",
            extra={"expected_account": stored.get("account_id"), "got_account": account_id},
        )
        return None

    area = stored.get("area")
    return area if isinstance(area, dict) else None
