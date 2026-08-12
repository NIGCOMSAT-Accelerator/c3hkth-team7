"""Malaria baseline from the Malaria Atlas Project.

The malaria arm of SHELTER's cascade only makes sense where the parasite
already circulates. Standing water breeds *Anopheles*, but vectors without a
reservoir of infection don't produce an outbreak — so flooding a non-endemic
district is an agricultural emergency, not a public-health one.

This module supplies that gate. Without it the pipeline would attach a malaria
warning to every persistent flood anywhere, which is exactly the kind of
over-alerting that gets a warning service muted.

MAP's GeoServer is keyless. A failure returns an unavailable baseline, and the
Oracle then declines to assert the malaria cascade rather than guessing.
"""

from __future__ import annotations

import httpx

from app.config import settings
from app.logging_config import describe, get_logger
from app.models.schemas import BBox, HealthBaseline

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


async def malaria_baseline(bbox: BBox) -> HealthBaseline:
    """*P. falciparum* parasite rate at the AOI, 2–10 age band.

    Uses a WMS `GetFeatureInfo` point query against the MAP raster layer — the
    lightest way to sample one pixel without downloading a national coverage.

    NOTE: written from the GeoServer WMS contract; not validated against a live
    MAP response. Failure is handled and degrades the cascade to "not asserted".

    ## Upstream status, verified 2026-08-12

    `data.malariaatlas.org/geoserver` returns **504 Gateway Time-out** for every request —
    `GetCapabilities`, `GetFeatureInfo`, and the `GetMap` tile requests MAP's own portal
    makes. The host itself serves 200; only the GeoServer path is down. That is an upstream
    outage, not a fault here, and it is why `worker-1` logs `malaria atlas lookup failed` on
    every cycle. The layer name was *also* stale (MAP version-prefixes them and retires the
    old ones — see `config.malaria_atlas_layer`), so this would have failed on a healthy
    server too; both are now correct and the request will start answering when MAP returns.

    `MALARIA_ATLAS_ENABLED=false` skips it in the meantime. Returning the same unavailable
    baseline as the failure path is what makes that switch safe: the malaria cascade is
    gated on `available and endemic`, so disabling this asserts nothing rather than
    asserting non-endemic.
    """
    if not settings.malaria_atlas_enabled:
        log.debug("malaria atlas disabled; cascade will not be asserted")
        return HealthBaseline()

    lon, lat = bbox.centroid
    # A minimal bbox around the point; GetFeatureInfo needs a viewport, and we
    # query its centre pixel.
    delta = 0.05
    params = {
        "service": "WMS",
        "version": "1.1.1",
        "request": "GetFeatureInfo",
        "layers": f"{settings.malaria_atlas_workspace}:{settings.malaria_atlas_layer}",
        "query_layers": f"{settings.malaria_atlas_workspace}:{settings.malaria_atlas_layer}",
        "srs": "EPSG:4326",
        "bbox": f"{lon - delta},{lat - delta},{lon + delta},{lat + delta}",
        "width": "3",
        "height": "3",
        "x": "1",
        "y": "1",
        "info_format": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(
                f"{settings.malaria_atlas_url.rstrip('/')}"
                f"/{settings.malaria_atlas_workspace}/ows",
                params=params,
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        # `describe` rather than `str(exc)`: the failure that actually happens here is a
        # timeout, and every `httpx` timeout class stringifies to the empty string — which is
        # how this shipped as `{"msg": "malaria atlas lookup failed", "error": ""}` in the
        # runtime logs, a warning that named the source and then withheld the reason.
        log.warning(
            "malaria atlas lookup failed; malaria cascade not asserted",
            extra={"error": describe(exc)},
        )
        return HealthBaseline()

    value = _extract_rate(payload)
    if value is None:
        return HealthBaseline()

    baseline = HealthBaseline(
        malaria_pfpr=value,
        endemic=value >= settings.malaria_endemic_threshold,
        available=True,
    )
    log.info(
        "malaria baseline resolved",
        extra={"pfpr": round(value, 4), "endemic": baseline.endemic},
    )
    return baseline


def _extract_rate(payload: dict) -> float | None:
    """First numeric property from the GetFeatureInfo feature collection.

    MAP names the band differently across layer versions (`GRAY_INDEX`,
    `PfPR2-10`, …), so take the first plausible numeric rather than binding to
    one key that will silently break on the next release.
    """
    for feature in payload.get("features", []):
        for key, raw in (feature.get("properties") or {}).items():
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            # Layers publish either a 0–1 rate or a 0–100 percentage.
            if 0.0 <= value <= 1.0:
                return value
            if 1.0 < value <= 100.0 and "index" in key.lower():
                return value / 100.0
    return None
