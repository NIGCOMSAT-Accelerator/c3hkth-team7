"""Cache on db1.

Postgres is the record; this is the fast path in front of it. Everything here is
disposable — a cold cache is a latency event, never a correctness one, and no
caller may depend on a hit.

**Three rules, enforced structurally rather than by convention:**

1. **Every write carries a TTL.** `ttl_seconds` is a required positional
   argument on `set_json`. This is the load-bearing rule: eviction policy is
   server-wide on both Redis and Dragonfly, so we cannot enable an evictor to
   bound this database without also putting the db0 job streams at risk of being
   dropped. Since eviction is therefore off, an untimed key would live forever.
   Making the argument required means you cannot forget it.

2. **Reads never raise.** A cache failure must degrade to a database read, not
   to a 500. Every getter swallows and logs.

3. **Keys are namespaced.** `CACHE_PREFIX` fronts every key so a shared instance
   stays inspectable and one `SCAN` can find everything we own.
"""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.logging_config import get_logger
from app.queue.redis_client import get_cache

log = get_logger(__name__)


def key(*parts: str) -> str:
    """Build a namespaced key. `key("assessment", aoi_id)`."""
    return ":".join((settings.cache_prefix, *parts))


#: TTL for derived terrain rasters (HAND, TWI, slope), in seconds — 30 days.
#:
#: Terrain is the one input that genuinely does not change: the flow-accumulation
#: pass behind `eo/terrain.py` is the most expensive computation in the pipeline and
#: its answer is identical next month. A long TTL is what makes step 3 of the ML
#: roadmap effectively free after the first cycle.
#:
#: Not infinite, because eviction is off (see rule 1 above) so an untimed key would
#: be permanent, and because a DEM *product* revision should eventually be picked up.
TERRAIN_TTL_SECONDS = 30 * 24 * 3600

#: TTL for rainfall climatology windows used by SPI — 7 days.
#:
#: The historical distribution moves only as new observations accumulate, and one
#: week of drift cannot meaningfully shift a 20-year gamma fit. Short enough that a
#: corrected upstream record propagates within a cycle or two.
CLIMATOLOGY_TTL_SECONDS = 7 * 24 * 3600


def terrain_key(bbox: object) -> str:
    """Cache key for a bbox's derived terrain profile.

    Rounded to 3 decimal places (~100 m at the equator) so that trivially different
    bboxes describing the same field share an entry. Finer precision would make the
    cache useless — every request would miss — and coarser would blend genuinely
    different terrain.
    """
    return key(
        "terrain",
        f"{getattr(bbox, 'west', 0):.3f},{getattr(bbox, 'south', 0):.3f},"
        f"{getattr(bbox, 'east', 0):.3f},{getattr(bbox, 'north', 0):.3f}",
    )


def climatology_key(bbox: object, source: str, window_days: int) -> str:
    """Cache key for a rainfall climatology series backing an SPI computation."""
    return key(
        "climatology",
        source,
        str(window_days),
        f"{getattr(bbox, 'west', 0):.2f},{getattr(bbox, 'south', 0):.2f},"
        f"{getattr(bbox, 'east', 0):.2f},{getattr(bbox, 'north', 0):.2f}",
    )


# --------------------------------------------------------------------------- #
# JSON values — the common case, since everything cached here is a Pydantic
# model already serialised to JSON.
# --------------------------------------------------------------------------- #


async def get_json(cache_key: str) -> Any | None:
    """Fetch and decode. Returns None on a miss, a decode error, or an outage.

    A corrupt entry is treated as a miss and deleted: the caller then reads
    through to Postgres, which is always right, and the bad key stops being
    re-read on every request.
    """
    import json

    try:
        raw = await get_cache().get(cache_key)
    except Exception as exc:
        log.debug("cache read failed", extra={"key": cache_key, "error": str(exc)})
        return None

    if raw is None:
        return None

    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        log.warning("discarding undecodable cache entry", extra={"key": cache_key})
        await delete(cache_key)
        return None


