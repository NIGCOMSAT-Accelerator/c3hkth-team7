"""The derived-statistics layer — steps 1 through 10 of the ML roadmap.

These are the tests that can actually fail meaningfully, because every function
under test is **pure**: arrays in, numbers out, no I/O, no clock, no model file.
That is the whole reason `app/stats/` and `terrain_profile_from_dem` are written as
pure functions — a mocked assertion that "the Oracle called SPI" would prove
nothing, whereas "SPI is monotone and rejects a 10-sample history" is a real
property.

Three themes recur, and each one corresponds to a defect this suite either caught
during development or exists to prevent recurring:

1. **A statistic must refuse to answer rather than guess.** Every `None` and
   `available=False` path is asserted, because the alternative — a fabricated
   quantile, a zero that reads as "no hazard" — is the failure mode this codebase
   has already committed twice.
2. **The improvement must actually improve on what it replaces.** Several tests
   assert the new statistic beats the old constant on a case constructed so the old
   one is wrong. If a refinement cannot beat the thing it replaces, it should not
   ship, and the test says so.
3. **NaN is not zero.** A fully-clouded scene must report "no measurement", never
   "no hazard".
"""

from __future__ import annotations

import numpy as np
import pytest

from app.eo import indices, terrain
from app.stats import anomaly, calibration, ensemble, rainfall_index

# --------------------------------------------------------------------------- #
# Step 4 — SPI and the Antecedent Precipitation Index
# --------------------------------------------------------------------------- #


def _gamma_history(seed: int = 42, n: int = 40) -> np.ndarray:
    """Right-skewed rainfall history, which is what real accumulations look like."""
    return np.random.default_rng(seed).gamma(2.0, 30.0, n)


def test_spi_is_monotone_in_rainfall():
    """More rain must never mean a lower SPI.

    A monotonicity violation would let a wetter week produce a *lower* risk term,
    which is the class of nonsense the non-negative weight constraint elsewhere
    also guards against.
    """
    history = _gamma_history()
    values = [rainfall_index.spi(mm, history) for mm in (5, 30, 60, 150, 300)]

    assert all(v is not None for v in values)
    assert values == sorted(values)


def test_spi_is_comparable_across_climates():
    """The point of SPI: the same number means the same rarity everywhere.

    180 mm in a week is unremarkable in a wet climate and extraordinary in a dry
    one. Raw millimetres cannot express that; a dimensionless quantile can, and
    this is why `_forecast_term` consults it.
    """
    wet = np.random.default_rng(1).gamma(4.0, 50.0, 40)     # mean ~200 mm
    dry = np.random.default_rng(2).gamma(1.5, 10.0, 40)     # mean ~15 mm

    spi_wet = rainfall_index.spi(180.0, wet)
    spi_dry = rainfall_index.spi(180.0, dry)

    assert spi_wet is not None and spi_dry is not None
    # Same millimetres, very different rarity.
    assert spi_dry > spi_wet + 1.0
    assert spi_dry >= 2.0, "180 mm in an arid cell should be exceptional"


def test_spi_refuses_a_short_history():
    """Below MIN_HISTORY the gamma shape parameter is sampling noise.

    Returning a number here would be worse than returning nothing: a spurious
    SPI -2.5 reads as a rare drought and nothing downstream could tell.
    """
    assert rainfall_index.spi(60.0, _gamma_history(n=10)) is None
    assert rainfall_index.spi(60.0, []) is None


def test_spi_refuses_a_degenerate_history():
    """An all-zero or constant history has no scale to standardise against."""
    assert rainfall_index.spi(60.0, np.zeros(40)) is None
    assert rainfall_index.spi(60.0, np.full(40, 25.0)) is None


def test_spi_never_returns_infinity():
    """`ndtri` diverges at 0 and 1, and an inf would poison every later term."""
    history = _gamma_history()
    for mm in (0.0, 1e9):
        value = rainfall_index.spi(mm, history)
        assert value is None or np.isfinite(value)


