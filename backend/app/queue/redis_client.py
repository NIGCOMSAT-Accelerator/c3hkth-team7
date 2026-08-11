"""Redis / Dragonfly connection pools.

**Two pools, two logical databases, on one instance.**

    db0  (`REDIS_URL`)  job streams, dead-letter, dedupe keys. Durable.
    db1  (`CACHE_URL`)  cache. Every key disposable, every write TTL'd.

Why split at all, given it is one server? Blast radius and observability. A
`FLUSHDB` while debugging the cache cannot take the job queue with it, `DBSIZE`
means something on each, and the two have genuinely different durability
requirements — losing a cache entry costs a round trip, losing a stream entry
means a satellite scan silently never ran.

**What the split does NOT buy, and this matters:** eviction policy is a
*server-wide* setting on both Redis and Dragonfly, not per-database. Enabling
`allkeys-lru` so db1 can shed pressure would also let the server drop db0 stream
entries. So the deployment runs with eviction **off**, and `app/store/cache.py`
requires an explicit TTL on every write. Bounded memory is the caller's
responsibility, enforced in the function signature, rather than something we
hope the evictor gets right.

Works unchanged against Dragonfly — it speaks the Redis wire protocol. One
operational catch: Dragonfly must be started with `--dbnum=2` or higher, or
`SELECT 1` fails and every cache call errors. The compose file sets it.
"""

from __future__ import annotations

import redis.asyncio as aioredis

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

_client: aioredis.Redis | None = None
_cache_client: aioredis.Redis | None = None


def _build(url: str) -> aioredis.Redis:
    return aioredis.from_url(
        url,
        encoding="utf-8",
        decode_responses=True,
        health_check_interval=30,
    )


def get_redis() -> aioredis.Redis:
    """db0 — streams and durable keys. Safe to call from anywhere."""
    global _client
    if _client is None:
        _client = _build(settings.redis_url)
    return _client


def get_cache() -> aioredis.Redis:
    """db1 — cache only.

    Prefer `app.store.cache`, which enforces the TTL contract and key prefixing.
    Reach for this directly only when you need a Redis primitive the cache module
    does not wrap.
    """
    global _cache_client
    if _cache_client is None:
        _cache_client = _build(settings.cache_url)
    return _cache_client


async def close_redis() -> None:
    """Close both pools. Called from every process's shutdown path."""
    global _client, _cache_client
    if _client is not None:
        await _client.aclose()
        _client = None
    if _cache_client is not None:
        await _cache_client.aclose()
        _cache_client = None


async def ping() -> bool:
    """db0 liveness. This is the one /health gates `status` on, because the
    queue is what the pipeline cannot run without."""
    try:
        return bool(await get_redis().ping())
    except Exception:
        return False


async def ping_cache() -> bool:
    """db1 liveness. Reported separately: a dead cache degrades latency, a dead
    db0 stops the pipeline. They are not the same incident."""
    try:
        return bool(await get_cache().ping())
    except Exception:
        return False


async def server_info() -> dict[str, str]:
    """Server identity and eviction policy, for /health.

    Surfaces two things worth seeing on a dashboard rather than discovering
    during an incident: whether we are talking to Redis or Dragonfly, and whether
    eviction is on — which, per the module docstring, would put the job streams
    at risk.
    """
    try:
        info = await get_redis().info("server")
        policy = await get_redis().config_get("maxmemory-policy")
    except Exception:
        return {}

    return {
        # Dragonfly reports itself under `dragonfly_version`; Redis does not.
        "server": "dragonfly" if info.get("dragonfly_version") else "redis",
        "version": str(
            info.get("dragonfly_version") or info.get("redis_version") or "unknown"
        ),
        "maxmemory_policy": str(policy.get("maxmemory-policy", "unknown")),
    }
