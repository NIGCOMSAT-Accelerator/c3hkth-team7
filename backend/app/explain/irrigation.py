"""Soil moisture → irrigate, or hold.

The narrowest and most useful of the three surfaces: it turns a measurement into **one decision**.

## Why a decision rather than a description

"Soil moisture is 0.31" asks the farmer to do the interpreting, which is the work the platform
exists to remove. "Hold — the soil is still wet at root depth" is actionable. But it is also the
surface where being wrong costs the most, so two constraints apply that the other two do not need.

## The two constraints

**A recommendation is only given when the evidence supports one.** With no soil or moisture
measurement this returns the honest "we cannot tell you" rather than a default. A confident
"irrigate" derived from missing data would waste water, fuel and a day's labour — and it would be
indistinguishable, to the reader, from a measured one.

**Waterlogging inverts the advice**, so it is handled before anything else. Irrigating a plot that
is already holding water is the most damaging instruction this module could produce, and it is
exactly what a naive "moisture is low → irrigate" rule would emit for a field whose surface has
dried over saturated subsoil.
"""

from __future__ import annotations

from app.explain.base import (
    GROUNDING,
    attribution_block,
    evidence_block,
    explain,
    provenance_block,
)
from app.models.enums import HazardType
from app.models.schemas import RiskAssessment

_SYSTEM = (
    GROUNDING
    + "\n\nYour task: say whether to irrigate now or hold, and why, in two sentences at most. "
    "Start with the decision word — 'Irrigate' or 'Hold' — so it is readable at a glance.\n"
    "\n"
    "If the measurements do not support a recommendation, say plainly that you cannot advise on "
    "irrigation this cycle and what is missing. That is a correct answer. A confident guess is "
    "not: irrigating unnecessarily costs water, fuel and a day's labour, and irrigating a "
    "waterlogged field damages the crop.\n"
    "\n"
    "Never recommend irrigating when the measurements show standing water or waterlogging."
)

#: Hazards where irrigation is categorically wrong.
#:
#: Checked in code rather than left to the prompt. A model instructed not to recommend irrigation
#: on a waterlogged field will usually comply; "usually" is not good enough when the failure
#: drowns a crop, so the model is never asked in the first place.
_TOO_WET = frozenset(
    {
        HazardType.CROP_WATERLOGGING,
        HazardType.FLOOD_INUNDATION,
        HazardType.FLOOD_FORECAST,
    }
)

_HOLD_WET = (
    "Hold — do not irrigate. The measurements show water already sitting on this plot, and "
    "adding more would keep the roots starved of air."
)

_UNKNOWN = (
    "We cannot advise on irrigation this cycle: no soil-moisture measurement was available for "
    "this plot. Check the soil by hand at root depth before deciding."
)

_DRY = (
    "The measurements point to dry conditions on this plot. Check the soil at root depth before "
    "irrigating — the satellite sees the surface, which dries first."
)


def _has_moisture_signal(assessment: RiskAssessment) -> bool:
    """Whether any measurement speaks to soil water at all.

    A **measured** soil-moisture reading settles this outright — that is what SMAP is for, and it is
    checked first because it is a number rather than an inference.

    The keyword scan below it is the pre-SMAP path, retained rather than deleted for two reasons: an
    assessment written before `soil_moisture` existed has no reading to check, and SMAP legitimately
    returns unavailable (a fill cell, an outage) while rainfall and drainage evidence still speak to
    soil water. Deliberately conservative in both branches: a missing signal must produce "we cannot
    tell you", never a guess.
    """
    if assessment.soil_moisture.available:
        return True
    terms = ("soil", "moisture", "drain", "water", "rain", "drought", "dry")
    return any(
        any(term in fact.lower() for term in terms) for fact in assessment.evidence
    )


