"""Autonomous watch loop.

This is what makes SHELTER an agent rather than an API: nobody has to ask. The
loop wakes on an interval, queues a scan for every area every active subscriber
watches, and goes back to sleep. Sentinel-1 revisits West Africa roughly every
6 days and Sentinel-2 every 5, so the default 6-hour cycle comfortably catches
each new pass without hammering the catalogues.

Runs inside the API process by default (one container, hackathon-friendly). For
a deployment with more than one API replica, disable it there and run a single
instance alongside the workers instead — otherwise every replica queues the
same scans.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from datetime import datetime, timedelta, timezone

from app.agents import pipeline
from app.config import settings
from app.eo import sources as source_registry
from app.logging_config import get_logger
from app.store import repository
from app.webhooks import publisher as webhook_publisher

log = get_logger(__name__)

_task: asyncio.Task | None = None
_last_cycle: datetime | None = None

#: When attribution was last reconciled. In-process, deliberately.
#:
#: Persisting it would need a row, and losing it on restart is harmless: the sweep is idempotent,
#: so an extra run costs one pass over the area list and changes nothing. A restart loop would
#: reconcile more often than intended, which is wasteful but never wrong — the opposite trade
#: (a durable timer that skips the sweep after a crash) leaves areas unbillable.
_last_reconcile: datetime | None = None


async def _reconcile_attribution_if_due() -> None:
    """Repair unattributed areas, at most every `attribution_reconcile_hours`. Never raises.

    ## Why this belongs on the loop at all

    `record_attribution` is best-effort at creation — it must be, because a Mongo hiccup cannot be
    allowed to block a farmer from registering a plot. The consequence is that a failed write
    leaves an area that is **monitored but unbillable**, and nothing reports it: assessments keep
    being produced and simply never appear on an invoice. That is a silent revenue hole, which is
    precisely the class of bug a periodic sweep exists to close.

    ## Why it rides this loop rather than having its own timer

    Same reasoning as the Fahis and webhook sweeps above: this process already wakes on an
    interval, and a second timer would be a second thing to supervise. Its own cadence is enforced
    by the timestamp check rather than by the sleep, because the watch loop is 6-hourly and this
    wants to be daily.

    ## Why the failure is swallowed

    A billing repair must never cost the cycle its scans, which are the product. Logged loudly
    enough to notice, and retried on the next cycle regardless.
    """
    global _last_reconcile

    if settings.attribution_reconcile_hours <= 0:
        return

    now = datetime.now(timezone.utc)
    if _last_reconcile is not None:
        due_at = _last_reconcile + timedelta(hours=settings.attribution_reconcile_hours)
        if now < due_at:
            return

    from app.iam import store as iam_store

    if not iam_store.available():
        # No IAM store configured — this deployment has no billing to reconcile. Not an error, and
        # not worth a log line every cycle.
        return

    # Stamped BEFORE the run, not after. A sweep that dies partway must not retry on every
    # subsequent cycle: it is idempotent so the next scheduled pass finishes the job, whereas
    # stamping on success would turn one persistent failure into a sweep on every wake-up.
    _last_reconcile = now

    try:
        result = await iam_store.reconcile_attribution()
        if result.get("attributed") or result.get("backfilled"):
            log.info("attribution reconciled", extra=result)
        if result.get("unresolved"):
            # Areas with no resolvable owner. Each one is an area being monitored that nobody is
            # billed for, so this is surfaced rather than counted silently.
            log.warning(
                "areas remain unattributed after reconciliation",
                extra={"unresolved": result["unresolved"], "checked": result["checked"]},
            )
    except Exception:
        log.exception("attribution reconcile failed; scans were unaffected")


async def _cycle() -> int:
    """One pass: queue new scans, then sweep for verifications now due."""
    global _last_cycle
    subscribers = await repository.list_subscribers(active_only=True)

    queued = 0
    if subscribers:
        areas = sum(len(s.areas) for s in subscribers)
        log.info(
            "watch cycle: dispatching Scout",
            extra={
                "subscribers": len(subscribers),
                "areas": areas,
                # Each area is an independent job on the Scout stream, which is what
                # makes the pipeline parallel across datasets.
                "parallel_jobs": areas,
            },
        )
        queued = await pipeline.enqueue_all(subscribers)
    else:
        # The bootstrap state. Deliberately explicit rather than a bare
        # "0 subscribers": an operator watching a fresh deployment needs to know
        # the service is healthy and *waiting*, not broken or misconfigured.
        # Every store is up by this point — only the trigger is absent.
        log.info(
            "BOOTSTRAP: no active subscribers — Scout has nothing to poll. "
            "The watch loop is running and will dispatch on the next cycle once an "
            f"area is registered (POST {settings.api_prefix}/subscribers, or the "
            "/subscribe page). "
            "Registration queues an immediate first scan, so there is no wait for "
            "this interval to elapse.",
            extra={
                "subscribers": 0,
                "areas": 0,
                "next_cycle_seconds": settings.scheduler_interval_seconds,
                # Surfaced here because a bootstrap operator's next question is
                # "are the feeds even reachable?" — this answers it without a
                # separate /health call.
                "sources_configured": sum(
                    1 for s in source_registry.SOURCES if source_registry.configured(s)
                ),
                "sources_total": len(source_registry.SOURCES),
            },
        )

    # Fahis sweep. Deferred by days, so it rides the same loop rather than
    # needing its own timer — see agents/fahis.verify_after_for.
    #
    # Wrapped separately: verification is a nice-to-have that must never cost the
    # cycle its scans, which are the actual product.
    try:
        verifications = await pipeline.enqueue_due_verifications()
        if verifications:
            log.info("verification sweep", extra={"jobs_queued": verifications})
    except Exception:
        log.exception("verification sweep failed; scans were unaffected")

    # Webhook retry sweep. Same reasoning as the Fahis sweep above: Redis Streams
    # cannot delay a message and a sleeping worker loses its timer on restart, so
    # `webhook_deliveries.next_attempt_at` is a Postgres column this loop queries.
    #
    # Wrapped separately for the same reason too — a partner's dead endpoint must
    # never cost the cycle its scans.
    try:
        result = await webhook_publisher.sweep()
        if result.get("attempted"):
            log.info("webhook retry sweep", extra=result)
    except Exception:
        log.exception("webhook sweep failed; scans were unaffected")

    # Attribution repair. Rate-limited internally to its own cadence — see the function.
    await _reconcile_attribution_if_due()

    _last_cycle = datetime.now(timezone.utc)
    log.info(
        "watch cycle complete",
        extra={"subscribers": len(subscribers), "jobs_queued": queued},
    )
    return queued


async def _loop() -> None:
    # Small startup delay so the API is serving before the first burst of
    # satellite reads competes with it for the event loop.
    await asyncio.sleep(15)

    while True:
        try:
            await _cycle()
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed cycle must never kill the loop — the next satellite pass
            # is only hours away and the service has to still be watching.
            log.exception("watch cycle failed; continuing")

        # Jitter keeps multiple deployments from synchronising onto the same
        # catalogue endpoints at the same instant.
        jitter = random.uniform(0, settings.scheduler_jitter_seconds)
        await asyncio.sleep(settings.scheduler_interval_seconds + jitter)


def start() -> None:
    global _task
    if not settings.scheduler_enabled:
        log.info("scheduler disabled by configuration")
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop())
    log.info(
        "watch loop started",
        extra={"interval_seconds": settings.scheduler_interval_seconds},
    )


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _task
    _task = None
    log.info("watch loop stopped")


def status() -> dict:
    return {
        "enabled": settings.scheduler_enabled,
        "running": _task is not None and not _task.done(),
        "interval_seconds": settings.scheduler_interval_seconds,
        "last_cycle": _last_cycle.isoformat() if _last_cycle else None,
        # Reported so an operator can tell "the sweep has not run yet" from "it ran and found
        # nothing" — the two look identical in the absence of a timestamp, and only one of them
        # means areas may be sitting unbillable.
        "attribution_reconcile_hours": settings.attribution_reconcile_hours,
        "last_attribution_reconcile": (
            _last_reconcile.isoformat() if _last_reconcile else None
        ),
    }


async def trigger_now() -> int:
    """Run a cycle immediately, outside the schedule. Used by the API."""
    return await _cycle()
