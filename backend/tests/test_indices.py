"""Index maths.

These guard the invariant that matters most in this pipeline: invalid pixels
must stay invalid. A cloud that gets averaged in as a zero turns a blind scene
into a confident "everything is fine".
"""

from __future__ import annotations

import numpy as np
import pytest

from app.eo import indices


def test_ndvi_healthy_vegetation_is_high():
    red = np.array([[0.05]], dtype="float32")
    nir = np.array([[0.45]], dtype="float32")
    assert indices.ndvi(red, nir)[0, 0] == pytest.approx(0.8, abs=1e-5)


def test_ndvi_bare_soil_is_low():
    red = np.array([[0.30]], dtype="float32")
    nir = np.array([[0.35]], dtype="float32")
    assert indices.ndvi(red, nir)[0, 0] < 0.15


def test_normalized_difference_survives_zero_sum():
    """A zero-sum denominator must yield NaN, not an inf or a divide crash."""
    red = np.zeros((2, 2), dtype="float32")
    nir = np.zeros((2, 2), dtype="float32")
    assert np.all(np.isnan(indices.ndvi(red, nir)))


def test_nan_propagates_through_index():
    red = np.array([[0.1, np.nan]], dtype="float32")
    nir = np.array([[0.5, 0.5]], dtype="float32")
    result = indices.ndvi(red, nir)
    assert np.isfinite(result[0, 0])
    assert np.isnan(result[0, 1])


def test_scl_mask_blanks_cloud_classes():
    array = np.ones((1, 4), dtype="float32")
    # 4 = vegetation (keep), 8/9 = cloud, 3 = shadow
    scl = np.array([[4, 8, 9, 3]], dtype="float32")
    masked = indices.apply_scl_mask(array, scl)
    assert np.isfinite(masked[0, 0])
    assert np.all(np.isnan(masked[0, 1:]))


def test_sar_water_mask_flags_low_backscatter():
    vv = np.array([[-20.0, -5.0, np.nan]], dtype="float32")
    mask = indices.sar_water_mask(vv, threshold_db=-16.0)
    assert mask[0, 0] == 1.0  # smooth water reflects away
    assert mask[0, 1] == 0.0  # rough land scatters back
    assert np.isnan(mask[0, 2])


def test_to_db_handles_nonpositive_power():
    linear = np.array([[1.0, 0.0, -1.0]], dtype="float32")
    db = indices.to_db(linear)
    assert db[0, 0] == pytest.approx(0.0)
    assert np.isnan(db[0, 1])
    assert np.isnan(db[0, 2])


def test_fraction_below_excludes_nan_rather_than_counting_it():
    """The regression that matters: a fully-clouded scene must not report 0%
    stress, because that reads as 'healthy' downstream."""
    array = np.array([[0.1, 0.9, np.nan, np.nan]], dtype="float32")
    # Two valid pixels, one below threshold -> 0.5, not 0.25.
    assert indices.fraction_below(array, 0.5) == pytest.approx(0.5)


def test_fraction_below_all_invalid_is_zero():
    array = np.full((2, 2), np.nan, dtype="float32")
    assert indices.fraction_below(array, 0.5) == 0.0


def test_summarize_reports_valid_fraction():
    array = np.array([[0.2, 0.4, np.nan, np.nan]], dtype="float32")
    stats = indices.summarize("ndvi", array)
    assert stats.valid_fraction == pytest.approx(0.5)
    assert stats.mean == pytest.approx(0.3, abs=1e-6)


def test_summarize_empty_is_zero_confidence_not_a_crash():
    stats = indices.summarize("ndvi", np.full((3, 3), np.nan, dtype="float32"))
    assert stats.valid_fraction == 0.0
    assert stats.mean == 0.0


# --------------------------------------------------------------------------- #
# Polygon masking
# --------------------------------------------------------------------------- #


def _strip_ring():
    """A riverside strip — the shape most flood-exposed smallholdings actually have.

    Its envelope is three times its own area, which is the error this masking exists to
    remove.
    """
    return [
        [7.0, 9.0],
        [7.030, 9.004],
        [7.030, 9.006],
        [7.0, 9.002],
        [7.0, 9.0],
    ]


def _strip_bbox():
    from app.models.schemas import BBox

    return BBox(west=7.0, south=9.0, east=7.030, north=9.006)


def test_masking_recovers_the_true_flooded_fraction():
    """A fully flooded strip must read ~100%, not its envelope's ~35%.

    This is the defect the polygon layer exists to fix: `OracleAgent._severity` reads
    `inundated_fraction` directly, so a 2.8x dilution can turn a WARNING into a WATCH for
    a farmer whose whole field is under water.
    """
    ring, bbox = _strip_ring(), _strip_bbox()
    mask = indices.rasterise_ring(ring, bbox, (60, 300))

    # SAR VV in dB: the field is flooded, everything outside it is dry ground.
    vv = np.full((60, 300), -8.0, dtype=np.float32)
    vv[mask] = -20.0

    diluted = indices.fraction_below(vv, -16.0)
    masked = indices.fraction_below(indices.apply_ring_mask(vv, ring, bbox), -16.0)

    assert diluted < 0.45, f"envelope reading should be diluted, got {diluted}"
    assert masked > 0.95, f"masked reading should be near-total, got {masked}"