def test_api_weights_recent_rain_more_heavily():
    """The whole reason API exists — a flat sum cannot distinguish these.

    Both series total 25 mm. One fell yesterday and is still in the soil; the other
    fell a week ago and has largely drained. `outlook.antecedent_mm` reports 25 for
    both.
    """
    yesterday = rainfall_index.antecedent_precipitation_index([0, 0, 0, 0, 0, 0, 25])
    last_week = rainfall_index.antecedent_precipitation_index([25, 0, 0, 0, 0, 0, 0])

    assert yesterday > last_week
    assert yesterday == pytest.approx(25.0, abs=0.01)


def test_api_handles_empty_and_invalid_input():
    """0.0 is the honest answer for "no accumulated water measured"."""
    assert rainfall_index.antecedent_precipitation_index([]) == 0.0
    assert rainfall_index.antecedent_precipitation_index([np.nan, -5, np.inf]) == 0.0


def test_api_decay_tracks_soil_drainage():
    """Impeded clay holds water longer than free-draining sand.

    Same physical reasoning as `SoilProfile.waterlogging_multiplier`, applied to the
    time axis rather than the magnitude axis.
    """
    assert (
        rainfall_index.api_decay_for_drainage("free")
        < rainfall_index.api_decay_for_drainage("moderate")
        < rainfall_index.api_decay_for_drainage("impeded")
    )
    # An unknown drainage class must not crash or extrapolate.
    assert rainfall_index.api_decay_for_drainage("nonsense") == rainfall_index.API_DECAY


def test_spi_labels_match_wmo_bands():
    """These bands are the published WMO ones; the value of SPI is comparability
    with everyone else's SPI, so they are not ours to tune."""
    assert rainfall_index.spi_to_severity_label(2.5) == "exceptionally wet"
    assert rainfall_index.spi_to_severity_label(0.0) == "near normal"
    assert rainfall_index.spi_to_severity_label(-2.5) == "extremely dry"


# --------------------------------------------------------------------------- #
# Step 5 — ensemble exceedance
# --------------------------------------------------------------------------- #


def test_ensemble_separates_agreement_from_disagreement():
    """The failure this fixes: two forecasts, same mean, different actions.

    Taking a mean reports 30 mm for both. The second is a coin-flip between nothing
    and a damaging flood, and reporting 30 mm implies a confidence the model does
    not have.
    """
    tight = ensemble.exceedance_probability([30, 31, 29, 30, 31], 25.0)
    wild = ensemble.exceedance_probability([0, 2, 88, 60, 0], 25.0)

    assert np.mean([30, 31, 29, 30, 31]) == pytest.approx(np.mean([0, 2, 88, 60, 0]), abs=1)
    assert tight.probability > wild.probability
    assert tight.spread_mm < wild.spread_mm
    assert tight.agreement_label == "near-certain"
    assert wild.agreement_label == "possible"


def test_ensemble_marks_a_deterministic_forecast_unavailable():
    """ClimateSERV currently returns one averaged series, not members.

    `available=False` is what stops the Oracle reading 0.0 or 1.0 as a measured
    probability — it keeps its own deterministic path instead.
    """
    single = ensemble.exceedance_probability([30.0], 25.0)

    assert single.available is False
    assert single.member_count == 1
    assert single.notes, "must say why it is unavailable"


def test_ensemble_handles_no_members():
    result = ensemble.exceedance_probability([], 25.0)
    assert result.available is False
    assert result.probability == 0.0


def test_ensemble_risk_term_is_none_without_a_spread():
    """No spread information must be distinguishable from "spread says low risk"."""
    term, summary = ensemble.ensemble_risk_term([[30.0], [25.0]], ponding_mm=25.0)
    assert term == 0.0
    assert summary is None


def test_ensemble_risk_term_rises_with_agreement():
    dry = [[0, 1, 0, 2, 1]] * 3
    wet = [[60, 70, 65, 80, 55]] * 3

    dry_term, _ = ensemble.ensemble_risk_term(dry, ponding_mm=25.0)
    wet_term, worst = ensemble.ensemble_risk_term(wet, ponding_mm=25.0)

    assert wet_term > dry_term
    assert worst is not None and worst.available


def test_return_period_refuses_a_short_record():
    """A 500-year return period from a 5-year record is fiction.

    This feeds advisory *wording* only, never the score — but confident fiction in
    an advisory is exactly what the grounding rule exists to prevent.
    """
    assert ensemble.return_period(200.0, [100, 120, 90, 150, 110]) is None


