"""Pipeline orchestration.

Two ways to run the four agents:

`run_inline`  — Scout → Analyst → Oracle → Herald in one coroutine. Used by the
                on-demand API endpoint and by tests, where a caller is waiting
                for the answer and queue hops would only add latency.

`enqueue_*`   — publish to the Redis stream and let workers pick it up. Used by
                the autonomous scheduler, where nobody is waiting and the
                decoupling buys back-pressure and crash recovery.

Both paths run the same agent objects, so behaviour cannot drift between them.
"""

from __future__ import annotations

import asyncio

from app import tracing
from app.agents.analyst import AnalystAgent
from app.agents.fahis import FahisAgent
from app.agents.herald import HeraldAgent
from app.agents.oracle import OracleAgent
from app.agents.scout import ScoutAgent
from app.config import settings
from app.logging_config import get_logger
from app.models.enums import JobStage
from app.models.schemas import (
    Alert,
    AreaOfInterest,
    JobEnvelope,
    RiskAssessment,
    Subscriber,
)
from app.queue import broker

log = get_logger(__name__)

scout = ScoutAgent()
analyst = AnalystAgent()
oracle = OracleAgent()
herald = HeraldAgent()
#: Off the main line — see agents/fahis.py. Never enqueued by Herald; the
#: scheduler sweeps it days later.
fahis = FahisAgent()


async def _assess_stages(aoi: AreaOfInterest) -> RiskAssessment:
    """Stages 1–3, with no trace of their own.

    Split out so `assess` and `run_inline` can each own the trace boundary without
    either duplicating the stage sequence or nesting two `trace` blocks — nesting
    would mint a second id and split one logical run across two.
    """
    scout_result = await scout.execute(aoi)
    analyst_result = await analyst.execute(scout_result)
    return await oracle.execute((aoi, analyst_result))


async def assess(aoi: AreaOfInterest) -> RiskAssessment:
    """Stages 1–3: imagery to decision, without dispatching anything.

    Traced like the queued path, so `POST /risk/assess` is as followable as a
    scheduled scan. The queued path takes its stage from the worker's consumer loop;
    here all three run in one coroutine, so the trace is bound once and
    `Agent.execute` supplies the per-stage detail.
    """
    with tracing.trace(tracing.new_run_id(), stage="inline", aoi_id=aoi.id):
        return await _assess_stages(aoi)


async def run_inline(subscriber: Subscriber, aoi: AreaOfInterest) -> Alert | None:
    """Full pipeline for one subscriber and one area.

    Returns None when the Herald suppressed dispatch (below floor, or a
    duplicate) — the assessment is still persisted either way.

    One trace spans all four stages. `run_for_subscriber` gathers several of these
    concurrently and each gets its own id, because asyncio copies the context per
    task — the case a module-level global would corrupt.
    """
    with tracing.trace(tracing.new_run_id(), stage="inline", aoi_id=aoi.id):
        assessment = await _assess_stages(aoi)
        return await herald.execute((subscriber, assessment))


async def run_for_subscriber(subscriber: Subscriber) -> list[Alert]:
    """Every area a subscriber watches, concurrently.

    Areas are independent, so one failing AOI must not cost the subscriber
    their other alerts.
    """
    if not subscriber.areas:
        return []

    results = await asyncio.gather(
        *(run_inline(subscriber, aoi) for aoi in subscriber.areas),
        return_exceptions=True,
    )

    alerts: list[Alert] = []
    for aoi, result in zip(subscriber.areas, results, strict=True):
        if isinstance(result, Exception):
            log.exception(
                "pipeline failed for area",
                extra={
                    "subscriber_id": subscriber.id,
                    "aoi_id": aoi.id,
                    "error": str(result),
                },
            )
        elif result is not None:
            alerts.append(result)
    return alerts


# --------------------------------------------------------------------------- #
# Queued path
# --------------------------------------------------------------------------- #


async def enqueue_scan(subscriber: Subscriber, aoi: AreaOfInterest) -> str:
    """Hand one area to the workers.

    This is where a run's trace begins. The id minted here is copied onto every
    subsequent envelope, so one `run_id` follows this area through all four stages
    and both worker pools.
    """
    run_id = tracing.new_run_id()
    job = JobEnvelope(
        stage=JobStage.SCOUT,
        subscriber_id=subscriber.id,
        aoi_id=aoi.id,
        payload={"aoi": aoi.model_dump(mode="json")},
        run_id=run_id,
    )
    # Bound here so the enqueue itself is attributable — otherwise the first log
    # line of a run ("job enqueued") is the one line you cannot correlate.
    with tracing.trace(run_id, stage="enqueue", aoi_id=aoi.id):
        await broker.enqueue(job)
    return job.id


async def enqueue_all(subscribers: list[Subscriber]) -> int:
    """Queue every area of every active subscriber. Returns the job count."""
    queued = 0
    for subscriber in subscribers:
        if not subscriber.active:
            continue
        for aoi in subscriber.areas:
            await enqueue_scan(subscriber, aoi)
            queued += 1
    log.info("scan cycle queued", extra={"jobs": queued})
    return queued


# --------------------------------------------------------------------------- #
# Verification (Fahis) — deferred, off the main line
# --------------------------------------------------------------------------- #


async def enqueue_verification(assessment: RiskAssessment) -> str | None:
    """Hand one assessment to Fahis.

    Called by the scheduler's sweep, never by Herald. The delay is the entire
    point: an assessment cannot be verified until its forecast window has closed
    and the world has had time to report on it.
    """
    if not settings.fahis_enabled:
        return None

    # A fresh trace, deliberately not the original scan's. Verification runs days
    # later and is a distinct unit of work — reusing the scan's id would make one
    # `run_id` span a week and mix two very different failure modes in one query.
    # `assessment_id` is the join between them, and it is already on both.
    run_id = tracing.new_run_id()
    job = JobEnvelope(
        stage=JobStage.FAHIS,
        aoi_id=assessment.aoi_id,
        payload={"assessment": assessment.model_dump(mode="json")},
        run_id=run_id,
    )
    with tracing.trace(run_id, stage="enqueue", aoi_id=assessment.aoi_id):
        await broker.enqueue(job)
    return job.id


async def enqueue_due_verifications(limit: int | None = None) -> int:
    """Queue every assessment now past its `verify_after`. Returns the count.

    Idempotent: `assessments_due_for_verification` anti-joins on `verifications`,
    so an interrupted sweep does not re-verify completed work.
    """
    if not settings.fahis_enabled:
        return 0

    from app.store import repository

    due = await repository.assessments_due_for_verification(
        limit or settings.fahis_batch_size
    )
    for assessment in due:
        await enqueue_verification(assessment)

    if due:
        log.info("verification sweep queued", extra={"jobs": len(due)})
    return len(due)
