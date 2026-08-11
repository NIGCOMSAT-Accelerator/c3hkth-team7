"""Risk fusion and severity gating.

The rules under test here are safety rules, not arithmetic: a low-confidence
reading must not be able to raise an emergency, a flood must carry its
downstream cascade, and the malaria arm must not fire where malaria isn't
endemic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.agents.oracle import CONFIDENCE_ESCALATION_FLOOR, OracleAgent
from app.models.enums import HazardType, Severity
from app.models.schemas import (
    AnalystResult,
    ExposureSummary,
    ForecastPoint,
    HealthBaseline,
    RainfallOutlook,
    SoilProfile,
)


@pytest.fixture
def oracle() -> OracleAgent:
    return OracleAgent()


def outlook(*rainfall_mm: float, forecast: bool = True, antecedent: float = 0.0):
    now = datetime.now(timezone.utc)
    return RainfallOutlook(
        points=[
            ForecastPoint(day=i, date=now + timedelta(days=i), risk=0.0, rainfall_mm=mm)
            for i, mm in enumerate(rainfall_mm)
        ],
        forecast_available=forecast,
        antecedent_mm=antecedent,
        source="climateserv-gefs" if forecast else "gpm-imerg",
    )


def analysis(**kwargs) -> AnalystResult:
    defaults = dict(
        aoi_id="aoi_test",
        inundated_fraction=0.0,
        stressed_crop_fraction=0.0,
        confidence=0.9,
        source="fused",
    )
    return AnalystResult(**{**defaults, **kwargs})


ENDEMIC = HealthBaseline(malaria_pfpr=0.22, endemic=True, available=True)
NON_ENDEMIC = HealthBaseline(malaria_pfpr=0.01, endemic=False, available=True)
UNKNOWN_HEALTH = HealthBaseline()


# --------------------------------------------------------------------------- #
# Severity gating
# --------------------------------------------------------------------------- #


def test_low_confidence_cannot_raise_an_emergency(oracle):
    """The core safety rule. A shaky reading caps at WATCH however bad it looks."""
    severity = oracle._severity(
        score=0.95, confidence=CONFIDENCE_ESCALATION_FLOOR - 0.1
    )
    assert severity is Severity.WATCH


def test_high_confidence_allows_emergency(oracle):
    assert oracle._severity(score=0.95, confidence=0.9) is Severity.EMERGENCY


def test_low_confidence_does_not_suppress_low_severity(oracle):
    """The cap only applies to escalation — it must not silence an advisory."""
    assert oracle._severity(score=0.25, confidence=0.2) is Severity.ADVISORY


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0.00, Severity.INFO),
        (0.25, Severity.ADVISORY),
        (0.45, Severity.WATCH),
        (0.65, Severity.WARNING),
        (0.85, Severity.EMERGENCY),
    ],
)
def test_severity_thresholds(oracle, score, expected):
    assert oracle._severity(score=score, confidence=0.95) is expected


# --------------------------------------------------------------------------- #
# Hazard classification
# --------------------------------------------------------------------------- #


def test_standing_water_classifies_as_flood(oracle):
    hazard = oracle._classify(
        analysis(inundated_fraction=0.40), outlook(0, 0), ExposureSummary()
    )
    assert hazard is HazardType.FLOOD_INUNDATION


def test_wet_and_stressed_is_waterlogging(oracle):
    hazard = oracle._classify(
        analysis(inundated_fraction=0.15, stressed_crop_fraction=0.30),
        outlook(0, 0),
        ExposureSummary(),
    )
    assert hazard is HazardType.CROP_WATERLOGGING


def test_stressed_but_dry_is_drought_not_waterlogging(oracle):
    """Getting this inversion wrong would tell a farmer to drain a dry field."""
    hazard = oracle._classify(
        analysis(inundated_fraction=0.01, stressed_crop_fraction=0.45),
        outlook(0, 0),
        ExposureSummary(),
    )
    assert hazard is HazardType.CROP_DROUGHT_STRESS


def test_heavy_rain_ahead_without_water_yet_is_forecast_flood(oracle):
    hazard = oracle._classify(analysis(), outlook(0, 0, 80.0), ExposureSummary())
    assert hazard is HazardType.FLOOD_FORECAST


def test_saturated_lowland_without_forecast_is_still_a_flood_setup(oracle):
    """What the antecedent sources buy us: a flood warning with no forecast."""
    hazard = oracle._classify(
        analysis(),
        outlook(forecast=False, antecedent=120.0),
        ExposureSummary(lowland_fraction=0.4, sources=["copernicus-dem"]),
    )
    assert hazard is HazardType.FLOOD_FORECAST


def test_saturated_ground_on_flat_terrain_is_not_escalated(oracle):
    """Antecedent rain alone isn't a flood — it needs somewhere to collect."""
    hazard = oracle._classify(
        analysis(),
        outlook(forecast=False, antecedent=120.0),
        ExposureSummary(lowland_fraction=0.02, sources=["copernicus-dem"]),
    )
    assert hazard is not HazardType.FLOOD_FORECAST


