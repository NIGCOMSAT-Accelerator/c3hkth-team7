"""Persistence.

**Postgres is the record. Redis db1 is a cache in front of it. Nothing here
depends on a cache hit.**

Every function that existed when this was Redis-only keeps its exact signature,
so `api/routes/*`, `agents/herald.py`, `queue/worker.py` and `scheduler.py` are
unchanged by the migration. What changed is underneath:

* **Assessments are append-only.** The old implementation wrote one key per AOI
  under a 48-hour TTL and overwrote it every cycle, which made a historical
  timeline impossible to build. `save_assessment` now inserts a row and refreshes
  a cache entry; `assessment_history` reads the series back.
* **Alert history is unbounded.** The old one `ltrim`'d to 500 per subscriber and
  200 globally. An insurer needs a whole season on demand.
* **Spatial queries are index lookups.** `subscribers_intersecting` uses the GiST
  index instead of loading every subscriber and testing in Python.

Cache policy: read-through with a TTL on the two hot reads (a subscriber, an
AOI's latest assessment), invalidated on write. Feeds and history are not cached —
they are paginated and change on every cycle, so a cache would mostly miss.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db import session as db
from app.logging_config import get_logger
from app.models.enums import DeliveryMode
from app.models.schemas import (
    Advisory,
    Alert,
    AreaOfInterest,
    BBox,
    DeliveryReceipt,
    ForecastPoint,
    RiskAssessment,
    SourceCitation,
    Subscriber,
    Verification,
)
from app.store import cache

log = get_logger(__name__)


def _sub_cache_key(subscriber_id: str) -> str:
    return cache.key("subscriber", subscriber_id)


def _assessment_cache_key(aoi_id: str) -> str:
    return cache.key("assessment", aoi_id)


class DuplicateAreaError(RuntimeError):
    """Raised when an area id is already in use.

    A distinct type because the route must answer 409, not 500: a caller retrying after a
    partial failure needs to know the area already exists rather than seeing an opaque server
    error and retrying forever. Reached in practice when a previous request created the row and
    then failed downstream.
    """


class LastAreaError(RuntimeError):
    """Raised when deleting an area would leave a subscriber watching nowhere.

    A distinct type rather than a bool, because the route must return 409 with an explanation:
    "not found" and "refused, and here is why" are different answers and a caller can act on
    only one of them.
    """


# --------------------------------------------------------------------------- #
# Subscribers
# --------------------------------------------------------------------------- #


async def save_subscriber(subscriber: Subscriber) -> Subscriber:
    """Upsert a subscriber with its areas and channel bindings.

    One transaction: a subscriber that exists with no channels would be silently
    unreachable, and one with no areas would be watched nowhere.

    Areas and channels are replaced wholesale rather than diffed. That keeps the
    write simple and matches how the API presents them (send the full desired
    set). It does mean AOI ids must be stable across an edit or their assessment
    history is orphaned — the API layer reuses ids for unchanged areas.
    """
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO subscribers (id, name, kind, language, active, created_at)
                VALUES ($1, $2, $3::subscriber_kind, $4, $5, $6)
                ON CONFLICT (id) DO UPDATE SET
                    name       = EXCLUDED.name,
                    kind       = EXCLUDED.kind,
                    language   = EXCLUDED.language,
                    active     = EXCLUDED.active,
                    updated_at = now()
                """,
                subscriber.id,
                subscriber.name,
                subscriber.kind.value,
                subscriber.language,
                subscriber.active,
                subscriber.created_at,
            )

            await conn.execute(
                "DELETE FROM areas_of_interest WHERE subscriber_id = $1", subscriber.id
            )
            for area in subscriber.areas:
                await conn.execute(
                    """
                    INSERT INTO areas_of_interest (
                        id, subscriber_id, name, west, south, east, north,
                        country, admin1, admin2, crop, hectares, delivery_mode
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::delivery_mode)
                    """,
                    area.id,
                    subscriber.id,
                    area.name,
                    area.bbox.west,
                    area.bbox.south,
                    area.bbox.east,
                    area.bbox.north,
                    area.country,
                    area.admin1,
                    area.admin2,
                    area.crop,
                    area.hectares,
                    area.delivery_mode.value,
                )

            await conn.execute(
                "DELETE FROM channel_bindings WHERE subscriber_id = $1", subscriber.id
            )
            for binding in subscriber.channels:
                await conn.execute(
                    """
                    INSERT INTO channel_bindings (
                        subscriber_id, channel, address, enabled, min_severity, aoi_id,
                        min_score
                    ) VALUES ($1, $2::channel, $3, $4, $5::severity, $6, $7)
                    ON CONFLICT (subscriber_id, channel, address) DO UPDATE SET
                        enabled      = EXCLUDED.enabled,
                        min_severity = EXCLUDED.min_severity,
                        aoi_id       = EXCLUDED.aoi_id,
                        -- In the UPDATE too. Omitting it here is the bug this shape invites: the
                        -- INSERT would carry a new dial and an upsert over an existing binding
                        -- would keep the old one, so lowering a threshold would appear to save and
                        -- silently not apply.
                        min_score    = EXCLUDED.min_score
                    """,
                    subscriber.id,
                    binding.channel.value,
                    binding.address,
                    binding.enabled,
                    binding.min_severity.value,
                    # None means "every area" — see migration 013 and `channels_for`.
                    binding.aoi_id,
                    # None means "no score filter" — see migration 017. Distinct from 0.0, which is
                    # an explicit choice of the lowest setting.
                    binding.min_score,
                )

    # Invalidate rather than repopulate: the next read re-hydrates from the row
    # we just committed, so cache and database cannot disagree.
    await cache.delete(_sub_cache_key(subscriber.id))

    log.info(
        "subscriber saved",
        extra={
            "subscriber_id": subscriber.id,
            "areas": len(subscriber.areas),
            "channels": len(subscriber.channels),
        },
    )
    return subscriber


