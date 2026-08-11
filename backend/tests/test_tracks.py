"""The per-track report-card modules, and the single-source rule that keeps them honest.

## What is being protected

Three surfaces describe one plot: the alert email, the portal's alert card, and the plain-text
channels. They have already drifted apart twice in this codebase — the card content before
`card_fields`, the email chrome before the shared layout — and both times the symptom was a
subscriber reading two different summaries of the same event.

The tracks multiply that risk, because each one carries an agronomic *threshold* as well as a
number. "Warn above 25% inundation" written in two places is a divergence waiting for one of them to
be tuned. So the thresholds exist exactly once, in `app/dispatch/tracks.py`, and the tests below
assert that neither the frontend nor any other backend module keeps a copy.

## The absent-data rule, restated because a module view makes breaking it tempting

`available=False` produces **no track**, never a track reading zero. A module saying
"Soil water: 0.00 m3/m3" claims the pore space is empty; the honest statement is that SMAP did not
overfly this cell. An empty module is visually obvious in a way an absent row is not, which is
exactly what invites someone to fill it in with a default.

`0.0` from a source that *did* answer is a different thing and must render: "no standing water
detected" is a useful, reassuring answer, and it is the whole point of the INFO heartbeat.
"""

from __future__ import annotations

import pathlib
import re
from datetime import UTC, datetime, timedelta

from app.dispatch.email_channel import EmailDispatcher
from app.dispatch.tracks import _ACUTE, _NOTABLE, _REASSURING, tracks
from app.email import layout
from app.models.enums import HazardType, Severity
from app.models.schemas import (
    Advisory,
    DataFreshness,
    Explanations,
    ForecastPoint,
    HealthBaseline,
    RiskAssessment,
    SituationChange,
    SoilMoisture,
    SoilProfile,
)

NOW = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)
FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"


def _assessment(**kwargs) -> RiskAssessment:
    base = dict(
        aoi_id="aoi_1",
        aoi_name="Alspecs Farms Kobape",
        hazard=HazardType.CROP_WATERLOGGING,
        severity=Severity.WARNING,
        score=0.71,
        confidence=0.88,
    )
    return RiskAssessment(**{**base, **kwargs})


def _advisory() -> Advisory:
    return Advisory(
        aoi_id="aoi_1",
        headline="Standing water on a third of your field",
        body="Radar measured water across 31% of the plot yesterday.",
        actions=["Clear the drainage channel on the low side"],
        explanations=Explanations(crop="Maize tolerates two to three days."),
        severity=Severity.WARNING,
        hazard=HazardType.CROP_WATERLOGGING,
    )


# --------------------------------------------------------------------------- #
# Absent data must never become a zero reading
# --------------------------------------------------------------------------- #


def test_nothing_measured_produces_no_modules():
    """A fully clouded cycle with no radar pass.

    Zero tracks, so the surface can say "we could not look". Five modules reading zero would assert
    a healthy, dry, unstressed field on the strength of no measurement at all.
    """
    assert tracks(_assessment()) == []


def test_an_unavailable_source_is_omitted_rather_than_zeroed():
    """SMAP is a swath instrument and does not overfly every cell every day."""
    result = tracks(
        _assessment(
            stressed_crop_fraction=0.4,
            soil_moisture=SoilMoisture(volumetric=0.0, available=False),
            health=HealthBaseline(malaria_pfpr=0.0, endemic=False, available=False),
        )
    )
    keys = {t.key for t in result}
    assert "soil_water" not in keys, "an unavailable SMAP reading became a 0.00 m3/m3 module"
    assert "malaria" not in keys, "unknown endemicity asserted a malaria baseline"
    assert keys == {"crop"}


def test_a_measured_zero_does_produce_a_module():
    """The distinction that makes the heartbeat worth sending.

    `None` means we did not look; `0.0` means we looked and found nothing. The second is the
    reassuring answer an INFO alert exists to deliver, so it must render.
    """
    result = tracks(_assessment(inundated_fraction=0.0, stressed_crop_fraction=0.0))
    by_key = {t.key: t for t in result}

    assert set(by_key) == {"flood", "crop"}
    assert by_key["flood"].weight == _REASSURING
    assert "No standing water" in by_key["flood"].meaning