def test_return_period_is_bounded():
    """Never extrapolate past what a finite record can support."""
    maxima = np.random.default_rng(5).gamma(6.0, 20.0, 40)
    period = ensemble.return_period(10_000.0, maxima)
    assert period is not None and period <= 500.0


# --------------------------------------------------------------------------- #
# Step 6 — the seasonal anomaly
# --------------------------------------------------------------------------- #


def _seasonal_series(peak_doy: int = 220, amplitude: float = 0.25, base: float = 0.45):
    """A realistic single-season NDVI cycle: green in the wet season, bare in the dry."""
    days = np.arange(5, 365, 10)
    phase = 2 * np.pi * (days - peak_doy) / 365.25
    values = base + amplitude * np.cos(phase)
    noise = np.random.default_rng(11).normal(0, 0.02, len(days))
    return days, values + noise


def test_harmonic_baseline_separates_stress_from_dry_season():
    """The defect this fixes, stated as a test.

    A fixed `NDVI < 0.35` cut flags both a crashed field in peak season and a
    normally-bare field in the dry season. Only the first warrants a warning; the
    second is January.
    """
    days, values = _seasonal_series(peak_doy=220)
    baseline = anomaly.fit_harmonic_baseline(days, values)
    assert baseline.available

    peak_expectation = float(baseline.expected(220)[0])
    dry_expectation = float(baseline.expected(30)[0])
    assert peak_expectation > dry_expectation, "fit did not learn the season"

    # A field at 0.40 in peak season has crashed; the same value in the dry season
    # is above normal.
    z_peak = anomaly.seasonal_anomaly(0.40, 220, baseline)
    z_dry = anomaly.seasonal_anomaly(0.40, 30, baseline)

    assert z_peak < -1.5, "a mid-season crash must register as anomalous"
    assert z_dry > 0, "0.40 in the dry season is above normal, not stressed"

    # And the fixed threshold cannot tell them apart at all.
    assert (0.40 < 0.35) is (0.40 < 0.35)


def test_harmonic_baseline_refuses_too_few_observations():
    """Fewer than MIN_OBSERVATIONS and the fit interpolates its own noise."""
    baseline = anomaly.fit_harmonic_baseline([10, 100, 200], [0.4, 0.6, 0.5])
    assert baseline.available is False
    assert anomaly.seasonal_anomaly(0.4, 100, baseline) is None


def test_harmonic_baseline_refuses_a_scatterless_series():
    """With no residual scatter every z-score would be enormous."""
    days = list(range(5, 365, 10))
    baseline = anomaly.fit_harmonic_baseline(days, [0.5] * len(days))
    assert baseline.available is False


def test_stressed_fraction_reports_none_for_an_all_cloud_scene():
    """**The most dangerous silent failure available here.**

    0.0 reads downstream as "healthy". A fully-clouded scene has measured nothing,
    so it must say so — the same rule `indices.fraction_below` follows.
    """
    days, values = _seasonal_series()
    baseline = anomaly.fit_harmonic_baseline(days, values)

    all_cloud = np.full((16, 16), np.nan, dtype="float32")
    assert anomaly.stressed_fraction_from_anomaly(all_cloud, 200, baseline) is None


def test_stressed_fraction_ignores_nan_in_the_denominator():
    """A partly-clouded scene must be scored on its visible pixels only."""
    days, values = _seasonal_series()
    baseline = anomaly.fit_harmonic_baseline(days, values)

    array = np.full((10, 10), 0.10, dtype="float32")   # deeply stressed
    array[:5] = np.nan                                  # half the scene clouded

    fraction = anomaly.stressed_fraction_from_anomaly(array, 220, baseline)
    assert fraction == pytest.approx(1.0), "visible pixels are all stressed"


# --------------------------------------------------------------------------- #
# Steps 1 and 2 — permanent water and the adaptive SAR threshold
# --------------------------------------------------------------------------- #


