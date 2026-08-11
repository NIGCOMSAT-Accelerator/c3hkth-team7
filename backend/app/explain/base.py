"""Shared plumbing for the explanation surfaces.

One place for the three things all of them must do identically, because doing any of them
differently in one surface is how a grounding rule quietly stops applying:

  * build a prompt from `RiskAssessment.evidence` **and nothing else**;
  * never raise — return the deterministic fallback on an absent provider, a refusal, or any
    exception;
  * stay short, because these are read on a phone beside the advisory, not instead of it.
"""

from __future__ import annotations

from app.llm import client
from app.logging_config import get_logger
from app.models.schemas import RiskAssessment

log = get_logger(__name__)

#: Hard ceiling on any explanation, in tokens.
#:
#: **Not the visible length.** A reasoning model spends completion tokens on internal thinking
#: before emitting any, and that thinking counts against `max_tokens`. Verified against Gemini
#: 2.5 Flash on the live endpoint: at 220 all three surfaces came back truncated mid-sentence
#: ("Satellite radar shows 2% of"), because the budget was consumed before the answer began.
#: At 20 the response carried `finish_reason: length` and no `content` field at all.
#:
#: Measured, not guessed. Against Gemini 2.5 Flash on the real endpoint, for the longest of the
#: three prompts:
#:
#:     max_tokens=900   used 1214 → truncated ("...or rainfall for")
#:     max_tokens=1400  used 1686 → complete
#:     max_tokens=2000  used 2254 → complete, no better
#:
#: Note `used` EXCEEDS the ceiling: the reported total includes the prompt, so `max_tokens` bounds
#: the completion only. 1400 is the first value that finishes a sentence, with headroom for a
#: longer evidence list.
#:
#: The *brevity* that matters is enforced by the prompt ("two or three sentences"), not by
#: starving the budget — a truncated explanation is worse than a slightly long one, and far worse
#: than the deterministic template.
#:
#: A non-reasoning model (gpt-4o, Llama) will use a small fraction of this, so the ceiling costs
#: nothing there. It is sized for the worst case because the failure it prevents is silent.
MAX_TOKENS = 1400

#: The instruction every surface shares.
#:
#: Repeated verbatim rather than paraphrased per module: the grounding rule is the same rule in
#: each, and three slightly different wordings is how one of them ends up permitting a number the
#: others forbid.
GROUNDING = (
    "You explain satellite measurements to smallholder farmers in Nigeria and Sub-Saharan "
    "Africa.\n"
    "\n"
    "RULES, in order of importance:\n"
    "1. Use ONLY the measurements given. Never introduce a number, percentage, date or place "
    "that is not in them. If you want to say something the measurements do not support, say "
    "less instead.\n"
    "2. If a measurement says data was unavailable, say so plainly. Do not fill the gap with a "
    "typical value or a guess.\n"
    "3. Write for someone reading on a phone, in short sentences, in English a second-language "
    "reader follows easily. No jargon: say 'radar' not 'SAR', 'plant growth' not 'NDVI'.\n"
    "4. Do not greet, sign off, or repeat the question. Give the explanation only.\n"
    "5. Never tell them to evacuate, or contradict official emergency guidance. You inform a "
    "decision; you do not issue instructions from an authority you do not have."
)


def evidence_block(assessment: RiskAssessment) -> str:
    """The measurements a surface may reason from, formatted for a prompt.

    **This is the whole grounding boundary.** Only `evidence` is included — not
    `exposure.population`, not `score`, not `soil` — for the same reason
    `advisory/generator.py` is restricted to it: the evidence list is the Oracle's own
    human-readable account of what was measured, and anything else is a derived value a model
    would be liable to restate as a fact of its own.

    A test asserts that other assessment fields never reach these prompts.
    """
    if not assessment.evidence:
        return "- (no measurements were available this cycle)"
    return "\n".join(f"- {fact}" for fact in assessment.evidence)


def provenance_block(assessment: RiskAssessment) -> str:
    """Where each figure came from, formatted for a prompt. **Not measurements.**

    ## Why this is separate from `evidence_block`

    The grounding rule forbids `score`, `confidence` and any other derived value from reaching a
    prompt, because a model handed a number restates it as a fact of its own. Provenance is a
    different category: it is a statement about *how the measurements were obtained*, not a new
    measurement. "Measured by radar on 9 August by a trained model" adds no figure the model could
    misattribute — it adds the citation a reader needs to check the figures already present.

    Kept in its own function rather than folded into `evidence_block` so the boundary stays
    inspectable: a test can assert exactly what may cross, and a future field cannot slip in by
    being appended to a list that also carries measurements.

    ## Why the farmer sees it at all

    Every number in an advisory traces to a satellite measurement, and that traceability is the
    product's argument for being believed. Stating it inline — rather than only in a portal panel —
    means the citation survives an SMS, a WhatsApp message and a voice note, which is where most
    subscribers actually read this.
    """
    lines: list[str] = []

    if assessment.data_sources:
        lines.append(f"- Data sources: {', '.join(sorted(set(assessment.data_sources)))}")

    # WHEN, and by WHICH method. `computed_at` is deliberately excluded: it is when the arithmetic
    # ran, always "now", and says nothing about how current the observation is.
    for observed_at, platform, method, what in (
        (
            assessment.observed_at_flood,
            assessment.platform_flood,
            assessment.method_flood,
            "standing water",
        ),
        (
            assessment.observed_at_stress,
            assessment.platform_stress,
            assessment.method_stress,
            "crop condition",
        ),
    ):
        if observed_at is None:
            continue
        parts = [f"{what} measured {observed_at:%d %b %Y}"]
        if platform:
            parts.append(f"from {platform}")
        if method:
            # "trained-model" vs "heuristic" is the honesty that matters most here: a farmer is
            # entitled to know whether a figure came from a model or from a physical threshold.
            parts.append(f"by {method}")
        lines.append(f"- {', '.join(parts)}")

    if not lines:
        return "- (provenance was not recorded for this reading)"
    return "\n".join(lines)


