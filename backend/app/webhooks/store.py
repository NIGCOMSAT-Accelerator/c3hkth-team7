"""Subscription CRUD and the delivery ledger.

Every function here follows the repository convention in `app/store/repository.py`:
reads never raise (they degrade to empty), and a write failure is logged rather than
propagated where the caller is a background sweep. The one exception is subscription
creation, which is a synchronous API call — a business creating an endpoint must
learn immediately that it failed, not discover it when nothing arrives.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db import session as db
from app.logging_config import get_logger
from app.webhooks import engine

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Subscriptions
# --------------------------------------------------------------------------- #


async def create_subscription(
    name: str,
    url: str,
    *,
    events: list[str] | None = None,
    min_severity: str | None = None,
    aoi_ids: list[str] | None = None,
    owner_account_id: str | None = None,
    owner_workspace_id: str | None = None,
) -> dict:
    """Register an endpoint and mint its signing secret.

    Raises on failure, unlike the sweep paths: this is a synchronous API call and
    silently returning a broken subscription would mean the business discovers it
    only when no events ever arrive.
    """
    subscription_id = engine.new_subscription_id()
    secret = engine.new_secret()

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO webhook_subscriptions
                (id, name, url, secret, events, min_severity, aoi_ids,
                 owner_account_id, owner_workspace_id)
            VALUES ($1, $2, $3, $4, $5, $6::severity, $7, $8, $9)
            RETURNING *
            """,
            subscription_id,
            name,
            url,
            secret,
            events or [],
            min_severity,
            aoi_ids or [],
            # NULL means platform-owned — see migration 016. Never defaulted to a caller.
            owner_account_id,
            owner_workspace_id,
        )
    return dict(row)


async def get_subscription(subscription_id: str) -> dict | None:
    try:
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM webhook_subscriptions WHERE id = $1", subscription_id
            )
        return dict(row) if row else None
    except Exception as exc:
        log.debug("subscription read failed", extra={"error": str(exc)})
        return None


async def list_subscriptions(
    *, include_inactive: bool = False, owner_account_id: str | None = None
) -> list[dict]:
    """Subscriptions, optionally narrowed to one owner.

    **`owner_account_id=None` means UNRESTRICTED, not "platform-owned rows".** That asymmetry is
    deliberate and matches `Audience.permitted_subscriber_ids`: the platform read is the unfiltered
    one, and an aggregator caller always passes its own id. Conflating them is the mistake that made
    a brand-new aggregator see an unrelated account's data in `resolve_audience`, so the same shape
    is avoided here.

    An aggregator therefore never sees a platform-owned row (`owner_account_id IS NULL`), which is
    correct — those belong to the operations team.
    """
    try:
        clauses: list[str] = []
        args: list = []
        if not include_inactive:
            clauses.append("active")
        if owner_account_id is not None:
            args.append(owner_account_id)
            clauses.append(f"owner_account_id = ${len(args)}")

        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        async with db.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM webhook_subscriptions {where}ORDER BY created_at DESC",
                *args,
            )
        return [dict(r) for r in rows]
    except Exception as exc:
        log.debug("subscription list failed", extra={"error": str(exc)})
        return []


async def active_subscriptions() -> list[dict]:
    """Endpoints eligible for fan-out. Uses the partial index."""
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM webhook_subscriptions WHERE active")
        return [dict(r) for r in rows]
    except Exception as exc:
        # A fan-out that cannot read its subscriptions must not break the pipeline
        # stage that triggered it.
        log.warning("could not load webhook subscriptions", extra={"error": str(exc)})
        return []