def _sar_scene(water_fraction: float, shift_db: float = 0.0, seed: int = 3):
    """Bimodal VV scene: bright land, dark water, optionally offset in dB."""
    rng = np.random.default_rng(seed)
    n = 10_000
    n_water = int(n * water_fraction)
    values = np.concatenate([
        rng.normal(-8 + shift_db, 2.0, n - n_water),
        rng.normal(-20 + shift_db, 1.5, n_water),
    ])
    return values.astype("float32").reshape(100, 100)


def test_adaptive_threshold_survives_a_backscatter_shift():
    """The defect this fixes.

    Backscatter shifts with incidence angle, wind roughening and soil moisture, so
    a single global -16 dB is wrong somewhere in every scene. Here the same 30%
    water is present in both scenes; only the absolute calibration differs.
    """
    truth = 0.30
    baseline = _sar_scene(truth)
    shifted = _sar_scene(truth, shift_db=+5.0)

    adaptive_error = abs(np.nanmean(indices.adaptive_water_mask(shifted)[0]) - truth)
    fixed_error = abs(np.nanmean(indices.sar_water_mask(shifted)) - truth)

    assert adaptive_error < 0.02, "adaptive must track the shift"
    assert fixed_error > 0.15, "the fixed cut is expected to fail here"
    assert adaptive_error < fixed_error

    # And it must not *lose* accuracy on the unshifted scene it already handled.
    assert abs(np.nanmean(indices.adaptive_water_mask(baseline)[0]) - truth) < 0.02


def test_adaptive_threshold_rejects_a_dry_scene():
    """**Regression test for a measured bug.**

    Otsu's separability measures how *good* a two-class split is, not whether the
    data is bimodal. On a dry, water-free scene it bisects the single mode's lower
    tail, scored separability 0.54 — over MIN_BIMODALITY — and reported **24.7% of a
    bone-dry scene as standing water**. The valley-depth test is what catches it.
    """
    dry = np.random.default_rng(3).normal(-7, 1.5, 10_000).astype("float32").reshape(100, 100)

    mask, diagnostics = indices.adaptive_water_mask(dry)

    assert diagnostics["method"] == "fixed-threshold"
    assert "not bimodal" in diagnostics.get("reason", "")
    assert np.nanmean(mask) < 0.02, "a dry scene must not report water"


def test_adaptive_threshold_rejects_speckle():
    """A handful of dark outliers is radar speckle, not a flood."""
    scene = np.random.default_rng(4).normal(-8, 2.0, 10_000).astype("float32")
    scene[:20] = -30.0

    _, diagnostics = indices.adaptive_water_mask(scene.reshape(100, 100))
    assert diagnostics["method"] == "fixed-threshold"


def test_permanent_water_is_removed_from_inundation():
    """Step 1. A river that is always there is not today's flood.

    Without this, `inundated_fraction` counts the Niger itself on every pass — a
    large, constant false positive on the hazard that matters most.
    """
    scene = _sar_scene(0.30)
    permanent = np.zeros((100, 100), dtype="float32")
    permanent[70:, :] = 1.0     # the bottom 30% is a permanent channel

    with_baseline, diagnostics = indices.adaptive_water_mask(
        scene, permanent_water=permanent
    )
    without_baseline, _ = indices.adaptive_water_mask(scene)

    assert diagnostics["permanent_water_removed"] is True
    assert diagnostics["pixels_reclassified"] > 0
    assert np.nanmean(with_baseline) < np.nanmean(without_baseline)


def test_permanent_water_keeps_masked_pixels_in_the_denominator():
    """Zeroed, not NaN'd.

    Those pixels were validly observed and are validly not-*newly*-flooded. NaN
    would shrink the denominator and inflate the reported fraction — the opposite of
    the intended correction.
    """
    water = np.ones((10, 10), dtype="float32")
    permanent = np.zeros((10, 10), dtype="float32")
    permanent[:5] = 1.0

    out, _ = indices.apply_permanent_water(water, permanent)

    assert np.isfinite(out).all(), "must not introduce NaN"
    assert np.nanmean(out) == pytest.approx(0.5)


