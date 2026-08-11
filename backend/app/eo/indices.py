"""Spectral and radar indices.

Every function takes float32 arrays with NaN for invalid pixels and returns the
same, so masks propagate instead of being averaged away.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    # Type-only import. At runtime this module must stay numpy-only so the Oracle is
    # importable without the geospatial stack — see the module docstring.
    from app.models.schemas import BBox as BBoxLike

import numpy as np

from app.models.schemas import IndexStats

# Sentinel-2 Scene Classification Layer values we treat as unusable.
# 3 cloud shadow, 8 cloud medium, 9 cloud high, 10 thin cirrus, 11 snow/ice.
SCL_INVALID = (0, 1, 3, 8, 9, 10, 11)


def _normalized_difference(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """(a - b) / (a + b), guarding the zero-sum case."""
    denominator = a + b
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(np.abs(denominator) > 1e-6, (a - b) / denominator, np.nan)
    return out.astype("float32")


#: Per-collection reflectance scaling: `(scale, offset)` applied as `DN * scale + offset`.
#:
#: ## Why this exists, and what it cost to find
#:
#: A normalised difference is only scale-invariant when the two bands share a scale AND have no
#: offset. Sentinel-2 L2A is `DN / 10000` — a pure scale, so `(nir - red) / (nir + red)` on raw DN
#: gives the right answer and the pipeline never needed this.
#:
#: Landsat Collection-2 Level-2 is `DN * 0.0000275 - 0.2`. **The offset breaks the invariance.**
#: Measured over Ikorodu on a real scene:
#:
#:     raw DN ratio            NDVI = 0.119
#:     scaled to reflectance   NDVI = 0.246
#:
#: 0.119 is below `NDVI_STRESS_THRESHOLD`; 0.246 is not. So the unscaled figure reported *100% of
#: cropland stressed* on a scene that is not stressed at all — a false drought warning, in the
#: direction that tells a farmer to spend money they did not need to spend.
#:
#: Keyed by STAC collection because that is what the scene carries. An unknown collection gets
#: `(1.0, 0.0)`, i.e. left alone: a new optical source is more likely to be a pure-scale product
#: like Sentinel-2 than to share Landsat's offset, and silently applying Landsat's numbers to it
#: would be worse than doing nothing.
REFLECTANCE_SCALING: dict[str, tuple[float, float]] = {
    # USGS Landsat Collection-2 Level-2 surface reflectance.
    "landsat-c2-l2": (0.0000275, -0.2),
}


def to_reflectance(array: np.ndarray, collection: str | None) -> np.ndarray:
    """Convert raw DN to surface reflectance for collections that need it.

    A no-op for Sentinel-2 and for anything unlisted — see `REFLECTANCE_SCALING`. NaN propagates,
    since arithmetic on NaN stays NaN, so no-data pixels are unaffected.
    """
    if not collection:
        return array
    scale, offset = REFLECTANCE_SCALING.get(collection, (1.0, 0.0))
    if scale == 1.0 and offset == 0.0:
        return array

    out = array * scale + offset

    # Clamp to the physical range of a reflectance.
    #
    # The offset makes small DN values go NEGATIVE — measured on a real Ikorodu scene, DN 5734
    # scales to -0.042. Negative reflectance is not a measurement; it is the atmospheric correction
    # over-subtracting on dark targets (deep water, shadow), which USGS documents as expected.
    #
    # Clamped rather than NaN'd: these are real observations of very dark ground, and discarding
    # them would remove water bodies and shadow from the denominator and bias every fraction. A
    # tiny epsilon rather than exactly 0, so `(nir - red) / (nir + red)` cannot divide by zero when
    # both bands bottom out.
    return np.clip(out, 1e-6, 1.0)


def ndvi(red: np.ndarray, nir: np.ndarray) -> np.ndarray:
    """Vegetation vigour. Healthy canopy ≈ 0.6–0.9; bare soil ≈ 0.1–0.2."""
    return _normalized_difference(nir, red)


def ndwi(green: np.ndarray, nir: np.ndarray) -> np.ndarray:
    """McFeeters water index. Open water > 0.3."""
    return _normalized_difference(green, nir)


def ndmi(nir: np.ndarray, swir16: np.ndarray) -> np.ndarray:
    """Moisture in the canopy. Falls before NDVI does under drought stress,
    which is what buys the extra days of lead time."""
    return _normalized_difference(nir, swir16)


def apply_scl_mask(array: np.ndarray, scl: np.ndarray) -> np.ndarray:
    """Blank out cloud, shadow and snow using the Sentinel-2 SCL band."""
    if scl.shape != array.shape:
        rows = min(scl.shape[0], array.shape[0])
        cols = min(scl.shape[1], array.shape[1])
        array, scl = array[:rows, :cols], scl[:rows, :cols]
    masked = array.copy()
    masked[np.isin(scl.astype("int16"), SCL_INVALID)] = np.nan
    return masked


def sar_water_mask(vv_db: np.ndarray, threshold_db: float = -16.0) -> np.ndarray:
    """Standing water from Sentinel-1 VV backscatter, at a fixed threshold.

    Smooth open water reflects the radar pulse away from the sensor, so it
    returns very low backscatter. The -16 dB cut is the widely used starting
    threshold for C-band VV flood mapping.

    **Prefer `adaptive_water_mask` for new callers.** This fixed cut is retained as
    the final fallback because it cannot fail: it needs no histogram, no bimodality,
    and no successful fit. `adaptive_water_mask` degrades to exactly this.

    Returns a float mask (1.0 water, 0.0 land, NaN invalid) rather than bool so
    it can be averaged directly into a fraction.
    """
    mask = np.where(np.isnan(vv_db), np.nan, (vv_db < threshold_db).astype("float32"))
    return mask.astype("float32")


#: Search bounds for an adaptive water threshold, in dB.
#:
#: Open water in C-band VV essentially never backscatters above -8 dB, and a
#: threshold below -25 dB would classify only the radar noise floor. Constraining
#: the search to this window is what stops Otsu from "successfully" splitting a
#: scene that contains no water at all — on a dry scene the optimal split of
#: land-only backscatter lands outside these bounds and we reject it.
ADAPTIVE_THRESHOLD_BOUNDS_DB = (-25.0, -8.0)

#: Minimum between-class variance ratio for a split to be considered at all.
#:
#: A necessary condition, not a sufficient one — see `MAX_VALLEY_DEPTH` below for
#: why separability alone is not a bimodality test.
MIN_BIMODALITY = 0.35

#: Maximum histogram density at the chosen threshold, relative to the smaller of the
#: two class peaks, for the split to count as a genuine valley.
#:
#: **This guard exists because of a measured false positive.** Otsu's separability
#: measures how *good* a two-class split is, not whether the data is bimodal at all:
#: on a dry, water-free scene (VV ~ N(-7, 1.5)) it bisects the single mode's own
#: lower tail, scores separability 0.54 — comfortably over MIN_BIMODALITY — and
#: reported 24.7% of a bone-dry scene as standing water. That is precisely the
#: silent, confident failure this codebase's fallbacks exist to prevent.
#:
#: The fix is to test the shape rather than the split: in a truly bimodal histogram
#: the chosen threshold sits in a *trough* between two peaks, so the density there is
#: well below both. In a unimodal one it sits on the flank, where density is still
#: high. 0.60 means "the valley floor must be at least 40% lower than the shallower
#: peak", which separates the two cases decisively in testing.
MAX_VALLEY_DEPTH = 0.60

#: Minimum share of pixels on each side of the split.
#:
#: Otsu can carve off a handful of outlier pixels and call it a class. Requiring 0.5%
#: on both sides means a "water" mode must be a real feature of the scene rather than
#: speckle, without being so strict that a genuinely small flood is rejected.
MIN_CLASS_SHARE = 0.005


def otsu_threshold(values: np.ndarray, *, bounds: tuple[float, float] | None = None,
                   bins: int = 256) -> tuple[float | None, float]:
    """Otsu's method plus an explicit bimodality test.

    Returns `(threshold, separability)`. `threshold` is None when the histogram is
    **not genuinely bimodal inside `bounds`**, which is the case that matters: Otsu
    itself always returns a split, so using it unguarded turns a dry scene into a
    confidently-reported flood (see `MAX_VALLEY_DEPTH`).

    Three conditions must all hold, and each rejects a distinct failure:

    1. `separability >= MIN_BIMODALITY` — the split explains real variance.
    2. The threshold sits in a **trough**, not on a flank — the actual bimodality
       test, and the one that catches the dry-scene false positive.
    3. Both classes hold at least `MIN_CLASS_SHARE` of pixels — not speckle.

    One histogram pass plus a smoothing convolution. No iteration, no training data.
    """
    finite = values[np.isfinite(values)]
    if finite.size < 64:
        return None, 0.0

    lo, hi = bounds if bounds is not None else (float(finite.min()), float(finite.max()))
    data_lo, data_hi = float(finite.min()), float(finite.max())
    if data_hi - data_lo < 1e-6:
        return None, 0.0

    counts, edges = np.histogram(finite, bins=bins, range=(data_lo, data_hi))
    centres = (edges[:-1] + edges[1:]) / 2.0

    total = counts.sum()
    if total == 0:
        return None, 0.0

    probability = counts / total
    # Cumulative zeroth and first moments; the classic O(bins) formulation.
    omega = np.cumsum(probability)
    mu = np.cumsum(probability * centres)
    mu_total = mu[-1]

    # Between-class variance for every candidate split.
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (mu_total * omega - mu) ** 2 / (omega * (1.0 - omega))
    between = np.where(np.isfinite(between), between, 0.0)

    # Restrict to physically plausible water thresholds.
    allowed = (centres >= lo) & (centres <= hi)
    # Both classes must be substantial — omega is the cumulative share below the cut.
    allowed &= (omega >= MIN_CLASS_SHARE) & (omega <= 1.0 - MIN_CLASS_SHARE)
    if not allowed.any() or between[allowed].max() <= 0:
        return None, 0.0

    best = int(np.argmax(np.where(allowed, between, -np.inf)))

    total_variance = float(np.sum(probability * (centres - mu_total) ** 2))
    separability = float(between[best] / total_variance) if total_variance > 0 else 0.0
    if separability < MIN_BIMODALITY:
        return None, separability

    # --- the bimodality test proper -------------------------------------------
    # Smooth first: raw histogram noise creates spurious local minima, so an
    # unsmoothed trough test would accept almost anything.
    window = np.ones(9) / 9.0
    smooth = np.convolve(probability, window, mode="same")

    left_peak = float(smooth[:best].max()) if best > 0 else 0.0
    right_peak = float(smooth[best + 1:].max()) if best < len(smooth) - 1 else 0.0
    valley = float(smooth[best])
    shallower_peak = min(left_peak, right_peak)

    if shallower_peak <= 0:
        return None, separability
    if valley / shallower_peak > MAX_VALLEY_DEPTH:
        # Sitting on a flank, not in a trough — unimodal.
        return None, separability

    return float(centres[best]), float(np.clip(separability, 0.0, 1.0))


def adaptive_water_mask(
    vv_db: np.ndarray,
    *,
    permanent_water: np.ndarray | None = None,
    fallback_db: float = -16.0,
) -> tuple[np.ndarray, dict]:
    """Step 1 + step 2 — per-scene water mask with permanent water removed.

    Two corrections to `sar_water_mask`, which are the two cheapest accuracy gains
    available in this codebase:

    **Step 2 — the threshold adapts per scene.** Backscatter is not comparable
    between acquisitions: it shifts with incidence angle across the swath, with wind
    roughening the water surface, and with soil moisture on land. A single global
    -16 dB is therefore wrong somewhere in every scene. Otsu finds the valley
    between the land and water modes *in this scene's own histogram*, which is the
    quantity -16 dB is a national average of.

    **Step 1 — permanent water is subtracted.** A river that is always there and a
    newly flooded field both read as water. Without a baseline, `inundated_fraction`
    counts the Niger itself as inundation on every single pass — a large, constant
    false positive on the hazard that matters most. `permanent_water` (JRC Global
    Surface Water occurrence) marks what was already wet.

    Returns `(mask, diagnostics)`. The diagnostics dict is surfaced in
    `AnalystResult` and then in `evidence`, so an advisory can state that the figure
    excludes permanent water — provenance a reader can check, rather than a number
    to trust.
    """
    threshold, separability = otsu_threshold(vv_db, bounds=ADAPTIVE_THRESHOLD_BOUNDS_DB)

    diagnostics: dict = {
        "method": "otsu",
        "threshold_db": threshold,
        "separability": round(separability, 3),
        "permanent_water_removed": False,
    }

    if threshold is None or separability < MIN_BIMODALITY:
        # Unimodal histogram: either no water, or wall-to-wall water. Otsu's split
        # would be arbitrary, so use the physical constant, which at least fails in
        # a direction predictable from the physics.
        threshold = fallback_db
        diagnostics.update({"method": "fixed-threshold", "threshold_db": fallback_db,
                            "reason": "histogram not bimodal"})

    mask = np.where(np.isnan(vv_db), np.nan, (vv_db < threshold).astype("float32"))

    if permanent_water is not None:
        mask, water_diagnostics = apply_permanent_water(mask, permanent_water)
        diagnostics.update(water_diagnostics)

    return mask.astype("float32"), diagnostics


def apply_permanent_water(
    water: np.ndarray, permanent_water: np.ndarray
) -> tuple[np.ndarray, dict]:
    """Step 1 — remove permanent water from a water mask or probability raster.

    Separated from `adaptive_water_mask` because it applies to **both** inference
    paths: a trained U-Net is just as unable to know that the Niger is always there,
    since the question "is this pixel water?" and the question "is this pixel
    *newly* water?" are different, and only the latter is a hazard.

    Works on a probability raster as well as a binary mask — permanent pixels are
    set to 0.0 either way, which is the correct answer to "probability this is new
    inundation".
    """
    aligned = _align(permanent_water, water)
    if aligned is None:
        return water, {"permanent_water_removed": False, "reason": "shape mismatch"}

    # Zero rather than NaN: those pixels were validly observed and are validly
    # not-newly-flooded, so they belong in the denominator of the fraction. NaN
    # would shrink the denominator and inflate the reported fraction.
    before = float(np.nansum(water))
    out = np.where(aligned > 0, 0.0, water)
    after = float(np.nansum(out))

    return out.astype("float32"), {
        "permanent_water_removed": True,
        "pixels_reclassified": int(round(before - after)),
    }


def _align(source: np.ndarray, reference: np.ndarray) -> np.ndarray | None:
    """Crop two rasters to their common extent, or give up.

    Same defensive shape handling as `apply_scl_mask`: assets from different
    products are frequently a pixel or two apart, and a broadcast error must not
    fail the whole radar leg over a rounding difference in a bbox.
    """
    if source.shape == reference.shape:
        return source
    rows = min(source.shape[0], reference.shape[0])
    cols = min(source.shape[1], reference.shape[1])
    if rows == 0 or cols == 0:
        return None
    out = np.zeros_like(reference)
    out[:rows, :cols] = source[:rows, :cols]
    return out


def to_db(linear: np.ndarray) -> np.ndarray:
    """Sentinel-1 GRD ships linear power; models and thresholds want decibels."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return (10.0 * np.log10(np.where(linear > 0, linear, np.nan))).astype("float32")