# --------------------------------------------------------------------------- #
# Cascade — malaria gating
# --------------------------------------------------------------------------- #


def test_flood_in_endemic_area_cascades_to_malaria(oracle):
    cascade = oracle._cascade(
        HazardType.FLOOD_INUNDATION, analysis(inundated_fraction=0.35), ENDEMIC
    )
    assert HazardType.CROP_WATERLOGGING in cascade
    assert HazardType.MALARIA_RISK in cascade


def test_flood_in_non_endemic_area_does_not_assert_malaria(oracle):
    """Vectors without a reservoir of infection don't produce an outbreak."""
    cascade = oracle._cascade(
        HazardType.FLOOD_INUNDATION, analysis(inundated_fraction=0.35), NON_ENDEMIC
    )
    assert HazardType.CROP_WATERLOGGING in cascade
    assert HazardType.MALARIA_RISK not in cascade


def test_unknown_endemicity_does_not_assert_malaria(oracle):
    """Absent data must not become an implied claim."""
    cascade = oracle._cascade(
        HazardType.FLOOD_INUNDATION, analysis(inundated_fraction=0.35), UNKNOWN_HEALTH
    )
    assert HazardType.MALARIA_RISK not in cascade


def test_brief_flood_does_not_imply_malaria(oracle):
    """Water must persist to breed vectors; a small peak that drains does not."""
    cascade = oracle._cascade(
        HazardType.FLOOD_INUNDATION, analysis(inundated_fraction=0.05), ENDEMIC
    )
    assert HazardType.MALARIA_RISK not in cascade


def test_drought_has_no_cascade(oracle):
    assert oracle._cascade(HazardType.CROP_DROUGHT_STRESS, analysis(), ENDEMIC) == []


# --------------------------------------------------------------------------- #
# Risk terms
# --------------------------------------------------------------------------- #


def test_observed_term_saturates_at_forty_percent_inundation(oracle):
    neutral = SoilProfile(drainage="moderate", available=True)
    assert oracle._observed_term(
        HazardType.FLOOD_INUNDATION, analysis(inundated_fraction=0.40), neutral
    ) == pytest.approx(1.0)
    assert oracle._observed_term(
        HazardType.FLOOD_INUNDATION, analysis(inundated_fraction=0.80), neutral
    ) == pytest.approx(1.0)


def test_impeded_soil_raises_flood_risk_above_free_draining(oracle):
    """The same water on clay is worse than on sand."""
    wet = analysis(inundated_fraction=0.2)
    impeded = oracle._observed_term(
        HazardType.FLOOD_INUNDATION, wet, SoilProfile(drainage="impeded", available=True)
    )
    free = oracle._observed_term(
        HazardType.FLOOD_INUNDATION, wet, SoilProfile(drainage="free", available=True)
    )
    assert impeded > free


def test_unknown_soil_is_neutral(oracle):
    """Missing soil data must not tilt the score in either direction."""
    wet = analysis(inundated_fraction=0.2)
    unknown = oracle._observed_term(HazardType.FLOOD_INUNDATION, wet, SoilProfile())
    moderate = oracle._observed_term(
        HazardType.FLOOD_INUNDATION,
        wet,
        SoilProfile(drainage="moderate", available=True),
    )
    assert unknown == pytest.approx(moderate)


def test_forecast_term_reacts_to_a_single_heavy_day(oracle):
    """60 mm in one day must not be averaged away across a quiet week."""
    spike = oracle._forecast_term(outlook(0, 0, 60, 0, 0, 0, 0))
    flat = oracle._forecast_term(outlook(0, 0, 0, 0, 0, 0, 0))
    assert spike > 0.9
    assert flat == 0.0


def test_antecedent_counts_for_less_than_a_real_forecast(oracle):
    """Saturated ground is evidence, but it is not a prediction."""
    forecast_driven = oracle._forecast_term(outlook(150.0))
    antecedent_only = oracle._forecast_term(
        outlook(forecast=False, antecedent=150.0)
    )
    assert antecedent_only < forecast_driven


def test_forecast_term_empty_is_zero(oracle):
    assert oracle._forecast_term(RainfallOutlook()) == 0.0


def test_exposure_term_no_sources_is_zero(oracle):
    """No data must read as 'unknown', not as 'nobody there'."""
    assert oracle._exposure_term(ExposureSummary()) == 0.0


