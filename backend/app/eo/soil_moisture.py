"""Root-zone soil wetness from NASA SMAP, via OPeNDAP.

## The question this answers that nothing else could

`soil.py` reports what the ground is MADE OF — clay against sand, a property that has not changed in
ten thousand years. `rainfall.py` reports what FELL ON IT. Neither reports the state that actually
decides an irrigation call: **how much water is in the soil right now**.

The gap was doing real damage to Track A advisories. Two fields with identical NDVI and identical
weekly rainfall need opposite advice depending on their starting wetness — one is at field capacity
and another irrigation drowns the roots, the other is near wilting point and a day's delay costs
yield. Inferring that from rainfall alone cannot work, because the same 20 mm lands on soil that was
already saturated or already parched and the outcome differs completely.

SMAP measures it directly, with an L-band radiometer that sees through cloud and through canopy.

## Why `SPL3SMP_E` (9 km) and not `SPL2SMAP_S` (3 km)

The 3 km active-passive product is the higher resolution, and it does cover Nigeria. Measured on
2026-08-11: its newest granule over our AOIs was **2026-06-28** — six weeks stale, because it depends
on Sentinel-1 overlap that is no longer routinely produced. A six-week-old soil moisture reading is
not soil moisture; it is history.

`SPL3SMP_E` is enhanced-gridded 9 km, global, daily, and was **two days behind** on the same
measurement. For a weekly irrigation decision, currency beats resolution — and a Nigerian smallholder
plot is far smaller than either cell, so both are already an area average.

## Why the projection is done properly rather than linearly

SMAP ships on **EASE-Grid 2.0** (EPSG:6933), a cylindrical **equal-AREA** projection. Latitude is
therefore *not* linear in row index. A linear `(90 - lat) / 180 * n_rows` guess — which is the
obvious thing to write and what this adapter first did — put Kano at row 704 when the correct row is
643, and the granule's own `latitude` variable read back **7.61 degrees N for a cell requested at
11.96**. That is a ~480 km error, and it fails silently: the wrong cell still returns a physically
plausible number, so nothing downstream can tell it is the wrong place.

So `_grid_cell` transforms through `pyproj` (already a dependency, used by rasterio) and
`_verified_cell` then reads the granule's own coordinate arrays back and **rejects the sample if the
returned position is not within `MAX_LOCATION_ERROR_DEG` of what was asked for**. Getting the
projection right is necessary; proving it per-request is what makes it trustworthy.

## Degradation

Returns an unavailable `SoilMoisture` on every failure path — no token, no granule, all-fill cells, a
coordinate mismatch. The Oracle treats unknown wetness as unknown, never as dry, for the same reason
`ExposureSummary.sources` being empty means "unknown" rather than "nobody lives there". An invented
soil-moisture figure would drive a confident irrigation instruction from no measurement at all.
"""

from __future__ import annotations

import asyncio
import math
from urllib.parse import quote

import httpx

from app.config import settings
from app.eo import auth
from app.logging_config import get_logger
from app.models.schemas import BBox, SoilMoisture

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(60.0, connect=15.0)

#: EASE-Grid 2.0 global 9 km grid, as published by NSIDC for the SMAP enhanced products.
#: 3856 columns x 1624 rows. These are grid constants, not tuning knobs — they belong to the
#: product's definition and a wrong value silently reads the wrong place on Earth.
EASE2_COLUMNS = 3856
EASE2_ROWS = 1624
EASE2_CELL_METRES = 9008.055210146
EASE2_ORIGIN_X = -17367530.45
EASE2_ORIGIN_Y = 7314540.83

#: Half a cell is ~4.5 km, so a correct lookup can be off by up to ~0.06 degrees purely from
#: quantisation. Anything beyond this means the index arithmetic is wrong, not merely coarse —
#: the linear-approximation bug this guard exists to catch produced a 4.35 degree error.
MAX_LOCATION_ERROR_DEG = 0.15

#: How many granules to walk back through when the newest does not cover the cell.
#:
#: SMAP revisits the equator every 2-3 days, so four candidates spans more than one full cycle.
#: Bounded because each miss costs three range requests, and an unbounded walk on a real outage
#: would turn one slow assessment into a very slow one.
MAX_GRANULES_TO_TRY = 4

#: The AM (descending, ~6am local) overpass. Preferred over PM because near-dawn the soil and
#: canopy are closest to thermal equilibrium, which is the assumption the retrieval is built on.
SMAP_GROUP = "Soil_Moisture_Retrieval_Data_AM"

#: Fill is -9999. Volumetric water content is physically bounded to [0, 1] — m3 of water per m3 of
#: soil — so anything outside that is not a measurement.
_VALID_MIN = 0.0
_VALID_MAX = 1.0