def driver_block(assessment: RiskAssessment) -> str:
    """The exact contribution of each input to the score, formatted for a prompt.

    ## Why this is safe to put in a prompt when `score` is not

    The grounding rule keeps `score` out because a bare number invites a model to restate it as a
    finding of its own. These are different: each line states *which input contributed how much*,
    which is arithmetic the Oracle already performed (`weight * value`) rather than a conclusion.
    A model given this cannot invent a driver, because the drivers and their magnitudes are supplied.

    That is the whole point of the "narrate the drivers behind a risk score" surface. Without it,
    `narrate` had to guess which factor mattered most from prose — and a plausible guess about
    causation is exactly the kind of confident wrongness the grounding rule exists to prevent.

    Ordered largest-first by the Oracle, so the first line is the dominant driver.
    """
    if not assessment.score_drivers:
        return "- (driver breakdown was not recorded for this reading)"

    lines: list[str] = []
    for driver in assessment.score_drivers:
        share = f"{driver.contribution:.2f}"
        detail = f"; from {'; '.join(driver.inputs)}" if driver.inputs else "; not measured"
        lines.append(f"- {driver.label}: contributed {share} of the risk score{detail}")
    return "\n".join(lines)


def attribution_block(assessment: RiskAssessment) -> str:
    """Which input drove the crop-stress verdict, formatted for a prompt.

    ## Why this belongs in the irrigation surface especially

    "Irrigate or hold" is the one instruction in this product that costs a farmer money either way.
    A stress score alone cannot answer it, because identical scores arise from opposite causes:

      * **plant moisture** driving the verdict means the crop is short of water -> irrigate;
      * **its own history** driving it, with moisture fine, points at pests, nutrients or planting
        date -> irrigating wastes water and does not fix the problem.

    So this is not decoration on the explanation; it is the input that makes the recommendation a
    reasoned choice rather than a threshold. The contributions are exact rather than estimated — see
    `ml/inference.crop_stress_attribution`.

    Signed: positive means the input pushed toward stress, negative means it argued against it. Only
    the positive ones are listed, because a reader asking "why is my crop stressed" is not helped by
    the factors that said it was not.
    """
    attribution = assessment.stress_attribution
    if not attribution:
        return "- (no attribution available — the reading came from a fixed threshold)"

    from app.ml.inference import CROP_CHANNELS

    labels = dict(CROP_CHANNELS)
    pushing = sorted(
        ((name, value) for name, value in attribution.items() if value > 0),
        key=lambda item: item[1],
        reverse=True,
    )
    if not pushing:
        return "- no single input pushed toward stress"

    return "\n".join(
        f"- {labels.get(name, name)}: contributed {value:+.2f} toward the stress verdict"
        for name, value in pushing
    )


async def explain(
    *, system: str, user: str, fallback: str, surface: str
) -> str:
    """Run one explanation, or return `fallback`. Never raises.

    The fallback is not a courtesy — it is the contract. A farmer opening the portal during a
    flood must not find an empty panel because a provider was slow, a key expired, or the model
    declined. Plainer text is always better than none, which is the same reasoning that makes
    `_template` the most safety-critical line in the advisory generator.

    Deliberately NOT budget-gated. `llm/budget.py` guards chat and Fahis, where a human is asking
    repeatedly; these run once per assessment and are part of the product's output rather than a
    conversation. A token ceiling must never be the reason a warning arrives unexplained — the
    same rule that keeps advisory generation ungated, and a test enforces it.
    """
    if not client.available():
        return fallback

    try:
        text = await client.complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=MAX_TOKENS,
        )
    except client.LLMRefusal as exc:
        # A refusal is expected occasionally on hazard language and is not an error worth an
        # exception trace — it is a reason to use the deterministic text.
        log.info("explanation refused; using template", extra={"surface": surface, "reason": str(exc)})
        return fallback
    except Exception as exc:  # noqa: BLE001 — an explanation must never break an assessment
        log.warning(
            "explanation failed; using template",
            extra={"surface": surface, "error": f"{type(exc).__name__}: {exc}"},
        )
        return fallback

    cleaned = (text or "").strip()
    return cleaned or fallback
