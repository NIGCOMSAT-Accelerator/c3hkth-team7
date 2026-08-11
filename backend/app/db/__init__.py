"""PostgreSQL access.

`session.py` owns the pool, `migrations.py` applies the schema. Callers should
import `app.db.session` and use `acquire()` / `fetch()` rather than touching
asyncpg directly.
"""

from app.db.session import acquire, close_pool, execute, fetch, fetchrow, get_pool, ping

__all__ = [
    "acquire",
    "close_pool",
    "execute",
    "fetch",
    "fetchrow",
    "get_pool",
    "ping",
]