def test_unknown_endemicity_asserts_nothing_even_when_a_rate_is_present():
    """`available` and `endemic` are both required — matching `OracleAgent._cascade`."""
    for health in (
        HealthBaseline(malaria_pfpr=0.4, endemic=True, available=False),
        HealthBaseline(malaria_pfpr=0.4, endemic=False, available=True),
    ):
        result = tracks(_assessment(inundated_fraction=0.1, health=health))
        assert "malaria" not in {t.key for t in result}


# --------------------------------------------------------------------------- #
# Ordering — the bug that shipped in the first draft
# --------------------------------------------------------------------------- #


def test_the_module_explaining_the_alert_leads():
    """**The ranking bug this test exists for.**

    An early version sorted on the raw readings, which are not comparable: 0.31 is a third of the
    field under water (severe) for inundation, a comfortable 0.31 m3/m3 for soil water, and 126 mm is
    an ordinary wet-season week. On a *waterlogging* alert with 31% inundation, standing water ranked
    **third** — below routine rain and below the soil reading.

    Each builder now maps its own measurement onto the shared concern ladder with its own domain
    thresholds, so the module that says "clear drainage now" also sorts to the top.
    """
    result = tracks(
        _assessment(
            hazard=HazardType.CROP_WATERLOGGING,
            inundated_fraction=0.31,
            stressed_crop_fraction=0.18,
            soil_moisture=SoilMoisture(volumetric=0.487, available=True),
            forecast=[
                ForecastPoint(day=d, date=NOW + timedelta(days=d), risk=0.5, rainfall_mm=18.0)
                for d in range(7)
            ],
            forecast_is_prediction=True,
            health=HealthBaseline(malaria_pfpr=0.29, endemic=True, available=True),
        )
    )

    assert result[0].key == "flood", (
        f"a waterlogging alert with 31% inundation led with {result[0].key!r}; "
        f"order was {[t.key for t in result]}"
    )


def test_rain_never_outranks_a_measurement_of_the_plot():
    """Rain drives hazards but is not one.

    126 mm over a week is a lot of water and still tells a farmer less than "a third of your field
    is under water right now". A forecast of what may arrive must not outrank a measurement of what
    is already there.
    """
    result = tracks(
        _assessment(
            inundated_fraction=0.31,
            forecast=[
                ForecastPoint(day=d, date=NOW + timedelta(days=d), risk=0.9, rainfall_mm=60.0)
                for d in range(7)
            ],
            forecast_is_prediction=True,
        )
    )
    by_key = {t.key: t for t in result}
    assert by_key["rainfall"].weight <= _NOTABLE
    assert by_key["flood"].weight > by_key["rainfall"].weight


def test_a_saturated_reading_outranks_the_classified_hazard_when_it_is_worse():
    """Observed live at Yenagoa: a plot measuring 0.593 m3/m3 under a vegetation-anomaly label.

    The measurement wins. A fixed order keyed on the classification would bury the reading that
    actually matters, and "irrigate" must never be the top module when the pore space is full.
    """
    result = tracks(
        _assessment(
            hazard=HazardType.CROP_VEGETATION_ANOMALY,
            stressed_crop_fraction=0.12,
            soil_moisture=SoilMoisture(volumetric=0.593, available=True),
        )
    )
    assert result[0].key == "soil_water"
    assert "not irrigate" in result[0].meaning.lower()


def test_an_extreme_reading_reaches_the_top_band():
    """Sanity: the ladder's top is reachable, so `_ACUTE` is not dead code."""
    result = tracks(_assessment(inundated_fraction=0.62))
    assert result[0].weight == _ACUTE


# --------------------------------------------------------------------------- #
# Forecast and antecedent are different things
# --------------------------------------------------------------------------- #


