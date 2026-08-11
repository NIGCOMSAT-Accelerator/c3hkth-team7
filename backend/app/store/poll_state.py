"""Scout's memory: which (area, source) pairs are fresh, stale, or failing.

Three jobs:

* **Decide what is due.** `is_due` compares the last success against the source's
  own publication cadence from `app/eo/sources.py`. Terrain has not moved; CHIRPS
  publishes daily. Polling both on the same 6-hour cycle is waste at one end and
  pointless at the other.
* **Back off on failure.** Consecutive failures push the next attempt out
  exponentially, so a dead upstream is retried less often rather than hammered
  every cycle across every AOI at once.
* **Record what happened.** The gap between `last_polled_at` and
  `last_success_at` is the health signal an operator needs — polling hourly and
  succeeding never is invisible if you only track one.

Everything here degrades: a failure to read poll state returns "due", which means
Scout polls. Losing the memory costs a redundant fetch, never a skipped one — the
safe direction for an early-warning service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.db import session as db
from app.eo.sources import Source
from app.logging_config import get_logger

log = get_logger(__name__)

#: Backoff ceiling. Beyond this a source is checked once a day rather than never —
#: an upstream that was down for a week may well be back, and a permanently
#: abandoned poll would silently remove a signal.
_MAX_BACKOFF_HOURS = 24


@dataclass(frozen=True)
class PollState:
    aoi_id: str
    source_key: str
    last_polled_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    last_error: str | None
    cache_key: str | None
    metadata: dict


def _backoff_hours(failures: int) -> float:
    """Exponential backoff, capped.

    1h, 2h, 4h, 8h, 16h, then 24h forever. Deliberately not "give up": every
    source here is one link in a failover chain, and a chain that quietly loses a
    link degrades the assessment rather than announcing itself.
    """
    if failures <= 0:
        return 0.0
    return float(min(2 ** (failures - 1), _MAX_BACKOFF_HOURS))


def is_due(state: PollState | None, source: Source, *, now: datetime | None = None) -> bool:
    """Whether this source should be polled for this area right now.

    Never polled → due. That is what makes a newly registered subscriber's first
    scan immediate rather than waiting for the next cadence window.
    """
    if state is None:
        return True

    moment = now or datetime.now(timezone.utc)

    # A failing source waits out its backoff, measured from the last *attempt* —
    # using last success would retry a long-dead upstream every cycle forever.
    if state.consecutive_failures > 0 and state.last_polled_at is not None:
        wait = timedelta(hours=_backoff_hours(state.consecutive_failures))
        if moment < state.last_polled_at + wait:
            return False
        return True

    if state.last_success_at is None:
        return True

    # The cadence floor: no point re-asking before the upstream could have
    # republished.
    floor = timedelta(hours=source.min_interval_hours)
    return moment >= state.last_success_at + floor


async def get(aoi_id: str, source_key: str) -> PollState | None:
    """One pair's state. None on any failure, which reads as "due"."""
    try:
        row = await db.fetchrow(
            "SELECT * FROM source_poll_state WHERE aoi_id = $1 AND source_key = $2",
            aoi_id,
            source_key,
        )
    except Exception as exc:
        log.debug("poll state read failed", extra={"error": str(exc)})
        return None

    if row is None:
        return None

    return PollState(
        aoi_id=row["aoi_id"],
        source_key=row["source_key"],
        last_polled_at=row["last_polled_at"],
        last_success_at=row["last_success_at"],
        consecutive_failures=int(row["consecutive_failures"] or 0),
        last_error=row["last_error"],
        cache_key=row["cache_key"],
        metadata=row["metadata"] or {},
    )


async def get_many(aoi_id: str) -> dict[str, PollState]:
    """Every source's state for one area, keyed by source.

    One query rather than one per source: Scout evaluates all 13 sources per AOI
    per cycle, and 13 round trips × every AOI is the kind of N+1 that only shows up
    under load.
    """
    try:
        rows = await db.fetch(
            "SELECT * FROM source_poll_state WHERE aoi_id = $1", aoi_id
        )
    except Exception as exc:
        log.debug("poll state batch read failed", extra={"error": str(exc)})
        return {}

    return {
        row["source_key"]: PollState(
            aoi_id=row["aoi_id"],
            source_key=row["source_key"],
            last_polled_at=row["last_polled_at"],
            last_success_at=row["last_success_at"],
            consecutive_failures=int(row["consecutive_failures"] or 0),
            last_error=row["last_error"],
            cache_key=row["cache_key"],
            metadata=row["metadata"] or {},
        )
        for row in rows
    }


