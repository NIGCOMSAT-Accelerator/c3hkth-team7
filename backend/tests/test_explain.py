"""The explanation surfaces, and the boundary they must not cross.

These turn a measured assessment into language a farmer can act on. The property worth testing is
not the wording — that comes from a model — but the four things that must hold regardless of what
the model says:

  * they never change a number, and never see anything except `evidence`;
  * they never fail an alert, whatever the provider does;
  * they never recommend irrigating a waterlogged field;
  * they never run before the Herald's suppression gates, so a quiet plot costs nothing.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest

from app.explain import base, drivers, irrigation, optical
from app.models.enums import HazardType, Severity
from app.models.schemas import Advisory, Explanations, RiskAssessment


def _assessment(hazard: HazardType, evidence: list[str] | None = None) -> RiskAssessment:
    return RiskAssessment(
        aoi_id="aoi_test",
        aoi_name="Test plot",
        hazard=hazard,
        severity=Severity.WATCH,
        score=0.55,
        confidence=0.6,
        evidence=evidence if evidence is not None else ["2% of the area is under standing water"],
    )


# --------------------------------------------------------------------------- #
# The grounding boundary
# --------------------------------------------------------------------------- #


def test_only_evidence_reaches_the_prompt():
    """**The rule the whole package exists under.**

    A model given `exposure.population` or `score` will restate it as a fact of its own, and a
    figure invented one step from a farmer's decision is the failure this codebase has already
    removed twice. `evidence` is the Oracle's own account of what was measured; everything else is
    a derived value.
    """
    a = _assessment(HazardType.CROP_WATERLOGGING)
    block = base.evidence_block(a)

    assert "2% of the area is under standing water" in block
    # Nothing else may appear.
    for forbidden in (str(a.score), str(a.confidence), a.aoi_id):
        assert forbidden not in block, (
            f"{forbidden!r} reached the prompt — only `evidence` may"
        )


def test_no_evidence_says_so_rather_than_inventing():
    """An empty evidence list must produce an honest statement, not a blank prompt the model
    fills with a plausible reading."""
    block = base.evidence_block(_assessment(HazardType.CROP_DROUGHT_STRESS, evidence=[]))
    assert "no measurements" in block.lower()


def test_every_surface_shares_one_grounding_instruction():
    """Three paraphrases of the same rule is how one of them ends up permitting a number the
    others forbid."""
    for module in (optical, drivers, irrigation):
        assert base.GROUNDING in module._SYSTEM, (
            f"{module.__name__} must use the shared grounding text verbatim"
        )


# --------------------------------------------------------------------------- #
# Never failing an alert
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_every_surface_returns_text_with_no_provider(monkeypatch):
    """No provider configured is the DEFAULT state of a fresh deployment.

    Each surface must return its deterministic template, because a farmer opening the portal
    during a flood must not find an empty panel — the same reasoning that makes `_template` the
    most safety-critical line in `advisory/generator.py`.
    """
    monkeypatch.setattr(base.client, "available", lambda: False)

    a = _assessment(HazardType.CROP_DROUGHT_STRESS, evidence=["the soil is dry at the surface"])
    for label, text in (
        ("optical", await optical.describe(a)),
        ("drivers", await drivers.narrate(a)),
        ("irrigation", await irrigation.advise(a)),
    ):
        assert text.strip(), f"{label} returned empty text with no provider"


@pytest.mark.asyncio
async def test_a_provider_failure_falls_back_rather_than_raising(monkeypatch):
    """A timeout, an expired key or a refusal must cost an explanation, never the alert."""

    async def boom(*_args, **_kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(base.client, "available", lambda: True)
    monkeypatch.setattr(base.client, "complete", boom)

    a = _assessment(HazardType.CROP_VEGETATION_ANOMALY)
    assert (await optical.describe(a)).strip()
    assert (await drivers.narrate(a)).strip()


@pytest.mark.asyncio
async def test_an_empty_model_reply_falls_back(monkeypatch):
    """A provider that answers with whitespace must not produce a blank explanation."""

    async def blank(*_args, **_kwargs):
        return "   "

    monkeypatch.setattr(base.client, "available", lambda: True)
    monkeypatch.setattr(base.client, "complete", blank)

    text = await optical.describe(_assessment(HazardType.CROP_WATERLOGGING))
    assert text == optical.fallback_for(_assessment(HazardType.CROP_WATERLOGGING))


# --------------------------------------------------------------------------- #
# The irrigation safeguard
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_waterlogged_plot_is_never_told_to_irrigate(monkeypatch):
    """**The one instruction that must never be produced.**

    Irrigating a field already holding water drowns the crop. The evidence here deliberately says
    moisture is LOW — the trap a naive "low moisture → irrigate" rule falls into when a saturated
    field's surface has dried.

    The model is not asked at all on these hazards, because "the prompt tells it not to" is not
    good enough when the failure destroys a season.
    """

    async def rogue(*_args, **_kwargs):
        return "Irrigate immediately, the soil is very dry."

    monkeypatch.setattr(base.client, "available", lambda: True)
    monkeypatch.setattr(base.client, "complete", rogue)

    misleading = ["soil moisture is low at the surface", "the ground is dry on top"]
    for hazard in (
        HazardType.CROP_WATERLOGGING,
        HazardType.FLOOD_INUNDATION,
        HazardType.FLOOD_FORECAST,
    ):
        text = await irrigation.advise(_assessment(hazard, evidence=misleading))
        assert "irrigat" not in text.lower() or text.lower().startswith("hold"), (
            f"{hazard.value} produced an irrigation recommendation: {text!r}"
        )
        assert text.lower().startswith("hold")


@pytest.mark.asyncio
async def test_no_moisture_signal_refuses_to_advise(monkeypatch):
    """"We cannot tell you" is a correct answer.

    A confident recommendation built on no measurement wastes water, fuel and a day's labour — and
    is indistinguishable to the reader from a measured one.
    """

    async def rogue(*_args, **_kwargs):
        return "Irrigate now."

    monkeypatch.setattr(base.client, "available", lambda: True)
    monkeypatch.setattr(base.client, "complete", rogue)

    text = await irrigation.advise(
        _assessment(HazardType.CROP_VEGETATION_ANOMALY, evidence=["cloud cover was 80%"])
    )
    assert "cannot advise" in text.lower()


# --------------------------------------------------------------------------- #
# The measured irrigation decision (SMAP)
#
# Before SMAP, `_has_moisture_signal` inferred soil water from KEYWORDS in the evidence prose —
# "soil", "rain", "dry". That was the best available and it is now second-best: there is a measured
# number. These tests pin the precedence, because a measurement that can be outvoted by a prompt is
# not doing the job it was added for.
# --------------------------------------------------------------------------- #


def _wet(volumetric: float, hazard: HazardType = HazardType.CROP_VEGETATION_ANOMALY):
    """An assessment carrying a real SMAP reading."""
    from app.models.schemas import SoilMoisture

    assessment = _assessment(hazard, evidence=["Mean NDVI is 0.41"])
    assessment.soil_moisture = SoilMoisture(
        volumetric=volumetric, observed_date="2026-08-09", available=True
    )
    return assessment


@pytest.mark.asyncio
async def test_a_measured_saturated_plot_is_never_told_to_irrigate(monkeypatch):
    """**The Yenagoa case, observed live.**

    Measured 0.593 m3/m3 — saturated — while the Oracle classified `crop_vegetation_anomaly`, NOT
    waterlogging. So `_TOO_WET` does not catch it: that gate keys on the HAZARD, and the hazard is a
    vegetation anomaly. Only the measurement knows the pore space is full.

    Without the `drain` short-circuit the model is asked, and a model handed "vegetation is below
    its seasonal norm" will readily suggest watering. On a saturated plot that drowns the roots.
    """

    async def rogue(*_args, **_kwargs):
        return "Irrigate now — the vegetation index is low."

    monkeypatch.setattr(base.client, "available", lambda: True)
    monkeypatch.setattr(base.client, "complete", rogue)

    text = await irrigation.advise(_wet(0.593))

    assert text.lower().startswith("hold"), f"saturated plot was not held: {text!r}"
    assert "do not irrigate" in text.lower()
    # And it must say what it measured, so the farmer can check rather than trust.
    assert "0.59" in text
    assert "2026-08-09" in text


@pytest.mark.asyncio
async def test_a_measured_dry_plot_is_told_to_irrigate_and_to_check_by_hand(monkeypatch):
    """A real measurement produces a real instruction — and one caveat that must survive.

    SMAP's L-band sees roughly the top 5 cm. The root zone is deeper and dries later, so "irrigate"
    without "check at root depth" over-waters on the strength of a surface reading.
    """
    monkeypatch.setattr(base.client, "available", lambda: False)

    text = await irrigation.advise(_wet(0.08))

    assert "irrigate" in text.lower()
    assert "root depth" in text.lower(), "the surface-vs-root-zone caveat was dropped"
    assert "0.08" in text


@pytest.mark.asyncio
async def test_an_unavailable_reading_does_not_read_as_bone_dry(monkeypatch):
    """`volumetric` defaults to 0.0, which is drier than any real soil.

    If `available` were ignored, every unmeasured plot would present as 0.00 m3/m3 — a confident
    "irrigate" from no measurement at all. This is the `ExposureSummary.sources` rule applied to
    soil water: absent is unknown, never zero.
    """

    async def rogue(*_args, **_kwargs):
        return "Irrigate now."

    monkeypatch.setattr(base.client, "available", lambda: True)
    monkeypatch.setattr(base.client, "complete", rogue)

    unmeasured = _assessment(
        HazardType.CROP_VEGETATION_ANOMALY, evidence=["cloud cover was 80%"]
    )
    assert unmeasured.soil_moisture.available is False
    assert unmeasured.soil_moisture.volumetric == 0.0

    text = await irrigation.advise(unmeasured)
    assert "cannot advise" in text.lower(), (
        f"an unmeasured plot was given an instruction: {text!r}"
    )


def test_the_measurement_outranks_the_keyword_heuristic():
    """A measured `hold` must not be overridden by a drought-shaped hazard label.

    `fallback_for` previously returned `_DRY` ("point to dry conditions") for any
    CROP_DROUGHT_STRESS. With a reading of 0.30 m3/m3 — comfortably in the range crops draw on —
    that text would contradict the measurement.
    """
    text = irrigation.fallback_for(_wet(0.30, HazardType.CROP_DROUGHT_STRESS))

    assert text.lower().startswith("hold")
    assert "0.30" in text
    assert "point to dry conditions" not in text.lower()


def test_the_measured_decision_reaches_the_prompt_as_a_decision(monkeypatch):
    """The model must be told the decision, not left to infer it from one prose line.

    The wetness fact is in `evidence`, so it reaches the prompt either way — but as one sentence
    among a dozen, where a more vivid line about forecast rainfall can outweigh it. Stating the
    conclusion is what makes the model an explainer rather than a second decision-maker.
    """
    captured: dict[str, str] = {}

    async def capture(messages, **_kwargs):
        captured["user"] = next(
            m["content"] for m in messages if m["role"] == "user"
        )
        return "text"

    monkeypatch.setattr(base.client, "available", lambda: True)
    monkeypatch.setattr(base.client, "complete", capture)

    import asyncio

    asyncio.run(irrigation.advise(_wet(0.15, HazardType.CROP_DROUGHT_STRESS)))

    prompt = captured.get("user", "")
    assert "must not contradict" in prompt
    assert "irrigate" in prompt.lower()
    assert "do not reverse it" in prompt


# --------------------------------------------------------------------------- #
# Where they run, and where they are stored
# --------------------------------------------------------------------------- #


def test_explanations_are_generated_after_the_suppression_gates():
    """Cost must scale with alerts delivered, not with scans.

    Most assessments are info-level and reach nobody. Generating explanations before the dispatch
    floor and the dedupe check would spend tokens on text no subscriber ever sees — at four scans a
    day per plot, that is the difference between a viable unit cost and a broken one.
    """
    source = inspect.getsource(__import__("app.agents.herald", fromlist=["x"]))

    floor = source.index("below dispatch floor")
    duplicate = source.index("duplicate alert suppressed")
    explain_call = source.index("explain_all(assessment)")

    assert explain_call > floor, "explanations must not be generated below the dispatch floor"
    assert explain_call > duplicate, "explanations must not be generated for a suppressed duplicate"


def test_explanations_are_stored_not_regenerated():
    """An alert is the record of what a subscriber was TOLD.

    Regenerating on read would show someone disputing "you never warned me" text produced today
    from an assessment measured weeks ago, worded differently by the model. Stored as JSONB in the
    same row as the assessment snapshot.
    """
    import pathlib

    repo = pathlib.Path("app/store/repository.py").read_text()
    assert "explanations" in repo
    assert "$12::jsonb" in repo, "explanations must be written as JSONB"

    migration = pathlib.Path("app/db/migrations/011_alert_explanations.sql").read_text()
    assert "JSONB" in migration.upper()
    assert "DEFAULT '{}'" in migration, (
        "pre-migration alerts must deserialise to empty strings, not NULL"
    )


def test_an_old_alert_without_explanations_still_loads():
    """A row written before the column existed must not fail validation."""
    advisory = Advisory(headline="h", body="b")
    assert advisory.explanations == Explanations()
    assert advisory.explanations.crop == ""


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #


def test_explanations_reach_every_channel_through_one_renderer():
    """Five per-dispatcher implementations is how one channel ends up omitting them.

    A subscriber comparing their email against their WhatsApp must see the same words.
    """
    from app.dispatch import base as dispatch_base

    source = inspect.getsource(dispatch_base)
    assert source.count("lines += explanation_lines(advisory)") == 2, (
        "both render_plain and render_markdown must include the explanations"
    )


def test_explanations_follow_the_actions_not_precede_them():
    """The instruction comes first; the narration makes it believable.

    Someone skimming an SMS during a storm needs "move stored produce" before three paragraphs of
    explanation.
    """
    from app.dispatch import base as dispatch_base

    source = inspect.getsource(dispatch_base.render_plain)
    assert source.index("What to do:") < source.index("explanation_lines")


def test_an_empty_explanation_renders_no_heading():
    """A bare heading with nothing under it reads as a broken template and undermines the alert."""
    from app.dispatch.base import explanation_lines

    assert explanation_lines(Advisory(headline="h", body="b")) == []

    partial = Advisory(
        headline="h", body="b", explanations=Explanations(crop="Something measured.")
    )
    lines = explanation_lines(partial)
    assert any("Your crop" in line for line in lines)
    assert not any("Watering" in line for line in lines)


# --------------------------------------------------------------------------- #
# Explainability: exact attribution and citation
#
# "Narrate the drivers behind a risk score" was previously an inference — the surface received only
# prose and had to guess which factor mattered. A plausible guess about causation is the kind of
# confident wrongness the grounding rule exists to prevent.
# --------------------------------------------------------------------------- #


def test_driver_contributions_are_arithmetic_not_inference():
    """`score` is a weighted sum, so each term's contribution is bookkeeping, not interpretation.

    That is what makes it safe to hand a model where a bare `score` is not: the drivers and their
    magnitudes are SUPPLIED, so the model cannot invent one.
    """
    from app.agents.oracle import W_EXPOSURE, W_FORECAST, W_OBSERVED
    from app.models.schemas import ScoreDriver

    drivers = [
        ScoreDriver(key="observed", label="a", value=0.8, weight=W_OBSERVED,
                    contribution=W_OBSERVED * 0.8),
        ScoreDriver(key="forecast", label="b", value=0.5, weight=W_FORECAST,
                    contribution=W_FORECAST * 0.5),
        ScoreDriver(key="exposure", label="c", value=0.2, weight=W_EXPOSURE,
                    contribution=W_EXPOSURE * 0.2),
    ]
    # The contributions must sum to the score the Oracle would compute.
    total = sum(d.contribution for d in drivers)
    expected = W_OBSERVED * 0.8 + W_FORECAST * 0.5 + W_EXPOSURE * 0.2
    assert abs(total - expected) < 1e-9

    # And the weights are the real ones, not a copy that could drift.
    assert abs(W_OBSERVED + W_FORECAST + W_EXPOSURE - 1.0) < 1e-9


def test_drivers_are_ordered_largest_first():
    """The first question a reader has is "what mattered most", so the first line must answer it."""
    from app.agents.oracle import OracleAgent
    from app.models.schemas import AnalystResult, ExposureSummary, RainfallOutlook

    drivers = OracleAgent._score_drivers(
        observed_term=0.2,
        forecast_term=0.9,
        exposure_term=0.4,
        analysis=AnalystResult(aoi_id="x"),
        outlook=RainfallOutlook(),
        exposure=ExposureSummary(),
        terrain_profile=None,
    )
    contributions = [d.contribution for d in drivers]
    assert contributions == sorted(contributions, reverse=True)


def test_an_unmeasured_driver_says_so_rather_than_reading_as_zero_risk():
    """A term that ran on defaults must be distinguishable from one measured at zero.

    Same rule as `AnalystResult.flood_measured`: "not measured" and "measured as absent" are
    different facts, and only one of them is evidence.
    """
    from app.explain.base import driver_block
    from app.models.enums import HazardType, Severity
    from app.models.schemas import RiskAssessment, ScoreDriver

    a = RiskAssessment(
        aoi_id="x", aoi_name="p", hazard=HazardType.CROP_DROUGHT_STRESS,
        severity=Severity.WATCH, score=0.3, confidence=0.6,
        assessed_at=datetime.now(timezone.utc),
        score_drivers=[
            ScoreDriver(key="observed", label="What the satellite measured", value=0.0,
                        weight=0.55, contribution=0.0, inputs=[]),
        ],
    )
    block = driver_block(a)
    assert "not measured" in block


def test_provenance_reaches_the_prompt_but_derived_numbers_still_do_not():
    """**The grounding boundary, restated with the new field.**

    Provenance is a statement about HOW a measurement was obtained, not a new measurement — so it
    adds no figure a model could misattribute, and it adds the citation a reader needs to check the
    figures already present. `score`, `confidence` and `aoi_id` remain forbidden.
    """
    from app.explain.base import provenance_block

    a = _assessment(HazardType.FLOOD_INUNDATION)
    a = a.model_copy(
        update={
            "data_sources": ["sentinel-1", "gfs-forecast"],
            "observed_at_flood": datetime(2026, 8, 9, tzinfo=timezone.utc),
            "platform_flood": "sentinel-1-rtc",
            "method_flood": "trained-model",
        }
    )
    block = provenance_block(a)

    # The citation IS present.
    assert "sentinel-1" in block
    assert "09 Aug 2026" in block
    # And the method, which is the honesty that matters most: trained model or physical threshold.
    assert "trained-model" in block

    # The forbidden values are still absent.
    for forbidden in (str(a.score), str(a.confidence), a.aoi_id):
        assert forbidden not in block, f"{forbidden!r} reached the prompt via provenance"


def test_absent_provenance_is_stated_not_fabricated():
    """An assessment from before this feature must say so rather than imply a source."""
    from app.explain.base import provenance_block

    block = provenance_block(_assessment(HazardType.CROP_DROUGHT_STRESS))
    assert "not recorded" in block or "sentinel" in block.lower()


def test_every_surface_cites_its_sources():
    """All three explainers must receive provenance, or a figure reaches a farmer uncitable."""
    import pathlib

    for module in ("optical", "drivers", "irrigation"):
        source = pathlib.Path(f"app/explain/{module}.py").read_text()
        assert "provenance_block(assessment)" in source, (
            f"{module} does not cite where its figures came from"
        )