def _measured_advice(assessment: RiskAssessment) -> str | None:
    """Deterministic text straight from the SMAP reading, or None when there is none.

    Preferred over the model whenever a measurement exists: the mapping from a volumetric water
    content to irrigate/hold/drain is `SoilMoisture.irrigation_advice`, a table lookup on a measured
    number. Paraphrasing that through an LLM would spend tokens and add the one step at which the
    figure could be restated wrongly — the same reasoning as `chat/answers.py`'s zero-token rung.

    The date is always stated. SMAP publishes ~2 days in arrears, and a farmer who watered
    yesterday must be able to see that this reading predates that.
    """
    wetness = assessment.soil_moisture
    if not wetness.available:
        return None
    advice = wetness.irrigation_advice
    if advice is None:
        return None

    measured = f"{wetness.volumetric:.2f} m3/m3, measured on {wetness.observed_date}"
    if advice == "drain":
        return (
            f"Hold — do not irrigate, and clear the drainage channels if you can. Satellite radar "
            f"measured this plot saturated ({measured}): the soil has no air left in it, and more "
            f"water will rot the roots."
        )
    if advice == "irrigate":
        return (
            f"Irrigate. Satellite radar measured the soil water on this plot below what the crop "
            f"can draw on ({measured}). Check at root depth first — the satellite reads the top of "
            f"the soil, which dries before the root zone does."
        )
    return (
        f"Hold — no irrigation needed now. Satellite radar measured the soil water on this plot in "
        f"the range crops draw on comfortably ({measured})."
    )


def fallback_for(assessment: RiskAssessment) -> str:
    """Deterministic irrigation text, including the refusal case."""
    if assessment.hazard in _TOO_WET:
        return _HOLD_WET
    # A measured reading outranks every heuristic below, including the drought-stress guess.
    measured = _measured_advice(assessment)
    if measured is not None:
        return measured
    if not _has_moisture_signal(assessment):
        return _UNKNOWN
    if assessment.hazard is HazardType.CROP_DROUGHT_STRESS:
        return _DRY
    return _UNKNOWN


async def advise(assessment: RiskAssessment) -> str:
    """Irrigate or hold, with the reason. Never raises.

    Returns the deterministic "hold" immediately for a waterlogged or flooded plot **without
    calling the model at all** — see `_TOO_WET`. The one instruction that must never be produced is
    not left to a prompt.
    """
    if assessment.hazard in _TOO_WET:
        return _HOLD_WET

    if not _has_moisture_signal(assessment):
        # Nothing to reason from. Asking the model here invites a plausible recommendation built
        # on no measurement, which is the failure this surface is most exposed to.
        return _UNKNOWN

    # A SATURATED plot gets the deterministic "do not irrigate" without a model call, exactly as
    # `_TOO_WET` does. The hazard classification can miss this — a plot can be measured saturated
    # while the Oracle classifies a vegetation anomaly rather than waterlogging, which is precisely
    # the Yenagoa case observed live (0.593 m3/m3, hazard crop_vegetation_anomaly). "Irrigate" must
    # not be reachable by a prompt when the measurement says the pore space is already full.
    if assessment.soil_moisture.irrigation_advice == "drain":
        drain_text = _measured_advice(assessment)
        if drain_text is not None:
            return drain_text

    user = (
        f"Plot: {assessment.aoi_name}\n"
        f"Finding: {assessment.hazard.value.replace('_', ' ')}\n"
        f"\nMeasurements:\n{evidence_block(assessment)}\n"
        # Provenance, so the explanation can cite what measured it — see `provenance_block`
        # for why this is not a grounding-rule violation.
        f"\nWhere these figures came from:\n{provenance_block(assessment)}\n"
        # WHICH input drove the stress verdict. This is what makes the recommendation a reasoned
        # choice: moisture-driven stress means irrigate, history-driven stress usually does not.
        f"\nWhat drove the stress reading:\n{attribution_block(assessment)}\n"
        "\nShould this plot be irrigated now, or held? If the main driver is NOT plant moisture, "
        "say so plainly and suggest looking at pests, nutrients or planting date instead of "
        "watering — irrigating the wrong problem wastes water the farmer paid for."
    )
    # The measured reading, stated as the decision it already implies. Reaching here means the plot
    # is not saturated (that returned above), so the model's job is to explain a hold or an
    # irrigate that has ALREADY been decided by measurement — not to decide it. Without this the
    # prompt carries the wetness only as one prose line among a dozen, which is exactly how a
    # measurement gets outvoted by a more vivid sentence about rainfall.
    if assessment.soil_moisture.available:
        wetness = assessment.soil_moisture
        user += (
            f"\n\nThe measured decision, which you must not contradict: "
            f"{wetness.irrigation_advice} "
            f"(soil water {wetness.volumetric:.2f} m3/m3 on {wetness.observed_date}, "
            f"which is '{wetness.status}' for this crop). Explain that decision; do not reverse it."
        )
    return await explain(
        system=_SYSTEM,
        user=user,
        fallback=fallback_for(assessment),
        surface="irrigation",
    )