def test_exposure_term_scales_with_population(oracle):
    small = oracle._exposure_term(
        ExposureSummary(population=1_000, sources=["worldpop"])
    )
    large = oracle._exposure_term(
        ExposureSummary(population=50_000, sources=["worldpop"])
    )
    assert large > small


def test_lowland_terrain_amplifies_exposure(oracle):
    """Low ground concentrates whatever water arrives."""
    flat = ExposureSummary(
        population=20_000, lowland_fraction=0.0, sources=["worldpop", "copernicus-dem"]
    )
    low = ExposureSummary(
        population=20_000, lowland_fraction=0.8, sources=["worldpop", "copernicus-dem"]
    )
    assert oracle._exposure_term(low) > oracle._exposure_term(flat)


def test_cropland_contributes_to_exposure(oracle):
    """This is an agricultural service — cropland is exposure, not scenery."""
    bare = ExposureSummary(cropland_fraction=0.0, sources=["worldcover"])
    farmed = ExposureSummary(cropland_fraction=0.6, sources=["worldcover"])
    assert oracle._exposure_term(farmed) > oracle._exposure_term(bare)


# --------------------------------------------------------------------------- #
# Evidence & provenance
# --------------------------------------------------------------------------- #


def test_evidence_flags_radar_only_reading(oracle):
    facts = oracle._evidence(
        analysis(source="sar", inundated_fraction=0.3),
        outlook(5.0),
        ExposureSummary(),
        SoilProfile(),
        UNKNOWN_HEALTH,
    )
    assert any("radar-only" in f for f in facts)


def test_evidence_distinguishes_antecedent_from_forecast(oracle):
    facts = oracle._evidence(
        analysis(),
        outlook(forecast=False, antecedent=64.0),
        ExposureSummary(),
        SoilProfile(),
        UNKNOWN_HEALTH,
    )
    joined = " ".join(facts)
    # "in the measured window", not "already fell in the past week". The antecedent window is now
    # lagged 45 days to clear CHIRPS's publication delay, so "the past week" was literally untrue —
    # the figure describes a window about six weeks back. The distinction that MATTERS is
    # observation vs forecast, and that is what the assertion checks.
    assert "64 mm" in joined
    assert "measured window" in joined
    assert "no forward forecast" in joined


def test_evidence_says_so_when_rainfall_is_entirely_missing(oracle):
    facts = oracle._evidence(
        analysis(), RainfallOutlook(), ExposureSummary(), SoilProfile(), UNKNOWN_HEALTH
    )
    assert any("unavailable" in f for f in facts)


def test_evidence_reports_impeded_drainage(oracle):
    facts = oracle._evidence(
        analysis(inundated_fraction=0.3),
        outlook(10.0),
        ExposureSummary(),
        SoilProfile(clay_g_kg=400, drainage="impeded", available=True),
        UNKNOWN_HEALTH,
    )
    assert any("drain slowly" in f for f in facts)


def test_sources_records_every_contributor(oracle):
    sources = oracle._sources(
        analysis(source="fused"),
        outlook(1.0),
        ExposureSummary(sources=["worldpop", "worldcover"]),
        SoilProfile(available=True),
        ENDEMIC,
    )
    assert "sentinel-1" in sources
    assert "sentinel-2" in sources
    assert "climateserv-gefs" in sources
    assert "worldpop" in sources
    assert "worldcover" in sources
    assert "soilgrids" in sources
    assert "malaria-atlas" in sources


def test_sources_omits_absent_datasets(oracle):
    sources = oracle._sources(
        analysis(source="sar"),
        RainfallOutlook(),
        ExposureSummary(),
        SoilProfile(),
        UNKNOWN_HEALTH,
    )
    assert sources == ["sentinel-1"]


# --------------------------------------------------------------------------- #
# An unmeasured leg is not a low reading
#
# `inundated_fraction` and `stressed_crop_fraction` both default to 0.0, so a failed leg was
# indistinguishable from a measured absence. The drought branch keys on `inundated < 0.05` — a
# claim that the ground is DRY — so a radar failure during a real flood classified as
# CROP_DROUGHT_STRESS. That does not under-warn; it tells a farmer to irrigate a flooded field.
# --------------------------------------------------------------------------- #


def test_a_radar_failure_cannot_be_classified_as_drought():
    """**The inverted-advice bug.**

    Stressed crop plus an UNMEASURED flood reading must not resolve to drought. The stress is real;
    the dryness is unknown, and only one of those may drive the advice.
    """
    oracle = OracleAgent()
    hazard = oracle._classify(
        analysis(
            stressed_crop_fraction=0.55,
            inundated_fraction=0.0,   # the default a failed leg leaves behind
            flood_measured=False,     # ...but it was never measured
            source="optical",
        ),
        outlook(0, 0),
        ExposureSummary(),
        None,
    )
    assert hazard is not HazardType.CROP_DROUGHT_STRESS, (
        "an unmeasured flood reading cannot support the claim that the ground is dry"
    )


