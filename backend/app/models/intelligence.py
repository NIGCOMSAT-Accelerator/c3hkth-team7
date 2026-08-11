"""The intelligence vocabulary — what each alert category means, for every audience.

## Why this is server-side and shared

A severity value on its own is not actionable. `"watch"` tells an integrator which colour to use
and nothing about what to do, so every partner ends up encoding their own interpretation of our
categories — and the moment two partners disagree about what Watch warrants, the platform has
stopped delivering *intelligence* and gone back to delivering numbers.

So the meaning ships with the payload. One table here, read by:

  * the **webhook payload**, so an aggregator's system acts on our interpretation rather than
    inventing one;
  * the **Web UI**, whose `frontend/lib/intelligence.ts` mirrors this file.

A test asserts the two stay in step. They are separate files because the frontend cannot import
Python, but a drift between them would mean a subscriber's portal and their aggregator's dashboard
describing the same alert differently — which is worse than either wording alone.

## Confidence is banded, and the bands are not arbitrary

Below `CONFIDENCE_ESCALATION_FLOOR` (0.65) the Oracle caps severity at Watch. So a low-confidence
reading is not merely "less certain" — it is *structurally incapable* of being a warning, and an
integrator building an escalation rule needs to know that rather than inferring it from a decimal.
`severity_capped` states it outright.
"""

from __future__ import annotations

from app.models.enums import HazardType, Severity

#: The Oracle's own escalation floor, imported rather than restated.
#:
#: `settings.confidence_escalation_floor` does not exist — it is a module constant in
#: `agents/oracle.py`. Copying the number here would let the two drift, and the drift would be
#: invisible: `severity_capped` would claim a reading could have escalated when the Oracle had
#: already capped it, or the reverse. An integrator building an escalation rule on that would be
#: building on a lie.
#:
#: Imported lazily inside the functions that need it, because `agents/oracle.py` pulls in the
#: risk layer and this module is imported by the webhook publisher — a module-scope import would
#: drag the Oracle into every webhook delivery.

#: What each category means and what it warrants.
#:
#: Written for a human to read in a dashboard, not for a machine to switch on — a partner switches
#: on `category`, which is the stable machine token. The prose may be improved; the token may not
#: change without a contract version.
CATEGORY: dict[Severity, dict[str, str]] = {
    Severity.INFO: {
        "label": "Info",
        "meaning": (
            "A routine reading. Conditions were measured and nothing needs attention."
        ),
        "response": "No action needed. Useful for tracking how a season is developing.",
        "urgency": "No time pressure",
    },
    Severity.ADVISORY: {
        "label": "Advisory",
        "meaning": (
            "Something has changed that is worth knowing, but it does not threaten the crop yet."
        ),
        "response": "Plan around it rather than reacting to it.",
        "urgency": "Within a few days",
    },
    Severity.WATCH: {
        "label": "Watch",
        "meaning": (
            "Conditions that could develop into a problem are present now. Not yet a threat, but "
            "the direction matters."
        ),
        "response": (
            "Prepare what would be slow to arrange later — drainage, labour, somewhere dry for "
            "stored produce. Inspect the area if possible."
        ),
        "urgency": "Next day or two",
    },
    Severity.WARNING: {
        "label": "Warning",
        "meaning": (
            "A hazard is likely to affect this area. The measurement and the outlook agree."
        ),
        "response": (
            "Act now. Move what can be moved, clear drainage, and notify others farming nearby."
        ),
        "urgency": "Today",
    },
    Severity.EMERGENCY: {
        "label": "Emergency",
        "meaning": "A severe hazard is happening or imminent, with high confidence.",
        "response": (
            "Act immediately and follow official emergency guidance. SHELTER informs that "
            "decision; it does not replace it."
        ),
        "urgency": "Immediately",
    },
}


#: Which intelligence track a hazard belongs to.
#:
#: Mirrors `app/iam/tracks.py`, which is authoritative about *deliverability*. This map is only
#: about classification — every hazard belongs to a track whether or not that track is live.
HAZARD_TRACK: dict[HazardType, str] = {
    HazardType.CROP_WATERLOGGING: "agricultural",
    HazardType.CROP_DROUGHT_STRESS: "agricultural",
    HazardType.CROP_VEGETATION_ANOMALY: "agricultural",
    HazardType.FLOOD_INUNDATION: "environmental",
    HazardType.FLOOD_FORECAST: "environmental",
    # Cascade-only: `OracleAgent._classify` never returns it as a primary hazard.
    HazardType.MALARIA_RISK: "public_health",
}


def confidence_band(confidence: float) -> str:
    """A machine-stable band name for a confidence value.

    Four bands rather than a raw float, because an integrator writing "escalate above 0.7" is
    encoding a threshold they cannot maintain — ours may change as models are trained. The band is
    the contract; the float is provided alongside for anyone who wants it.
    """
    if confidence >= 0.85:
        return "high"
    from app.agents.oracle import CONFIDENCE_ESCALATION_FLOOR

    if confidence >= CONFIDENCE_ESCALATION_FLOOR:
        return "good"
    if confidence >= 0.4:
        return "limited"
    return "low"


def _escalation_floor() -> float:
    """The Oracle's escalation floor. Imported at call time — see the note above."""
    from app.agents.oracle import CONFIDENCE_ESCALATION_FLOOR

    return CONFIDENCE_ESCALATION_FLOOR


def describe(severity: Severity, confidence: float, hazard: HazardType) -> dict:
    """The `intelligence` block for a webhook payload.

    Everything an integrator needs to route and act on an alert without reimplementing our
    thresholds: the machine token, the human wording, the confidence band, and whether severity was
    structurally capped.
    """
    meta = CATEGORY[severity]
    band = confidence_band(confidence)

    return {
        # The stable machine token. Switch on this.
        "category": severity.value,
        "label": meta["label"],
        "meaning": meta["meaning"],
        "response": meta["response"],
        "urgency": meta["urgency"],
        "confidence": round(confidence, 3),
        "confidence_band": band,
        # True when confidence sits below the escalation floor, so the Oracle could not have
        # raised this above Watch however severe the measurement. An integrator seeing a Watch
        # with this set knows the ceiling was the data, not the hazard.
        "severity_capped": confidence < _escalation_floor(),
        "track": HAZARD_TRACK.get(hazard, "agricultural"),
    }