def summarize(name: str, array: np.ndarray) -> IndexStats:
    """Collapse an index raster into the stats the risk model consumes."""
    finite = array[np.isfinite(array)]
    total = array.size or 1

    if finite.size == 0:
        return IndexStats(
            name=name, mean=0.0, std=0.0, p10=0.0, p90=0.0, valid_fraction=0.0
        )

    return IndexStats(
        name=name,
        mean=float(np.mean(finite)),
        std=float(np.std(finite)),
        p10=float(np.percentile(finite, 10)),
        p90=float(np.percentile(finite, 90)),
        valid_fraction=float(finite.size / total),
    )


def fraction_below(array: np.ndarray, threshold: float) -> float:
    """Share of *valid* pixels under a threshold. NaNs are excluded, not counted
    as passing — otherwise a fully-clouded scene reports zero stress."""
    finite = array[np.isfinite(array)]
    if finite.size == 0:
        return 0.0
    return float(np.count_nonzero(finite < threshold) / finite.size)


def fraction_above(array: np.ndarray, threshold: float) -> float:
    finite = array[np.isfinite(array)]
    if finite.size == 0:
        return 0.0
    return float(np.count_nonzero(finite > threshold) / finite.size)


# --------------------------------------------------------------------------- #
# Polygon masking
#
# The imagery layer windows a COG by BBOX — that is what `rasterio.windows.from_bounds`
# takes and it does not change. What masking adds is discarding the pixels inside that
# window which fall OUTSIDE the subscriber's actual field.
#
# Measured on realistic shapes, envelope vs true polygon:
#
#     square 1 km field    98.5 ha vs  98.5 ha    1.0x
#     L-shaped field       68.1 ha vs  98.5 ha    1.4x
#     riverside strip      72.9 ha vs 218.8 ha    3.0x
#
# The strip is the shape most flood-exposed smallholdings have — a plot along a watercourse.
# Without masking, two-thirds of the pixels feeding `inundated_fraction` are somebody else's
# land, and a real flood gets diluted toward the threshold. `OracleAgent._severity` reads
# those fractions directly, so the dilution can turn a WARNING into a WATCH.
#
# ## Masked pixels become NaN, and that is the load-bearing detail
#
# `fraction_below` / `fraction_above` / `summarize` all exclude non-finite values rather
# than counting them as passing — the invariant that stops a fully-clouded scene reporting
# 0% stress. Masking therefore writes NaN, which means outside-polygon pixels are EXCLUDED
# from the denominator, exactly like cloud.
#
# Writing zeros instead would be catastrophic and silent: on an SAR water mask, zero means
# "not water", so a strip field would report a third of its true flooded fraction and read
# as safe. There is a regression test for this.
# --------------------------------------------------------------------------- #