def test_antecedent_rain_is_not_worded_as_a_forecast():
    """Only GEFS predicts. CHIRPS, IMERG and ERA5 report how wet the ground already is.

    `ForecastPoint` carries no flag for this, so an early draft read `getattr(p, "forecast", False)`
    — silently always False, which would have labelled every real forecast as rain already fallen.
    The flag now travels on the assessment.
    """
    points = [
        ForecastPoint(day=d, date=NOW + timedelta(days=d), risk=0.4, rainfall_mm=20.0)
        for d in range(7)
    ]

    already = next(
        t
        for t in tracks(_assessment(forecast=points, forecast_is_prediction=False))
        if t.key == "rainfall"
    )
    assert "already fallen" in already.meaning
    assert "not a forecast" in already.meaning

    predicted = next(
        t
        for t in tracks(_assessment(forecast=points, forecast_is_prediction=True))
        if t.key == "rainfall"
    )
    assert "forecast" in predicted.meaning
    assert "already fallen" not in predicted.meaning


def test_the_prediction_flag_defaults_to_the_safe_reading():
    """False by default.

    Describing a real forecast as rain already fallen understates a warning; the reverse invents a
    prediction nobody made. Understating is the recoverable error.
    """
    assert _assessment().forecast_is_prediction is False


# --------------------------------------------------------------------------- #
# Every hazard has a track mapping
# --------------------------------------------------------------------------- #


def test_every_hazard_maps_to_a_track():
    """A new `HazardType` with no mapping would silently never win a tie-break.

    Not a crash — just a card whose modules disagree with its headline about what the alert is
    about, which is the kind of defect nobody reports.
    """
    from app.dispatch.tracks import _HAZARD_TRACK

    unmapped = sorted(h.value for h in HazardType if h not in _HAZARD_TRACK)
    assert not unmapped, (
        f"these hazards have no track mapping: {unmapped}. Add them to `_HAZARD_TRACK` so the "
        f"modules and the headline agree about what the alert is about."
    )


def test_the_mapping_names_only_real_tracks():
    """A typo'd target would silently never match, so the tie-break would quietly not happen."""
    from app.dispatch.tracks import _HAZARD_TRACK

    known = {"flood", "crop", "soil_water", "rainfall", "malaria"}
    assert set(_HAZARD_TRACK.values()) <= known


# --------------------------------------------------------------------------- #
# One source for the thresholds
# --------------------------------------------------------------------------- #


def test_the_frontend_keeps_no_copy_of_the_thresholds():
    """**The single-source rule, enforced.**

    Every band, sentence and cut-off lives in `app/dispatch/tracks.py`. A TypeScript copy is how the
    email and the portal would come to describe one plot differently — the same drift `card_fields`
    and the shared email layout were each written to end, and both of those started as one
    "harmless" duplicated constant.

    Checks the track component and the types for the numeric cut-offs specifically, because those are
    what a well-meaning refactor reintroduces.
    """
    suspects = [
        FRONTEND / "components/TrackModules.tsx",
        FRONTEND / "lib/types.ts",
    ]
    # The inundation and crop-stress cut-offs, as they appear in the builders.
    cutoffs = ("0.005", "0.05", "0.15", "0.30", "0.25", "0.50", "150.0", "0.35")

    for path in suspects:
        if not path.exists():  # pragma: no cover - backend-only checkout
            continue
        source = path.read_text()
        for cutoff in cutoffs:
            assert cutoff not in source, (
                f"{path.name} contains the threshold {cutoff}, which belongs only in "
                f"app/dispatch/tracks.py. Render `reading` and `meaning` as the server sends them."
            )


def test_the_frontend_does_not_reword_a_reading():
    """The wording is the backend's too.

    A phrase like "under water" appearing in the component means it is composing its own sentence
    from the numbers rather than rendering `meaning`, which is the same divergence one level down.
    """
    component = FRONTEND / "components/TrackModules.tsx"
    if not component.exists():  # pragma: no cover
        return

    body = component.read_text()
    # Strip comments: the rationale legitimately quotes the backend's phrasing to explain itself.
    code = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    code = re.sub(r"//.*$", "", code, flags=re.M)

    for phrase in ("under water", "stressed", "Irrigate", "saturated", "mm expected"):
        assert phrase not in code, (
            f"{phrase!r} is composed in the component; render the server's `meaning` instead"
        )


# --------------------------------------------------------------------------- #
# Chrome parity — the alert email must look like the rest of the platform
# --------------------------------------------------------------------------- #