def _subscriber_from_rows(row, area_rows, channel_rows) -> Subscriber:
    """Assemble a Subscriber from its three result sets."""
    return Subscriber(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        language=row["language"],
        active=row["active"],
        created_at=row["created_at"],
        areas=[
            AreaOfInterest(
                id=a["id"],
                name=a["name"],
                bbox=BBox(
                    west=a["west"], south=a["south"], east=a["east"], north=a["north"]
                ),
                country=a["country"],
                admin1=a["admin1"],
                admin2=a["admin2"],
                crop=a["crop"],
                hectares=a["hectares"],
                # Explicit, like every other field. Dropping it here would make every area read
                # back as `direct` — so an aggregator's "do not contact my farmer" setting would
                # persist correctly and then be ignored on every dispatch.
                delivery_mode=a["delivery_mode"],
            )
            for a in area_rows
        ],
        channels=[
            {
                "channel": c["channel"],
                "address": c["address"],
                "enabled": c["enabled"],
                "min_severity": c["min_severity"],
                # Explicit, like the rest. A missing key here would silently drop every
                # per-area override on read — the binding would persist correctly and then
                # behave as though it applied everywhere, which is the one failure mode that
                # sends a subscriber's alerts to the wrong channel with nothing to see.
                "aoi_id": c["aoi_id"],
                # Same reasoning as `aoi_id` above, and the same failure if omitted: the dial would
                # save correctly and then be ignored on every dispatch, so a subscriber who asked
                # for fewer messages would keep receiving all of them with nothing on screen to
                # explain why. `.get` rather than `[...]` because this row may predate migration
                # 017 in a cache entry written by an older build.
                "min_score": c.get("min_score"),
            }
            for c in channel_rows
        ],
    )


async def get_subscriber(subscriber_id: str) -> Subscriber | None:
    """One subscriber, cache-first."""
    cached = await cache.get_json(_sub_cache_key(subscriber_id))
    if cached is not None:
        try:
            return Subscriber.model_validate(cached)
        except Exception:
            # Shape drift after a schema change. Treat as a miss and re-read.
            await cache.delete(_sub_cache_key(subscriber_id))

    async with db.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM subscribers WHERE id = $1", subscriber_id)
        if row is None:
            return None
        areas = await conn.fetch(
            "SELECT * FROM areas_of_interest WHERE subscriber_id = $1 ORDER BY created_at",
            subscriber_id,
        )
        channels = await conn.fetch(
            "SELECT * FROM channel_bindings WHERE subscriber_id = $1 ORDER BY id",
            subscriber_id,
        )

    subscriber = _subscriber_from_rows(row, areas, channels)
    await cache.set_json(
        _sub_cache_key(subscriber_id),
        subscriber.model_dump(mode="json"),
        settings.cache_default_ttl_seconds,
    )
    return subscriber


async def list_subscribers(*, active_only: bool = False) -> list[Subscriber]:
    """Every subscriber, hydrated.

    Three queries total rather than one per subscriber: the scheduler calls this
    every cycle, and an N+1 here would scale with the subscriber base on the hot
    path. Grouping happens in Python, which is cheap next to the round trips it
    saves.
    """
    where = "WHERE active" if active_only else ""

    async with db.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM subscribers {where} ORDER BY created_at"
        )
        if not rows:
            return []

        ids = [r["id"] for r in rows]
        area_rows = await conn.fetch(
            "SELECT * FROM areas_of_interest WHERE subscriber_id = ANY($1::text[]) "
            "ORDER BY created_at",
            ids,
        )
        channel_rows = await conn.fetch(
            "SELECT * FROM channel_bindings WHERE subscriber_id = ANY($1::text[]) "
            "ORDER BY id",
            ids,
        )

    areas_by_sub: dict[str, list] = {}
    for area in area_rows:
        areas_by_sub.setdefault(area["subscriber_id"], []).append(area)
    channels_by_sub: dict[str, list] = {}
    for channel in channel_rows:
        channels_by_sub.setdefault(channel["subscriber_id"], []).append(channel)

    return [
        _subscriber_from_rows(
            row, areas_by_sub.get(row["id"], []), channels_by_sub.get(row["id"], [])
        )
        for row in rows
    ]


async def delete_subscriber(subscriber_id: str) -> bool:
    """Remove a subscriber. Areas, channels and alerts cascade."""
    result = await db.execute("DELETE FROM subscribers WHERE id = $1", subscriber_id)
    await cache.delete(_sub_cache_key(subscriber_id))
    # asyncpg returns the command tag, e.g. "DELETE 1".
    return result.rsplit(" ", 1)[-1] != "0"