def rasterise_ring(
    ring: list[list[float]],
    bbox: BBoxLike,
    shape: tuple[int, int],
) -> np.ndarray:
    """A boolean mask, True inside the ring, matching a windowed read's grid.

    `shape` is `(height, width)` of the array already read — taken from the data rather
    than recomputed, because `cog.read_window` clamps `out_shape` to the window's real
    extent and a mask built from an assumed size would be misaligned by a pixel or two at
    the edges.

    The affine transform maps the bbox onto that grid. `rasterio.transform.from_bounds`
    rather than hand arithmetic: it handles the north-up flip, which is the classic way a
    hand-rolled version ends up mirroring the mask vertically — a failure that looks
    plausible and quietly measures the wrong half of a field.

    Returns all-True when rasterio is unavailable or the ring is degenerate. Failing open
    is deliberate: an unmasked reading is the pre-existing behaviour and is merely less
    precise, whereas an all-False mask would report every field as having no valid pixels,
    which the Oracle would read as no hazard.
    """
    height, width = shape
    if height < 1 or width < 1 or not ring or len(ring) < 4:
        return np.ones(shape, dtype=bool)

    try:
        # Imported here, not at module scope. `app/eo/indices.py` is numpy-only by design so
        # the Oracle stays importable without GDAL — the same reasoning as `exposure.py`
        # importing `cog`/`stac` inside its raster functions.
        from rasterio.features import rasterize
        from rasterio.transform import from_bounds
    except ImportError:  # pragma: no cover — GDAL absent in unit-test environments
        return np.ones(shape, dtype=bool)

    try:
        transform = from_bounds(
            bbox.west, bbox.south, bbox.east, bbox.north, width, height
        )
        mask = rasterize(
            [({"type": "Polygon", "coordinates": [ring]}, 1)],
            out_shape=shape,
            transform=transform,
            fill=0,
            # all_touched=True includes any pixel the ring passes through, rather than only
            # those whose CENTRE is inside. For a smallholding a few pixels across, centre-
            # only sampling discards the boundary and can lose a third of a narrow field —
            # which would reintroduce the very error this function exists to remove.
            all_touched=True,
            dtype="uint8",
        )
        inside = mask.astype(bool)
    except Exception:  # noqa: BLE001 — a mask failure must not fail the assessment
        return np.ones(shape, dtype=bool)

    # A ring smaller than one pixel rasterises to nothing. Fall back to the full window
    # rather than reporting zero valid pixels: `check_monitorable` should have rejected such
    # an AOI at registration, and if one slipped through, an imprecise reading beats none.
    if not inside.any():
        return np.ones(shape, dtype=bool)

    return inside


def apply_ring_mask(
    array: np.ndarray,
    ring: list[list[float]] | None,
    bbox: BBoxLike,
) -> np.ndarray:
    """Set pixels outside the ring to NaN, so they are excluded from every statistic.

    A no-op when `ring` is None — the pin-and-radius case, where the bbox IS the geometry
    and there is nothing to exclude. Callers therefore need no branch.

    Returns a float32 copy rather than mutating in place: the caller may hold the raw band
    for a second index (NDVI and NDMI share B08), and masking one must not corrupt the other.
    """
    if not ring or len(ring) < 4:
        return array

    inside = rasterise_ring(ring, bbox, array.shape)

    out = array.astype(np.float32, copy=True)
    out[~inside] = np.nan
    return out