def _alert_html() -> str:
    assessment = _assessment(
        inundated_fraction=0.31,
        stressed_crop_fraction=0.18,
        soil_moisture=SoilMoisture(volumetric=0.487, available=True),
        soil=SoilProfile(drainage="impeded", available=True),
        health=HealthBaseline(malaria_pfpr=0.29, endemic=True, available=True),
        change=SituationChange(previous_severity="watch", direction="up"),
        freshness=DataFreshness(observed_at=NOW, platform="sentinel-1-rtc"),
        data_sources=["sentinel-1-rtc", "smap-l3"],
        evidence=["31% of the plot is under standing water"],
    )
    return EmailDispatcher._html(_advisory(), assessment)


def test_the_alert_email_carries_the_shared_chrome():
    """**The drift this closes.**

    Eleven kinds of account mail went through `email/layout`; the hazard advisory built its own
    document and so had no SHELTER mark, no footer, no FreePass/NIGCOMSAT attribution, a different
    hairline colour, no preheader and no dark-mode rule.

    That asymmetry is worse than ordinary inconsistency: the one email a subscriber receives *because
    the service is working* was the only one that did not look like the service, and deciding whether
    to act on a flood warning starts with deciding whether it is genuine.
    """
    html = _alert_html()

    for description, probe in (
        ("SHELTER wordmark", ">SHELTER<"),
        ("satellite mark", 'viewBox="0 0 40 40"'),
        ("FreePass logo", 'alt="FreePass"'),
        ("NIGCOMSAT logo", 'alt="NIGCOMSAT"'),
        ("footer attribution", "FreePass Holding Co"),
        ("inbox preheader", "max-height:0;overflow:hidden;opacity:0"),
        ("dark-mode rule", "prefers-color-scheme: dark"),
        ("shared hairline token", layout.HAIRLINE),
    ):
        assert probe in html, f"the alert email is missing the {description}"


def test_the_alert_email_uses_no_private_colour_literals():
    """It carried `#3407561a` — a hairline the shared layout does not use.

    One divergent literal is how a second design system starts. The tokens are in `email/layout`.
    """
    assert "#3407561a" not in _alert_html()


def test_the_severity_colours_the_header_and_only_the_header():
    """An EMERGENCY should read red before a word is parsed.

    `accent` is the single parameter an advisory may vary, deliberately: a second `render_alert()`
    entry point would let the chrome fork again, one divergence at a time.
    """
    html = _alert_html()
    assert "#dd7400" in html, "the WARNING accent did not reach the header band"

    # **Confined to the header.** Asserting the brand purple is also present would be wrong here and
    # was: an advisory carries no call-to-action button, so `BRAND` legitimately does not appear at
    # all. What matters is that the severity colour has not leaked into the body or the footer, where
    # it would restyle furniture the rest of the platform shares.
    header, _, rest = html.partition("</td>")
    assert "#dd7400" in header, "the accent is not in the header cell"
    assert "#dd7400" not in rest, (
        "the severity colour reaches past the header band; only the header may vary"
    )

    # And the footer is the shared one, untinted.
    assert layout.HAIRLINE in rest
    assert "FreePass Holding Co" in rest


def test_the_email_renders_every_module_the_portal_would():
    """Same list, same order, two layouts.

    A subscriber who reads the email and then opens the portal must find the same figures. Comparing
    against `tracks()` directly is what makes that assertable rather than aspirational.
    """
    assessment = _assessment(
        inundated_fraction=0.31,
        stressed_crop_fraction=0.18,
        soil_moisture=SoilMoisture(volumetric=0.487, available=True),
        health=HealthBaseline(malaria_pfpr=0.29, endemic=True, available=True),
    )
    html = EmailDispatcher._html(_advisory(), assessment)

    expected = tracks(assessment)
    assert expected, "fixture measured nothing; this test would pass vacuously"

    for track in expected:
        assert track.label in html, f"{track.label!r} is missing from the email"
        assert track.reading in html, f"{track.reading!r} is missing from the email"

    # Order preserved, not merely presence.
    positions = [html.index(t.label) for t in expected]
    assert positions == sorted(positions), (
        "the email renders the modules in a different order than the portal receives them"
    )


def test_an_assessment_with_no_measurements_renders_no_module_section():
    """A heading with nothing under it reads as a broken email."""
    html = EmailDispatcher._html(_advisory(), _assessment())
    assert "What we measured" not in html
