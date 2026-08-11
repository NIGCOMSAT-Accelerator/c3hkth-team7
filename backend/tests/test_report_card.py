"""The report card — answer first, evidence on demand.

Modelled on Apple Weather's overview-plus-drilldown: the questions that decide whether to act TODAY
sit at the top of every alert, and the reasoning stays one tap (or one scroll) below.

## What the card answers that severity alone cannot

WATCH that has been WATCH for a fortnight is a background condition. WATCH that was INFO yesterday
is a developing event. Same label, opposite urgency — only the comparison separates them, and
without it every alert reads with the same weight, which is how a real escalation gets skimmed.

## The rule these tests mostly exist to protect

**A field is omitted when its input is unknown, never defaulted.** A first assessment has no
previous run; a plot with no fitted baseline has no seasonal norm; a clouded cycle measured no crop
condition. Printing "no change" or "normal" there would assert something false — and this is a
platform whose entire credibility rests on not inventing numbers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.dispatch.base import card_fields, render_markdown, render_plain, situation_lines
from app.models.enums import HazardType, Severity
from app.models.schemas import (
    Advisory,
    DataFreshness,
    Explanations,
    RiskAssessment,
    SituationChange,
    SoilMoisture,
)

NOW = datetime(2026, 8, 11, 16, 30, tzinfo=timezone.utc)


def _assessment(**overrides) -> RiskAssessment:
    base = {
        "aoi_id": "aoi_x",
        "aoi_name": "Test plot",
        "hazard": HazardType.CROP_VEGETATION_ANOMALY,
        "severity": Severity.WATCH,
        "score": 0.41,
        "confidence": 0.88,
    }
    return RiskAssessment(**{**base, **overrides})


def _advisory() -> Advisory:
    return Advisory(
        headline="Crop stress rising",
        body="Vegetation is below its seasonal norm.",
        actions=["Check the field"],
        broadcast_text="stress",
        language="en",
        generated_by="test",
        explanations=Explanations(crop="", drivers="", irrigation=""),
    )


def test_a_first_assessment_claims_no_comparison():
    """**The rule.** Nothing to compare against must render as nothing, not as "no change"."""
    fields = dict(card_fields(_assessment()))

    assert "Status" in fields, "the status is the answer and must always be present"
    assert "Since last check" not in fields, (
        "a first-ever assessment reports a change it cannot know"
    )
    assert "Compared with normal" not in fields, (
        "a plot with no fitted baseline reports a seasonal comparison it cannot know"
    )
    assert "Soil water" not in fields, "no soil measurement produced an irrigation instruction"


def test_a_rising_reading_says_so_and_names_the_previous_level():
    """"Rising" alone is not checkable; "rising, was INFO" is."""
    fields = dict(
        card_fields(
            _assessment(
                change=SituationChange(
                    previous_severity="info", previous_score=0.10, direction="up"
                )
            )
        )
    )

    assert fields["Since last check"] == "Rising — was INFO"


def test_steady_is_reported_rather_than_hidden():
    """A standing condition is a real answer, and it is the one that says "do not panic".

    Hiding it would leave the reader unable to tell "unchanged" from "we did not check".
    """
    fields = dict(
        card_fields(
            _assessment(
                change=SituationChange(previous_severity="watch", direction="steady")
            )
        )
    )
    assert "Unchanged" in fields["Since last check"]


def test_confidence_is_a_word_not_a_percentage():
    """"62%" invites arithmetic nobody should perform on a confidence.

    The bands map to `CONFIDENCE_ESCALATION_FLOOR` — below 0.65 severity is capped at WATCH, so
    "low" is not a vague adjective, it names the regime the reading is in.
    """
    for confidence, expected in ((0.9, "High"), (0.7, "Medium"), (0.55, "Low")):
        fields = dict(card_fields(_assessment(confidence=confidence)))
        assert fields["Confidence"] == expected, f"{confidence} -> {expected}"
        assert "%" not in fields["Confidence"]


def test_the_irrigation_call_is_a_verb_not_a_number():
    """A farmer acts on "irrigate", not on 0.19 m3/m3 — but the figure is shown so it is checkable.

    `drain` must never read as merely "wet": at saturation the pore space is full and roots are
    anaerobic, which is an action rather than a state.
    """
    saturated = _assessment(
        soil_moisture=SoilMoisture(volumetric=0.52, available=True, observed_date="2026-08-09")
    )
    value = dict(card_fields(saturated))["Soil water"]

    assert "Do not irrigate" in value
    assert "0.52" in value, "the measurement is not shown, so the instruction cannot be checked"


def test_freshness_states_the_last_look_and_the_absent_leg():
    """A "no flooding detected" from a six-day-old pass is a different claim from a fresh one.

    And an absent leg is a fact about the reading, not a blank — a subscriber cannot otherwise tell
    "radar saw no water" from "radar did not look".
    """
    fields = dict(
        card_fields(
            _assessment(
                freshness=DataFreshness(
                    observed_at=NOW,
                    platform="sentinel-1-rtc",
                    next_expected=NOW + timedelta(days=6),
                    caveat="Cloud blocked the optical view, so crop condition was not measured",
                )
            )
        )
    )

    assert "sentinel-1-rtc" in fields["Last look"]
    assert "11 Aug" in fields["Last look"]
    assert "Around 17 Aug" == fields["Next expected"]
    assert "Cloud blocked" in fields["Note"]


def test_text_and_html_render_the_same_content():
    """One source, so a subscriber's SMS and their email cannot summarise one alert differently.

    That drift is exactly what `email_layout` was created to end for account mail, and an alert is
    the message where it would matter most.
    """
    assessment = _assessment(
        change=SituationChange(previous_severity="info", direction="up", vs_seasonal="browner"),
        freshness=DataFreshness(observed_at=NOW, platform="sentinel-2"),
    )

    fields = card_fields(assessment)
    lines = situation_lines(assessment)

    # Every field appears in the text rendering, label and value.
    for label, value in fields:
        assert any(label in line and value in line for line in lines), (
            f"{label!r} is in card_fields but not rendered as text"
        )


def test_the_card_precedes_the_advisory_prose():
    """Answer first. The reasoning is what makes an alert judgeable, but it is several paragraphs
    and a farmer decides in the first few seconds."""
    assessment = _assessment(
        change=SituationChange(previous_severity="info", direction="up")
    )
    advisory = _advisory()

    for rendered in (
        render_plain(advisory, assessment),
        render_markdown(advisory, assessment),
    ):
        glance_at = rendered.find("AT A GLANCE")
        body_at = rendered.find(advisory.body)
        assert glance_at >= 0, "the card is missing from a renderer"
        assert glance_at < body_at, "the prose comes before the card, burying the answer"
        # And nothing was removed.
        assert advisory.body in rendered
        assert advisory.actions[0] in rendered


def test_derived_soil_fields_are_on_the_wire():
    """`status` and `irrigation_advice` are computed fields, not bare properties.

    Without `@computed_field` a client sees only `volumetric` and has to re-implement the
    wilting-point and field-capacity thresholds — a second copy of an agronomic decision, in a
    language that cannot import the first. The web card and the email would then be one edit away
    from disagreeing about whether to water a field.
    """
    dumped = SoilMoisture(volumetric=0.19, available=True).model_dump()

    assert dumped["status"] == "dry"
    assert dumped["irrigation_advice"] == "irrigate"
