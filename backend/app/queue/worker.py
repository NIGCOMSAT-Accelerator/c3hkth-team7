"""Queue worker.

Each stage runs as its own consumer task against its own stream, and hands the
next stage a job rather than calling it. That decoupling is what lets the
inference stage — the slow one, minutes of COG reads and forward passes — back
up without stalling discovery, and lets a crashed worker's in-flight job be
reclaimed instead of lost.

Run every stage in one process:

    python -m app.queue.worker

Or scale a single hot stage independently:

    python -m app.queue.worker --stages analyst --concurrency 4
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import socket

from app import tracing
from app.agents.pipeline import analyst, fahis, herald, oracle, scout
from app.config import settings
from app.db import session as db
from app.logging_config import configure_logging, get_logger
from app.models.enums import TRAINABLE_VERDICTS, JobStage, JobStatus
from app.models.schemas import (
    AnalystResult,
    AreaOfInterest,
    JobEnvelope,
    RiskAssessment,
    ScoutResult,
)
from app.queue import broker
from app.queue.redis_client import close_redis
from app.store import repository
from app.webhooks import publisher as webhook_publisher
from app.webhooks import schemas as webhook_schemas

log = get_logger(__name__)

_shutdown = asyncio.Event()


# --------------------------------------------------------------------------- #
# Stage handlers
# --------------------------------------------------------------------------- #


def _next(job: JobEnvelope, stage: JobStage, payload: dict) -> JobEnvelope:
    """Build the successor envelope, carrying the trace forward.

    A helper rather than three hand-written constructors: `run_id` has to be copied
    at every hand-off, and a single omission silently truncates the trace at that
    stage — the failure is invisible until someone tries to follow a run and finds
    it stops after Analyst. Funnelling every hand-off through one place makes that
    impossible to get wrong.
    """
    return JobEnvelope(
        stage=stage,
        subscriber_id=job.subscriber_id,
        aoi_id=job.aoi_id,
        run_id=job.run_id,
        payload=payload,
    )


async def _handle_scout(job: JobEnvelope) -> JobEnvelope | None:
    aoi = AreaOfInterest.model_validate(job.payload["aoi"])
    result = await scout.execute(aoi)
    return _next(
        job,
        JobStage.ANALYST,
        {"aoi": job.payload["aoi"], "scout": result.model_dump(mode="json")},
    )


async def _handle_analyst(job: JobEnvelope) -> JobEnvelope | None:
    scout_result = ScoutResult.model_validate(job.payload["scout"])
    result = await analyst.execute(scout_result)
    return _next(
        job,
        JobStage.ORACLE,
        {"aoi": job.payload["aoi"], "analysis": result.model_dump(mode="json")},
    )


async def _handle_oracle(job: JobEnvelope) -> JobEnvelope | None:
    aoi = AreaOfInterest.model_validate(job.payload["aoi"])
    analysis = AnalystResult.model_validate(job.payload["analysis"])
    assessment = await oracle.execute((aoi, analysis))
    return _next(
        job,
        JobStage.HERALD,
        {"assessment": assessment.model_dump(mode="json")},
    )


async def _handle_herald(job: JobEnvelope) -> JobEnvelope | None:
    assessment = RiskAssessment.model_validate(job.payload["assessment"])

    if not job.subscriber_id:
        # An assessment with no recipient is still worth storing — the
        # dashboard reads it — but there is nobody to notify.
        await repository.save_assessment(assessment)
        log.warning("herald job has no subscriber", extra={"job_id": job.id})
        return None

    subscriber = await repository.get_subscriber(job.subscriber_id)
    if subscriber is None:
        # Unsubscribed between queueing and delivery. Not an error.
        await repository.save_assessment(assessment)
        log.info(
            "subscriber no longer exists; dropping",
            extra={"subscriber_id": job.subscriber_id},
        )
        return None

    await herald.execute((subscriber, assessment))
    return None  # end of pipeline


async def _handle_fahis(job: JobEnvelope) -> JobEnvelope | None:
    """Verify one past assessment against outside reporting.

    End of the line — returns None always. Fahis enqueues nothing by design: any
    downstream stage could carry web-sourced text back toward an advisory, which
    is the failure the grounding rule exists to prevent.
    """
    assessment = RiskAssessment.model_validate(job.payload["assessment"])
    verification = await fahis.execute(assessment)

    # Attach the alert this verdict judges, so a partner can correlate.
    #
    # Fahis is handed only the assessment and cannot know the alert, so `alert_id` was written NULL
    # on every row — a column dead since creation, and a webhook contract documenting a correlation
    # key the payload never carried. Resolved HERE rather than inside the agent, which must keep
    # reaching nothing but the search backend.
    #
    # None stays None when the assessment was never dispatched (below the severity floor, or
    # suppressed as a duplicate). Those are still verified — accuracy is measured on what we
    # concluded, not on what we chose to send — and they correlate on `assessment_id`.
    if verification.alert_id is None:
        verification.alert_id = await repository.alert_id_for_assessment(assessment.id)

    await repository.save_verification(verification)

    # Publish the verdict to subscribed partners.
    #
    # ## Why HERE and not inside `FahisAgent`
    #
    # `FahisAgent.run` returns a `Verification` and touches nothing else — no persistence, no
    # fan-out. Keeping it that way is what makes `next_stage is None` a meaningful boundary rather
    # than a formality: the agent cannot reach anything, so it cannot leak web-sourced text toward
    # an advisory even by accident.
    #
    # ## Why this does not breach that boundary
    #
    # This is a READ of a record already written, published on a channel that cannot re-enter the
    # pipeline. `VerificationEventData` carries no severity, no score and no advisory — so a
    # verdict never travels alongside a revised assessment, which is the shape that would put
    # unattributed prose one hop from a number a farmer acts on.
    #
    # Failure is swallowed by `publish`, and deliberately: a partner's endpoint being unreachable
    # must never cost us the ground truth we just recorded.
    await webhook_publisher.publish(
        "shelter.verification",
        webhook_schemas.VerificationEventData(
            verification_id=verification.id,
            alert_id=verification.alert_id,
            assessment_id=verification.assessment_id,
            aoi_id=verification.aoi_id,
            claimed_hazard=verification.claimed_hazard.value,
            claimed_severity=verification.claimed_severity.value,
            assessed_at=verification.assessed_at,
            verdict=verification.verdict.value,
            confidence=verification.confidence,
            rationale=verification.rationale,
            sources=[
                {
                    "url": source.url,
                    "title": source.title,
                    "tier": source.tier.value
                    if hasattr(source.tier, "value")
                    else str(source.tier),
                    "published": source.published,
                }
                for source in verification.sources
            ],
            trainable=verification.verdict in TRAINABLE_VERDICTS,
            verified_at=verification.verified_at,
        ).model_dump(mode="json"),
        # Routed on the CLAIMED severity, so a partner filtering `min_severity: watch` receives
        # verdicts for exactly the alerts they were sent. Filtering on the verdict instead would
        # mean an integrator who only wants warnings still gets every info-level verdict.
        severity=verification.claimed_severity.value,
        aoi_id=verification.aoi_id,
    )
    return None


_HANDLERS = {
    JobStage.SCOUT: _handle_scout,
    JobStage.ANALYST: _handle_analyst,
    JobStage.ORACLE: _handle_oracle,
    JobStage.HERALD: _handle_herald,
    JobStage.FAHIS: _handle_fahis,
}


# --------------------------------------------------------------------------- #
# Consumer loop
# --------------------------------------------------------------------------- #


async def run_stage(stage: JobStage, consumer_name: str) -> None:
    """Consume one stage's stream until shutdown."""
    handler = _HANDLERS[stage]
    log.info("worker started", extra={"stage": stage.value, "consumer": consumer_name})

    # Pick up anything a previous worker died holding.
    await broker.reclaim_stalled(stage, consumer_name)

    async for stream_id, job in broker.consume(stage, consumer_name):
        if _shutdown.is_set():
            break

        job.status = JobStatus.RUNNING

        # Bind the trace for the whole hand-off, not just the handler: the enqueue
        # of the successor and the failure path are the two moments most worth
        # correlating, and both sit outside `handler(job)`.
        #
        # asyncio copies the context per task, so concurrent consumers in this same
        # process each get their own value — which is why `--concurrency 4` does not
        # interleave trace ids.
        with tracing.trace(job.run_id, stage=stage.value, aoi_id=job.aoi_id):
            try:
                next_job = await handler(job)
            except Exception as exc:
                await broker.fail(stage, stream_id, job, str(exc))
                continue

            if next_job is not None:
                await broker.enqueue(next_job)
            await broker.ack(stage, stream_id)


