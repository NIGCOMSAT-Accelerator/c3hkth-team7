"""The per-track modules of a report card — one definition, three renderings.

## What a "track" is, and why the card needed splitting up

`card_fields` answers *what should I do about this alert?* in six rows. That is the right answer to
the right question, and it is deliberately short — a farmer decides in the first few seconds.

It is the wrong shape for the second question, which arrives a moment later: *what is actually
happening on my field?* An alert classified `crop_stress` still measured standing water, soil
moisture, rainfall and a malaria baseline. Those measurements exist, they are already on the
`RiskAssessment`, and before this module the only place they surfaced was a flat `evidence` list of
English sentences — unstructured, unrankable, and impossible to render as anything but prose.

Apple Weather is the reference the user asked for, and the pattern worth taking is not the
animation: it is that the summary is one glanceable answer and **each contributing measurement is
its own module you can open**. Precipitation, UV, wind and air quality are separate cards with
their own units and their own baselines, because they are separate physical quantities. Flooding,
crop health, soil water, rainfall and malaria are too.

## The rule that makes this safe: a track is built from a measurement or it is ABSENT

Every field below traces to a number the Analyst measured or a source that answered. There is no
`or 0`, no "assume normal", no interpolation. `available=False` produces **no track**, not a track
reading zero — because a module saying "Soil water: 0.00 m3/m3" is a claim that the pore space is
empty, and the honest statement is that SMAP did not overfly this cell.

That is the same discipline `card_fields` follows for its rows and `_exposure_term` follows in the
Oracle, and it is the one that has been violated twice in this codebase's history (a hard-coded
"35% of the area is cropland", a 75/25 split of an OSM count). A per-track view multiplies the
opportunity, because an empty module is visually obvious and invites filling in.

## Why the ordering is by measured concern, not by a fixed list

The tracks are returned most-relevant-first. A plot whose flood fraction is 62% should lead with
water even when the Oracle classified the hazard as a vegetation anomaly — a real case, observed at
Yenagoa, where a saturated soil-moisture measurement sat under a crop-stress classification. A fixed
order would bury the most important module on exactly the alerts where it matters most.

**The sort key is a concern band, not the raw reading**, and that distinction is load-bearing. The
readings are not comparable: 0.31 is a third of the field under water (severe) for inundation and a
comfortable 0.31 m3/m3 for soil water; 126 mm is an ordinary wet-season week. Sorting the raw values
put routine rain and a saturated soil reading above 31% inundation on a *waterlogging* alert — the
module that explained the alert ranked third. Each builder now maps its own measurement onto the
shared `_ACUTE.._REASSURING` ladder with its own domain thresholds, set next to the wording so the
module that says "drowns roots within days" cannot also sort to the bottom.

Two consequences worth stating, because they are choices rather than fallout:

  * **Rain is capped at `_NOTABLE`.** It drives hazards but is not one. A measurement of what is on
    the plot outranks a forecast of what may arrive.
  * **Malaria never leads.** It is a district baseline, not a change on this plot.

The classified hazard's own track breaks ties, so the card and the modules agree about what the alert
is about.

## Rendered by three surfaces, and this module renders none of them

`tracks(assessment)` returns data. `dispatch/email_html` lays it out as tables, the portal lays it
out as tappable cards, and `situation_lines` ignores it entirely (SMS has no room and the card rows
are the priority there). Keeping the layout out of here is what lets the email and the web card be
different shapes without becoming different *content* — the same mistake `card_fields` was written
to prevent one level up.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.enums import HazardType
from app.models.schemas import RiskAssessment


@dataclass(frozen=True)
class Track:
    """One measured dimension of a plot's situation.

    `key` is stable and machine-readable (the frontend keys icons and routes off it); `label` is
    what a subscriber reads. `reading` is the headline value already formatted with its unit, so no
    consumer has to know that soil moisture is m3/m3 and inundation is a percentage.
    """

    key: str
    label: str
    #: The headline value, formatted with its unit. Never empty — a track with nothing to say is
    #: not constructed at all.
    reading: str
    #: One sentence of plain language: what this reading means for this field.
    meaning: str
    #: `rising` | `easing` | `steady` | None. None means there is no prior reading to compare.
    direction: str | None = None
    #: 0..1 **concern**, used only for ORDERING the modules. Not shown — a farmer should not be
    #: invited to do arithmetic on a severity, and `card_fields` already bands confidence into words
    #: for the same reason.
    #:
    #: **This is not the raw reading.** Each builder maps its own measurement onto the shared
    #: `_ACUTE.._REASSURING` ladder, because the raw numbers are not comparable: 0.31 means "a third of the
    #: field is under water" (severe) for inundation and "0.31 m3/m3" (comfortable) for soil water.
    #: An early draft sorted on the raw values and buried 31% inundation below routine wet-season
    #: rain on a waterlogging alert — the module that explained the alert ranked third.
    weight: float = 0.0
    #: Which upstream datasets produced this reading. The provenance a subscriber can check.
    sources: list[str] = field(default_factory=list)
    #: Detail rows, `(label, value)`, shown when the module is opened.
    detail: list[tuple[str, str]] = field(default_factory=list)


#: Which track a hazard classification is "about", for tie-breaking.
#:
#: A hazard with no entry simply does not win ties — that is correct rather than a gap, because
#: `HazardType` includes classifications like `malaria_risk` that have their own track and others
#: that are cascades rather than measurements.
_HAZARD_TRACK: dict[HazardType, str] = {
    HazardType.FLOOD_INUNDATION: "flood",
    HazardType.FLOOD_FORECAST: "flood",
    # Waterlogging is a water problem the crop is suffering from, so standing water is the module
    # that explains it. The crop-health module still appears — it just does not win the tie.
    HazardType.CROP_WATERLOGGING: "flood",
    HazardType.CROP_DROUGHT_STRESS: "soil_water",
    HazardType.CROP_VEGETATION_ANOMALY: "crop",
    HazardType.MALARIA_RISK: "malaria",
}


#: The shared concern ladder every track maps onto.
#:
#: Ordering modules requires one comparable scale, and the raw readings are not one: an inundated
#: fraction, a volumetric water content and a millimetre total have different units *and* different
#: severity curves. So each builder classifies its own reading into one of these bands using its own
#: domain thresholds, and only the band is compared.
#:
#: The bands are deliberately coarse. A finer scale would imply the thresholds are precise, and they
#: are agronomic judgements — 25% inundation is not meaningfully different from 27%.
_ACUTE = 0.90  # needs action today
_HIGH = 0.70  # needs action this week
_NOTABLE = 0.45  # worth knowing, not urgent
_ROUTINE = 0.20  # measured, normal
_REASSURING = 0.05  # measured, nothing wrong — still worth showing


def _pct(fraction: float) -> str:
    """A percentage with no decimal place.

    `62%`, not `61.8%`. The Analyst's precision is real but the *accuracy* over a 20 m pixel grid
    does not support a tenth of a percent, and a decimal invites the reader to trust a digit the
    measurement cannot carry.
    """
    return f"{round(fraction * 100)}%"


def _flood_track(a: RiskAssessment) -> Track | None:
    """Standing water, from the SAR water mask.

    Absent when no radar leg ran this cycle — `inundated_fraction is None`. Note that **0.0 is a
    real measurement** and must produce a track: "no standing water detected" is a useful and
    reassuring answer, and is exactly what a heartbeat INFO alert exists to say. Only `None` means
    we did not look.
    """
    if a.inundated_fraction is None:
        return None

    fraction = a.inundated_fraction
    # Thresholds are agronomic, and the concern band is set alongside the wording so the two cannot
    # disagree: the module that says "drowns roots within days" must also sort to the top.
    if fraction <= 0.005:
        meaning = "No standing water detected on your plot in the latest radar pass."
        concern = _REASSURING
    elif fraction < 0.05:
        meaning = "A small amount of standing water. Usually normal near channels and low corners."
        concern = _ROUTINE
    elif fraction < 0.15:
        meaning = "Part of your plot is under water. Check drainage on the low side before it spreads."
        concern = _NOTABLE
    elif fraction < 0.30:
        meaning = (
            "A substantial part of your plot is under water. Clear drainage now — most crops "
            "tolerate only two to three days of it."
        )
        concern = _HIGH
    else:
        meaning = (
            "A large part of your plot is under water. Standing water this extensive drowns "
            "roots within days."
        )
        concern = _ACUTE

    detail: list[tuple[str, str]] = []
    if a.method_flood:
        detail.append(("How it was measured", a.method_flood))
    if a.observed_at_flood:
        detail.append(("Radar pass", f"{a.observed_at_flood:%d %b %Y %H:%M} UTC"))

    return Track(
        key="flood",
        label="Standing water",
        reading=_pct(fraction),
        meaning=meaning,
        weight=concern,
        sources=[s for s in a.data_sources if "sentinel-1" in s or "sar" in s.lower()],
        detail=detail,
    )


def _crop_track(a: RiskAssessment) -> Track | None:
    """Vegetation stress, from the optical indices and the crop-stress model.

    `stress_attribution` names *why* the canopy is browner when the model could apportion it. It is
    a dict of driver → contribution, and it is rendered as detail rather than as the headline
    because attribution is the least certain thing on the card.
    """
    if a.stressed_crop_fraction is None:
        return None

    fraction = a.stressed_crop_fraction
    if fraction <= 0.005:
        meaning = "Your crop canopy looks healthy in the latest clear image."
        concern = _REASSURING
    elif fraction < 0.10:
        meaning = (
            "A small patch is showing stress. Worth a walk to that corner of the field."
        )
        concern = _ROUTINE
    elif fraction < 0.25:
        meaning = (
            "A noticeable share of your crop is under stress — browner or thinner than a "
            "healthy canopy."
        )
        concern = _NOTABLE
    elif fraction < 0.50:
        meaning = "A large share of your crop is under stress. Yield is affected at this extent."
        concern = _HIGH
    else:
        meaning = (
            "Most of your crop is showing stress. At this extent the cause is usually water, "
            "not pests."
        )
        concern = _ACUTE

    detail: list[tuple[str, str]] = []
    # Attribution, largest first, and only where the model actually apportioned something.
    for driver, share in sorted(
        a.stress_attribution.items(), key=lambda kv: -float(kv[1] or 0)
    ):
        if isinstance(share, int | float) and share > 0:
            detail.append(
                (f"Likely cause — {str(driver).replace('_', ' ')}", _pct(float(share)))
            )
    if a.method_stress:
        detail.append(("How it was measured", a.method_stress))
    if a.observed_at_stress:
        detail.append(("Clear image", f"{a.observed_at_stress:%d %b %Y %H:%M} UTC"))

    return Track(
        key="crop",
        label="Crop health",
        reading=f"{_pct(fraction)} stressed",
        meaning=meaning,
        weight=concern,
        sources=[s for s in a.data_sources if "sentinel-2" in s or "landsat" in s],
        detail=detail,
    )


def _soil_water_track(a: RiskAssessment) -> Track | None:
    """Root-zone soil water, from SMAP.

    Absent when `available` is False, which is the common case: SMAP is a swath instrument and does
    not overfly every cell every day. A track reading 0.00 would assert bone-dry soil.

    **The measurement outranks every heuristic**, which is why `irrigation_advice` is a computed
    field on the model rather than derived here. A plot can measure 0.593 m3/m3 while the Oracle
    classifies a vegetation anomaly, and "irrigate" must not be reachable when the pore space is
    already full.
    """
    sm = a.soil_moisture
    if not sm.available:
        return None

    advice = {
        "irrigate": "Irrigate if you can. The root zone is drier than your crop wants.",
        "hold": "No irrigation needed. There is enough water in the root zone.",
        "drain": "Do not irrigate. The soil is saturated — drain it if you have a way to.",
    }.get(sm.irrigation_advice or "", "")

    # BOTH ends of the scale are actionable — too dry and too wet each need doing something today —
    # and a comfortable reading is the one that can sit lower down the card. So this is a U-shape
    # rather than a ramp, which is exactly why soil water cannot share the fractions' mapping.
    concern = {
        "very dry": _ACUTE,
        "saturated": _HIGH,
        "dry": _HIGH,
        "wet": _NOTABLE,
        "adequate": _REASSURING,
    }.get(sm.status, _ROUTINE)

    detail = [("Reading", f"{sm.volumetric:.3f} m3/m3"), ("Condition", sm.status)]
    if sm.observed_date:
        detail.append(("Measured", sm.observed_date))
    if a.soil.available and a.soil.drainage != "unknown":
        detail.append(("Soil drainage", a.soil.drainage))
    # From iSDAsoil — see `eo/soil_texture.py`. Present exactly when `sm.status` was judged against
    # this plot's own wilting-point/field-capacity band rather than the wide loam default, so this
    # line is the reader-facing half of that provenance, not decoration.
    if sm.texture_class:
        detail.append(("Soil texture", sm.texture_class))

    return Track(
        key="soil_water",
        label="Soil water",
        reading=sm.status.capitalize(),
        meaning=advice or f"Root-zone soil water is {sm.status}.",
        weight=concern,
        sources=[s for s in a.data_sources if "smap" in s.lower() or "isda" in s.lower()],
        detail=detail,
    )


def _rainfall_track(a: RiskAssessment) -> Track | None:
    """The rainfall outlook, from whichever rung of the chain answered.

    **Forecast and antecedent are different things**, and this track must not collapse them. Only
    GEFS predicts; CHIRPS, IMERG and ERA5 report how wet the ground already is. The `meaning`
    wording therefore changes on `forecast_available`, because "120 mm expected" and "120 mm already
    fell" call for opposite actions.
    """
    if not a.forecast:
        return None

    total = sum(p.rainfall_mm for p in a.forecast)
    days = len(a.forecast)
    # Read from the assessment, NOT inferred from the points. `ForecastPoint` carries no such flag,
    # so an earlier draft of this used `getattr(p, "forecast", False)` — which is silently always
    # False and would have labelled every real GEFS forecast as rain that had already fallen.
    predicted = a.forecast_is_prediction

    if predicted:
        reading = f"{total:.0f} mm expected"
        meaning = (
            f"About {total:.0f} mm of rain is forecast over the next {days} days."
            if total >= 1
            else f"Little or no rain is forecast over the next {days} days."
        )
    else:
        reading = f"{total:.0f} mm already fell"
        meaning = (
            f"About {total:.0f} mm has already fallen over the past {days} days — this is how "
            f"wet the ground already is, not a forecast."
        )

    return Track(
        key="rainfall",
        label="Rain",
        reading=reading,
        meaning=meaning,
        # Banded on the weekly total. 150 mm is roughly where accumulation over a week starts
        # driving waterlogging on impeded soils; below ~10 mm there is nothing to plan around.
        #
        # Rain is capped at _NOTABLE deliberately, even when the total is extreme: it is a *driver*
        # of the hazard, not the hazard. A measurement of what is happening on the plot — water
        # standing on it, a canopy already browning — outranks a forecast of what may arrive. This
        # is the ranking bug that put routine wet-season rain above 31% inundation.
        weight=(_NOTABLE if total >= 150 else _ROUTINE if total >= 10 else _REASSURING),
        sources=[
            s
            for s in a.data_sources
            if any(k in s.lower() for k in ("chirps", "imerg", "era5", "gefs", "gfs"))
        ],
        detail=[("Window", f"{days} days"), ("Total", f"{total:.0f} mm")],
    )


def _malaria_track(a: RiskAssessment) -> Track | None:
    """The malaria baseline, from the Malaria Atlas.

    Asserted **only when endemic and available** — unknown endemicity asserts nothing, matching
    `_cascade` in the Oracle. This is a standing baseline for the district rather than a
    measurement of the plot, and the wording says so: a subscriber who reads it as "malaria
    detected on my farm" has been misled by us.
    """
    h = a.health
    if not (h.available and h.endemic):
        return None

    return Track(
        key="malaria",
        label="Malaria risk",
        reading=f"{_pct(h.malaria_pfpr)} background rate",
        meaning=(
            "This district is malaria-endemic, and standing water raises mosquito breeding for "
            "two to three weeks afterwards. This is a district-level baseline, not a reading "
            "from your plot."
        ),
        # A standing district baseline, not a change on this plot, so it never leads the card —
        # even in a high-prevalence district. Ranked above nothing, below every measurement.
        weight=_ROUTINE if h.malaria_pfpr >= 0.2 else _REASSURING,
        sources=[s for s in a.data_sources if "malaria" in s.lower()],
        detail=[("District baseline", f"{h.malaria_pfpr:.1%} P. falciparum prevalence")],
    )


_BUILDERS = (
    _flood_track,
    _crop_track,
    _soil_water_track,
    _rainfall_track,
    _malaria_track,
)


def tracks(assessment: RiskAssessment) -> list[Track]:
    """Every track this assessment can support, most relevant first.

    Returns `[]` for an assessment with no measurements at all — which is a real state (every
    upstream unavailable) and must render as "we could not look this cycle" rather than as five
    modules reading zero.
    """
    built = [t for builder in _BUILDERS if (t := builder(assessment)) is not None]

    hazard_key = _HAZARD_TRACK.get(assessment.hazard)
    # Sort by measured weight, with the classified hazard's own track winning ties so the modules
    # and the card agree about what the alert is about. `key` last, purely so the order is stable
    # across runs rather than dependent on dict iteration.
    built.sort(key=lambda t: (-t.weight, t.key != hazard_key, t.key))
    return built