async def get_area(aoi_id: str) -> tuple[str, AreaOfInterest] | None:
    """`(subscriber_id, area)` for one monitored area, or None.

    Returns the owner alongside the area so a caller can authorise without a second query —
    every route touching an area has to prove it belongs to the requester, and splitting that
    into two steps is how a check gets forgotten.
    """
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, subscriber_id, name, west, south, east, north,
                   country, admin1, admin2, crop, hectares, delivery_mode
            FROM areas_of_interest WHERE id = $1
            """,
            aoi_id,
        )
    if row is None:
        return None
    return row["subscriber_id"], AreaOfInterest(
        id=row["id"],
        name=row["name"],
        bbox=BBox(
            west=row["west"], south=row["south"], east=row["east"], north=row["north"]
        ),
        country=row["country"],
        admin1=row["admin1"],
        admin2=row["admin2"],
        crop=row["crop"],
        hectares=row["hectares"],
        delivery_mode=row["delivery_mode"],
    )


async def owner_of_area(aoi_id: str) -> str | None:
    """Which subscriber owns this area, or None if the area is unknown.

    Public because **tenancy needs it, not just cache invalidation**. An assessment is keyed by
    `aoi_id`, and an `aoi_id` alone carries no tenant — so `GET /risk/areas/{aoi_id}` could not tell
    whose plot it was serving and returned any named plot's full assessment to an anonymous caller.
    This is the join that lets the route answer "may this caller see it?".
    """
    return await _owner_of(aoi_id)


async def _owner_of(aoi_id: str) -> str | None:
    """Which subscriber owns this area. Used to target cache invalidation."""
    async with db.acquire() as conn:
        return await conn.fetchval(
            "SELECT subscriber_id FROM areas_of_interest WHERE id = $1", aoi_id
        )


async def update_area(
    aoi_id: str,
    *,
    name: str | None = None,
    bbox: BBox | None = None,
    crop: str | None = None,
    hectares: float | None = None,
    delivery_mode: DeliveryMode | None = None,
) -> AreaOfInterest | None:
    """Edit one area in place. None when it does not exist.

    ## Why this exists rather than reusing `save_subscriber`

    `save_subscriber` replaces every area wholesale — `DELETE` then re-`INSERT`. That is
    correct for a full-set write but wrong for an edit: the AOI id is the join key for
    `assessments`, so a caller who re-sends the set with a freshly minted id silently orphans
    the entire monitoring history for that plot. The subscriber sees their timeline reset to
    empty and nothing reports an error.

    Updating in place keeps the id, so a renamed or resized plot keeps every past assessment.

    `geom` is a generated column, so PostGIS recomputes the envelope from the new corners
    automatically — there is nothing to keep in step by hand.

    A resize deliberately does NOT delete past assessments. They were true of the area as it
    was measured, and discarding them would erase the record of a flood that did happen.
    """
    fields: list[str] = []
    values: list[object] = []

    def add(column: str, value: object) -> None:
        values.append(value)
        fields.append(f"{column} = ${len(values)}")

    if name is not None:
        add("name", name)
    if bbox is not None:
        add("west", bbox.west)
        add("south", bbox.south)
        add("east", bbox.east)
        add("north", bbox.north)
    if crop is not None:
        add("crop", crop)
    if hectares is not None:
        add("hectares", hectares)
    if delivery_mode is not None:
        # Cast inline: `add` builds a bare `$n` placeholder, and asyncpg will not coerce a Python
        # string into a Postgres ENUM without one. Without the cast this raises at execute time
        # rather than at import, so it would have shipped.
        values.append(delivery_mode.value)
        fields.append(f"delivery_mode = ${len(values)}::delivery_mode")

    if not fields:
        # Nothing to change. Return the current row rather than failing — a PATCH with no
        # effective change is a no-op, not an error.
        existing = await get_area(aoi_id)
        return existing[1] if existing else None

    values.append(aoi_id)
    async with db.acquire() as conn:
        updated = await conn.execute(
            f"UPDATE areas_of_interest SET {', '.join(fields)} WHERE id = ${len(values)}",
            *values,
        )
    if updated.endswith("0"):
        return None

    # Invalidate the subscriber cache.
    #
    # `get_subscriber` is cache-first, and the portal reads areas through it — so without this
    # an edit persists to Postgres and stays INVISIBLE on the dashboard until the TTL lapses.
    # The subscriber sees their old plot name and concludes the save failed, which is the worst
    # kind of bug: the write worked and the UI says otherwise.
    owner = await _owner_of(aoi_id)
    if owner:
        await cache.delete(_sub_cache_key(owner))

    result = await get_area(aoi_id)
    return result[1] if result else None


async def delete_area(aoi_id: str) -> bool:
    """Stop monitoring one area. True when a row was removed.

    ## Assessment history is deliberately KEPT

    There is no `ON DELETE CASCADE` here and none is added. An assessment records what the
    satellite measured on a date; deleting the area does not make that untrue. A subscriber who
    removes a plot after a flood season must not thereby erase the evidence that they were
    warned — that record is the service's own accountability, and Fahis's verdicts hang off it.

    The rows become unreachable from the subscriber's view, which is the intent of "stop
    monitoring", and remain available to an operator investigating a complaint.

    Refuses to remove a subscriber's LAST area: a subscriber with none is watched nowhere
    while still appearing active, which reads as a working subscription that silently delivers
    nothing. The caller should deactivate or delete the subscriber instead, and the route says
    so.
    """
    async with db.acquire() as conn:
        owner = await conn.fetchval(
            "SELECT subscriber_id FROM areas_of_interest WHERE id = $1", aoi_id
        )
        if owner is None:
            return False

        remaining = await conn.fetchval(
            "SELECT count(*) FROM areas_of_interest WHERE subscriber_id = $1", owner
        )
        if remaining <= 1:
            raise LastAreaError(
                "This is the only area being monitored. Removing it would leave the "
                "subscription active but watching nowhere — add another area first, or "
                "deactivate the subscription."
            )

        removed = await conn.execute(
            "DELETE FROM areas_of_interest WHERE id = $1", aoi_id
        )

    # Invalidate the subscriber cache.
    #
    # `get_subscriber` is cache-first, and the portal reads areas through it — so without this
    # an edit persists to Postgres and stays INVISIBLE on the dashboard until the TTL lapses.
    # The subscriber sees their old plot name and concludes the save failed, which is the worst
    # kind of bug: the write worked and the UI says otherwise.
    await cache.delete(_sub_cache_key(owner))
    return not removed.endswith("0")


async def add_area(subscriber_id: str, area: AreaOfInterest) -> AreaOfInterest | None:
    """Add one area to an existing subscriber. None when the subscriber is unknown.

    Targeted insert rather than a full `save_subscriber` round trip, for the same reason
    `update_area` exists: re-sending the whole set risks re-minting ids and orphaning history.
    """
    async with db.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM subscribers WHERE id = $1", subscriber_id
        )
        if not exists:
            return None

        taken = await conn.fetchval(
            "SELECT subscriber_id FROM areas_of_interest WHERE id = $1", area.id
        )
        if taken is not None:
            # Checked before the insert so the message can name the situation, rather than
            # letting a UniqueViolation surface as a 500 the caller cannot act on.
            raise DuplicateAreaError(
                f"An area with id {area.id} already exists. Use PATCH to change it, or "
                f"choose a different id."
            )

        await conn.execute(
            """
            INSERT INTO areas_of_interest (
                id, subscriber_id, name, west, south, east, north,
                country, admin1, admin2, crop, hectares, delivery_mode
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::delivery_mode)
            """,
            area.id,
            subscriber_id,
            area.name,
            area.bbox.west,
            area.bbox.south,
            area.bbox.east,
            area.bbox.north,
            area.country,
            area.admin1,
            area.admin2,
            area.crop,
            area.hectares,
            area.delivery_mode.value,
        )

    # Invalidate the subscriber cache.
    #
    # `get_subscriber` is cache-first, and the portal reads areas through it — so without this
    # an edit persists to Postgres and stays INVISIBLE on the dashboard until the TTL lapses.
    # The subscriber sees their old plot name and concludes the save failed, which is the worst
    # kind of bug: the write worked and the UI says otherwise.
    await cache.delete(_sub_cache_key(subscriber_id))
    return area


async def subscribers_intersecting(bbox: BBox) -> list[Subscriber]:
    """Active subscribers with an area overlapping this footprint.

    The query the GiST index exists for: previously this would have meant loading
    every subscriber and testing in Python.

    **No callers yet.** The scheduler currently iterates every active subscriber's
    own areas (`pipeline.enqueue_all`), which needs no spatial query. This becomes
    load-bearing for the reverse direction — "a flood footprint arrived; who is
    inside it?" — which is how a broadcast or a third-party alert would fan out.
    """
    rows = await db.fetch(
        """
        SELECT DISTINCT s.id
        FROM subscribers s
        JOIN areas_of_interest a ON a.subscriber_id = s.id
        WHERE s.active
          AND ST_Intersects(
                a.geom,
                ST_MakeEnvelope($1, $2, $3, $4, 4326)::geography
              )
        """,
        bbox.west,
        bbox.south,
        bbox.east,
        bbox.north,
    )
    subscribers = [await get_subscriber(r["id"]) for r in rows]
    return [s for s in subscribers if s is not None]


# --------------------------------------------------------------------------- #
# Assessments — append-only series
# --------------------------------------------------------------------------- #


async def save_assessment(
    assessment: RiskAssessment, *, ttl_seconds: int | None = None
) -> None:
    """Append an assessment and refresh the latest-assessment cache.

    `ttl_seconds` is kept for signature compatibility with the Redis-only
    version and now controls only the **cache** entry — the row itself is
    permanent. That inversion is the point of the migration: history is retained,
    and "too stale to show" becomes a query-time decision rather than a deletion.
    """
    forecast_rows = [
        (
            assessment.id,
            assessment.assessed_at,
            point.day,
            point.date,
            point.risk,
            point.rainfall_mm,
            point.note,
        )
        for point in assessment.forecast
    ]

    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO assessments (
                    id, aoi_id, aoi_name, hazard, severity, score, confidence,
                    lead_time_days, exposure, soil, health, evidence, cascade,
                    data_sources, assessed_at,
                    -- The FEATURES behind the verdict, so a Fahis outcome becomes a training row
                    -- rather than only a calibration point. See 012_assessment_features.sql.
                    score_drivers, stress_attribution,
                    inundated_fraction, stressed_crop_fraction,
                    observed_at_flood, observed_at_stress,
                    platform_flood, platform_stress, method_flood, method_stress
                ) VALUES (
                    $1,$2,$3,$4::hazard_type,$5::severity,$6,$7,$8,
                    $9::jsonb,$10::jsonb,$11::jsonb,$12::text[],
                    $13::hazard_type[],$14::text[],$15,
                    $16::jsonb,$17::jsonb,$18,$19,$20,$21,$22,$23,$24,$25
                )
                ON CONFLICT (id, assessed_at) DO NOTHING
                """,
                assessment.id,
                assessment.aoi_id,
                assessment.aoi_name,
                assessment.hazard.value,
                assessment.severity.value,
                assessment.score,
                assessment.confidence,
                assessment.lead_time_days,
                assessment.exposure.model_dump(mode="json"),
                assessment.soil.model_dump(mode="json"),
                assessment.health.model_dump(mode="json"),
                assessment.evidence,
                [h.value for h in assessment.cascade],
                assessment.data_sources,
                assessment.assessed_at,
                [d.model_dump(mode="json") for d in assessment.score_drivers],
                assessment.stress_attribution,
                assessment.inundated_fraction,
                assessment.stressed_crop_fraction,
                assessment.observed_at_flood,
                assessment.observed_at_stress,
                assessment.platform_flood,
                assessment.platform_stress,
                assessment.method_flood,
                assessment.method_stress,
            )
            if forecast_rows:
                await conn.executemany(
                    """
                    INSERT INTO forecast_points (
                        assessment_id, assessed_at, day, date, risk, rainfall_mm, note
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7)
                    ON CONFLICT (assessment_id, assessed_at, day) DO NOTHING
                    """,
                    forecast_rows,
                )

    await cache.set_json(
        _assessment_cache_key(assessment.aoi_id),
        assessment.model_dump(mode="json"),
        ttl_seconds or settings.cache_assessment_ttl_seconds,
    )