async def run(stages: list[JobStage], concurrency: int = 1) -> None:
    """Run consumers for the given stages until interrupted."""
    # Open the pool before any consumer starts. Workers never migrate — the API
    # container owns that, and a worker racing it on CREATE TABLE is exactly what
    # the advisory lock in migrations.py exists to prevent.
    if not await db.ping():
        log.error("postgres unreachable; stage handlers that persist will fail")

    hostname = socket.gethostname()
    tasks = [
        asyncio.create_task(
            run_stage(stage, f"{hostname}-{os.getpid()}-{stage.value}-{index}")
        )
        for stage in stages
        for index in range(concurrency)
    ]

    await _shutdown.wait()

    log.info("shutting down workers")
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await close_redis()
    await db.close_pool()


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _shutdown.set)


def main() -> None:
    parser = argparse.ArgumentParser(description="SHELTER pipeline worker")
    parser.add_argument(
        "--stages",
        default="all",
        help="Comma-separated stages to consume, or 'all' (default).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Consumers per stage. Raise for analyst, which is I/O bound.",
    )
    args = parser.parse_args()

    if args.stages == "all":
        stages = list(JobStage)
    else:
        stages = [JobStage(s.strip()) for s in args.stages.split(",") if s.strip()]

    configure_logging()
    log.info(
        "worker booting",
        extra={
            "stages": [s.value for s in stages],
            "concurrency": args.concurrency,
            "redis": settings.redis_url,
        },
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _install_signal_handlers(loop)
    try:
        loop.run_until_complete(run(stages, args.concurrency))
    finally:
        loop.close()


if __name__ == "__main__":
    main()