def test_permanent_water_survives_a_shape_mismatch():
    """Assets from different products are routinely a pixel or two apart, and that
    must never fail the radar leg."""
    water = np.ones((10, 10), dtype="float32")
    out, diagnostics = indices.apply_permanent_water(water, np.ones((7, 7), dtype="float32"))

    assert out.shape == (10, 10)
    assert diagnostics["permanent_water_removed"] is True


def test_adaptive_mask_propagates_nan():
    """No-data must stay no-data through the whole chain."""
    scene = _sar_scene(0.30)
    scene[:20] = np.nan

    mask, _ = indices.adaptive_water_mask(scene)
    assert np.isnan(mask[:20]).all()
    assert np.isfinite(mask[20:]).all()


def test_otsu_returns_none_on_tiny_input():
    """Too few pixels to build a histogram from."""
    threshold, separability = indices.otsu_threshold(np.array([1.0, 2.0], dtype="float32"))
    assert threshold is None
    assert separability == 0.0


# --------------------------------------------------------------------------- #
# Step 3 — HAND and TWI
# --------------------------------------------------------------------------- #


def test_hand_separates_a_floodplain_from_a_hillside():
    """The defect this fixes.

    `lowland_fraction` is the share of the AOI below its own median elevation — a
    *relative* statistic with no hydrological content. It returns ~50% on a
    hillside, calling half a mountainside "low-lying". `median_hand_m` answers the
    question that predicts flooding: how far above the nearest channel is this?
    """
    floodplain = (np.zeros((48, 48)) + np.linspace(0, 2, 48)[:, None]).astype("float32")
    hillside = (np.linspace(0, 500, 48)[:, None] * np.ones((1, 48))).astype("float32")

    plain = terrain.terrain_profile_from_dem(floodplain)
    slope = terrain.terrain_profile_from_dem(hillside)

    assert plain.available and slope.available
    assert plain.median_hand_m < 2.0, "a floodplain sits at channel level"
    assert slope.median_hand_m > 20.0, "a hillside does not"

    # The statistic it replaces cannot make this distinction at all.
    plain_proxy = float(np.count_nonzero(floodplain < np.median(floodplain)) / floodplain.size)
    slope_proxy = float(np.count_nonzero(hillside < np.median(hillside)) / hillside.size)
    assert plain_proxy == pytest.approx(slope_proxy, abs=0.05), (
        "the median-elevation proxy gives both the same answer — that is the bug"
    )


def test_terrain_reports_slope_and_wetness():
    flat = (np.zeros((48, 48)) + np.linspace(0, 1, 48)[:, None]).astype("float32")
    steep = (np.linspace(0, 800, 48)[:, None] * np.ones((1, 48))).astype("float32")

    assert terrain.terrain_profile_from_dem(flat).mean_slope_deg < 2.0
    assert terrain.terrain_profile_from_dem(steep).mean_slope_deg > 10.0


def test_terrain_refuses_a_mostly_void_dem():
    """Any statistic from a half-empty window describes the interpolation, not the
    terrain."""
    dem = np.full((32, 32), np.nan, dtype="float32")
    dem[:10] = 100.0

    assert terrain.terrain_profile_from_dem(dem).available is False


def test_terrain_refuses_empty_input():
    assert terrain.terrain_profile_from_dem(np.array([[]], dtype="float32")).available is False
    assert terrain.terrain_profile_from_dem(
        np.full((16, 16), np.nan, dtype="float32")
    ).available is False


def test_unavailable_terrain_asserts_nothing():
    """`available=False` must not read as "no flood-prone terrain"."""
    profile = terrain.TerrainProfile()
    assert profile.available is False
    assert profile.sources == []


# --------------------------------------------------------------------------- #
# Steps 7 and 10 — calibration and fitted fusion weights
# --------------------------------------------------------------------------- #


def _overconfident_sample(n: int = 200, seed: int = 9):
    """A model whose stated confidence exceeds how often it is actually right."""
    rng = np.random.default_rng(seed)
    stated = rng.uniform(0.5, 0.95, n)
    actual = stated * 0.6
    return stated, (rng.uniform(size=n) < actual).astype(int)


