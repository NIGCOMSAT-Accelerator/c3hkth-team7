"""Exposure: who and what is inside the hazard footprint.

Exposure is what turns a hazard into a consequence — 30% inundation over empty
scrub and over a rice-growing ward with 40,000 people are not the same alert.

Four independent sources, each optional and each recorded in
`ExposureSummary.sources` when it answers:

- **ESA WorldCover** — real cropland/water/built-up fractions by counting
  classified pixels. This replaces an earlier hard-coded "35% of the area is
  cropland" guess, which was the weakest number in the whole pipeline.
- **Copernicus DEM GLO-30** — the share of the AOI sitting below its own median
  elevation, i.e. where water collects first.
- **WorldPop** — population via the zonal-stats REST API.
- **OpenStreetMap** — settlements and health facilities, counted separately
  rather than split by a ratio.

A source that fails leaves its field at zero and its name out of `sources`, so
"nothing there" stays distinguishable from "we don't know".
"""

from __future__ import annotations

import asyncio
import json

import httpx

from app.config import settings
from app.eo.geometry import area_hectares, bbox_geojson
from app.logging_config import describe, get_logger
from app.models.schemas import BBox, ExposureSummary

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

#: How long to chase a WorldPop async task before giving up.
#:
#: 5 x 3s = 15s worst case. Bounded because population MODULATES a risk score and never
#: triggers a hazard, so it must not hold a scan open — the Analyst is already the slow stage.
_WORLDPOP_POLL_ATTEMPTS = 5
_WORLDPOP_POLL_SECONDS = 3.0

# Re-exported so callers can reach the geometry helpers through this module.
__all__ = ["area_hectares", "bbox_geojson", "exposure_for"]


async def exposure_for(bbox: BBox) -> ExposureSummary:
    """Gather every exposure signal concurrently."""
    landcover, terrain, population, osm = await asyncio.gather(
        _worldcover_fractions(bbox),
        _dem_lowland_fraction(bbox),
        _worldpop_population(bbox),
        _osm_counts(bbox),
        return_exceptions=True,
    )

    summary = ExposureSummary()
    sources: list[str] = []

    if isinstance(landcover, dict) and landcover:
        summary.cropland_fraction = landcover["cropland"]
        summary.water_fraction = landcover["water"]
        summary.builtup_fraction = landcover["builtup"]
        summary.cropland_hectares = round(
            landcover["cropland"] * area_hectares(bbox), 1
        )
        sources.append("worldcover")

    if isinstance(terrain, float):
        summary.lowland_fraction = terrain
        sources.append("copernicus-dem")

    if isinstance(population, int) and population > 0:
        summary.population = population
        sources.append("worldpop")

    if isinstance(osm, tuple):
        summary.settlements, summary.health_facilities = osm
        sources.append("openstreetmap")

    summary.sources = sources
    log.info(
        "exposure gathered",
        extra={
            "sources": sources,
            "population": summary.population,
            "cropland_ha": summary.cropland_hectares,
        },
    )
    return summary


# --------------------------------------------------------------------------- #
# ESA WorldCover — real land classification
# --------------------------------------------------------------------------- #


