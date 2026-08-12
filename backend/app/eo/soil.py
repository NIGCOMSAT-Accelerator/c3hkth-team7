"""Soil drainage from SoilGrids.

Two AOIs with identical inundation behave very differently depending on what is
under the water. Heavy clay holds it — roots sit anaerobic for weeks and the
crop is lost. Free-draining sand sheds it in days and the same flood is a
scare rather than a harvest.

That difference is why this exists: it scales how long a waterlogging event
persists, and therefore how severe it is.

ISRIC SoilGrids is a keyless REST service. A failure returns an unavailable
profile with a neutral multiplier, so the risk model is unchanged rather than
skewed.
"""

from __future__ import annotations

import httpx

from app.config import settings
from app.logging_config import describe, get_logger
from app.models.schemas import BBox, SoilProfile

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


async def soil_profile(bbox: BBox) -> SoilProfile:
    """Clay/sand content at the AOI centroid, mapped to a drainage class.

    Queried at the centroid rather than tiled across the AOI: soil texture
    varies over kilometres, not metres, and one point read is enough to
    classify drainage for an area this size.
    """
    lon, lat = bbox.centroid

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(
                f"{settings.soilgrids_base_url.rstrip('/')}/properties/query",
                params=[
                    ("lon", str(lon)),
                    ("lat", str(lat)),
                    ("property", "clay"),
                    ("property", "sand"),
                    ("depth", settings.soilgrids_depth),
                    ("value", "mean"),
                ],
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        log.warning("soilgrids lookup failed", extra={"error": describe(exc)})
        return SoilProfile()

    clay = _extract(payload, "clay")
    sand = _extract(payload, "sand")

    if clay is None and sand is None:
        return SoilProfile()

    clay = clay or 0.0
    sand = sand or 0.0
    profile = SoilProfile(
        clay_g_kg=clay,
        sand_g_kg=sand,
        drainage=_classify(clay, sand),
        available=True,
    )
    log.info(
        "soil profile resolved",
        extra={"clay": clay, "sand": sand, "drainage": profile.drainage},
    )
    return profile


def _classify(clay: float, sand: float) -> str:
    """Map texture to a drainage class.

    Thresholds follow the USDA texture triangle's coarse divisions: clay-rich
    soils drain slowly, sand-dominated soils drain freely, everything between
    is moderate.
    """
    if clay >= settings.soilgrids_heavy_clay_threshold:
        return "impeded"
    if sand >= 650.0:
        return "free"
    return "moderate"


def _extract(payload: dict, name: str) -> float | None:
    """Pull the mean value for one property out of the SoilGrids response.

    SoilGrids returns integers scaled by `d_factor`; dividing by it yields the
    documented unit (g/kg for clay and sand).
    """
    layers = payload.get("properties", {}).get("layers", [])
    for layer in layers:
        if layer.get("name") != name:
            continue
        factor = float(layer.get("unit_measure", {}).get("d_factor", 1) or 1)
        for depth in layer.get("depths", []):
            mean = depth.get("values", {}).get("mean")
            if mean is None:
                continue
            return float(mean) / factor
    return None