async def set_json(cache_key: str, value: Any, ttl_seconds: int) -> None:
    """Store a JSON-serialisable value under a mandatory TTL.

    `ttl_seconds` is required and must be positive — see rule 1 in the module
    docstring. Pass `settings.cache_default_ttl_seconds` if you have no better
    figure; that is a deliberate choice, not a way to opt out.
    """
    import json

    if ttl_seconds <= 0:
        # Programmer error, not a runtime condition. Raising is right: a
        # persistent key in this database is a slow memory leak on an instance
        # that also holds the job queue.
        raise ValueError(
            f"cache TTL must be positive, got {ttl_seconds} for {cache_key!r}. "
            "Eviction is disabled server-wide to protect the db0 job streams, "
            "so an untimed cache key would never be reclaimed."
        )

    try:
        await get_cache().set(
            cache_key,
            json.dumps(value, separators=(",", ":"), default=str),
            ex=ttl_seconds,
        )
    except Exception as exc:
        # A failed cache write is not worth failing the request over — the value
        # is already committed to Postgres by this point.
        log.debug("cache write failed", extra={"key": cache_key, "error": str(exc)})


async def set_text(cache_key: str, value: str, ttl_seconds: int) -> None:
    """Store a raw string under a mandatory TTL. Same contract as `set_json`."""
    if ttl_seconds <= 0:
        raise ValueError(f"cache TTL must be positive, got {ttl_seconds}")
    try:
        await get_cache().set(cache_key, value, ex=ttl_seconds)
    except Exception as exc:
        log.debug("cache write failed", extra={"key": cache_key, "error": str(exc)})


async def get_text(cache_key: str) -> str | None:
    try:
        return await get_cache().get(cache_key)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Invalidation
# --------------------------------------------------------------------------- #


async def delete(*keys: str) -> None:
    """Drop keys. Called on write-through so a stale entry can't outlive an edit."""
    if not keys:
        return
    try:
        await get_cache().delete(*keys)
    except Exception as exc:
        log.debug("cache delete failed", extra={"error": str(exc)})


async def delete_prefix(*parts: str) -> int:
    """Delete every key under a namespace. Returns how many were removed.

    Uses `SCAN`, never `KEYS` — this runs against the same instance as the job
    streams, and `KEYS` on a large keyspace blocks the server. Deletes in batches
    so one call can't build an unbounded argument list either.
    """
    pattern = key(*parts) + "*"
    removed = 0
    batch: list[str] = []

    try:
        cache = get_cache()
        async for found in cache.scan_iter(match=pattern, count=500):
            batch.append(found)
            if len(batch) >= 500:
                removed += await cache.delete(*batch)
                batch.clear()
        if batch:
            removed += await cache.delete(*batch)
    except Exception as exc:
        log.debug("cache prefix delete failed", extra={"pattern": pattern, "error": str(exc)})
        return removed

    return removed


# --------------------------------------------------------------------------- #
# Counters — rate limiting and metrics
# --------------------------------------------------------------------------- #


async def incr(cache_key: str, ttl_seconds: int) -> int:
    """Increment a counter, setting the TTL on first write.

    The TTL is applied only when the counter is created, so a fixed window
    starts at the first hit rather than sliding forward on every one. That is
    what makes this usable for rate limiting.

    Returns 0 when the cache is unreachable — i.e. **fails open**. For rate
    limiting that is the right default here: a cache outage should not lock
    every subscriber out of a warning service. A limiter that must fail closed
    needs its own explicit handling at the call site.
    """
    try:
        cache = get_cache()
        pipe = cache.pipeline()
        pipe.incr(cache_key)
        pipe.expire(cache_key, ttl_seconds, nx=True)
        count, _ = await pipe.execute()
        return int(count)
    except Exception as exc:
        log.debug("cache incr failed", extra={"key": cache_key, "error": str(exc)})
        return 0


async def stats() -> dict[str, int]:
    """Key count for /health. Cheap; `DBSIZE` is O(1) on both servers."""
    try:
        return {"keys": int(await get_cache().dbsize())}
    except Exception:
        return {}
