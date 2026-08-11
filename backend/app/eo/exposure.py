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

import httpx

from app.config import settings
from app.eo.geometry import area_hectares, bbox_geojson
from app.logging_config import get_logger
from app.models.schemas import BBox, ExposureSummary

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

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
                extra={"item": scene.item_id, "error": str(exc)[:90]},
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
        log.warning("dem read failed", extra={"error": str(exc)})
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


async def _worldpop_population(bbox: BBox) -> int:
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
            data = response.json().get("data", {})
            return int(float(data.get("total_population", 0) or 0))
    except Exception as exc:
        log.warning("worldpop lookup failed", extra={"error": str(exc)})
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
        log.warning("overpass query failed", extra={"error": str(exc)})
        return 0

    if not elements:
        return 0
    # `out count` returns one synthetic element carrying the totals as tags.
    tags = elements[0].get("tags", {})
    return int(tags.get("total") or tags.get("nodes") or 0)


# `area_hectares` and `bbox_geojson` live in `app.eo.geometry` — they are
# dependency-free and shared with the rainfall chain, which must not pull in
# the geospatial stack.