async def _worldcover_fractions(bbox: BBox) -> dict[str, float]:
    """Fraction of the AOI in each class we care about.

    WorldCover is a single-band classified raster: each pixel holds a class
    code, so a windowed read plus a value count gives exact fractions.
    """
    # Imported here rather than at module scope so the Oracle stays importable
    # without GDAL/rasterio installed — only this path actually reads rasters.
    import numpy as np

    from app.eo import cog, stac
    from app.eo.cog import CogReadError

    scenes = await stac.search_worldcover(bbox)
    if not scenes:
        log.info("no WorldCover tile for AOI")
        return {}

    # Try EVERY returned tile, not just the first.
    #
    # WorldCover is a 3-degree tiled global mosaic, and STAC returns every tile whose bounds
    # intersect the AOI's — including one the AOI only touches at the edge. Measured over Kano:
    # STAC returned two tiles, and for the first the computed window was `row_off=36000` on a
    # 36000-row raster, i.e. entirely outside it, so `cog.read_window` clipped it to nothing and
    # raised. Taking `scenes[0]` meant a coin-flip on tile order decided whether exposure worked —
    # and when it lost, `worldcover read failed` appeared on every cycle while the SECOND tile held
    # the data all along.
    #
    # `map` is the class raster and `data` the older key; both are categorical, so `band="map"`
    # selects nearest-neighbour resampling in `cog` (see `CATEGORICAL_BANDS`) — bilinear here would
    # interpolate between land-cover CODES and invent classes that do not exist.
    grid = None
    for scene in scenes:
        asset = scene.asset("map") or scene.asset("data")
        if asset is None:
            continue
        try:
            grid = await cog.read_band(
                asset.href,
                bbox,
                out_size=settings.exposure_tile_size,
                band="map",
            )
        except CogReadError as exc:
            log.debug(
                "worldcover tile did not cover the AOI; trying the next",
                extra={"item": scene.item_id, "error": describe(exc)[:90]},
            )
            continue
        if np.isfinite(grid).any():
            break
        grid = None

    if grid is None:
        log.warning(
            "no WorldCover tile covered this AOI",
            extra={"tiles_tried": len(scenes)},
        )
        return {}

    valid = grid[np.isfinite(grid)]
    if valid.size == 0:
        return {}

    codes = valid.astype("int16")
    total = float(codes.size)
    return {
        "cropland": float(
            np.count_nonzero(codes == settings.worldcover_cropland_class) / total
        ),
        "water": float(
            np.count_nonzero(codes == settings.worldcover_water_class) / total
        ),
        "builtup": float(
            np.count_nonzero(codes == settings.worldcover_builtup_class) / total
        ),
    }


# --------------------------------------------------------------------------- #
# Copernicus DEM — where water collects
# --------------------------------------------------------------------------- #


async def _dem_lowland_fraction(bbox: BBox) -> float:
    """Share of the AOI materially below its own median elevation.

    Relative rather than absolute: a floodplain at 20 m and a highland valley at
    600 m both concentrate water in their local depressions, and an absolute
    threshold would only ever describe one of them.
    """
    import numpy as np

    from app.eo import cog, stac
    from app.eo.cog import CogReadError

    scenes = await stac.search_dem(bbox)
    if not scenes:
        log.info("no DEM tile for AOI")
        raise LookupError("no DEM coverage")

    asset = scenes[0].asset("data") or scenes[0].asset("map")
    if asset is None:
        raise LookupError("DEM scene has no data asset")

    try:
        elevation = await cog.read_band(
            asset.href, bbox, out_size=settings.exposure_tile_size
        )
    except CogReadError as exc:
        log.warning("dem read failed", extra={"error": describe(exc)})
        raise LookupError(str(exc)) from exc

    valid = elevation[np.isfinite(elevation)]
    if valid.size == 0:
        raise LookupError("DEM window is entirely nodata")

    median = float(np.median(valid))
    threshold = median - settings.dem_lowland_offset_m
    return float(np.count_nonzero(valid < threshold) / valid.size)


# --------------------------------------------------------------------------- #
# WorldPop
# --------------------------------------------------------------------------- #


def _lenient_json(body: str) -> dict:
    """Parse the FIRST JSON value in a body and ignore whatever follows.

    ## Why this is needed, verified against the live API

    `api.worldpop.org` emits valid JSON and then appends **PHP warning HTML**:

        {
            "status": "created", "taskid": "ec98015f-…"
        }<br />
        <b>Warning</b>:  Trying to access array offset on value of type bool in
        <b>/srv/www/api.worldpop.org/html/app/ServicesController.php</b> on line <b>278</b>

    `response.json()` therefore raises `JSONDecodeError: Extra data: line 7 column 2
    (char 152)` — the exact error seen on `worker-1`. The JSON itself is complete and
    correct; only the trailing diagnostics break a strict parser.

    `raw_decode` reads one value and reports where it stopped, which is precisely the
    tolerance needed. Deliberately NOT a regex or a `split("<")`: the response may
    legitimately contain `<` inside a string, and a decoder that understands JSON
    grammar cannot be fooled by that.
    """
    return json.JSONDecoder().raw_decode(body.lstrip())[0]