async def set_active(subscription_id: str, active: bool, *, reason: str = "") -> bool:
    try:
        async with db.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE webhook_subscriptions
                SET active = $2, updated_at = now(),
                    last_error = COALESCE(NULLIF($3, ''), last_error),
                    failure_streak = CASE WHEN $2 THEN 0 ELSE failure_streak END
                WHERE id = $1
                """,
                subscription_id,
                active,
                reason,
            )
        return result.endswith("1")
    except Exception as exc:
        log.warning("subscription toggle failed", extra={"error": str(exc)})
        return False


async def delete_subscription(subscription_id: str) -> bool:
    try:
        async with db.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM webhook_subscriptions WHERE id = $1", subscription_id
            )
        return result.endswith("1")
    except Exception as exc:
        log.warning("subscription delete failed", extra={"error": str(exc)})
        return False


async def rotate_secret(subscription_id: str) -> str | None:
    """Mint a new signing secret, returning it once.

    Rotation is a hard cutover rather than a grace period with two valid secrets.
    That is the safer default here: a leaked secret can forge flood alerts into a
    payout engine, so it must stop working the moment it is rotated. The cost is
    that the business must deploy the new secret promptly, which the API response
    says explicitly.
    """
    secret = engine.new_secret()
    try:
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE webhook_subscriptions
                SET secret = $2, updated_at = now()
                WHERE id = $1
                RETURNING secret
                """,
                subscription_id,
                secret,
            )
        return row["secret"] if row else None
    except Exception as exc:
        log.warning("secret rotation failed", extra={"error": str(exc)})
        return None


async def record_outcome(
    subscription_id: str, *, success: bool, error: str | None = None
) -> None:
    """Update an endpoint's health, and auto-disable a persistently dead one.

    The streak resets on any success, so an endpoint with intermittent outages is
    never disabled — only one that has failed continuously past the threshold.
    """
    try:
        async with db.acquire() as conn:
            if success:
                await conn.execute(
                    """
                    UPDATE webhook_subscriptions
                    SET failure_streak = 0, last_success_at = now(),
                        last_error = NULL, updated_at = now()
                    WHERE id = $1
                    """,
                    subscription_id,
                )
                return

            row = await conn.fetchrow(
                """
                UPDATE webhook_subscriptions
                SET failure_streak = failure_streak + 1,
                    last_failure_at = now(), last_error = $2, updated_at = now()
                WHERE id = $1
                RETURNING failure_streak, url
                """,
                subscription_id,
                (error or "")[:500],
            )
            if row and row["failure_streak"] >= settings.webhook_max_consecutive_failures:
                await conn.execute(
                    "UPDATE webhook_subscriptions SET active = false, updated_at = now() "
                    "WHERE id = $1",
                    subscription_id,
                )
                log.warning(
                    "webhook endpoint auto-disabled after consecutive failures",
                    extra={
                        "subscription_id": subscription_id,
                        "url": row["url"],
                        "failures": row["failure_streak"],
                    },
                )
    except Exception as exc:
        log.debug("outcome record failed", extra={"error": str(exc)})


# --------------------------------------------------------------------------- #
# Deliveries
# --------------------------------------------------------------------------- #


async def enqueue_delivery(
    subscription_id: str, event: str, payload: dict, *, delivery_id: str
) -> bool:
    """Persist a delivery *before* it is attempted.

    Ordering is the at-least-once guarantee: if the process dies mid-request, a
    retryable row exists. Writing after the attempt would lose the event entirely
    on a crash — at-most-once, which is the wrong tradeoff when the payload may
    trigger an insurance payout.
    """
    try:
        async with db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO webhook_deliveries
                    (id, subscription_id, event, payload, status, next_attempt_at)
                VALUES ($1, $2, $3, $4::jsonb, 'pending', now())
                ON CONFLICT (id) DO NOTHING
                """,
                delivery_id,
                subscription_id,
                event,
                json.dumps(payload, sort_keys=True, default=str),
            )
        return True
    except Exception as exc:
        log.warning("delivery enqueue failed", extra={"error": str(exc)})
        return False


async def due_deliveries(limit: int | None = None) -> list[dict]:
    """Deliveries ready for an attempt, joined to their endpoint.

    Joins rather than making the sweep issue a query per delivery, and filters on
    `active` so a disabled endpoint's backlog stops being retried without needing
    to be deleted.
    """
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT d.*, s.url, s.secret, s.active
                FROM webhook_deliveries d
                JOIN webhook_subscriptions s ON s.id = d.subscription_id
                WHERE d.status IN ('pending', 'failed')
                  AND d.next_attempt_at <= now()
                  AND s.active
                ORDER BY d.next_attempt_at
                LIMIT $1
                """,
                limit or settings.webhook_sweep_batch_size,
            )
        return [dict(r) for r in rows]
    except Exception as exc:
        log.debug("due deliveries read failed", extra={"error": str(exc)})
        return []


