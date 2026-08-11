"""PostgreSQL connection pool.

One lazily-built asyncpg pool per process, mirroring how `queue/redis_client.py`
handles Redis — same lifecycle, same "safe to call from anywhere" contract, so
there is one mental model for both stores.

Two things happen per connection rather than per query:

* **JSONB codec.** asyncpg hands back `str` for `jsonb` by default. Registering
  `json.loads`/`json.dumps` once means every caller gets dicts, and nobody has to
  remember which columns need decoding.
* **pgvector codec.** Registered so `VECTOR(n)` round-trips as a Python list
  instead of a string. Best-effort: a deployment without the extension still
  gets a working pool, it just cannot use the embedding tables.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Per-connection setup. Runs once when a pool slot is created."""
    await conn.set_type_codec(
        "jsonb",
        encoder=lambda v: json.dumps(v, separators=(",", ":")),
        decoder=json.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "json",
        encoder=lambda v: json.dumps(v, separators=(",", ":")),
        decoder=json.loads,
        schema="pg_catalog",
    )

    # pgvector's codec lives in the `pgvector` package and needs the extension
    # present in the database. Neither is fatal: the pool is still usable for
    # every non-vector query, which is all of the core pipeline.
    try:
        from pgvector.asyncpg import register_vector

        await register_vector(conn)
    except Exception as exc:  # noqa: BLE001 — import error or missing extension
        log.debug("pgvector codec not registered", extra={"error": str(exc)})


async def get_pool() -> asyncpg.Pool:
    """The shared pool, built on first use."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.postgres_dsn,
            min_size=settings.postgres_pool_min,
            max_size=settings.postgres_pool_max,
            command_timeout=settings.postgres_command_timeout,
            init=_init_connection,
        )
        log.info(
            "postgres pool ready",
            extra={
                "min_size": settings.postgres_pool_min,
                "max_size": settings.postgres_pool_max,
            },
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("postgres pool closed")


@asynccontextmanager
async def acquire() -> AsyncIterator[asyncpg.Connection]:
    """Borrow a connection. Use for multi-statement work and transactions."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


async def fetch(query: str, *args: Any) -> list[asyncpg.Record]:
    pool = await get_pool()
    return await pool.fetch(query, *args)


async def fetchrow(query: str, *args: Any) -> asyncpg.Record | None:
    pool = await get_pool()
    return await pool.fetchrow(query, *args)


async def fetchval(query: str, *args: Any) -> Any:
    pool = await get_pool()
    return await pool.fetchval(query, *args)


async def execute(query: str, *args: Any) -> str:
    pool = await get_pool()
    return await pool.execute(query, *args)


async def ping() -> bool:
    """Cheap liveness check for /health. Never raises."""
    try:
        return await fetchval("SELECT 1") == 1
    except Exception:
        return False


async def extensions() -> dict[str, bool]:
    """Which of the three required extensions are actually installed.

    Surfaced on /health because a database missing PostGIS looks fine until the
    first spatial query, and missing pgvector looks fine until the first
    retrieval — both a long way from startup.
    """
    try:
        rows = await fetch(
            "SELECT extname FROM pg_extension WHERE extname = ANY($1::text[])",
            ["postgis", "vector", "timescaledb"],
        )
    except Exception:
        return {"postgis": False, "vector": False, "timescaledb": False}

    present = {r["extname"] for r in rows}
    return {name: name in present for name in ("postgis", "vector", "timescaledb")}