def _assessment_from_row(row, forecast_rows) -> RiskAssessment:
    return RiskAssessment(
        id=row["id"],
        aoi_id=row["aoi_id"],
        aoi_name=row["aoi_name"],
        hazard=row["hazard"],
        severity=row["severity"],
        score=row["score"],
        confidence=row["confidence"],
        lead_time_days=row["lead_time_days"],
        exposure=row["exposure"] or {},
        soil=row["soil"] or {},
        health=row["health"] or {},
        evidence=list(row["evidence"] or []),
        cascade=list(row["cascade"] or []),
        data_sources=list(row["data_sources"] or []),
        assessed_at=row["assessed_at"],
        forecast=[
            ForecastPoint(
                day=f["day"],
                date=f["date"],
                risk=f["risk"],
                rainfall_mm=f["rainfall_mm"],
                note=f["note"],
            )
            for f in forecast_rows
        ],
    )


async def count_assessments_by_area(
    aoi_ids: list[str], *, since: datetime | None = None, until: datetime | None = None
) -> dict[str, int]:
    """Assessment counts per area over a period. `{aoi_id: count}`.

    **The Postgres half of billing, and it is deliberately tenant-blind.** The caller resolves
    which `aoi_id`s belong to whom in Mongo (`iam.store.owned_aoi_ids`) and passes them in, so
    this table needs no tenant column and the risk layer keeps running when Mongo is down.

    Counted in SQL with a `GROUP BY` rather than fetched and tallied: an aggregator at scale has
    thousands of areas and years of history, and pulling every row back to count it is the kind
    of thing that works in testing and times out on a real invoice.

    Areas with no assessments in the window are absent rather than zero — the caller decides
    whether an unmeasured area is billable, which is a commercial question, not a data one.
    """
    if not aoi_ids:
        return {}

    clauses = ["aoi_id = ANY($1::text[])"]
    values: list[object] = [aoi_ids]
    if since is not None:
        values.append(since)
        clauses.append(f"assessed_at >= ${len(values)}")
    if until is not None:
        values.append(until)
        clauses.append(f"assessed_at < ${len(values)}")

    async with db.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT aoi_id, count(*) AS n
            FROM assessments
            WHERE {' AND '.join(clauses)}
            GROUP BY aoi_id
            """,
            *values,
        )
    return {row["aoi_id"]: int(row["n"]) for row in rows}


async def all_area_ids() -> list[tuple[str, str]]:
    """Every `(aoi_id, subscriber_id)` pair. Used by the attribution reconciliation sweep.

    Small by construction — one row per monitored plot — so a full scan is appropriate here in a
    way it would not be for assessments.
    """
    async with db.acquire() as conn:
        rows = await conn.fetch("SELECT id, subscriber_id FROM areas_of_interest")
    return [(row["id"], row["subscriber_id"]) for row in rows]


async def get_assessment(aoi_id: str) -> RiskAssessment | None:
    """Most recent assessment for an area, cache-first.

    Falls back to the newest row within the cache TTL window. Beyond that we
    return None rather than something stale — the same honesty rule the Redis TTL
    used to enforce by deleting, now enforced by the query.
    """
    cached = await cache.get_json(_assessment_cache_key(aoi_id))
    if cached is not None:
        try:
            return RiskAssessment.model_validate(cached)
        except Exception:
            await cache.delete(_assessment_cache_key(aoi_id))

    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.cache_assessment_ttl_seconds
    )

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM assessments
            WHERE aoi_id = $1 AND assessed_at >= $2
            ORDER BY assessed_at DESC
            LIMIT 1
            """,
            aoi_id,
            cutoff,
        )
        if row is None:
            return None
        forecast = await conn.fetch(
            "SELECT * FROM forecast_points WHERE assessment_id = $1 AND assessed_at = $2 "
            "ORDER BY day",
            row["id"],
            row["assessed_at"],
        )

    assessment = _assessment_from_row(row, forecast)
    await cache.set_json(
        _assessment_cache_key(aoi_id),
        assessment.model_dump(mode="json"),
        settings.cache_assessment_ttl_seconds,
    )
    return assessment


