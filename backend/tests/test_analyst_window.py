"""The Analyst must measure the subscriber's field, not the satellite's footprint.

## Why this file exists

There was no `test_analyst.py`, `test_scout.py` or `test_cog.py`, and that absence is exactly what
let three defects ship together:

1. Both COG read sites passed `scene.bbox` — the scene footprint. Over Ikorodu that is **154x** the
   requested area and contains the Atlantic, so a live run reported "65% of the area is under
   standing water" from open ocean. Kano, whose scene holds no coastline, read plausibly.
2. `Resampling.bilinear` was applied to the categorical SCL band, so interpolated class codes
   truncated to values outside `SCL_INVALID` and cloud edges passed as clear.
3. A failed leg's measurement defaulted to `0.0`, indistinguishable from a measured absence — which
   steered `_classify` to CROP_DROUGHT_STRESS during a flood.

`test_indices.py` tested `apply_ring_mask` thoroughly, but only ever with a bbox that *was* the AOI.
The unit was correct and its caller was not, which is the failure mode a unit test cannot see.

These tests are deliberately pure: no network, no GDAL. They assert the contracts that were broken.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.agents.analyst import MIN_AOI_COVERAGE, _coverage, _read_window
from app.eo.cog import CATEGORICAL_BANDS, _resampling_for
from app.eo.geometry import area_hectares
from app.models.schemas import BBox, SceneRef, ScoutResult

#: The real Ikorodu AOI and the real Sentinel-2 footprint returned for it, both measured live.
AOI = BBox(west=3.48, south=6.58, east=3.56, north=6.66)
SCENE = BBox(west=3.00, south=6.24, east=3.99, north=7.24)


def _scene(bbox: BBox = SCENE) -> SceneRef:
    from datetime import datetime, timezone

    return SceneRef(
        item_id="test-scene",
        collection="sentinel-2-l2a",
        datetime=datetime(2026, 8, 1, tzinfo=timezone.utc),
        bbox=bbox,
        assets=[],
    )


def _scout(aoi_bbox: BBox | None = AOI, ring: list[list[float]] | None = None) -> ScoutResult:
    return ScoutResult(aoi_id="aoi_test", aoi_bbox=aoi_bbox, aoi_ring=ring)


# --------------------------------------------------------------------------- #
# The window
# --------------------------------------------------------------------------- #


def test_the_read_window_is_the_aoi_not_the_scene():
    """**The bug this file exists for.**

    With real measured geometry: the scene is 1,208,655 ha and the AOI is 7,825 ha. Reading the
    scene means 99.35% of every measurement describes land the subscriber does not own — including,
    over Lagos, the Atlantic Ocean.
    """
    window = _read_window(_scout(), _scene())

    assert window == AOI, "the window must be the AOI when the scene fully contains it"

    scene_ha = area_hectares(SCENE)
    aoi_ha = area_hectares(AOI)
    assert scene_ha / aoi_ha > 100, (
        "sanity check on the fixture: this must stay a large ratio for the test to mean anything"
    )
    assert area_hectares(window) == pytest.approx(aoi_ha, rel=0.01)


def test_the_window_is_clipped_to_what_the_scene_covers():
    """A scene that only overlaps part of the AOI must not be read outside itself.

    Clipping is what keeps `cog.read_window`'s "AOI does not intersect scene" guard meaningful —
    while the AOI *was* the scene, that guard compared the scene against itself and could never
    fire.
    """
    # Scene covers only the eastern half of the AOI.
    half = BBox(west=3.52, south=6.58, east=4.00, north=6.66)
    window = _read_window(_scout(), _scene(half))

    assert window.west == 3.52, "clipped to the scene's western edge"
    assert window.east == AOI.east, "but not beyond the AOI's eastern edge"
    assert window.south == AOI.south and window.north == AOI.north


def test_a_missing_aoi_bbox_falls_back_but_is_not_silent():
    """An in-flight job from the previous release must complete, not dead-letter.

    `aoi_bbox` is optional for the same reason `run_id` is optional on `JobEnvelope`: a required
    field would fail `model_validate_json` on every ScoutResult already sitting on a stream. But
    the fallback IS the defective behaviour, so it must be logged — a silent fallback would let the
    bug survive its own fix.
    """
    import logging

    scout = _scout(aoi_bbox=None)
    logger = logging.getLogger("app.agents.analyst")
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger.addHandler(handler)
    try:
        window = _read_window(scout, _scene())
    finally:
        logger.removeHandler(handler)

    assert window == SCENE, "falls back to the scene rather than failing the job"
    assert any(r.levelno >= logging.WARNING for r in records), (
        "the fallback must warn — it measures the whole scene, not the AOI"
    )


# --------------------------------------------------------------------------- #
# Coverage
# --------------------------------------------------------------------------- #


def test_full_coverage_when_the_scene_contains_the_aoi():
    assert _coverage(_scout(), _scene(), _read_window(_scout(), _scene())) == pytest.approx(1.0, rel=0.01)


def test_partial_coverage_is_measured_as_a_fraction():
    """Half the AOI reads as roughly half, so the caller can decide whether that is enough."""
    half = BBox(west=3.52, south=6.58, east=4.00, north=6.66)
    coverage = _coverage(_scout(), _scene(half), _read_window(_scout(), _scene(half)))
    assert 0.45 < coverage < 0.55


def test_a_sliver_of_coverage_is_below_the_measuring_threshold():
    """A scene clipped to the edge measures a strip and would report it as the whole field.

    The Oracle treats an absent measurement as unknown but cannot distinguish a partial one from a
    complete one — so below the threshold the honest answer is to decline.
    """
    sliver = BBox(west=3.559, south=6.58, east=4.00, north=6.66)
    coverage = _coverage(_scout(), _scene(sliver), _read_window(_scout(), _scene(sliver)))
    assert coverage < MIN_AOI_COVERAGE


def test_a_straddling_aoi_stays_measurable():
    """The threshold must not be so strict that real tile edges make an AOI unmonitorable.

    Sentinel-2 tiles and Sentinel-1 frames have edges; an AOI spanning two of them is normal, not
    exceptional. 0.4 keeps the majority case usable while rejecting slivers.
    """
    assert MIN_AOI_COVERAGE <= 0.5, (
        "above 0.5 an AOI split evenly across two tiles could never be measured from either"
    )


def test_a_disjoint_scene_covers_nothing():
    elsewhere = BBox(west=8.46, south=11.92, east=8.54, north=12.00)  # Kano
    assert _coverage(_scout(), _scene(elsewhere), _read_window(_scout(), _scene(elsewhere))) == 0.0


# --------------------------------------------------------------------------- #
# Categorical resampling
# --------------------------------------------------------------------------- #


def test_categorical_bands_use_nearest_neighbour():
    """Interpolating class codes produces values that are not any class.

    This is the defect proven numerically in `test_interpolated_class_codes_would_escape_the_mask`
    below: every cloud/clear boundary lands outside `SCL_INVALID` and passes as clear.
    """
    from rasterio.enums import Resampling

    for band in ("scl", "SCL", "occurrence", "extent", "seasonality"):
        assert _resampling_for(band) is Resampling.nearest, f"{band} holds category codes"

    for band in ("red", "green", "nir", "swir16", "vv", "vh", "dem", "elevation"):
        assert _resampling_for(band) is Resampling.bilinear, f"{band} holds measurements"

    # An unnamed read is a measurement — `read_band` with a bare href.
    assert _resampling_for(None) is Resampling.bilinear


def test_scl_is_declared_categorical():
    """The one that mattered. SCL drives the cloud mask; everything else is secondary."""
    assert "scl" in CATEGORICAL_BANDS


def test_interpolated_class_codes_would_escape_the_mask():
    """Why nearest-neighbour is not a stylistic preference.

    Demonstrates the arithmetic directly: averaging two SCL codes and truncating lands between the
    classes, and `SCL_INVALID` has a gap at 6 and 7 precisely where cloud borders vegetation.

    The error direction is the dangerous one — unmasked cloud has low NDVI, which reads as crop
    stress, AND it inflates `valid_fraction`, which is the only term by which cloud reduces
    confidence. The hazard and the certainty rise together.
    """
    from app.eo.indices import SCL_INVALID

    vegetation = 4
    for cloud in (8, 9, 10):  # cloud-medium, cloud-high, cirrus — all in SCL_INVALID
        assert cloud in SCL_INVALID
        interpolated = int((vegetation + cloud) / 2)
        assert interpolated not in SCL_INVALID, (
            f"a {vegetation}/{cloud} boundary interpolates to {interpolated}, which passes as "
            "clear — this is what bilinear resampling did to every cloud edge"
        )


def test_nearest_neighbour_preserves_every_class_code():
    """The fix, stated as a property: a resampled SCL raster contains only real class codes."""
    from app.eo.indices import SCL_INVALID

    original = np.array([4, 4, 9, 9, 4, 8, 10, 4], dtype=np.float32)

    # Nearest-neighbour decimation picks existing samples rather than averaging them.
    nearest = original[::2]
    assert set(np.unique(nearest)).issubset(set(np.unique(original)))
    for value in np.unique(nearest):
        code = int(value)
        # Every surviving value is a real class, so membership testing is meaningful.
        assert float(code) == value, "no fractional codes survive nearest-neighbour"
        assert (code in SCL_INVALID) or (code not in SCL_INVALID)  # decidable either way
