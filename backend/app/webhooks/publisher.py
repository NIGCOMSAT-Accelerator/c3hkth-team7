"""Fan-out and the retry sweep.

Two entry points:

`publish`  — called from the Herald after an alert, and from Fahis after a verdict.
             Enqueues one delivery row per matching endpoint and attempts each once.
`sweep`    — called by the scheduler each cycle. Retries what is due.

**The constraint that shapes both:** neither may raise, and neither may block the
pipeline. `publish` runs inside a queue stage that is delivering a flood warning; a
business's dead endpoint must not slow that down, let alone fail it. So the first
attempt is fire-and-forget and everything else is the sweep's problem.

**Why retries live in the scheduler rather than a delayed queue message.** Redis
Streams cannot delay delivery, and a worker sleeping on a timer loses it on restart.
`next_attempt_at` is a Postgres column the sweep queries — the same reasoning as
`assessments.verify_after` for Fahis.
"""

from __future__ import annotations

import asyncio

from app.config import settings
from app.logging_config import get_logger
from app.webhooks import engine, store

log = get_logger(__name__)


async def publish(event: str, data: dict, *, severity: str | None = None,
                  aoi_id: str | None = None) -> int:
    """Fan an event out to every matching endpoint. Returns the number enqueued.

    Never raises. A failure to publish is logged and the pipeline continues — the
    alternative would be a business integration able to break the warning pipeline
    by taking their endpoint down.
    """
    if not settings.webhook_engine_enabled:
        return 0

    try:
        subscriptions = await store.active_subscriptions()
    except Exception as exc:
        log.warning("webhook fan-out skipped", extra={"error": str(exc)})
        return 0

    targets = [s for s in subscriptions if engine.matches(s, event, severity, aoi_id)]
    if not targets:
        return 0

    # Concurrent, and `return_exceptions` so one endpoint hanging to its full
    # timeout cannot delay the others or surface as an unhandled task exception.
    results = await asyncio.gather(
        *(_publish_one(s, event, data) for s in targets), return_exceptions=True
    )
    enqueued = sum(1 for r in results if r is True)

    log.info(
        "webhook event published",
        extra={"event": event, "matched": len(targets), "enqueued": enqueued},
    )
    return enqueued


async def _publish_one(subscription: dict, event: str, data: dict) -> bool:
    delivery_id = engine.new_delivery_id()
    payload = engine.event_payload(event, data, delivery_id=delivery_id)

    if not await store.enqueue_delivery(
        subscription["id"], event, payload, delivery_id=delivery_id
    ):
        return False

    # Attempt immediately. A business integration expects near-real-time delivery,
    # and waiting for the next scheduler cycle would add up to six hours of latency
    # to an EMERGENCY alert.
    await _attempt(
        delivery_id=delivery_id,
        subscription_id=subscription["id"],
        url=subscription["url"],
        secret=subscription["secret"],
        event=event,
        payload=payload,
    )
    return True


async def _attempt(*, delivery_id: str, subscription_id: str, url: str,
                   secret: str, event: str, payload: dict) -> bool:
    """One attempt, with the outcome recorded on both the delivery and the endpoint."""
    body = engine.canonical_body(payload)

    delivered, status_code, error = await engine.deliver(
        url, body, secret, event=event, delivery_id=delivery_id
    )

    if delivered:
        await store.mark_delivered(delivery_id, status_code or 200)
        await store.record_outcome(subscription_id, success=True)
        return True

    await store.mark_failed(
        delivery_id,
        status_code=status_code,
        error=error,
        retryable=engine.is_retryable(status_code),
    )
    await store.record_outcome(subscription_id, success=False, error=error)
    log.info(
        "webhook delivery failed",
        extra={
            "delivery_id": delivery_id,
            "event": event,
            "status": status_code,
            "retryable": engine.is_retryable(status_code),
        },
    )
    return False


async def sweep() -> dict:
    """Retry every due delivery. Called once per scheduler cycle.

    Batched (`WEBHOOK_SWEEP_BATCH_SIZE`) so one endpoint with a large backlog cannot
    monopolise a cycle, and the remainder is simply picked up next time — the queue
    drains without any single sweep running long.
    """
    if not settings.webhook_engine_enabled:
        return {"attempted": 0, "delivered": 0}

    due = await store.due_deliveries()
    if not due:
        return {"attempted": 0, "delivered": 0}

    results = await asyncio.gather(
        *(
            _attempt(
                delivery_id=d["id"],
                subscription_id=d["subscription_id"],
                url=d["url"],
                secret=d["secret"],
                event=d["event"],
                # The stored bytes, not a re-serialisation: re-encoding could
                # reorder keys and invalidate the signature the receiver checks.
                payload=d["payload"] if isinstance(d["payload"], dict)
                else __import__("json").loads(d["payload"]),
            )
            for d in due
        ),
        return_exceptions=True,
    )

    delivered = sum(1 for r in results if r is True)
    log.info(
        "webhook retry sweep complete",
        extra={"attempted": len(due), "delivered": delivered},
    )
    return {"attempted": len(due), "delivered": delivered}