async def assessment_history(
    aoi_id: str, *, days: int = 30, limit: int = 500
) -> list[RiskAssessment]:
    """An area's assessment series, newest first.

    **This is what the Redis-only implementation could not do**, and the reason
    the intelligence timeline was unbuildable. Forecast points are fetched in one
    query and grouped, not per-assessment.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM assessments
            WHERE aoi_id = $1 AND assessed_at >= $2
            ORDER BY assessed_at DESC
            LIMIT $3
            """,
            aoi_id,
            since,
            limit,
        )
        if not rows:
            return []

        forecast_rows = await conn.fetch(
            "SELECT * FROM forecast_points "
            "WHERE assessment_id = ANY($1::text[]) ORDER BY day",
            [r["id"] for r in rows],
        )

    by_assessment: dict[str, list] = {}
    for point in forecast_rows:
        by_assessment.setdefault(point["assessment_id"], []).append(point)

    return [
        _assessment_from_row(row, by_assessment.get(row["id"], [])) for row in rows
    ]


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #


async def save_alert(alert: Alert) -> Alert:
    """Persist an alert with its receipts.

    The assessment is stored as JSONB alongside its foreign key. Deliberate: an
    alert is a record of what was *said at the time*, so it must not change if the
    assessment row is later corrected.
    """
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO alerts (
                    id, subscriber_id, assessment_id, assessed_at, assessment,
                    headline, body, actions, broadcast_text, language,
                    generated_by, explanations, created_at
                ) VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8::text[],$9,$10,$11,$12::jsonb,$13)
                ON CONFLICT (id) DO NOTHING
                """,
                alert.id,
                alert.subscriber_id,
                alert.assessment.id,
                alert.assessment.assessed_at,
                alert.assessment.model_dump(mode="json"),
                alert.advisory.headline,
                alert.advisory.body,
                alert.advisory.actions,
                alert.advisory.broadcast_text,
                alert.advisory.language,
                alert.advisory.generated_by,
                # JSONB, alongside the assessment snapshot in the same row. Stored rather than
                # regenerated on read: an alert is the record of what a subscriber was TOLD, and
                # re-running the model weeks later would produce a different account of the same
                # finding — see `models.schemas.Explanations`.
                alert.advisory.explanations.model_dump(mode="json"),
                alert.created_at,
            )
            if alert.receipts:
                await conn.executemany(
                    """
                    INSERT INTO delivery_receipts (
                        alert_id, channel, address, status, provider_message_id,
                        error, attempted_at
                    ) VALUES ($1,$2::channel,$3,$4::delivery_status,$5,$6,$7)
                    """,
                    [
                        (
                            alert.id,
                            r.channel.value,
                            r.address,
                            r.status.value,
                            r.provider_message_id,
                            r.error,
                            r.attempted_at,
                        )
                        for r in alert.receipts
                    ],
                )
    return alert


def _alert_from_row(row, receipt_rows) -> Alert:
    assessment = row["assessment"]
    if isinstance(assessment, str):
        # Defensive: only reachable if the jsonb codec did not register.
        assessment = json.loads(assessment)

    # Same defence for the explanations column. `.get` rather than `row["explanations"]` because a
    # query written before this column existed (or one selecting an explicit column list) would
    # otherwise raise KeyError on a row that is otherwise perfectly readable.
    explanations = row.get("explanations") if hasattr(row, "get") else None
    if isinstance(explanations, str):
        explanations = json.loads(explanations)

    return Alert(
        id=row["id"],
        subscriber_id=row["subscriber_id"],
        assessment=RiskAssessment.model_validate(assessment),
        advisory=Advisory(
            headline=row["headline"],
            body=row["body"],
            actions=list(row["actions"] or []),
            broadcast_text=row["broadcast_text"],
            language=row["language"],
            generated_by=row["generated_by"],
            # `{}` for a pre-migration alert, which yields three empty strings rather than a
            # validation error on a row that was written before explanations existed.
            explanations=explanations or {},
        ),
        receipts=[
            DeliveryReceipt(
                channel=r["channel"],
                address=r["address"],
                status=r["status"],
                provider_message_id=r["provider_message_id"],
                error=r["error"],
                attempted_at=r["attempted_at"],
            )
            for r in receipt_rows
        ],
        created_at=row["created_at"],
    )


async def get_alert(alert_id: str) -> Alert | None:
    async with db.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM alerts WHERE id = $1", alert_id)
        if row is None:
            return None
        receipts = await conn.fetch(
            "SELECT * FROM delivery_receipts WHERE alert_id = $1 ORDER BY id", alert_id
        )
    return _alert_from_row(row, receipts)


async def list_alerts(
    subscriber_id: str | None = None, *, limit: int = 50
) -> list[Alert]:
    """Recent alerts, newest first. Omit `subscriber_id` for the global feed.

    Two queries regardless of result size — receipts for the whole page are
    fetched together and grouped, so this does not degrade as `limit` rises.
    """
    async with db.acquire() as conn:
        if subscriber_id:
            rows = await conn.fetch(
                "SELECT * FROM alerts WHERE subscriber_id = $1 "
                "ORDER BY created_at DESC LIMIT $2",
                subscriber_id,
                limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM alerts ORDER BY created_at DESC LIMIT $1", limit
            )
        if not rows:
            return []

        receipt_rows = await conn.fetch(
            "SELECT * FROM delivery_receipts WHERE alert_id = ANY($1::text[]) "
            "ORDER BY id",
            [r["id"] for r in rows],
        )

    by_alert: dict[str, list] = {}
    for receipt in receipt_rows:
        by_alert.setdefault(receipt["alert_id"], []).append(receipt)

    return [_alert_from_row(row, by_alert.get(row["id"], [])) for row in rows]


async def set_alert_audio(
    alert_id: str, audio_key: str, duration_seconds: float | None = None
) -> None:
    """Record the object-store key for an alert's voice note.

    A key, not a URL — presigned URLs expire, so the durable reference is the key
    and the URL is minted per request by `store.objects.audio_url`.

    **No callers yet:** nothing synthesises speech. See the status note in
    `store/objects.py` — this is the storage half of a feature whose producing half
    (a TTS stage) does not exist.
    """
    await db.execute(
        "UPDATE alerts SET audio_key = $2, audio_seconds = $3 WHERE id = $1",
        alert_id,
        audio_key,
        duration_seconds,
    )


# --------------------------------------------------------------------------- #
# Verification (Fahis)
# --------------------------------------------------------------------------- #


async def schedule_verification(
    assessment_id: str, assessed_at: datetime, verify_after: datetime | None
) -> None:
    """Mark when an assessment becomes eligible for verification.

    A Postgres column rather than a delayed queue message: the wait is days, Redis
    Streams cannot delay delivery, and a sleeping worker would lose the timer on
    restart. NULL means never eligible.
    """
    await db.execute(
        "UPDATE assessments SET verify_after = $3 "
        "WHERE id = $1 AND assessed_at = $2",
        assessment_id,
        assessed_at,
        verify_after,
    )


async def assessments_due_for_verification(limit: int = 20) -> list[RiskAssessment]:
    """Assessments past their `verify_after` with no verdict yet.

    The anti-join is what makes this idempotent: once a verification row exists
    the assessment stops being returned, so a sweep interrupted halfway does not
    re-verify what it already finished.
    """
    now = datetime.now(timezone.utc)

    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.* FROM assessments a
            LEFT JOIN verifications v ON v.assessment_id = a.id
            WHERE a.verify_after IS NOT NULL
              AND a.verify_after <= $1
              AND v.id IS NULL
            ORDER BY a.verify_after
            LIMIT $2
            """,
            now,
            limit,
        )
        if not rows:
            return []

        forecast_rows = await conn.fetch(
            "SELECT * FROM forecast_points WHERE assessment_id = ANY($1::text[]) "
            "ORDER BY day",
            [r["id"] for r in rows],
        )

    by_assessment: dict[str, list] = {}
    for point in forecast_rows:
        by_assessment.setdefault(point["assessment_id"], []).append(point)

    return [_assessment_from_row(r, by_assessment.get(r["id"], [])) for r in rows]