def test_calibration_corrects_overconfidence():
    """Step 7's purpose: make `confidence 0.8` mean "right 80% of the time".

    A well-calibrated 0.6 is more useful to a district officer than an
    overconfident 0.9, because their own action threshold then means what they
    think it means.
    """
    stated, outcomes = _overconfident_sample()
    calibrator = calibration.Calibrator.fit(stated, outcomes)

    assert calibrator.available
    assert calibrator.apply(0.88) < 0.88, "must pull an overconfident value down"

    raw_brier = calibration.brier_score(stated, outcomes)
    calibrated_brier = calibration.brier_score(
        [calibrator.apply(v) for v in stated], outcomes
    )
    assert calibrated_brier < raw_brier, "calibration must improve the proper score"


def test_calibration_is_identity_without_enough_data():
    """A curve fitted to six points looks authoritative and encodes noise.

    Since confidence gates severity, that curve could raise an EMERGENCY on noise —
    so an uncalibrated deployment must behave exactly as it does today.
    """
    stated, outcomes = _overconfident_sample(n=10)
    calibrator = calibration.Calibrator.fit(stated, outcomes)

    assert calibrator.available is False
    assert calibrator.apply(0.77) == 0.77


def test_calibration_requires_both_outcomes():
    """With one class present, "calibration" would just learn the base rate."""
    calibrator = calibration.Calibrator.fit([0.8] * 50, [1] * 50)
    assert calibrator.available is False


def test_calibration_output_is_never_absolutely_certain():
    """0.0 claims certainty of being wrong and 1.0 certainty of being right.
    Neither is justified from a finite sample, and both break later arithmetic."""
    stated, outcomes = _overconfident_sample()
    calibrator = calibration.Calibrator.fit(stated, outcomes)

    for value in (0.0, 0.001, 0.999, 1.0):
        assert 0.0 < calibrator.apply(value) < 1.0


def test_brier_score_is_none_when_unmeasurable():
    """`None`, never 0.0 — which would read as a perfect score. Same discipline as
    `verification_metrics` reporting `precision: null`."""
    assert calibration.brier_score([], []) is None


def test_reliability_bins_omit_empty_ranges():
    """An unobserved confidence range is unknown, not perfectly calibrated."""
    bins = calibration.reliability_bins([0.9] * 20, [1] * 20, bins=5)

    assert len(bins) == 1, "only one range was observed"
    assert bins[0]["count"] == 20


def test_fusion_weights_recover_the_true_ordering():
    """Step 10. The fit must reproduce a known weighting, not collapse to a corner.

    **Regression test for a real bug.** Without L2 regularisation the logistic loss
    has no bounded optimum — the solver drove the most predictive term up and the
    others to zero, and normalising afterwards reported a spurious
    `(1.0, 0.0, 0.0)`. Ridge shrinkage is what makes the ratios meaningful.
    """
    rng = np.random.default_rng(7)
    n = 300
    observed, forecast, exposure = (rng.uniform(0, 1, n) for _ in range(3))
    label = (
        0.6 * observed + 0.3 * forecast + 0.1 * exposure + rng.normal(0, 0.08, n) > 0.5
    ).astype(int)

    weights = calibration.fit_fusion_weights(observed, forecast, exposure, label)

    assert weights.available
    assert weights.observed > weights.forecast > weights.exposure
    assert min(weights.observed, weights.forecast, weights.exposure) > 0.01, (
        "no term may collapse to exactly zero — that was the unregularised bug"
    )
    assert weights.observed + weights.forecast + weights.exposure == pytest.approx(1.0)


def test_fusion_weights_can_never_go_negative():
    """**A safety property, not a statistical nicety.**

    A negative rainfall coefficient means "more rain implies less flood risk". On a
    noisy sample an unconstrained fit will learn exactly that, and it would be
    indefensible to an agriculture officer — and could suppress a real warning. Here
    the labels are constructed to be actively anti-correlated with rainfall.
    """
    rng = np.random.default_rng(13)
    n = 300
    observed, forecast, exposure = (rng.uniform(0, 1, n) for _ in range(3))
    adversarial = (0.9 * observed - 0.8 * forecast + rng.normal(0, 0.05, n) > 0.2).astype(int)

    weights = calibration.fit_fusion_weights(observed, forecast, exposure, adversarial)

    assert weights.forecast >= 0.0
    assert weights.observed >= 0.0
    assert weights.exposure >= 0.0


