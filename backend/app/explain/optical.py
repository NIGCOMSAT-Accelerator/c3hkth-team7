"""Optical series → what the crop is actually doing, in plain language.

Answers the question a farmer asks first: *is my crop doing better or worse than it should be?*

The Analyst measures a stressed-crop fraction from Sentinel-2 optical indices, with a Sentinel-1
radar fallback when cloud closes the optical window. Those are the numbers. This turns the
direction they point in into a sentence — never into a different number.
"""

from __future__ import annotations

from app.explain.base import GROUNDING, evidence_block, explain, provenance_block
from app.models.enums import HazardType
from app.models.schemas import RiskAssessment

_SYSTEM = (
    GROUNDING
    + "\n\nYour task: describe what the plant growth measurements show about this plot, in two "
    "or three sentences. Say whether conditions look better, worse or unchanged, and what that "
    "means for the crop. If the measurement came from radar because cloud blocked the optical "
    "view, mention that the reading still holds — radar sees through cloud."
)

#: What to say with no provider, a refusal, or a failure.
#:
#: Written per hazard rather than as one generic line, because "your crop is under stress" and
#: "your field is holding water" call for different actions, and a template that covered both
#: vaguely would be worse than the evidence list on its own.
_FALLBACK: dict[HazardType, str] = {
    HazardType.CROP_DROUGHT_STRESS: (
        "The satellite reading shows part of this plot under drought stress — the crop is "
        "growing more slowly than healthy plants at this stage would."
    ),
    HazardType.CROP_WATERLOGGING: (
        "The satellite reading shows water sitting on part of this plot. Waterlogged roots "
        "cannot take up nutrients, so growth slows even after the surface dries."
    ),
    HazardType.CROP_VEGETATION_ANOMALY: (
        "The satellite reading shows this plot growing differently from what is normal for the "
        "season. It is not yet clear whether that is water, nutrients or pest damage."
    ),
    HazardType.FLOOD_INUNDATION: (
        "Standing water was detected across part of this plot in the latest radar pass."
    ),
    HazardType.FLOOD_FORECAST: (
        "Conditions ahead point to flooding on or near this plot."
    ),
    HazardType.MALARIA_RISK: (
        "Standing water on or near this plot creates breeding conditions for mosquitoes."
    ),
}

_GENERIC = (
    "The satellite measurements for this plot are listed below. No plain-language summary is "
    "available this cycle."
)


def fallback_for(assessment: RiskAssessment) -> str:
    """The deterministic sentence for this hazard. Public so tests can assert it exists."""
    return _FALLBACK.get(assessment.hazard, _GENERIC)


async def describe(assessment: RiskAssessment) -> str:
    """Two or three sentences on what the crop is doing. Never raises."""
    user = (
        f"Plot: {assessment.aoi_name}\n"
        f"Finding: {assessment.hazard.value.replace('_', ' ')}\n"
        f"\nMeasurements:\n{evidence_block(assessment)}\n"
        # Provenance, so the explanation can cite what measured it — see `provenance_block`
        # for why this is not a grounding-rule violation.
        f"\nWhere these figures came from:\n{provenance_block(assessment)}\n"
        "\nDescribe what the plant growth measurements show about this plot."
    )
    return await explain(
        system=_SYSTEM,
        user=user,
        fallback=fallback_for(assessment),
        surface="optical",
    )