def test_the_same_inputs_DO_give_drought_when_flooding_was_measured():
    """The control. With both legs measured, 0% water plus stressed crop IS drought.

    Without this the test above could pass by breaking drought classification altogether.
    """
    oracle = OracleAgent()
    hazard = oracle._classify(
        analysis(
            stressed_crop_fraction=0.55,
            inundated_fraction=0.0,
            flood_measured=True,
            stress_measured=True,
        ),
        outlook(0, 0),
        ExposureSummary(),
        None,
    )
    assert hazard is HazardType.CROP_DROUGHT_STRESS


def test_an_unmeasured_leg_contributes_no_observed_risk():
    """Not 0.0 risk because the field is fine — 0.0 because nothing was measured.

    Numerically identical here, but the distinction reaches the farmer through `evidence`, and
    `stats/anomaly` and `eo/terrain` already draw it the same way.
    """
    oracle = OracleAgent()
    term = oracle._observed_term(
        HazardType.FLOOD_INUNDATION,
        analysis(inundated_fraction=0.0, flood_measured=False),
        SoilProfile(),
    )
    assert term == 0.0


def test_a_radar_failure_is_disclosed_in_the_evidence():
    """`source == "optical"` had NO caveat, so a radar failure reached the farmer as silence.

    Every other sensor state said something. This one — the leg that measures flooding — said
    nothing, while `inundated_fraction` read as 0.0 beside it.
    """
    oracle = OracleAgent()
    facts = oracle._evidence(
        analysis(source="optical", flood_measured=False, stress_measured=True),
        outlook(0, 0),
        ExposureSummary(),
        SoilProfile(),
        UNKNOWN_HEALTH,
        None,
    )
    joined = " ".join(facts).lower()
    assert "radar" in joined and (
        "unavailable" in joined or "not measured" in joined
    ), f"a radar failure must be stated, got: {facts}"


def test_partial_coverage_is_disclosed():
    """A reading over part of the field must say so.

    A percentage alone cannot tell the reader that the satellite pass only reached half their land.
    """
    oracle = OracleAgent()
    facts = oracle._evidence(
        analysis(
            inundated_fraction=0.30,
            flood_measured=True,
            flood_coverage=0.55,
            source="sar",
        ),
        outlook(0, 0),
        ExposureSummary(),
        SoilProfile(),
        UNKNOWN_HEALTH,
        None,
    )
    joined = " ".join(facts).lower()
    assert "55%" in joined or "covers about" in joined, (
        f"partial coverage must be stated, got: {facts}"
    )


def test_full_coverage_is_not_mentioned():
    """Silence when there is nothing to caveat — a note on every alert trains readers to skip it."""
    oracle = OracleAgent()
    facts = oracle._evidence(
        analysis(inundated_fraction=0.30, flood_measured=True, flood_coverage=1.0, source="sar"),
        outlook(0, 0),
        ExposureSummary(),
        SoilProfile(),
        UNKNOWN_HEALTH,
        None,
    )
    assert not any("covers about" in f for f in facts)


def test_a_measured_dry_week_is_not_reported_as_missing_data():
    """**Zero millimetres is a finding, not an absence.**

    The branch was `elif outlook.antecedent_mm > 0`, so a genuinely dry week fell through to
    "Rainfall data was unavailable for this cycle". In the Sahel that is the most decision-relevant
    reading there is: a farmer told the data is missing behaves differently from one told no rain
    fell. Same conflation as the Analyst's `.get(field, 0.0)`, in a different place.
    """
    oracle = OracleAgent()
    facts = oracle._evidence(
        analysis(),
        RainfallOutlook(
            points=[], forecast_available=False, antecedent_mm=0.0,
            source="climateserv-chirps",   # a rung DID answer
        ),
        ExposureSummary(), SoilProfile(), UNKNOWN_HEALTH, None,
    )
    joined = " ".join(facts)
    assert "unavailable" not in joined.lower(), (
        f"a measured zero must not read as missing data, got: {facts}"
    )
    assert "0 mm" in joined


def test_a_genuinely_absent_source_still_says_unavailable():
    """The control. When NO rung answered, the honest word is still 'unavailable'."""
    oracle = OracleAgent()
    facts = oracle._evidence(
        analysis(),
        RainfallOutlook(points=[], forecast_available=False, antecedent_mm=0.0, source="none"),
        ExposureSummary(), SoilProfile(), UNKNOWN_HEALTH, None,
    )
    assert any("unavailable" in f.lower() for f in facts)
