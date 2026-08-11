"""Rainfall, terrain and soil → why the risk score is what it is.

Answers the question that decides whether a warning is believed: *why?*

The Oracle fuses five measured inputs into a score. A subscriber shown "0.55" learns nothing they
can act on, and a subscriber shown "0.55, and here is the arithmetic" learns less than one told
"three days of heavy rain on clay that drains slowly, on ground that collects water". This narrates
the causes the Oracle already recorded — it does not recompute them.

## Forecast and antecedent are different things, and the prompt says so

Only ClimateSERV GEFS predicts; CHIRPS, IMERG and ERA5 report how wet the ground already is.
`RainfallOutlook.forecast_available` gates that distinction and the Oracle weights antecedent
lower. Collapsing them would let this surface say "rain is coming" when the evidence only supports
"the ground is already wet" — a difference that changes what a farmer does today.
"""

from __future__ import annotations

from app.explain.base import (
    GROUNDING,
    driver_block,
    evidence_block,
    explain,
    provenance_block,
)
from app.models.schemas import RiskAssessment

_SYSTEM = (
    GROUNDING
    + "\n\nYour task: explain in two or three sentences WHY this risk level was reached, using "
    "the measurements as causes. Prefer cause-and-effect over restating figures: 'heavy rain on "
    "soil that drains slowly' is more useful than repeating the millimetres.\n"
    "\n"
    "Be careful with rainfall: a forecast means rain is expected, while an antecedent reading "
    "means the ground is already wet from rain that has fallen. Never turn one into the other. "
    "If the measurements say rainfall data was unavailable, say that the risk rests on the "
    "satellite imagery alone."
)

_FALLBACK = (
    "This risk level comes from the measurements listed below — what the satellite saw on the "
    "ground, combined with rainfall and soil conditions where those were available."
)

_NO_RAINFALL = (
    "This risk level rests on the satellite imagery alone: rainfall data was not available this "
    "cycle, so no forecast contributed to it. That is why the confidence is lower than usual."
)


def fallback_for(assessment: RiskAssessment) -> str:
    """Deterministic driver text.

    Distinguishes the no-rainfall case specifically, because that is both common on this stack and
    the single most important caveat a reader needs — a score built on imagery alone deserves to be
    treated differently, and `rainfall._flat_series` flags it rather than inventing a value.
    """
    unavailable = any(
        "rainfall" in fact.lower() and "unavailable" in fact.lower()
        for fact in assessment.evidence
    )
    return _NO_RAINFALL if unavailable else _FALLBACK


async def narrate(assessment: RiskAssessment) -> str:
    """Two or three sentences on what is driving the risk. Never raises."""
    user = (
        f"Plot: {assessment.aoi_name}\n"
        f"Risk category: {assessment.severity.value}\n"
        f"Days of lead time: {assessment.lead_time_days}\n"
        f"\nMeasurements:\n{evidence_block(assessment)}\n"
        # The EXACT contribution of each input, not something to be inferred from the prose above.
        # See `driver_block` for why this is safe where a bare `score` would not be.
        f"\nWhat drove the risk score:\n{driver_block(assessment)}\n"
        f"\nWhere these figures came from:\n{provenance_block(assessment)}\n"
        "\nExplain why this risk level was reached. Name the biggest driver first. "
        "End with one short sentence citing which satellite or forecast the figures came from, "
        "so the reader can check them."
    )
    return await explain(
        system=_SYSTEM,
        user=user,
        fallback=fallback_for(assessment),
        surface="drivers",
    )