async def mark_delivered(delivery_id: str, status_code: int) -> None:
    try:
        async with db.acquire() as conn:
            await conn.execute(
                """
                UPDATE webhook_deliveries
                SET status = 'delivered', attempts = attempts + 1,
                    response_status = $2, delivered_at = now(),
                    next_attempt_at = NULL, last_error = NULL
                WHERE id = $1
                """,
                delivery_id,
                status_code,
            )
    except Exception as exc:
        log.debug("mark delivered failed", extra={"error": str(exc)})


async def mark_failed(
    delivery_id: str, *, status_code: int | None, error: str | None, retryable: bool
) -> None:
    """Record a failed attempt and schedule the next, or abandon it.

    `abandoned` is terminal and deliberately distinct from `failed`: it stays
    visible in the history so an integration support question has an answer,
    whereas deleting it would make the event look like it was never generated.
    """
    try:
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT attempts FROM webhook_deliveries WHERE id = $1", delivery_id
            )
            if row is None:
                return
            attempts = int(row["attempts"]) + 1

            delay = engine.next_attempt_delay(attempts) if retryable else None
            if delay is None:
                await conn.execute(
                    """
                    UPDATE webhook_deliveries
                    SET status = 'abandoned', attempts = $2, response_status = $3,
                        last_error = $4, next_attempt_at = NULL
                    WHERE id = $1
                    """,
                    delivery_id, attempts, status_code, (error or "")[:500],
                )
            else:
                await conn.execute(
                    """
                    UPDATE webhook_deliveries
                    SET status = 'failed', attempts = $2, response_status = $3,
                        last_error = $4, next_attempt_at = $5
                    WHERE id = $1
                    """,
                    delivery_id, attempts, status_code, (error or "")[:500],
                    engine.due_at(delay),
                )
    except Exception as exc:
        log.debug("mark failed failed", extra={"error": str(exc)})


async def delivery_history(
    subscription_id: str, *, limit: int = 100
) -> list[dict]:
    """Recent deliveries for one endpoint, newest first.

    The support-thread query. `payload` is excluded: it is the largest column by
    far and the operator question is about status and timing, not content.
    """
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, event, status, attempts, response_status, last_error,
                       created_at, delivered_at, next_attempt_at
                FROM webhook_deliveries
                WHERE subscription_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                subscription_id,
                limit,
            )
        return [dict(r) for r in rows]
    except Exception as exc:
        log.debug("delivery history read failed", extra={"error": str(exc)})
        return []


async def delivery_stats(subscription_id: str) -> dict:
    """Counts by status, for the endpoint's own health view."""
    try:
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    count(*)                                        AS total,
                    count(*) FILTER (WHERE status = 'delivered')    AS delivered,
                    count(*) FILTER (WHERE status = 'abandoned')    AS abandoned,
                    count(*) FILTER (WHERE status IN ('pending','failed')) AS in_flight
                FROM webhook_deliveries WHERE subscription_id = $1
                """,
                subscription_id,
            )
        if row is None:
            return {"total": 0, "delivered": 0, "abandoned": 0, "in_flight": 0}
        stats = {k: int(row[k] or 0) for k in ("total", "delivered", "abandoned", "in_flight")}
        # None rather than 0.0 when nothing has been attempted — 0% success would
        # read as total failure. Same discipline as `verification_metrics`.
        stats["success_rate"] = (
            stats["delivered"] / stats["total"] if stats["total"] else None
        )
        return stats
    except Exception as exc:
        log.debug("delivery stats read failed", extra={"error": str(exc)})
        return {"total": 0, "delivered": 0, "abandoned": 0, "in_flight": 0, "success_rate": None}


async def prune_history() -> int:
    """Drop delivery rows past the retention window.

    Only terminal rows: an in-flight delivery must survive its own retry schedule
    even if that schedule outlives the retention window.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=settings.webhook_history_retention_days
    )
    try:
        async with db.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM webhook_deliveries "
                "WHERE created_at < $1 AND status IN ('delivered', 'abandoned')",
                cutoff,
            )
        return int(result.split()[-1]) if result else 0
    except Exception as exc:
        log.debug("history prune failed", extra={"error": str(exc)})
        return 0