def test_masked_pixels_are_excluded_not_counted_as_dry():
    """Outside-polygon pixels must become NaN, never zero.

    On an SAR water mask zero means "not water", so writing zeros would understate a flood
    by exactly the envelope ratio — and silently, because the output still looks like a
    valid fraction. This is the same invariant `apply_scl_mask` relies on for cloud.
    """
    ring, bbox = _strip_ring(), _strip_bbox()
    mask = indices.rasterise_ring(ring, bbox, (60, 300))

    vv = np.full((60, 300), -8.0, dtype=np.float32)
    out = indices.apply_ring_mask(vv, ring, bbox)

    assert np.isnan(out).sum() == int((~mask).sum()), (
        "every pixel outside the ring must be NaN"
    )
    assert not np.isnan(out[mask]).any(), "pixels inside the ring must survive"


def test_masking_is_a_noop_without_a_ring():
    """A pin-and-radius AOI has no outline, and that is a supported first-class case."""
    vv = np.full((8, 8), -8.0, dtype=np.float32)
    assert np.array_equal(indices.apply_ring_mask(vv, None, _strip_bbox()), vv)


def test_degenerate_ring_fails_open():
    """A mask that cannot be built must return all-True, never all-False.

    An all-False mask reports zero valid pixels, which the Oracle reads as no hazard — so
    failing open to an imprecise reading is strictly safer than failing closed to silence.
    """
    mask = indices.rasterise_ring([[7.0, 9.0], [7.0, 9.0]], _strip_bbox(), (8, 8))
    assert mask.all()


# --------------------------------------------------------------------------- #
# Reflectance scaling
#
# A normalised difference is scale-invariant but NOT offset-invariant. Sentinel-2 L2A is a pure
# `DN / 10000` so raw-DN indices were always correct; Landsat Collection-2 L2 is
# `DN * 0.0000275 - 0.2`, and the offset breaks it.
# --------------------------------------------------------------------------- #


def test_sentinel2_needs_no_scaling():
    """Pure scale, so raw DN and reflectance give the same NDVI. This is why it never came up."""
    from app.eo.indices import to_reflectance

    raw = np.array([4203.0, 650.0, 14623.0], dtype=np.float32)
    assert np.allclose(to_reflectance(raw, "sentinel-2-l2a"), raw)


def test_an_unknown_collection_is_left_alone():
    """A new optical source is more likely pure-scale than to share Landsat's offset.

    Applying Landsat's numbers on a guess would corrupt every index from that source, which is
    worse than doing nothing and noticing.
    """
    from app.eo.indices import to_reflectance

    raw = np.array([1000.0, 2000.0], dtype=np.float32)
    assert np.allclose(to_reflectance(raw, "some-new-collection"), raw)
    assert np.allclose(to_reflectance(raw, None), raw)


def test_landsat_scaling_materially_raises_ndvi():
    """**The false-drought bug, with the real measured numbers.**

    Raw DN over Ikorodu gave NDVI 0.119; correctly scaled it is 0.246. The stress threshold sits
    between them, so the unscaled figure reported 100% of cropland stressed on a scene that is not
    stressed — a false drought warning telling a farmer to spend money they do not need to spend.
    """
    from app.eo.indices import ndvi, to_reflectance

    # Band means measured from the real 2026-07-23 Landsat scene over Ikorodu.
    red = np.full((4, 4), 15386.3, dtype=np.float32)
    nir = np.full((4, 4), 19245.4, dtype=np.float32)

    unscaled = float(np.nanmean(ndvi(red, nir)))
    scaled = float(
        np.nanmean(ndvi(to_reflectance(red, "landsat-c2-l2"), to_reflectance(nir, "landsat-c2-l2")))
    )

    # Computed from the band means above. The live per-pixel run over the same scene gave
    # 0.119 -> 0.246; these differ slightly because a ratio of means is not the mean of ratios,
    # which is exactly why the assertion below is on the RELATIONSHIP rather than on constants.
    assert unscaled == pytest.approx(0.111, abs=0.01)
    assert scaled == pytest.approx(0.192, abs=0.01)

    # **Doubled.** The offset compresses raw-DN NDVI toward zero, so every Landsat scene reads as
    # far more stressed than it is. `inference.predict_crop_stress` thresholds at NDVI < 0.35, and
    # both figures sit below it here — so on THIS scene the scaling does not flip the verdict, it
    # halves the error. On a healthier scene it is the difference between stressed and not.
    assert scaled > unscaled * 1.6, (
        "scaling must materially raise NDVI, or Landsat scenes systematically over-report stress"
    )


def test_scaled_landsat_reflectance_is_physically_plausible():
    """Surface reflectance lives in 0..1. Raw Landsat DN is ~15000, which is not a reflectance."""
    from app.eo.indices import to_reflectance

    raw = np.array([5734.0, 15386.0, 25743.0], dtype=np.float32)
    out = to_reflectance(raw, "landsat-c2-l2")
    assert np.all(out > 0.0) and np.all(out < 1.0)


def test_scaling_preserves_nodata():
    """NaN means no data and must survive the conversion — arithmetic on NaN stays NaN."""
    from app.eo.indices import to_reflectance

    raw = np.array([15386.0, np.nan, 20000.0], dtype=np.float32)
    out = to_reflectance(raw, "landsat-c2-l2")
    assert np.isnan(out[1])
    assert np.isfinite(out[0]) and np.isfinite(out[2])
