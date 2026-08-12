"""Vector (GeoParquet) reads over cloud-hosted feature collections — Overture Maps.

## Why this module is new in kind, and not a variation on `cog.py`

Every other reader in `app/eo/` reads **rasters**: `cog.py` does windowed range reads over
GeoTIFFs and hands back a numpy grid. Overture is **columnar vector data** — one row per
building or business, geometry in a column — so nothing in `cog.py` applies. That is a genuinely
new capability, and it is worth being explicit that it is, rather than filing it under "another
adapter".

**It is the same discipline, though**, which is why it belongs here at all. Parquet carries
per-row-group min/max statistics, so a bounding-box predicate reads only the row groups whose
`bbox` range overlaps the AOI. That is a range read over HTTP with a different index — exactly
`cog.py`'s bargain, applied to columns instead of tiles.

## DuckDB, in place, and NOT a copy into MinIO

The obvious plan is to download the Parquet to our own object store and query it there. That was
considered and rejected: it adds a multi-gigabyte sync job, a staleness question against a
monthly release, and a second copy of data that is already served anonymously — to make queries
that already complete in seconds. `httpfs` reads the remote file directly, so there is nothing to
sync and nothing to invalidate.

**No PySpark.** It needs a JVM in a slim Python image where torch is already the heavyweight, and
its value is distributing across machines we do not have. DuckDB is single-process, embedded, and
reads Parquet natively.

## Measured cost, because this is the module where cost is the whole design question

Against `release/2026-07-22.0`, from a laptop (2026-08-12):

| Query | Time |
|---|---|
| `theme=places` over one AOI, grouped by category | **12 s** |
| `theme=buildings` count over one AOI (all parts) | **~110 s** |

The gap is the point. `places` is ~0.6 GB/part and `buildings` is much larger with 128 row groups
per part, so a buildings query scans far more metadata. **110 seconds is too slow for a request
path** — hence `POI_ONLY_IN_REQUEST_PATH` below, and why buildings is offered as an explicit,
separately-budgeted call rather than folded into `exposure_for`.

## What Overture actually contains for Nigeria — measured, and one capability does not exist

Checked before building anything on it, and the results changed the plan:

| Theme | Lagos bbox | Verdict |
|---|---|---|
| `places` | **53,865 POIs** with categories, phones, websites | **Usable.** The commercial-density signal the platform has no proxy for |
| `buildings` | 1,227,116 footprints | Usable as *geometry*. But **0.19% have a height** and **0.07% a name** |
| `addresses` | **0** | **Does not exist here** |

`theme=addresses` covers **39 countries and none of them are in Africa** (the full list is US,
BR, MX, FR, IT, JP, DE, CA, AU, ES, NL, TW, PL, CO, BE, PT, CL, DK, FI, NO, CH, CZ, RS, AT, NZ,
EE, SK, HR, LT, UY, SI, LV, LU, HK, SG, IS, FO, GL, LI). So Overture cannot verify a Nigerian
address, and any plan that assumed it could is wrong. Nominatim structured search plus the
`polygon_geojson` outline in `eo/places.py` is what actually serves that need.

Likewise **height is not an available wealth or asset proxy in Nigeria** from this source. The
`meanHeight` column in Microsoft's `ms-buildings` is the candidate for that, and it is a separate
adapter over a different Parquet layout.

## Degrading

Every function here returns an empty/None result rather than raising, matching every other
adapter: a missing POI count must lower confidence, never fail a scan. DuckDB itself is an
**optional dependency** — the import is inside the call, and `available()` reports it — so a
deployment that has not installed it degrades with one log line instead of failing at import.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.config import settings
from app.logging_config import describe, get_logger
from app.models.schemas import BBox

log = get_logger(__name__)

#: Attribution. Overture is ODbL for OSM-derived records and CDLA-Permissive overall; the ODbL
#: obligation is the binding one, so it is stated wherever these results are shown.
ATTRIBUTION = "© Overture Maps Foundation, © OpenStreetMap contributors"

#: Only `places` is cheap enough to sit on a request path. Buildings is ~110 s per AOI.
#:
#: Named rather than commented so that a future caller reaching for buildings inside
#: `exposure_for` has to read this line first.
POI_ONLY_IN_REQUEST_PATH = True


@dataclass(frozen=True)
class CommercialProfile:
    """POI density and mix inside an AOI — the commercial-activity signal.

    ## Why this is not in `ExposureSummary`

    `exposure.py` answers "who and what is inside the hazard footprint" for **severity**
    weighting. This answers "what kind of place is this economically", which is a Track 4
    (credit) question and must not silently become a risk-score input. Keeping it in its own
    type is what makes that boundary reviewable — the same reason `Track` and `HazardType` are
    separate enums.

    `available=False` with `total=0` is **unknown**, not "no businesses here". The distinction is
    the "absent is not zero" invariant, and it matters more here than anywhere: an empty POI
    count read as "no commercial activity" would be a fabricated input to a credit decision.
    """

    total: int = 0
    #: Category → count, most common first. Overture's own `basic_category` taxonomy, passed
    #: through unmapped: inventing our own grouping would be a judgement call hidden inside a
    #: number, and the raw category is what a reviewer can check.
    categories: dict[str, int] = field(default_factory=dict)
    #: How many carry a phone number or website — a weak liveness signal, since a closed shop
    #: keeps its outline but tends to lose its contact details.
    contactable: int = 0
    available: bool = False


def available() -> bool:
    """True when GeoParquet reads are configured and DuckDB is importable.

    Both conditions, because either alone is a misconfiguration that would otherwise surface as
    an empty result: a set URL with no DuckDB reads nothing, and DuckDB with no URL has nothing
    to read.
    """
    if not (settings.overture_release_url or "").strip():
        return False
    from importlib.util import find_spec

    return find_spec("duckdb") is not None


def _connect():
    """A configured in-process DuckDB, or None.

    `httpfs` is what makes remote range reads work; without it DuckDB would try to open an
    `s3://` path as a local file. Installed per connection because DuckDB caches the extension
    on disk after the first fetch, so this is a no-op on every call but the first.
    """
    try:
        import duckdb
    except Exception as exc:  # noqa: BLE001 — optional dependency
        log.debug("duckdb unavailable", extra={"error": describe(exc)})
        return None

    try:
        con = duckdb.connect()
        con.execute("INSTALL httpfs; LOAD httpfs;")
        # Anonymous. Overture is a public bucket, and DuckDB would otherwise look for
        # credentials in the environment and fail differently depending on whether the host
        # happens to have unrelated AWS variables set.
        con.execute(f"SET s3_region='{settings.overture_s3_region}';")
        con.execute("SET enable_http_metadata_cache=true;")
        return con
    except Exception as exc:  # noqa: BLE001
        log.warning("could not open duckdb", extra={"error": describe(exc)})
        return None


def _theme_glob(theme: str, type_: str) -> str:
    return (
        f"{settings.overture_release_url.rstrip('/')}"
        f"/theme={theme}/type={type_}/*.parquet"
    )


async def commercial_profile(bbox: BBox) -> CommercialProfile:
    """POI count and category mix inside the AOI.

    Runs the blocking DuckDB query in a worker thread: DuckDB has no async API and this is
    called from async handlers, so without `to_thread` a 12-second query would stall the event
    loop and every concurrent request with it.

    Bounded by `overture_query_timeout_seconds`. A timeout returns an unavailable profile rather
    than raising, because this is a *contextual* signal — nothing in the hazard path depends on
    it, and a slow public bucket must not extend a scan.
    """
    if not available():
        return CommercialProfile()

    query = f"""
        SELECT basic_category AS category,
               count(*) AS n,
               count_if(
                   (phones IS NOT NULL AND len(phones) > 0)
                   OR (websites IS NOT NULL AND len(websites) > 0)
               ) AS contactable
        FROM read_parquet('{_theme_glob("places", "place")}')
        WHERE bbox.xmin >= {bbox.west} AND bbox.xmax <= {bbox.east}
          AND bbox.ymin >= {bbox.south} AND bbox.ymax <= {bbox.north}
        GROUP BY basic_category
        ORDER BY n DESC
    """

    try:
        rows = await asyncio.wait_for(
            asyncio.to_thread(_run, query),
            timeout=settings.overture_query_timeout_seconds,
        )
    except TimeoutError:
        log.info(
            "overture places query exceeded its budget; commercial profile unknown",
            extra={"budget_s": settings.overture_query_timeout_seconds},
        )
        return CommercialProfile()
    except Exception as exc:  # noqa: BLE001
        log.warning("overture places query failed", extra={"error": describe(exc)})
        return CommercialProfile()

    if rows is None:
        return CommercialProfile()

    categories: dict[str, int] = {}
    total = 0
    contactable = 0
    for category, count, with_contact in rows:
        total += int(count)
        contactable += int(with_contact or 0)
        # Overture leaves `basic_category` null on a substantial minority of records (452 of
        # ~1,300 over the Kano test AOI). Counted in the total but not invented a name for.
        if category:
            categories[str(category)] = int(count)

    # `available` keys on the query SUCCEEDING, not on finding anything. A genuine zero over
    # farmland is a real measurement and must be distinguishable from an outage — which is the
    # whole point of carrying the flag rather than inferring it from `total > 0`.
    return CommercialProfile(
        total=total,
        categories=categories,
        contactable=contactable,
        available=True,
    )


def _run(query: str) -> list[tuple] | None:
    """Execute one query on a fresh connection. Blocking; call via `to_thread`.

    A connection per query rather than a pooled one: these are minutes apart at most, DuckDB
    connections are cheap to open, and a long-lived connection holding HTTP metadata caches
    across a monthly Overture release is a staleness bug waiting to happen.
    """
    con = _connect()
    if con is None:
        return None
    try:
        return con.execute(query).fetchall()
    finally:
        con.close()