async def save_verification(verification: Verification) -> Verification:
    """Persist a verdict. One row per assessment; a re-run updates it."""
    await db.execute(
        """
        INSERT INTO verifications (
            id, assessment_id, aoi_id, alert_id, claimed_hazard, claimed_severity,
            assessed_at, verdict, confidence, rationale, sources, queries, verified_at
        ) VALUES (
            $1,$2,$3,$4,$5::hazard_type,$6::severity,$7,$8::verdict,$9,$10,
            $11::jsonb,$12::text[],$13
        )
        ON CONFLICT (assessment_id) DO UPDATE SET
            verdict     = EXCLUDED.verdict,
            confidence  = EXCLUDED.confidence,
            rationale   = EXCLUDED.rationale,
            sources     = EXCLUDED.sources,
            queries     = EXCLUDED.queries,
            verified_at = EXCLUDED.verified_at
        """,
        verification.id,
        verification.assessment_id,
        verification.aoi_id,
        verification.alert_id,
        verification.claimed_hazard.value,
        verification.claimed_severity.value,
        verification.assessed_at,
        verification.verdict.value,
        verification.confidence,
        verification.rationale,
        [s.model_dump(mode="json") for s in verification.sources],
        verification.queries,
        verification.verified_at,
    )
    log.info(
        "verification saved",
        extra={
            "assessment_id": verification.assessment_id,
            "verdict": verification.verdict.value,
        },
    )
    return verification


async def alert_id_for_assessment(assessment_id: str) -> str | None:
    """The alert dispatched from this assessment, if one was. None otherwise.

    ## Why this lookup exists

    `verifications.alert_id` is the correlation key the webhook contract documents for aggregators —
    "join on `alert_id` to close the loop on an alert you already received". But Fahis is handed only
    the assessment, so it had no way to know the alert and the column was written NULL every time.
    Dead since it was created, and the contract promised something the payload never carried.

    None is a legitimate answer, not a failure: an assessment below the dispatch floor or suppressed
    as a duplicate is still verified, because accuracy is measured on what we CONCLUDED rather than
    on what we chose to send. Those verdicts correlate on `assessment_id`, which is always present.
    """
    async with db.acquire() as conn:
        return await conn.fetchval(
            "SELECT id FROM alerts WHERE assessment_id = $1 ORDER BY created_at DESC LIMIT 1",
            assessment_id,
        )


async def verifications_for(assessment_ids: list[str]) -> dict[str, Verification]:
    """Verdicts for several assessments at once. `{assessment_id: Verification}`.

    One query rather than N. The alert queue renders up to twenty alerts and each may carry a
    verdict — per-alert lookups would make opening the page twenty round trips, which is the kind
    of thing that is invisible in testing with two alerts and slow for a real subscriber.

    Absent from the result means Fahis has not run yet, which is a normal state: verification is
    scheduled days after the assessment, so a fresh alert legitimately has no verdict. The caller
    must distinguish that from a verdict of `unverified`, which is a finding.
    """
    if not assessment_ids:
        return {}

    rows = await db.fetch(
        "SELECT * FROM verifications WHERE assessment_id = ANY($1::text[])",
        assessment_ids,
    )
    return {row["assessment_id"]: _row_to_verification(row) for row in rows}


def _row_to_verification(row) -> Verification:  # noqa: ANN001 — asyncpg Record
    """One row to a `Verification`. Shared by the single and batch readers so they cannot drift."""
    sources = row["sources"]
    if isinstance(sources, str):
        sources = json.loads(sources)

    return Verification(
        id=row["id"],
        assessment_id=row["assessment_id"],
        aoi_id=row["aoi_id"],
        alert_id=row["alert_id"],
        claimed_hazard=row["claimed_hazard"],
        claimed_severity=row["claimed_severity"],
        assessed_at=row["assessed_at"],
        verdict=row["verdict"],
        confidence=row["confidence"],
        rationale=row["rationale"],
        sources=sources or [],
        queries=list(row["queries"] or []),
        verified_at=row["verified_at"],
    )


async def get_verification(assessment_id: str) -> Verification | None:
    row = await db.fetchrow(
        "SELECT * FROM verifications WHERE assessment_id = $1", assessment_id
    )
    if row is None:
        return None
    return Verification(
        id=row["id"],
        assessment_id=row["assessment_id"],
        aoi_id=row["aoi_id"],
        alert_id=row["alert_id"],
        claimed_hazard=row["claimed_hazard"],
        claimed_severity=row["claimed_severity"],
        assessed_at=row["assessed_at"],
        verdict=row["verdict"],
        confidence=row["confidence"],
        rationale=row["rationale"],
        sources=[SourceCitation.model_validate(s) for s in (row["sources"] or [])],
        queries=list(row["queries"] or []),
        verified_at=row["verified_at"],
    )


