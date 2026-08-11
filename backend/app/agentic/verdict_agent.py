"""Fahis's adjudicator.

One structured-output call: given a past warning and the search results Fahis
gathered, decide whether independent reporting confirms it.

**Why this one is worth migrating even though it has no tools.** The old
implementation asked for JSON, walked a three-mode ladder (`json_schema` →
`json_object` → prompt-only), parsed the text, and then re-validated every field
by hand because a lower rung might not have enforced the shape. `output_type` on a
Pydantic model does all of that — the framework negotiates the strictest mode the
provider accepts and hands back a validated object.

**What deliberately did NOT move.** `FahisAgent._guard_verdict` still runs on the
result. Schema validation proves the model returned *a* verdict; it cannot prove
the verdict is *supported by the sources*. The guard downgrades
`refuted` → `unverified` with no credible source and `confirmed` → `partial` on
low-tier sources alone, because a verification agent eager to conclude
manufactures a ground truth that is really just search-index coverage — and
because the output looks like data, nobody notices until a model has been trained
on it.

The prompt is unchanged from the hand-rolled version, minus the "return JSON only"
instruction the framework now handles.
"""

from __future__ import annotations

# Module scope, for the same reason as chat_agent: `from __future__ import
# annotations` stringifies annotations and the schema builder resolves them
# against this module's globals.
from pydantic import BaseModel, Field
from pydantic_ai import Agent, NativeOutput

from app.agentic import provider
from app.logging_config import get_logger

log = get_logger(__name__)


class VerdictOutput(BaseModel):
    """What the adjudicator must return.

    Note `verdict` is a plain constrained string rather than the `Verdict` enum:
    `not_attempted` is ours to set (it means the search never ran, which the model
    is in no position to know), so offering it in the schema would invite a
    category error. `_guard_verdict` maps this onto the enum and rejects anything
    unexpected.
    """

    verdict: str = Field(
        description=(
            "One of: confirmed, partial, refuted, unverified. "
            "Use 'unverified' whenever you are unsure."
        )
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Your confidence in YOUR VERDICT, not in the original warning. "
            "One blog confirming is low; two agencies is high."
        ),
    )
    rationale: str = Field(
        description="Two or three sentences justifying the verdict."
    )
    source_indices: list[int] = Field(
        default_factory=list,
        description="Indices of the numbered sources you actually relied on.",
    )


_INSTRUCTIONS = """\
You verify whether a satellite-derived hazard warning matched what actually \
happened, using only the search results provided to you.

You are a check on an early-warning system, not its advocate. Your value comes \
entirely from being willing to return "unverified".

VERDICTS — choose exactly one:

  confirmed      Sources independently describe THIS hazard, in THIS area, in \
THIS time window. Requires at least one official or established media source.
  partial        Sources describe something real and related but materially \
different: right area but a different hazard, or a clearly different severity.
  refuted        A source AFFIRMATIVELY states the hazard did not occur, or \
reports normal conditions during the window. This requires a positive statement.
  unverified     Nothing found either way. Sources are absent, irrelevant, or \
about a different place or time.

CRITICAL RULES:

- "unverified" is the DEFAULT and the CORRECT answer whenever you are unsure. \
Most rural areas in Sub-Saharan Africa are not covered by indexed news. Silence \
means nobody reported it — it does NOT mean nothing happened.
- NEVER return "refuted" merely because you found nothing. Absence of evidence is \
not evidence of absence. "refuted" needs a source saying it did not happen.
- Match on PLACE and TIME, not just hazard words. An article about flooding in a \
different state, or from a previous year, verifies nothing.
- Cite only from the numbered sources given. Never introduce a fact, figure, \
place or date that is not in them.
"""


def build_agent():
    """Construct the adjudicator.

    Temperature is pinned to 0 regardless of `LLM_TEMPERATURE`: this is
    adjudication, not writing. Variance between runs over identical evidence would
    make the ground truth itself irreproducible, which defeats the point of
    recording it.
    """
    settings_override = dict(provider.model_settings())
    if "temperature" in settings_override:
        settings_override["temperature"] = 0.0

    return Agent[None, VerdictOutput](
        provider.build_model(),
        # `NativeOutput` — the schema as the provider's own response format, NOT as a tool.
        #
        # The default sends the output type as a function-calling tool. Gemini's
        # OpenAI-compatible endpoint rejects function calling combined with a JSON response mime
        # type outright:
        #
        #     400 "Function calling with a response mime type: 'application/json' is unsupported"
        #
        # and returns that error as a JSON *list* rather than an object, which Pydantic-AI cannot
        # parse — so the real cause surfaced as a misleading `finish_reason` validation error and
        # every adjudication silently fell back to UNVERIFIED. Verified against the live endpoint:
        # `tools` alone is fine, `response_format` alone is fine, the combination 400s.
        #
        # `NativeOutput` asks for the schema directly, so no tool is sent and the conflict cannot
        # arise. It is also the better fit here: adjudication needs one structured answer, not a
        # tool loop, and a provider enforcing the schema natively is stronger than one being asked
        # to call a function.
        output_type=NativeOutput(VerdictOutput),
        instructions=_INSTRUCTIONS,
        model_settings=settings_override,
        # Two retries: a schema violation is worth re-asking for, since the
        # alternative is discarding a search we already paid for.
        retries=2,
    )
