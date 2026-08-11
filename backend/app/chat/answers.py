"""Deterministic answers — the zero-token path.

Most questions a subscriber asks about an alert are the same handful, and the
answers are already computed and sitting in the assessment: *what does this mean*,
*what should I do*, *how sure are you*, *when*, *why did you send this*. The
Oracle produced `evidence`, the generator produced `actions`, and the assessment
carries `confidence` and `lead_time_days`.

Sending those to a model would spend tokens to paraphrase data we already have —
and paraphrasing is where invented numbers come from. So this module answers them
directly, and only genuinely novel questions reach the LLM.

**Deliberately conservative.** A miss costs an LLM call; a false positive gives a
farmer a canned answer to a specific question. So matching requires an explicit
intent phrase, and anything with extra substance in it falls through. Better to
pay for a call than to answer the wrong question confidently.

Every answer here is assembled from the subscriber's own assessment. Nothing is
generated, so nothing can be fabricated — the same property `_template` gives the
advisory path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.schemas import Alert

# --------------------------------------------------------------------------- #
# Intent matching
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Intent:
    name: str
    #: Phrases that identify the question. Matched on a normalised string.
    patterns: tuple[str, ...]


#: Order matters — the first match wins, so more specific intents come first.
#: "what should i do about the flooding" is ACTIONS, not MEANING, even though it
#: contains "what".
_INTENTS: tuple[Intent, ...] = (
    Intent(
        "actions",
        (
            "what should i do",
            "what do i do",
            "what can i do",
            "what actions",
            "how do i prepare",
            "what steps",
            "advise me",
        ),
    ),
    Intent(
        "confidence",
        (
            "how sure are you",
            "how confident",
            "how certain",
            "how reliable",
            "can i trust",
            "how accurate",
        ),
    ),
    Intent(
        "timing",
        (
            "when will",
            "how long",
            "how soon",
            "what time",
            "how many days",
            "when should i",
        ),
    ),
    Intent(
        "evidence",
        (
            "why did you send",
            "why did i get",
            "why this alert",
            "what evidence",
            "how do you know",
            "where is this from",
            "what data",
        ),
    ),
    Intent(
        "meaning",
        (
            "what does this mean",
            "what does my alert mean",
            "what is happening",
            "explain my alert",
            "what is this about",
            "summarise",
            "summarize",
        ),
    ),
    Intent(
        "cascade",
        (
            "what happens next",
            "what comes next",
            "what else",
            "what will happen",
        ),
    ),
    Intent(
        "status",
        (
            "any alerts",
            "do i have",
            "am i at risk",
            "current status",
            "anything new",
        ),
    ),
)

#: Words that mean the question is more specific than the intent it matched, so
#: the canned answer would be answering something else. Falls through to the LLM.
#:
#: These are the giveaways of a *reasoning* question — comparison, causation,
#: hypotheticals, or a named crop/practice we have no template for.
_ESCAPE_HATCHES: tuple[str, ...] = (
    "compare",
    "versus",
    " vs ",
    "instead",
    "better",
    "worse than",
    "last year",
    "last season",
    "history",
    "trend",
    "why does",
    "why is",
    "how does",
    "what if",
    "should i still",
    "is it true",
    "someone said",
    "i heard",
    "neighbour",
    "neighbor",
    "market",
    "price",
    "insurance",
    "loan",
    "fertiliser",
    "fertilizer",
    "pesticide",
    "variety",
)


def _normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    lowered = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", lowered).strip()


def classify(question: str) -> str | None:
    """Intent name, or None when this needs the LLM.

    Returns None for anything long, anything containing an escape-hatch word, or
    anything that matches no known phrase — all three mean the canned answer
    would probably be answering a different question than the one asked.
    """
    normalised = _normalise(question)

    # A long question carries specifics a template cannot address.
    if len(normalised.split()) > 14:
        return None

    if any(hatch.strip() in normalised for hatch in _ESCAPE_HATCHES):
        return None

    for intent in _INTENTS:
        if any(_normalise(p) in normalised for p in intent.patterns):
            return intent.name

    return None


# --------------------------------------------------------------------------- #
# Answer construction
# --------------------------------------------------------------------------- #

_SEVERITY_URGENCY = {
    "emergency": "This needs action today.",
    "warning": "This needs action within about 48 hours.",
    "watch": "Prepare this week.",
    "advisory": "Keep watch for now.",
    "info": "This is for information only.",
}


def answer_for(intent: str, alert: Alert) -> str | None:
    """Build the answer, or None if this alert lacks the data for it.

    Returning None rather than a partial answer matters: falling through to the
    LLM is always better than a reply that trails off.
    """
    assessment = alert.assessment
    hazard = assessment.hazard.value.replace("_", " ")
    area = assessment.aoi_name

    if intent == "actions":
        if not alert.advisory.actions:
            return None
        steps = "\n".join(f"{i}. {a}" for i, a in enumerate(alert.advisory.actions, 1))
        urgency = _SEVERITY_URGENCY.get(assessment.severity.value, "")
        return f"For {area}:\n\n{steps}\n\n{urgency}".strip()

    if intent == "confidence":
        percent = round(assessment.confidence * 100)
        # Explain what the number means rather than just stating it — and be
        # explicit about the cap, since it is the honest part of the answer.
        caveat = (
            " Because confidence is below 65%, this alert is capped at Watch level "
            "and cannot be raised to a Warning or Emergency on its own."
            if assessment.confidence < 0.65
            else ""
        )
        sources = (
            f" It draws on {len(assessment.data_sources)} data sources."
            if assessment.data_sources
            else ""
        )
        return (
            f"Confidence in the {area} assessment is {percent}%.{sources}{caveat} "
            "This is a forecast, not a guarantee."
        )

    if intent == "timing":
        return (
            f"This assessment looks {assessment.lead_time_days} days ahead from "
            f"{assessment.assessed_at:%d %B}. "
            f"{_SEVERITY_URGENCY.get(assessment.severity.value, '')} "
            "Your area is re-checked on every satellite pass, and you will hear "
            "from us again only if something changes."
        ).strip()

    if intent == "evidence":
        if not assessment.evidence:
            return None
        facts = "\n".join(f"• {e}" for e in assessment.evidence)
        return (
            f"The {assessment.severity.value} alert for {area} was based on:\n\n"
            f"{facts}\n\nNothing else was used to write it."
        )

    if intent == "meaning":
        parts = [f"{alert.advisory.headline}", "", alert.advisory.body]
        if assessment.evidence:
            parts += ["", "Why: " + assessment.evidence[0] + "."]
        return "\n".join(parts).strip()

    if intent == "cascade":
        if not assessment.cascade:
            return (
                f"No follow-on hazards are expected from the {hazard} in {area} "
                "at this stage. Your area is re-checked on every satellite pass."
            )
        following = ", ".join(h.value.replace("_", " ") for h in assessment.cascade)
        return (
            f"The {hazard} in {area} may lead to {following} in the weeks that "
            "follow. That is why the alert mentions it now, while there is still "
            "time to act on both."
        )

    if intent == "status":
        return (
            f"Your most recent alert is {assessment.severity.value} — {hazard} in "
            f"{area}, issued {alert.created_at:%d %B}. "
            f"{alert.advisory.headline}"
        )

    return None


def try_answer(question: str, alert: Alert | None) -> tuple[str, str] | None:
    """`(answer, intent)` when this can be answered without the LLM.

    The single entry point. `alert` is the subscriber's most recent; None means
    they have none, in which case there is nothing to answer from.
    """
    if alert is None:
        return None

    intent = classify(question)
    if intent is None:
        return None

    answer = answer_for(intent, alert)
    if answer is None:
        return None

    return answer, intent