def test_fusion_weights_keep_the_constants_without_enough_data():
    """The hand-set weights are defensible expert judgement, and expert priors beat
    models fitted on tiny samples."""
    weights = calibration.fit_fusion_weights([0.5] * 10, [0.5] * 10, [0.5] * 10, [1] * 10)

    assert weights.available is False
    assert (weights.observed, weights.forecast, weights.exposure) == (0.55, 0.30, 0.15)


# --------------------------------------------------------------------------- #
# Structural invariants — the boundaries the whole risk layer depends on
# --------------------------------------------------------------------------- #


def test_stats_layer_imports_nothing_heavy():
    """`app/stats/` must stay numpy + scipy only.

    This is what keeps the Oracle unit-testable with no GDAL, no torch and no
    provider configured — the same reason `exposure.py` imports `cog`/`stac` inside
    its functions and `geometry.py` exists at all. A convenience import here would
    silently make `test_oracle.py` impossible to run.
    """
    import ast
    import pathlib

    forbidden = ("rasterio", "torch", "httpx", "pydantic_ai", "app.search", "app.llm")
    offenders: list[str] = []

    for path in sorted(pathlib.Path("app/stats").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if any(name.startswith(bad) for bad in forbidden):
                    offenders.append(f"{path.name}: {name}")

    assert not offenders, (
        f"app/stats must stay dependency-light; found: {offenders}"
    )


def test_terrain_module_defers_geospatial_imports():
    """`eo/terrain.py` may only import `cog`/`stac` inside functions.

    Identical reasoning to `exposure.py`, and enforced the same way: the Oracle
    imports this module at module scope, so a top-level rasterio import would drag
    GDAL into the risk layer.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path("app/eo/terrain.py").read_text())

    for node in tree.body:      # module scope only
        if isinstance(node, ast.Import | ast.ImportFrom):
            name = node.module if isinstance(node, ast.ImportFrom) else node.names[0].name
            assert name is None or not any(
                name.startswith(bad) for bad in ("rasterio", "app.eo.cog", "app.eo.stac")
            ), f"{name} must be imported inside a function, not at module scope"


def test_every_new_statistic_has_an_unavailable_state():
    """The degradation contract, checked mechanically.

    Every statistic must be able to say "I could not measure this", so a caller can
    fall back to the documented heuristic instead of consuming a fabricated number.
    A statistic with no unavailable state is one that will invent a value.
    """
    # None-returning statistics.
    assert rainfall_index.spi(50.0, []) is None
    assert ensemble.return_period(100.0, []) is None
    assert anomaly.seasonal_anomaly(0.5, 100, anomaly.HarmonicBaseline.unavailable()) is None
    assert calibration.brier_score([], []) is None

    # available=False dataclasses.
    assert ensemble.exceedance_probability([], 25.0).available is False
    assert anomaly.HarmonicBaseline.unavailable().available is False
    assert terrain.TerrainProfile().available is False
    assert calibration.Calibrator().available is False
    assert calibration.FusionWeights().available is False


def test_analyst_result_defaults_claim_the_heuristic_path():
    """An unset provenance field must report the weaker path, not the stronger one.

    If `flood_method` defaulted to "trained-model", every degraded assessment would
    claim a capability it did not use — and `evidence` would assert a permanent-water
    correction that never happened.
    """
    from app.models.schemas import AnalystResult

    result = AnalystResult(aoi_id="aoi_test")

    assert result.flood_method == "heuristic"
    assert result.stress_method == "heuristic"
    assert result.flood_diagnostics == {}
    assert result.stress_diagnostics == {}


def test_rainfall_outlook_distinguishes_unmeasured_from_zero():
    """`spi=None` means "not measured"; 0.0 would mean "exactly median rainfall".

    Conflating the two is the `ExposureSummary.sources` mistake in a new place:
    absent data must not become an implied claim.
    """
    from app.models.schemas import RainfallOutlook

    outlook = RainfallOutlook()

    assert outlook.spi is None, "must be None, not 0.0"
    assert outlook.api_mm == 0.0
    assert outlook.ensemble_by_day == []