async def record_success(
    aoi_id: str,
    source_key: str,
    *,
    cache_key: str | None = None,
    cache_bytes: int | None = None,
    metadata: dict | None = None,
) -> None:
    """Mark a successful poll, clearing any backoff. Never raises."""
    now = datetime.now(timezone.utc)
    try:
        await db.execute(
            """
            INSERT INTO source_poll_state (
                aoi_id, source_key, last_polled_at, last_success_at,
                consecutive_failures, last_error, cache_key, cache_bytes,
                metadata, updated_at
            ) VALUES ($1, $2, $3, $3, 0, NULL, $4, $5, $6::jsonb, $3)
            ON CONFLICT (aoi_id, source_key) DO UPDATE SET
                last_polled_at       = $3,
                last_success_at      = $3,
                consecutive_failures = 0,
                last_error           = NULL,
                -- Keep the previous cache pointer when this poll cached nothing,
                -- so a metadata-only refresh does not orphan a stored crop.
                cache_key            = COALESCE($4, source_poll_state.cache_key),
                cache_bytes          = COALESCE($5, source_poll_state.cache_bytes),
                metadata             = $6::jsonb,
                updated_at           = $3
            """,
            aoi_id,
            source_key,
            now,
            cache_key,
            cache_bytes,
            metadata or {},
        )
    except Exception as exc:
        # Losing the record means a redundant re-poll next cycle. Acceptable; the
        # scan itself already succeeded and its result is in hand.
        log.debug(
            "poll state write failed",
            extra={"aoi_id": aoi_id, "source": source_key, "error": str(exc)},
        )


async def record_failure(aoi_id: str, source_key: str, error: str) -> None:
    """Increment the failure count and push out the next attempt. Never raises."""
    now = datetime.now(timezone.utc)
    try:
        await db.execute(
            """
            INSERT INTO source_poll_state (
                aoi_id, source_key, last_polled_at, consecutive_failures,
                last_error, updated_at
            ) VALUES ($1, $2, $3, 1, $4, $3)
            ON CONFLICT (aoi_id, source_key) DO UPDATE SET
                last_polled_at       = $3,
                consecutive_failures = source_poll_state.consecutive_failures + 1,
                last_error           = $4,
                updated_at           = $3
            """,
            aoi_id,
            source_key,
            now,
            error[:500],
        )
    except Exception as exc:
        log.debug("poll failure write failed", extra={"error": str(exc)})


async def summary(*, limit: int = 20) -> dict:
    """Aggregate freshness and failure counts, for `/health`.

    `stalest` answers the question that matters during an incident: which area has
    gone longest without a successful poll.
    """
    try:
        totals = await db.fetchrow(
            """
            SELECT
                count(*)                                        AS tracked,
                count(*) FILTER (WHERE consecutive_failures > 0) AS failing,
                count(*) FILTER (WHERE cache_key IS NOT NULL)    AS cached,
                coalesce(sum(cache_bytes), 0)                    AS cache_bytes
            FROM source_poll_state
            """
        )
        stalest = await db.fetch(
            """
            SELECT aoi_id, source_key, last_success_at, consecutive_failures,
                   last_error
            FROM source_poll_state
            WHERE consecutive_failures > 0
            ORDER BY consecutive_failures DESC, updated_at DESC
            LIMIT $1
            """,
            limit,
        )
    except Exception:
        return {}

    if totals is None:
        return {}

    return {
        "tracked_pairs": int(totals["tracked"] or 0),
        "failing_pairs": int(totals["failing"] or 0),
        "cached_payloads": int(totals["cached"] or 0),
        "cache_bytes": int(totals["cache_bytes"] or 0),
        "failing": [
            {
                "aoi_id": r["aoi_id"],
                "source": r["source_key"],
                "last_success_at": (
                    r["last_success_at"].isoformat() if r["last_success_at"] else None
                ),
                "consecutive_failures": int(r["consecutive_failures"]),
                "last_error": (r["last_error"] or "")[:200],
            }
            for r in stalest
        ],
    }