async def verification_metrics(*, days: int = 90) -> dict:
    """Accuracy over verified alerts.

    **Computed over trainable verdicts only** — CONFIRMED and REFUTED. Including
    UNVERIFIED would count every unreported real flood as a false positive, which
    would make the figure not merely imprecise but actively misleading: it would
    measure news coverage, not model accuracy.

    `coverage` is reported alongside so the denominator is never mistaken for the
    whole population.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    row = await db.fetchrow(
        """
        SELECT
            count(*) FILTER (WHERE verdict = 'confirmed')      AS confirmed,
            count(*) FILTER (WHERE verdict = 'partial')        AS partial,
            count(*) FILTER (WHERE verdict = 'refuted')        AS refuted,
            count(*) FILTER (WHERE verdict = 'unverified')     AS unverified,
            count(*) FILTER (WHERE verdict = 'not_attempted')  AS not_attempted,
            count(*)                                          AS total
        FROM verifications
        WHERE verified_at >= $1
        """,
        since,
    )

    if row is None:
        return {}

    confirmed = int(row["confirmed"] or 0)
    refuted = int(row["refuted"] or 0)
    trainable = confirmed + refuted
    total = int(row["total"] or 0)

    return {
        "window_days": days,
        "confirmed": confirmed,
        "partial": int(row["partial"] or 0),
        "refuted": refuted,
        "unverified": int(row["unverified"] or 0),
        "not_attempted": int(row["not_attempted"] or 0),
        "total": total,
        # None rather than 0 when there is nothing to divide by: a displayed 0%
        # accuracy would read as "always wrong" instead of "not yet measurable".
        "precision": round(confirmed / trainable, 3) if trainable else None,
        # What share of verdicts were conclusive at all. Low coverage is expected
        # for rural areas and is the honest caveat on `precision`.
        "coverage": round(trainable / total, 3) if total else None,
        "note": (
            "precision is computed over confirmed+refuted only; unverified "
            "means nobody reported it, not that nothing happened"
        ),
    }


# --------------------------------------------------------------------------- #
# Portal aggregates
# --------------------------------------------------------------------------- #


async def alert_counters(subscriber_id: str | None = None, *, days: int = 30) -> dict:
    """Counts for the dashboard tiles.

    `people_covered` is summed over **distinct AOIs**, not over alerts. Summing
    per-alert double-counts the same population whenever an area alerts twice —
    which the previous dashboard did.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    clause = "AND a.subscriber_id = $2" if subscriber_id else ""
    args = [since] + ([subscriber_id] if subscriber_id else [])

    row = await db.fetchrow(
        f"""
        WITH scoped AS (
            SELECT a.id, a.assessment
            FROM alerts a
            WHERE a.created_at >= $1 {clause}
        ),
        distinct_areas AS (
            SELECT DISTINCT
                assessment->>'aoi_id'                        AS aoi_id,
                (assessment->'exposure'->>'population')::bigint AS population
            FROM scoped
        )
        SELECT
            (SELECT count(*) FROM scoped)                        AS alerts,
            (SELECT count(*) FROM delivery_receipts r
              WHERE r.alert_id IN (SELECT id FROM scoped)
                AND r.status = 'sent')                           AS delivered,
            (SELECT coalesce(sum(population), 0) FROM distinct_areas) AS people_covered
        """,
        *args,
    )

    if row is None:
        return {"alerts": 0, "delivered": 0, "people_covered": 0}
    return {
        "alerts": int(row["alerts"] or 0),
        "delivered": int(row["delivered"] or 0),
        "people_covered": int(row["people_covered"] or 0),
    }


# --------------------------------------------------------------------------- #
# Vegetation-index history — the seasonal anomaly baseline (step 6)
#
# Migration 007. `NDVI < 0.35` is a fixed cut on a seasonal quantity, so it
# cannot separate a drowning field in August from a bare one in January. These
# two functions are the series that lets `app/stats/anomaly.py` fit a real
# seasonal norm and measure against it.
# --------------------------------------------------------------------------- #


async def record_index_observation(
    aoi_id: str,
    index_name: str,
    mean: float,
    valid_fraction: float,
    *,
    observed_at: datetime | None = None,
) -> None:
    """Append one index reading.

    Never raises. This is a *baseline accumulator*, not part of the assessment
    path — an insert failure must not cost a farmer their warning. It degrades to
    "the baseline grows more slowly", which is invisible this cycle and self-heals
    on the next.

    Readings from mostly-cloudy scenes are rejected outright: a mean computed from
    5% of the pixels describes a cloud gap, and letting it into the fit would bias
    the seasonal curve toward whatever happened to be visible.
    """
    if valid_fraction < 0.2:
        return

    stamp = observed_at or datetime.now(timezone.utc)
    try:
        async with db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO index_history
                    (aoi_id, index_name, day_of_year, mean, valid_fraction, observed_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (aoi_id, index_name, observed_at) DO NOTHING
                """,
                aoi_id,
                index_name,
                stamp.timetuple().tm_yday,
                float(mean),
                float(valid_fraction),
                stamp,
            )
    except Exception as exc:
        log.debug(
            "index history write failed",
            extra={"aoi_id": aoi_id, "index": index_name, "error": str(exc)},
        )


async def index_history(
    aoi_id: str, index_name: str, *, years: int = 5, limit: int = 2000
) -> tuple[list[int], list[float]]:
    """`(days_of_year, means)` for one AOI and index, for the harmonic fit.

    Returns two parallel lists rather than models because the only consumer is a
    least-squares fit that wants arrays. Empty lists when there is no history or the
    read fails — `fit_harmonic_baseline` then reports `available=False` and the
    caller keeps the threshold fallback, which is the documented degradation.

    Five years by default: enough to average out one anomalous season, short enough
    that a genuine land-use change (irrigation arriving, a plot converted) is not
    fought by a decade-old norm.
    """
    since = datetime.now(timezone.utc) - timedelta(days=365 * years)
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT day_of_year, mean FROM index_history
                WHERE aoi_id = $1 AND index_name = $2 AND observed_at >= $3
                ORDER BY observed_at DESC
                LIMIT $4
                """,
                aoi_id,
                index_name,
                since,
                limit,
            )
    except Exception as exc:
        log.debug("index history read failed", extra={"error": str(exc)})
        return [], []

    return [int(r["day_of_year"]) for r in rows], [float(r["mean"]) for r in rows]