async def _worldpop_population(bbox: BBox) -> int:
    """Population inside the footprint, or 0 when WorldPop cannot answer.

    ## The API ignores `runasync=false`, so this follows the task

    Verified live: `/services/stats` returns `{"status": "created", "taskid": …}` whether or
    not `runasync=false` is sent — there is no synchronous mode. The previous code read
    `data.total_population` from that first response, which never contains it, so **every
    lookup returned 0** even when the request succeeded. `_exposure_term` then treated the
    footprint as unknown, which is the honest reading but not the available one.

    So the task id is polled. Bounded deliberately: the Analyst is the slow stage already and
    population is a *modulating* input, never a hazard trigger — it must not hold a scan open.
    On timeout the answer is 0, which `ExposureSummary.sources` reports as WorldPop simply not
    being in the list. Absent is not zero: `_exposure_term` returns 0 for unknown rather than
    treating unknown as an empty footprint.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(
                f"{settings.worldpop_api_url.rstrip('/')}/services/stats",
                params={
                    "dataset": settings.worldpop_dataset,
                    "year": settings.worldpop_year,
                    "geojson": bbox_geojson(bbox),
                    "runasync": "false",
                },
            )
            response.raise_for_status()
            first = _lenient_json(response.text)

            # Answered outright — kept because the API contract may tighten, and this is the
            # cheap path when it does.
            direct = (first.get("data") or {}).get("total_population")
            if direct is not None:
                return int(float(direct or 0))

            task_id = first.get("taskid")
            if not task_id:
                log.warning(
                    "worldpop returned neither a population nor a task id",
                    extra={"status": first.get("status")},
                )
                return 0

            for _ in range(_WORLDPOP_POLL_ATTEMPTS):
                await asyncio.sleep(_WORLDPOP_POLL_SECONDS)
                task = await client.get(
                    f"{settings.worldpop_api_url.rstrip('/')}/tasks/{task_id}"
                )
                task.raise_for_status()
                payload = _lenient_json(task.text)

                if payload.get("status") == "finished":
                    data = payload.get("data") or {}
                    return int(float(data.get("total_population", 0) or 0))
                if payload.get("error"):
                    log.warning(
                        "worldpop task failed",
                        extra={"error_message": payload.get("error_message")},
                    )
                    return 0

            # Not an error — the free tier is genuinely slow, and observed staying "created"
            # well past this budget. Logged at INFO so it does not read as a fault.
            log.info(
                "worldpop task did not finish within the poll budget; population unknown",
                extra={"task_id": task_id},
            )
            return 0
    except Exception as exc:
        log.warning("worldpop lookup failed", extra={"error": describe(exc)})
        return 0


# --------------------------------------------------------------------------- #
# OpenStreetMap
# --------------------------------------------------------------------------- #


async def _osm_counts(bbox: BBox) -> tuple[int, int]:
    """Settlements and health facilities, counted independently.

    Two queries rather than one. A single combined `out count` returns only a
    union total, which previously had to be split by an invented ratio.
    """
    settlements, facilities = await asyncio.gather(
        _overpass_count(bbox, 'node["place"~"city|town|village|hamlet"]'),
        _overpass_count(bbox, 'node["amenity"~"hospital|clinic|doctors"]'),
        return_exceptions=True,
    )
    return (
        settlements if isinstance(settlements, int) else 0,
        facilities if isinstance(facilities, int) else 0,
    )


async def _overpass_count(bbox: BBox, selector: str) -> int:
    query = (
        "[out:json][timeout:25];"
        f"({selector}({bbox.south},{bbox.west},{bbox.north},{bbox.east}););"
        "out count;"
    )
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                settings.osm_overpass_url, data={"data": query}
            )
            response.raise_for_status()
            elements = response.json().get("elements", [])
    except Exception as exc:
        log.warning("overpass query failed", extra={"error": describe(exc)})
        return 0

    if not elements:
        return 0
    # `out count` returns one synthetic element carrying the totals as tags.
    tags = elements[0].get("tags", {})
    return int(tags.get("total") or tags.get("nodes") or 0)


# `area_hectares` and `bbox_geojson` live in `app.eo.geometry` — they are
# dependency-free and shared with the rainfall chain, which must not pull in
# the geospatial stack.
