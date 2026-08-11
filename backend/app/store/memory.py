"""Shared agent memory.

The store any agent *may* read and write. `agent_memory` was provisioned in
migration 003 with HNSW and GiST indexes and then — until this module — nothing
touched it, which made the table an orphan and three docstrings wrong.

**Current reality, stated plainly so the module isn't read as busier than it is:**
Fahis writes (`_file_memory` files each CONFIRMED as an outcome and each REFUTED as
a correction). **Nothing reads yet** — `recall` and `corrections_for` have no
callers.

That asymmetry is deliberate for now. Writing is safe and the rows are the only
ground truth an untrained deployment will ever accumulate, so collecting them early
costs nothing. Reading is the part that needs a decision: a recalled observation
*reads like evidence*, and routing it into severity or an advisory would let a few
news-coverage accidents retune the risk model. The first reader should be an
operator console, not the Oracle.

**What it is for, and what it deliberately is not.**

The pipeline hands typed results forward: `AreaOfInterest → ScoutResult →
AnalystResult → RiskAssessment → Alert`. That is *state*, it is complete, and it
must stay the only channel for it — a stage reading its input from a shared blob
instead of its typed payload would break the property that makes each stage
independently testable and retryable.

This is for the orthogonal thing: **observations that outlive a single run.**

    "Optical has been blinded here 5 cycles running"     (Scout, next cycle)
    "This AOI's NDVI baseline is unusually low"          (Analyst, next season)
    "We warned WARNING here and it was confirmed"        (Fahis, ever after)
    "The subscriber said the flood never arrived"        (operator)

None of that fits a stage payload, because it is not produced by the previous
stage. Without somewhere to put it, every run starts blind — which is why the only
cross-run memory in the system today is the Herald's dedupe key.

**Three rules:**

1. **Never raises.** Memory is an enhancement. A failed write must not fail a
   satellite scan; a failed recall returns nothing and the agent proceeds as it
   does today.
2. **Never a source of figures.** Recalled text may inform an agent's *judgement*
   and may be shown to an operator. It must never become a number in a
   `RiskAssessment` or an advisory — that is the grounding rule, and memory is a
   plausible-looking way to violate it, since a recalled observation reads like
   evidence.
3. **Embeddings are optional.** With no embedding provider, `recall` falls back to
   recency plus spatial overlap, which is still useful.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.db import session as db
from app.llm import embeddings
from app.logging_config import get_logger
from app.models.schemas import BBox

log = get_logger(__name__)


#: Coarse buckets, matching the `kind` column. Corrections are the valuable ones —
#: an operator or Fahis recording that an alert was wrong is the only ground truth
#: this system ever gets.
KIND_OBSERVATION = "observation"
KIND_OUTCOME = "outcome"
KIND_CORRECTION = "correction"
KIND_REFERENCE = "reference"


@dataclass(frozen=True)
class Memory:
    """One recalled entry."""

    id: int
    agent: str
    kind: str
    content: str
    aoi_id: str | None
    metadata: dict
    created_at: datetime
    #: Cosine distance when recalled semantically, else None.
    distance: float | None = None


async def remember(
    *,
    agent: str,
    kind: str,
    content: str,
    aoi_id: str | None = None,
    bbox: BBox | None = None,
    metadata: dict | None = None,
    ttl_days: int | None = None,
) -> bool:
    """Write one memory. Returns False on any failure, never raises.

    `bbox` is stored as geography so a later recall can be scoped spatially — "what
    do we know about anywhere near here", which is the useful question when a new
    AOI overlaps one we have seen before.

    `ttl_days` sets `expires_at` for memory that stops being true (a seasonal
    observation). None means permanent; corrections should always be permanent.
    """
    vector = None
    if embeddings.available():
        vector = await embeddings.embed(content)

    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=ttl_days) if ttl_days else None
    )

    try:
        await db.execute(
            """
            INSERT INTO agent_memory
                (agent, kind, aoi_id, content, embedding, geom, metadata, expires_at)
            VALUES ($1, $2, $3, $4, $5,
                    CASE WHEN $6::float8 IS NULL THEN NULL
                         ELSE ST_MakeEnvelope($6, $7, $8, $9, 4326)::geography END,
                    $10::jsonb, $11)
            """,
            agent,
            kind,
            aoi_id,
            content,
            vector,
            bbox.west if bbox else None,
            bbox.south if bbox else None,
            bbox.east if bbox else None,
            bbox.north if bbox else None,
            metadata or {},
            expires_at,
        )
    except Exception as exc:
        # Rule 1. A satellite scan must not fail because a note could not be filed.
        log.warning(
            "memory write failed",
            extra={"agent": agent, "kind": kind, "error": str(exc)},
        )
        return False

    log.info("memory written", extra={"agent": agent, "kind": kind, "aoi_id": aoi_id})
    return True


async def recall(
    query: str | None = None,
    *,
    aoi_id: str | None = None,
    bbox: BBox | None = None,
    agent: str | None = None,
    kind: str | None = None,
    limit: int = 5,
) -> list[Memory]:
    """Retrieve memories, semantically when possible. Never raises.

    Filter precedence, most to least specific: `aoi_id` (this exact area), then
    `bbox` (anywhere overlapping), then unscoped. Passing neither searches globally,
    which is rarely what an agent wants — a memory about Kebbi does not inform a
    scan of Borno.

    Expired entries are excluded here rather than deleted by a job: one fewer
    moving part, and `expires_at` stays auditable.
    """
    conditions = ["(expires_at IS NULL OR expires_at > now())"]
    args: list = []

    def _next() -> str:
        return f"${len(args) + 1}"

    if aoi_id:
        args.append(aoi_id)
        conditions.append(f"aoi_id = {_next()}")
    elif bbox:
        # Spatial overlap rather than exact AOI: a new area near a known one should
        # inherit what we learned there.
        args.extend([bbox.west, bbox.south, bbox.east, bbox.north])
        conditions.append(
            f"ST_Intersects(geom, ST_MakeEnvelope(${len(args) - 3}, ${len(args) - 2}, "
            f"${len(args) - 1}, ${len(args)}, 4326)::geography)"
        )

    if agent:
        args.append(agent)
        conditions.append(f"agent = {_next()}")
    if kind:
        args.append(kind)
        conditions.append(f"kind = {_next()}")

    where = " AND ".join(conditions)

    vector = None
    if query and embeddings.available():
        vector = await embeddings.embed(query)

    try:
        if vector is not None:
            args.append(vector)
            vector_param = _next()
            args.append(limit)
            rows = await db.fetch(
                f"""
                SELECT id, agent, kind, content, aoi_id, metadata, created_at,
                       (embedding <=> {vector_param}) AS distance
                FROM agent_memory
                WHERE {where} AND embedding IS NOT NULL
                ORDER BY embedding <=> {vector_param}
                LIMIT {_next()}
                """,
                *args,
            )
        else:
            # No query or no embedding provider — recency, still scoped.
            args.append(limit)
            rows = await db.fetch(
                f"""
                SELECT id, agent, kind, content, aoi_id, metadata, created_at,
                       NULL::float8 AS distance
                FROM agent_memory
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT {_next()}
                """,
                *args,
            )
    except Exception as exc:
        log.warning("memory recall failed", extra={"error": str(exc)})
        return []

    return [
        Memory(
            id=r["id"],
            agent=r["agent"],
            kind=r["kind"],
            content=r["content"],
            aoi_id=r["aoi_id"],
            metadata=r["metadata"] or {},
            created_at=r["created_at"],
            distance=r["distance"],
        )
        for r in rows
    ]


async def corrections_for(aoi_id: str, *, limit: int = 10) -> list[Memory]:
    """Recorded cases where we were wrong about this area.

    The highest-value read in this module. Weights are absent by default and
    inference falls back to thresholds, so these rows are the only signal that a
    given area is systematically mis-assessed.

    **No callers yet.** Fahis *writes* corrections (`_file_memory` files a REFUTED
    verdict as one), so the rows accumulate — but nothing reads them back. Reading
    them means deciding who acts on the signal, and the honest answer today is an
    operator, not the Oracle: feeding corrections into severity automatically would
    let a handful of news-coverage accidents retune the risk model.
    """
    return await recall(aoi_id=aoi_id, kind=KIND_CORRECTION, limit=limit)


async def stats() -> dict:
    """Counts by kind and agent, for /health and the operator console."""
    try:
        rows = await db.fetch(
            """
            SELECT agent, kind, count(*) AS n
            FROM agent_memory
            WHERE expires_at IS NULL OR expires_at > now()
            GROUP BY agent, kind
            ORDER BY n DESC
            """
        )
    except Exception:
        return {}

    return {
        "total": sum(int(r["n"]) for r in rows),
        "by_agent": {
            f"{r['agent']}.{r['kind']}": int(r["n"]) for r in rows
        },
    }
