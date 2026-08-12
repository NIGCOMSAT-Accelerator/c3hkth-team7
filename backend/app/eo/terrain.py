"""Step 3 — HAND and TWI, the terrain controls on flooding.

**What `lowland_fraction` actually measures today.** `exposure.py` computes the share
of the AOI sitting below its own median elevation. That is a *relative* statistic with
no hydrological content: on a uniform floodplain it returns ~50% regardless of flood
risk, and on a hillside it flags the lower slope as "lowland" even where water never
collects.

**What HAND measures.** Height Above Nearest Drainage — for every pixel, its
elevation minus the elevation of the drainage channel it flows into. That is the
established terrain control on fluvial flooding, because it answers the question that
actually predicts inundation: *how far above the river is this?* A pixel 0.5 m above
its channel floods routinely; one 40 m above does not, however low it sits relative
to the AOI median.

**TWI** (Topographic Wetness Index) complements it by capturing convergence:
`ln(a / tan β)`, where `a` is upslope contributing area and `β` is local slope. High
TWI means a lot of water arrives and leaves slowly — the hollows and valley bottoms
that saturate first. HAND says "close to a channel"; TWI says "water concentrates
here". They are different failure modes and both matter.

**Why this is cheap despite being the most computational step.** Terrain does not
move. Both rasters are derived once per AOI and cached indefinitely, so the amortised
cost across a deployment's lifetime rounds to zero — which is why §8.7 ranks this
third despite it being the only step needing flow accumulation.

**Implementation note.** Flow accumulation uses a D8 algorithm over a
priority-flood-filled DEM. `scipy` and `numpy` only: a full hydrology stack
(`richdem`, `pysheds`, `whitebox`) would add heavy binary dependencies to compute two
rasters we then reduce to four scalars. The D8 approximation is coarser than D-infinity
on smooth slopes, but the output here is an AOI-level *fraction*, not a routed
hydrograph, so the difference is immaterial at this aggregation.

Like `exposure.py`, the `cog`/`stac` imports are **inside** the fetch function, so
this module stays importable without GDAL and the Oracle stays unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage

from app.logging_config import describe, get_logger
from app.models.schemas import BBox

log = get_logger(__name__)

#: HAND below this (metres above nearest drainage) is flood-prone terrain.
#:
#: 5 m is the conventional cut for fluvial flood-hazard mapping on large African
#: river systems. It is deliberately generous: the Niger and Benue produce
#: multi-metre stage changes in a bad year, so a tighter threshold would exclude
#: terrain that genuinely floods.
HAND_FLOOD_THRESHOLD_M = 5.0

#: TWI above this marks convergent, slow-draining ground. ~7–8 is the usual
#: breakpoint for saturation-prone hollows in the literature.
TWI_WET_THRESHOLD = 7.5

#: Drainage network definition: cells whose upslope accumulation exceeds this
#: share of the AOI are treated as channels. 1% is a standard compromise —
#: smaller over-densifies the network on flat ground, larger misses tributaries.
CHANNEL_ACCUMULATION_FRACTION = 0.01

#: Multiple of sqrt(cell count) that peak accumulation must exceed before a
#: drainage network is believed to exist at all.
#:
#: **This guard exists because of a measured bug.** On terrain with no convergence
#: — a uniform planar slope — D8 routing is purely columnar, so peak accumulation
#: grows as `sqrt(area)` while the area-based threshold above grows as `area`. At
#: some window sizes that made *half the grid* satisfy the channel test, every such
#: cell got HAND 0, and `median_hand_m` came out **0.0 for a 500 m hillside** — the
#: exact opposite of the truth, and it would have made a mountainside look like a
#: floodplain.
#:
#: Real convergent terrain concentrates flow into far fewer cells, so peak
#: accumulation is many multiples of `sqrt(area)`. When it is not, the correct
#: reading is that this window drains as sheet flow with no channel in it, and HAND
#: is measured from the window's own base level instead.
MIN_PEAK_ACCUMULATION_RATIO = 3.0


@dataclass(frozen=True)
class TerrainProfile:
    """Hydrological terrain summary for one AOI.

    `available` False means the DEM could not be read, and callers must fall back to
    the elevation-percentile proxy rather than treating 0.0 as "no flood-prone
    terrain" — absent data must not become an implied claim.
    """

    #: Share of the AOI within HAND_FLOOD_THRESHOLD_M of its drainage network.
    #:
    #: **Read this together with `median_hand_m`, never alone.** On terrain with no
    #: flow convergence this fraction can still run high — the cells beside the
    #: window's outflow edge genuinely are at local base level — while the AOI is
    #: plainly not a floodplain. `median_hand_m` is the discriminator that holds in
    #: that case (250 m for a synthetic hillside against 1 m for a floodplain,
    #: stable across window sizes), which is why `_exposure_term` damps this
    #: fraction by median HAND rather than trusting it directly.
    flood_prone_fraction: float = 0.0
    #: Share with TWI above TWI_WET_THRESHOLD — convergent, slow-draining ground.
    wet_index_fraction: float = 0.0
    #: Median HAND in metres. Low values mean the whole AOI sits near channel level.
    median_hand_m: float = 0.0
    #: Mean slope in degrees. Steep terrain sheds water; flat terrain ponds.
    mean_slope_deg: float = 0.0
    available: bool = False
    sources: list[str] = field(default_factory=list)


def _fill_depressions(dem: np.ndarray) -> np.ndarray:
    """Priority-flood depression filling.

    Sinks in a DEM are mostly artefacts of the sensor, and D8 routing terminates in
    them — leaving whole basins with no downstream path and therefore no HAND. This
    raises each sink to the lowest elevation on its rim.

    Implemented as an iterative grey-scale morphological reconstruction: repeatedly
    take the pointwise max of the DEM and the erosion of the current surface. Cheaper
    to reason about than a heap-based priority flood and fast enough at the ~1000²
    windows this operates on.
    """
    filled = dem.copy()
    # Seed the interior high so reconstruction descends to the true surface; the
    # border keeps its own elevation so water can leave the window.
    interior = np.full_like(filled, np.nanmax(dem))
    interior[0, :] = dem[0, :]
    interior[-1, :] = dem[-1, :]
    interior[:, 0] = dem[:, 0]
    interior[:, -1] = dem[:, -1]

    structure = np.ones((3, 3), dtype=bool)
    for _ in range(200):
        eroded = ndimage.grey_erosion(interior, footprint=structure)
        updated = np.maximum(dem, eroded)
        if np.allclose(updated, interior, equal_nan=True):
            break
        interior = updated
    return np.where(np.isfinite(dem), interior, np.nan)


def _d8_flow_accumulation(dem: np.ndarray) -> np.ndarray:
    """Upslope contributing cell count, via D8 steepest descent.

    Cells are processed in descending elevation order, so every cell's own
    accumulation is final before it contributes downstream — a topological sort by
    elevation, which is what makes a single pass correct.
    """
    rows, cols = dem.shape
    accumulation = np.ones((rows, cols), dtype="float64")

    valid = np.isfinite(dem)
    if not valid.any():
        return accumulation

    work = np.where(valid, dem, -np.inf)
    order = np.argsort(work.ravel())[::-1]     # highest first

    # Eight neighbours; diagonals are 1/sqrt(2) further, which matters for slope.
    offsets = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    distances = [np.sqrt(2), 1.0, np.sqrt(2), 1.0, 1.0, np.sqrt(2), 1.0, np.sqrt(2)]

    for flat in order:
        r, c = divmod(int(flat), cols)
        if not valid[r, c]:
            continue

        steepest, target = 0.0, None
        for (dr, dc), dist in zip(offsets, distances, strict=True):
            nr, nc = r + dr, c + dc
            if not (0 <= nr < rows and 0 <= nc < cols) or not valid[nr, nc]:
                continue
            drop = (dem[r, c] - dem[nr, nc]) / dist
            if drop > steepest:
                steepest, target = drop, (nr, nc)

        if target is not None:
            accumulation[target] += accumulation[r, c]

    return accumulation


def _hand(dem: np.ndarray, channels: np.ndarray) -> np.ndarray:
    """Height above nearest drainage.

    Uses a Euclidean-nearest-channel assignment (`distance_transform_edt` with
    `return_indices`) rather than tracing each cell's flow path to a channel. On the
    small windows here the two agree closely, and the exact version is an order of
    magnitude slower for a difference that vanishes once reduced to a fraction.
    """
    if not channels.any():
        # No channel met the accumulation threshold — usually a tiny or very flat
        # AOI. Fall back to the window minimum, which is the local base level.
        base = float(np.nanmin(dem))
        return dem - base

    _, indices = ndimage.distance_transform_edt(~channels, return_indices=True)
    channel_elevation = dem[indices[0], indices[1]]
    hand = dem - channel_elevation
    # Negative HAND means below the nearest channel bed — a DEM artefact. Clamp to
    # zero: such a cell is at channel level, which is what matters here.
    return np.maximum(hand, 0.0)


def terrain_profile_from_dem(dem: np.ndarray, pixel_size_m: float = 30.0) -> TerrainProfile:
    """Compute HAND, TWI and slope statistics from an elevation window.

    Pure function over an array — no I/O — so it is directly unit-testable on
    synthetic terrain, which is how the flood-prone fraction is verified against
    known geometry.
    """
    if dem.size == 0 or not np.isfinite(dem).any():
        return TerrainProfile(available=False)

    finite_ratio = float(np.isfinite(dem).sum() / dem.size)
    if finite_ratio < 0.5:
        # More than half the window is void; any derived statistic would describe
        # the interpolation rather than the terrain.
        log.warning("DEM window mostly void", extra={"valid_fraction": round(finite_ratio, 2)})
        return TerrainProfile(available=False)

    # Interpolate small voids so flow routing has a continuous surface.
    work = dem.astype("float64")
    if not np.isfinite(work).all():
        median = float(np.nanmedian(work))
        work = np.where(np.isfinite(work), work, median)

    filled = _fill_depressions(work)
    accumulation = _d8_flow_accumulation(filled)

    # Only believe there is a channel network if flow actually concentrates. See
    # MIN_PEAK_ACCUMULATION_RATIO — without this check a planar slope reported
    # median HAND 0.0, i.e. a hillside indistinguishable from a floodplain.
    peak_accumulation = float(accumulation.max())
    convergence_floor = MIN_PEAK_ACCUMULATION_RATIO * np.sqrt(dem.size)

    if peak_accumulation < convergence_floor:
        # Sheet flow, no channel. Measure height above the window's own base
        # level, which is the honest local datum.
        channels = np.zeros_like(accumulation, dtype=bool)
    else:
        channel_threshold = max(2.0, CHANNEL_ACCUMULATION_FRACTION * dem.size)
        channels = accumulation >= channel_threshold

    hand = _hand(filled, channels)

    # Slope in degrees from the elevation gradient.
    dy, dx = np.gradient(filled, pixel_size_m)
    slope_rad = np.arctan(np.hypot(dx, dy))
    slope_deg = np.degrees(slope_rad)

    # TWI = ln(a / tan(beta)). tan(beta) floored so flat ground does not divide by
    # zero and produce an infinite wetness index.
    tan_beta = np.maximum(np.tan(slope_rad), 0.001)
    specific_area = accumulation * pixel_size_m
    with np.errstate(divide="ignore", invalid="ignore"):
        twi = np.log(np.maximum(specific_area, 1e-6) / tan_beta)

    valid = np.isfinite(dem)
    return TerrainProfile(
        flood_prone_fraction=float(np.count_nonzero(hand[valid] < HAND_FLOOD_THRESHOLD_M) / max(valid.sum(), 1)),
        wet_index_fraction=float(np.count_nonzero(twi[valid] > TWI_WET_THRESHOLD) / max(valid.sum(), 1)),
        median_hand_m=float(np.median(hand[valid])),
        mean_slope_deg=float(np.mean(slope_deg[valid])),
        available=True,
        sources=["copernicus-dem"],
    )


async def terrain_profile(bbox: BBox) -> TerrainProfile:
    """Fetch the DEM window and derive the terrain profile.

    Cached under a terrain-specific key with a long TTL: elevation does not change,
    so a cache hit here is free accuracy. Never raises — a failed DEM read yields
    `available=False` and the caller keeps the elevation-percentile proxy.
    """
    # Imported here, not at module scope, so the risk layer imports without GDAL.
    from app.eo import cog, stac
    from app.store import cache

    key = cache.terrain_key(bbox)
    if (cached := await cache.get_json(key)) is not None:
        try:
            return TerrainProfile(**cached)
        except (TypeError, ValueError):
            pass    # shape drift in a cached entry must not break the request

    try:
        scenes = await stac.search_dem(bbox)
        if not scenes:
            return TerrainProfile(available=False)
        asset = scenes[0].asset("elevation") or scenes[0].asset("data")
        if asset is None:
            return TerrainProfile(available=False)
        bands = await cog.read_bands({"dem": asset.href}, bbox)
    except Exception as exc:
        log.warning("terrain DEM read failed", extra={"error": describe(exc)})
        return TerrainProfile(available=False)

    profile = terrain_profile_from_dem(bands["dem"])

    if profile.available:
        await cache.set_json(key, profile.__dict__, ttl_seconds=cache.TERRAIN_TTL_SECONDS)
    return profile


# --------------------------------------------------------------------------- #
# Step 1 — JRC Global Surface Water permanent-water baseline
#
# Lives here rather than in `indices.py` because it needs STAC and COG access,
# and `indices.py` is deliberately numpy-only.
# --------------------------------------------------------------------------- #

#: Occurrence percentage above which a pixel counts as permanent water.
#:
#: JRC `occurrence` is the share of valid observations (1984-2021) in which a pixel
#: was water. 50% means "wet more often than not" — a river, lake or reservoir.
#:
#: Deliberately not lower: a seasonal floodplain that is wet 30% of the time is
#: exactly the terrain this service warns about, and masking it out would hide real
#: events. Deliberately not higher: at 90% only the deepest channels are excluded and
#: the braided reaches of the Niger would still read as new inundation.
PERMANENT_WATER_OCCURRENCE_PCT = 50.0


async def permanent_water_mask(bbox: BBox) -> np.ndarray | None:
    """Binary permanent-water mask for an AOI, from JRC Global Surface Water.

    Returns `None` when unavailable — the caller must then report inundation
    *without* the correction rather than assuming zero permanent water. Assuming
    zero is the status quo, so a failure here degrades to current behaviour instead
    of to a wrong answer in a new direction.

    Cached with the terrain TTL: the JRC product is a fixed historical summary, so
    a hit is free accuracy on every subsequent cycle.
    """
    from app.eo import cog, stac
    from app.store import cache

    key = cache.key("permanent-water", cache.terrain_key(bbox).rsplit(":", 1)[-1])
    if (cached := await cache.get_json(key)) is not None:
        try:
            return np.asarray(cached["mask"], dtype="float32")
        except (KeyError, TypeError, ValueError):
            pass

    try:
        scenes = await stac.search_surface_water(bbox)
        if not scenes:
            log.info("no JRC surface-water scene for AOI")
            return None
        asset = scenes[0].asset("occurrence") or scenes[0].asset("data")
        if asset is None:
            return None
        bands = await cog.read_bands({"occurrence": asset.href}, bbox)
    except Exception as exc:
        log.warning("permanent-water read failed", extra={"error": describe(exc)})
        return None

    occurrence = bands["occurrence"]
    # NaN means never observed, which is not the same as never wet — treat as land
    # so it stays in the denominator rather than silently shrinking the AOI.
    mask = np.where(
        np.isfinite(occurrence) & (occurrence > PERMANENT_WATER_OCCURRENCE_PCT), 1.0, 0.0
    ).astype("float32")

    # Only cache small masks; a full-resolution raster would blow the value size
    # limit and push out entries that matter more.
    if mask.size <= 512 * 512:
        await cache.set_json(
            key, {"mask": mask.tolist()}, ttl_seconds=cache.TERRAIN_TTL_SECONDS
        )
    return mask