#: Typical wilting point and field capacity for the loamy soils dominant across Nigeria's cropped
#: belt. These are wide agronomic bands, not a per-soil calculation: a genuine per-texture curve
#: needs van Genuchten parameters SoilGrids does not serve, and the honest band is more useful than
#: a precise-looking number derived from constants we do not have.
WILTING_POINT = 0.12
FIELD_CAPACITY = 0.32
SATURATION = 0.45


def _grid_cell(latitude: float, longitude: float) -> tuple[int, int]:
    """`(row, column)` in the EASE-Grid 2.0 9 km global grid.

    Via pyproj rather than arithmetic: EASE2 is equal-area, so a linear map from latitude to row is
    wrong everywhere except the standard parallel. See the module docstring for what that cost.
    """
    from pyproj import Transformer

    transformer = Transformer.from_crs("EPSG:4326", "EPSG:6933", always_xy=True)
    x, y = transformer.transform(longitude, latitude)
    column = int((x - EASE2_ORIGIN_X) // EASE2_CELL_METRES)
    row = int((EASE2_ORIGIN_Y - y) // EASE2_CELL_METRES)
    return (
        max(0, min(EASE2_ROWS - 1, row)),
        max(0, min(EASE2_COLUMNS - 1, column)),
    )


def _parse_dap_csv(body: str) -> list[float]:
    """Every numeric cell in a DAP4 CSV response, fill values included.

    Deliberately does NOT filter: the caller needs to distinguish "the cell is fill" from "the cell
    is dry", and `_parse_dap_csv_mean` in `rainfall.py` collapses that distinction because for
    rainfall a fill and a zero are both "no rain measured". Here they are not — see `soil_moisture`.
    """
    values: list[float] = []
    for line in (body or "").splitlines():
        if not line.startswith("/"):
            continue
        for cell in line.split(",")[1:]:
            try:
                values.append(float(cell.strip()))
            except ValueError:
                continue
    return values


async def _recent_granules(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> list[tuple[str, str]]:
    """`(date_label, opendap_href)` for recent SMAP granules, newest first.

    ## Why a LIST and not just the newest

    **SMAP's swath does not cover the same cells every day.** Measured over three consecutive
    granules at Kano's grid cell:

        2026-08-10   latitude = -9999   (not overflown)
        2026-08-09   latitude = 11.98   (covered)
        2026-08-08   latitude = -9999   (not overflown)

    The original adapter read only the newest granule, so soil moisture resolved roughly one day
    in three and reported "unavailable" the rest of the time — which downstream reads as "no
    measurement" and silently withdraws the irrigation advice. Walking back a few days turns an
    intermittent signal into a usable one, at the cost of one extra request on a miss.

    CMR is asked rather than the filename constructed, for the reason `rainfall._imerg_granules`
    documents: the granule name carries a revision number (`_R19240_001`) that advances without
    notice, so any hand-built URL eventually 404s.
    """
    try:
        response = await client.get(
            f"{settings.cmr_search_url.rstrip('/')}/granules.json",
            params={
                "short_name": settings.smap_short_name,
                # A few spare: SMAP publishes with a ~2-day lag and an occasional day is missing
                # entirely after a spacecraft event.
                # Enough to cover a gap in the swath — see `_recent_granules`. SMAP's own
                # revisit is 2-3 days at the equator, so five candidates spans more than one
                # full cycle without becoming a long serial walk on a total outage.
                "page_size": str(MAX_GRANULES_TO_TRY + 2),
                "sort_key": "-start_date",
            },
            headers=headers,
        )
        response.raise_for_status()
        entries = response.json().get("feed", {}).get("entry", [])
    except Exception as exc:  # noqa: BLE001
        log.warning("SMAP granule search failed", extra={"error": str(exc)})
        return None

    found: list[tuple[str, str]] = []
    for entry in entries:
        for link in entry.get("links", []):
            href = link.get("href", "")
            # Same host guard as IMERG: a CMR index change must not silently redirect our reads.
            if settings.imerg_opendap_host in href and "granules" in href:
                found.append((entry.get("time_start", "")[:10], href))
                break
    return found


async def _read_variable(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    href: str,
    variable: str,
    row: int,
    column: int,
) -> list[float]:
    """One variable over a 2x2 block at `(row, column)`.

    A 2x2 rather than a single cell because a single-index DAP4 constraint returns a header with no
    data row on this server — the same quirk `rainfall._imerg_antecedent` documents. The block also
    gives a fallback neighbour when the exact cell is fill, which over coastal AOIs is common.
    """
    constraint = quote(
        f"/{SMAP_GROUP}/{variable}[{row}:{row + 1}][{column}:{column + 1}]", safe="/"
    )
    try:
        response = await client.get(f"{href}.dap.csv?dap4.ce={constraint}", headers=headers)
        response.raise_for_status()
        return _parse_dap_csv(response.text)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "SMAP variable read failed",
            extra={"variable": variable, "error": str(exc)},
        )
        return []


async def soil_moisture(bbox: BBox) -> SoilMoisture:
    """Volumetric soil moisture at the AOI centroid, from the most recent overpass that saw it.

    Sampled at the centroid rather than averaged across the AOI: a subscriber plot is a few hundred
    metres across and one 9 km cell covers it entirely, so a "block average" would be averaging the
    same measurement with its neighbours and reporting a smoother number than was observed.

    ## Walks back through granules until one covers this cell

    SMAP's swath does not overfly the same place every day — measured, Kano's cell was covered on
    2026-08-09 and empty on both 08-08 and 08-10. Reading only the newest granule therefore returned
    "unavailable" about two days in three, which downstream withdraws the irrigation advice as
    though nothing had been measured. See `_recent_granules`.
    """
    headers = auth.earthdata_headers()
    if not headers:
        log.info("SMAP skipped: no NASA_EARTHDATA_TOKEN configured")
        return SoilMoisture()

    longitude, latitude = bbox.centroid
    row, column = _grid_cell(latitude, longitude)

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        granules = await _recent_granules(client, headers)
        if not granules:
            return SoilMoisture()

        for observed_date, href in granules[:MAX_GRANULES_TO_TRY]:
            reading = await _read_granule(
                client, headers, href, observed_date, row, column, latitude, longitude
            )
            if reading is not None:
                return reading

        log.info(
            "no recent SMAP granule covers this cell",
            extra={"row": row, "column": column, "tried": len(granules[:MAX_GRANULES_TO_TRY])},
        )
        return SoilMoisture()


async def _read_granule(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    href: str,
    observed_date: str,
    row: int,
    column: int,
    latitude: float,
    longitude: float,
) -> SoilMoisture | None:
    """One granule's reading for one cell, or **None when this granule did not see it**.

    None rather than an unavailable `SoilMoisture`, deliberately: the caller has more granules to
    try, and the two states are different — "this pass missed the cell" is not "there is no
    measurement". Returning an unavailable reading here would end the walk on the first gap.
    """
    if True:
        # Concurrent: three independent range reads against the same granule.
        moisture, latitudes, longitudes = await asyncio.gather(
            _read_variable(client, headers, href, "soil_moisture", row, column),
            _read_variable(client, headers, href, "latitude", row, column),
            _read_variable(client, headers, href, "longitude", row, column),
        )

    if not moisture:
        return None

    # ## The location proof
    #
    # The granule carries its own coordinates, so the cell can confirm where it is rather than
    # being trusted. This is the guard that would have caught the linear-index bug immediately
    # instead of after a plausible-looking 0.264 was read from 480 km away.
    valid_coordinates = [
        (la, lo)
        for la, lo in zip(latitudes, longitudes, strict=False)
        if -90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0
    ]
    if not valid_coordinates:
        # The commonest outcome, and NOT an error: this pass simply did not overfly the cell.
        # Debug rather than warning, because at one useful pass in three a warning per attempt
        # buries the log in normal behaviour.
        log.debug(
            "SMAP granule does not cover this cell; trying an earlier one",
            extra={"row": row, "column": column, "date": observed_date},
        )
        return None

    error_deg = min(
        math.hypot(la - latitude, lo - longitude) for la, lo in valid_coordinates
    )
    if error_deg > MAX_LOCATION_ERROR_DEG:
        log.error(
            "SMAP grid lookup landed in the wrong place; refusing the sample",
            extra={
                "requested_lat": round(latitude, 4),
                "requested_lon": round(longitude, 4),
                "error_deg": round(error_deg, 3),
                "row": row,
                "column": column,
            },
        )
        # Deliberately an unavailable READING rather than None: the grid is identical in every
        # granule, so a projection error will repeat on all of them. Walking on would turn a
        # code bug into a slow, silent "no data" instead of the loud refusal it should be.
        return SoilMoisture()

    # Fill is -9999 and volumetric water content cannot exceed 1. Excluded rather than averaged in,
    # the same NaN-is-not-zero rule `eo/indices.py` enforces: one fill cell averaged into a 2x2
    # would report roughly -2500 m3/m3.
    measured = [v for v in moisture if _VALID_MIN <= v <= _VALID_MAX]
    if not measured:
        log.debug(
            "SMAP cell is all fill for this overpass; trying an earlier one",
            extra={"row": row, "column": column, "date": observed_date},
        )
        return None

    value = sum(measured) / len(measured)
    log.info(
        "SMAP soil moisture measured",
        extra={
            "value": round(value, 4),
            "date": observed_date,
            "cells": len(measured),
            "location_error_deg": round(error_deg, 3),
        },
    )
    return SoilMoisture(
        volumetric=value,
        observed_date=observed_date,
        location_error_deg=error_deg,
        available=True,
    )
