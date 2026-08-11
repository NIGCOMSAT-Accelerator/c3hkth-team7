"""Migration runner.

Plain numbered `.sql` files applied in filename order, each recorded in
`schema_migrations` so it runs once. No Alembic: the schema is small, the files
are readable as documentation, and a hand-written runner has no version-graph
failure modes to debug during a hackathon.

Two properties worth stating because they are what make auto-migrate on startup
safe:

* **Advisory lock.** Several API replicas and workers boot at once. All of them
  call `apply_pending`, and one wins the lock while the rest wait and then find
  nothing to do. Without this they would race on `CREATE TABLE`.
* **Per-file transaction.** A file either fully applies or fully rolls back, and
  its bookkeeping row commits with it — so a crash mid-migration cannot leave the
  ledger claiming work that did not happen.

`${EMBEDDING_DIMENSIONS}` in a file is substituted before execution. pgvector
encodes dimensionality in the column type, so it cannot be a runtime parameter.
"""

from __future__ import annotations

import hashlib
import pathlib

from app.config import settings
from app.db.session import acquire
from app.logging_config import get_logger

log = get_logger(__name__)

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent / "migrations"

#: Arbitrary but fixed. Any process migrating this database takes this lock.
_LOCK_ID = 0x5348454C  # "SHEL"

_LEDGER = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def _substitutions() -> dict[str, str]:
    """Values interpolated into `.sql` files before execution.

    Both of these are DDL, not query parameters — a column's vector width and a
    hypertable's chunk interval are part of the schema, so they cannot be bound
    at runtime. Rendering them here keeps them configurable in one place while
    staying honest that changing one needs a migration.
    """
    return {
        "EMBEDDING_DIMENSIONS": str(settings.embedding_dimensions),
        "TIMESCALE_CHUNK_INTERVAL_DAYS": str(settings.timescale_chunk_interval_days),
    }


def _render(sql: str) -> str:
    for key, value in _substitutions().items():
        sql = sql.replace(f"${{{key}}}", value)
    return sql


def discover() -> list[pathlib.Path]:
    """Migration files in apply order.

    Sorted by filename, which is why they are numbered. A file inserted with an
    earlier number than one already applied will simply never run — noted here
    because that is a real way to confuse yourself.
    """
    if not MIGRATIONS_DIR.is_dir():
        return []
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode()).hexdigest()[:16]


async def apply_pending() -> list[str]:
    """Apply every unapplied migration. Returns the filenames that ran."""
    files = discover()
    if not files:
        log.warning("no migration files found", extra={"dir": str(MIGRATIONS_DIR)})
        return []

    applied: list[str] = []

    async with acquire() as conn:
        await conn.execute(_LEDGER)

        # Serialise concurrent booters. Released when this connection is
        # returned to the pool.
        await conn.execute("SELECT pg_advisory_lock($1)", _LOCK_ID)
        try:
            done = {
                r["filename"]
                for r in await conn.fetch("SELECT filename FROM schema_migrations")
            }

            for path in files:
                if path.name in done:
                    continue

                sql = _render(path.read_text())
                log.info("applying migration", extra={"migration": path.name})

                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (filename, checksum) "
                        "VALUES ($1, $2)",
                        path.name,
                        _checksum(sql),
                    )
                applied.append(path.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_ID)

    if applied:
        log.info("migrations applied", extra={"count": len(applied), "files": applied})
    else:
        log.info("schema up to date")
    return applied


async def pending() -> list[str]:
    """Names of migrations not yet applied. For /health and for CI checks."""
    try:
        async with acquire() as conn:
            await conn.execute(_LEDGER)
            done = {
                r["filename"]
                for r in await conn.fetch("SELECT filename FROM schema_migrations")
            }
    except Exception:
        return [p.name for p in discover()]
    return [p.name for p in discover() if p.name not in done]
