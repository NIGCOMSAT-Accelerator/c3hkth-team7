"""Explanation surfaces — measured findings turned into language a farmer can act on.

## What this package is, and the line it must not cross

Three surfaces, each taking an **already-computed** `RiskAssessment` and producing prose:

| Module | Question it answers |
|---|---|
| `optical` | "What is my crop actually doing?" — the vegetation trend in plain language |
| `drivers` | "Why is the risk what it is?" — rainfall, terrain and soil narrated as causes |
| `irrigation` | "Do I water today or hold?" — soil moisture converted into one decision |

**None of them can change a number.** They read `RiskAssessment` and return text; nothing here is
consumed by Scout, Analyst or Oracle, and `next_stage` does not exist in this package because
there is no pipeline to advance. That is the same boundary `agents/fahis.py` observes and
`tests/test_explain.py` asserts structurally.

## Why the risk agents still make no model call

CLAUDE.md's central invariant: Scout, Analyst and Oracle do no inference, which is what keeps
`score`/`confidence`/`severity` deterministic and `test_oracle.py` runnable with no provider
configured. It is tempting to put "turn this NDVI series into a sentence" inside the Analyst,
because that is where the series lives. Doing so would:

  * make the Analyst's output non-deterministic, so a rerun over the same imagery could differ;
  * put generated text on the path where a *number* reaches a farmer, which is the exact failure
    the grounding rule prevents and which this codebase has violated twice already.

So these run **after** the Oracle has decided, over its `evidence` list, and are presented beside
the advisory rather than inside it.

## Grounding is enforced the same way as in the advisory generator

Each surface receives only `RiskAssessment.evidence` plus the specific numeric fields it needs,
and is instructed never to add a figure. Every function returns a deterministic template when no
provider is configured, when the model refuses, or on any exception — so an unconfigured
deployment produces plainer text rather than nothing, and a farmer never sees an empty panel where
an explanation should be.

That fallback is the most safety-critical part of each module, exactly as `_template` is in
`advisory/generator.py`.

## One transport, no new abstraction

All three use `app/llm/client.py`, which already speaks only `POST /v1/chat/completions` with
parameters every OpenAI-compatible server accepts — so switching provider stays two environment
variables and no code change. A second factory layer would be two abstractions over one HTTP call,
with the new one untested.
"""

from typing import TYPE_CHECKING

from app.explain import drivers, irrigation, optical

if TYPE_CHECKING:
    from app.models.schemas import Explanations, RiskAssessment

__all__ = ["drivers", "explain_all", "irrigation", "optical"]


async def explain_all(assessment: "RiskAssessment") -> "Explanations":
    """All three surfaces for one assessment. Never raises.

    Run concurrently rather than in sequence: they share no state and each is one HTTP round trip,
    so serial execution would triple the latency on the Herald's critical path — the stage that
    delivers a flood warning.

    `return_exceptions=True` plus the per-surface fallbacks means one provider hiccup costs one
    explanation, not the alert. Each surface already returns its deterministic template on any
    failure, so an exception reaching here is unexpected — the guard exists because "unexpected"
    must still not break a dispatch.
    """
    import asyncio

    from app.models.schemas import Explanations

    results = await asyncio.gather(
        optical.describe(assessment),
        drivers.narrate(assessment),
        irrigation.advise(assessment),
        return_exceptions=True,
    )

    def text(value: object, fallback: str) -> str:
        return value if isinstance(value, str) and value.strip() else fallback

    return Explanations(
        crop=text(results[0], optical.fallback_for(assessment)),
        drivers=text(results[1], drivers.fallback_for(assessment)),
        irrigation=text(results[2], irrigation.fallback_for(assessment)),
    )