async def training_rows(
    *, days: int = 365, limit: int = 5000, hazard: str | None = None
) -> list[dict]:
    """Verified assessments with their FEATURES — the retraining set Fahis accumulates.

    ## What this is for, and how it differs from `verification_outcomes`

    `verification_outcomes` yields `(confidence, outcome)`. That is enough to **calibrate** — a
    scalar map from claimed reliability to observed reliability — and useless for **retraining**,
    because a model needs the inputs that produced a prediction rather than the prediction itself.

    A CONFIRMED flood should be able to teach that *"65% inundated, on impeded clay, with 48 mm
    forecast"* was correct. With only the confidence, all it can teach is "the pipeline was right at
    0.88" — the same scalar for every hazard and every input combination.

    So this returns one row per verified assessment carrying the measured fractions, the exact score
    drivers, the crop-stress channel attribution, and the provenance of each measurement. Those are
    features a LightGBM or RandomForest can actually fit against a real outcome — which is the
    sequencing that makes those models honest rather than fitted to our own rules.

    ## Only the two trainable verdicts

    CONFIRMED and REFUTED. PARTIAL is genuinely ambiguous — right area, wrong hazard or severity —
    and forcing it to 0 or 1 would inject the judgement the taxonomy deliberately refuses to make.
    UNVERIFIED means nobody reported it, which for a remote Nigerian LGA is the COMMON case and is
    not evidence the warning was wrong; training on it would fit news coverage.

    ## Rows where a leg did not measure are KEPT, with NULL

    `inundated_fraction` is None when the radar leg produced nothing. Dropping those rows would bias
    the set toward cycles where everything worked; substituting zero would teach the model that a
    blind cycle observed dry ground — the defect that made a radar failure classify as drought. A
    consumer must treat None as "unknown" and either impute explicitly or exclude that feature.

    Returns `[]` on any failure, so a caller reports "not enough data" rather than raising.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    clause = "AND a.hazard = $3::hazard_type" if hazard else ""
    params: list = [since, limit]
    if hazard:
        params.append(hazard)

    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT
                    v.id                AS verification_id,
                    v.assessment_id,
                    v.verdict::text     AS verdict,
                    v.confidence        AS verdict_confidence,
                    v.verified_at,
                    a.aoi_id, a.hazard::text AS hazard, a.severity::text AS severity,
                    a.score, a.confidence, a.assessed_at,
                    a.inundated_fraction, a.stressed_crop_fraction,
                    a.score_drivers, a.stress_attribution,
                    a.exposure, a.soil, a.health,
                    a.method_flood, a.method_stress,
                    a.platform_flood, a.platform_stress,
                    a.observed_at_flood, a.observed_at_stress
                FROM verifications v
                JOIN assessments a ON a.id = v.assessment_id
                WHERE v.verified_at >= $1
                  AND v.verdict IN ('confirmed', 'refuted')
                  {clause}
                ORDER BY v.verified_at DESC
                LIMIT $2
                """,
                *params,
            )
    except Exception as exc:
        log.warning("training row read failed", extra={"error": str(exc)})
        return []

    out: list[dict] = []
    for row in rows:
        record = dict(row)
        # The label, stated once so a consumer never re-derives it from the verdict string and gets
        # the polarity backwards. 1 = the hazard we warned about was independently confirmed.
        record["label"] = 1 if record["verdict"] == "confirmed" else 0
        out.append(record)
    return out


async def training_set_readiness(*, days: int = 365) -> dict:
    """Whether the accumulated set is large and balanced enough to fit anything.

    ## Why a readiness check rather than just a count

    A fitting script that runs on 3 rows produces a model, and that model is noise wearing the
    authority of `CONFIDENCE_TRAINED = 0.88`. The failure is silent: nothing errors, the weights
    load, and the pipeline gains escalation authority for a fitted constant.

    So the decision to fit is explicit and reported. `ready` is False until there are enough rows AND
    both classes are present — a set of 40 CONFIRMED and 0 REFUTED can only learn "always yes",
    which scores perfectly on its own data and is worthless.

    The thresholds are deliberately modest. This is a screening gate against fitting on noise, not a
    statistical power calculation; a tree model on 50 rows with real outcomes is a legitimate first
    step, whereas one on 3 is not.
    """
    rows = await training_rows(days=days)
    confirmed = sum(1 for r in rows if r["label"] == 1)
    refuted = len(rows) - confirmed

    #: Minimum rows and minimum per class before fitting is worth attempting.
    MIN_ROWS = 50
    MIN_PER_CLASS = 10

    reasons: list[str] = []
    if len(rows) < MIN_ROWS:
        reasons.append(f"only {len(rows)} verified rows; {MIN_ROWS} is the floor")
    if confirmed < MIN_PER_CLASS:
        reasons.append(f"only {confirmed} confirmed; {MIN_PER_CLASS} is the floor")
    if refuted < MIN_PER_CLASS:
        reasons.append(
            f"only {refuted} refuted; {MIN_PER_CLASS} is the floor. REFUTED is rare by design — "
            "Fahis downgrades it to UNVERIFIED without a credible source — so this is the "
            "constraint that will bind longest"
        )

    return {
        "rows": len(rows),
        "confirmed": confirmed,
        "refuted": refuted,
        "min_rows": MIN_ROWS,
        "min_per_class": MIN_PER_CLASS,
        "ready": not reasons,
        "blocking": reasons,
        "features_available": sorted(
            {
                key
                for row in rows
                for key, value in row.items()
                if value is not None and key not in {"verification_id", "assessment_id"}
            }
        ),
    }


async def verification_outcomes(
    *, days: int = 365, limit: int = 2000
) -> tuple[list[float], list[int]]:
    """`(confidences, outcomes)` pairs for calibration and weight fitting.

    Joins each verification back to the assessment it judged, so the pair is
    "what we claimed" against "what happened". `outcomes` is 1 for CONFIRMED,
    0 for REFUTED.

    **Only the two trainable verdicts are selected.** PARTIAL is genuinely
    ambiguous — right area, wrong hazard or severity — and forcing it to 0 or 1
    would inject a judgement the taxonomy deliberately refuses to make. UNVERIFIED
    and NOT_ATTEMPTED are excluded for the reason `verification_metrics` documents:
    including them would fit news coverage rather than model accuracy.

    Returns empty lists on any failure, so `Calibrator.fit` reports
    `available=False` and the pipeline keeps its uncalibrated confidence.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT a.confidence, v.verdict
                FROM verifications v
                JOIN assessments a ON a.id = v.assessment_id
                WHERE v.verified_at >= $1
                  AND v.verdict IN ('confirmed', 'refuted')
                ORDER BY v.verified_at DESC
                LIMIT $2
                """,
                since,
                limit,
            )
    except Exception as exc:
        log.debug("verification outcomes read failed", extra={"error": str(exc)})
        return [], []

    confidences = [float(r["confidence"]) for r in rows]
    outcomes = [1 if r["verdict"] == "confirmed" else 0 for r in rows]
    return confidences, outcomes
