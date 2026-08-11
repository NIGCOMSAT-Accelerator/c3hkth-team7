"""Redis Streams job broker.

Streams (not plain lists) because we need consumer groups: a crashed worker's
in-flight job stays pending and can be reclaimed rather than vanishing. That
matters when one job represents a several-minute satellite read.

One stream per pipeline stage — `shelter:scout`, `shelter:analyst`, and so on —
so stages scale independently and a slow inference stage never blocks discovery.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from redis.exceptions import ResponseError

from app.config import settings
from app.logging_config import get_logger
from app.models.enums import JobStage, JobStatus
from app.models.schemas import JobEnvelope
from app.queue.redis_client import get_redis

log = get_logger(__name__)

DEAD_LETTER_STREAM = f"{settings.queue_stream_prefix}:dead"


def stream_key(stage: JobStage) -> str:
    return f"{settings.queue_stream_prefix}:{stage.value}"


async def ensure_group(stage: JobStage) -> None:
    """Create the consumer group, tolerating the already-exists race."""
    redis = get_redis()
    try:
        await redis.xgroup_create(
            name=stream_key(stage),
            groupname=settings.queue_consumer_group,
            id="0",
            mkstream=True,
        )
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def enqueue(job: JobEnvelope) -> str:
    """Publish a job to its stage's stream."""
    redis = get_redis()
    await ensure_group(job.stage)
    message_id = await redis.xadd(
        stream_key(job.stage),
        {"job": job.model_dump_json()},
        maxlen=10_000,
        approximate=True,
    )
    log.info(
        "job enqueued",
        extra={"job_id": job.id, "stage": job.stage.value, "stream_id": message_id},
    )
    return message_id


async def consume(
    stage: JobStage,
    consumer_name: str,
    *,
    count: int = 1,
) -> AsyncIterator[tuple[str, JobEnvelope]]:
    """Yield `(stream_id, job)` pairs for one stage, forever.

    The caller must `ack()` on success and `fail()` on error — nothing is
    auto-acknowledged, so a worker that dies mid-job leaves it reclaimable.
    """
    redis = get_redis()
    await ensure_group(stage)
    key = stream_key(stage)

    while True:
        try:
            response = await redis.xreadgroup(
                groupname=settings.queue_consumer_group,
                consumername=consumer_name,
                streams={key: ">"},
                count=count,
                block=settings.queue_block_ms,
            )
        except Exception:
            log.exception("stream read failed", extra={"stage": stage.value})
            continue

        if not response:
            continue  # block timeout — normal idle

        for _stream, messages in response:
            for stream_id, fields in messages:
                raw = fields.get("job")
                if not raw:
                    await ack(stage, stream_id)
                    continue
                try:
                    job = JobEnvelope.model_validate_json(raw)
                except Exception:
                    log.exception(
                        "undecodable job, dead-lettering",
                        extra={"stream_id": stream_id, "stage": stage.value},
                    )
                    await _dead_letter(raw, "decode_error")
                    await ack(stage, stream_id)
                    continue
                yield stream_id, job


async def ack(stage: JobStage, stream_id: str) -> None:
    await get_redis().xack(
        stream_key(stage), settings.queue_consumer_group, stream_id
    )


async def fail(stage: JobStage, stream_id: str, job: JobEnvelope, error: str) -> None:
    """Retry with a bounded attempt count, then dead-letter.

    We ack the original either way: the retry is a fresh stream entry, so
    leaving the old one pending would double-count it.
    """
    job.attempts += 1
    job.error = error

    if job.attempts >= settings.queue_max_retries:
        job.status = JobStatus.FAILED
        log.error(
            "job exhausted retries",
            extra={"job_id": job.id, "stage": stage.value, "attempts": job.attempts},
        )
        await _dead_letter(job.model_dump_json(), error)
    else:
        job.status = JobStatus.RETRYING
        log.warning(
            "job retrying",
            extra={"job_id": job.id, "stage": stage.value, "attempts": job.attempts},
        )
        await enqueue(job)

    await ack(stage, stream_id)


async def _dead_letter(raw_job: str, error: str) -> None:
    await get_redis().xadd(
        DEAD_LETTER_STREAM,
        {"job": raw_job, "error": error},
        maxlen=5_000,
        approximate=True,
    )


async def reclaim_stalled(stage: JobStage, consumer_name: str, min_idle_ms: int = 300_000) -> int:
    """Take over jobs abandoned by a dead worker. Returns how many were claimed."""
    redis = get_redis()
    await ensure_group(stage)
    try:
        _cursor, claimed, _deleted = await redis.xautoclaim(
            name=stream_key(stage),
            groupname=settings.queue_consumer_group,
            consumername=consumer_name,
            min_idle_time=min_idle_ms,
            count=50,
        )
    except ResponseError:
        return 0
    if claimed:
        log.info(
            "reclaimed stalled jobs",
            extra={"stage": stage.value, "count": len(claimed)},
        )
    return len(claimed)


async def depth(stage: JobStage) -> int:
    try:
        return int(await get_redis().xlen(stream_key(stage)))
    except Exception:
        return 0
