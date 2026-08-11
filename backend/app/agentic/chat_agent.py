"""Herald's chat agent.

The conversational surface: a subscriber who received *"31% of your cropland is
under standing water, harvest low-lying plots first"* asks why, how sure we are,
and what waterlogging does to rice.

**The rule this module exists to enforce.** Hazard figures come only from the
subscriber's own assessments. Web search supplies background context — what a
hazard does, what an agency announced — and may **never** contribute a number
about their hazard. Allowed: *"waterlogging starves rice roots of oxygen; three
days is usually survivable, a week often is not."* Not allowed: *"other sources
say 45% of your area is flooded."* The first adds understanding; the second
replaces a measured figure with an unattributed one, which is the failure the
grounding rule in `advisory/generator.py` exists to prevent and which this
codebase has violated twice.

**Scoping is now a type, not a convention.** `ChatDeps.subscriber_id` reaches the
tools through `RunContext[ChatDeps]`, so there is no tool parameter in which the
model could ask for a different subscriber's data. The previous implementation
closed over the identifier and relied on a test to assert no tool exposed it;
`deps_type` makes that structural. It matters because `GET /alerts` is still
unauthenticated — an unscoped chat endpoint would be a more convenient
exfiltration interface than the raw one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Module-scope import, unlike the lazy pattern used elsewhere for optional
# dependencies. `from __future__ import annotations` turns the tool signatures
# into strings, and Pydantic-AI resolves them against this module's globals when
# it builds each tool's JSON schema — so `RunContext` must be importable here, not
# just inside `build_agent()`. pydantic-ai is a hard requirement for this package
# anyway; `provider.build_model()` keeps the *model* construction lazy.
from pydantic_ai import Agent, RunContext

from app.agentic import provider
from app.config import settings
from app.logging_config import get_logger
from app.models.schemas import SourceCitation
from app.search import client as search
from app.store import repository

log = get_logger(__name__)


@dataclass
class ChatDeps:
    """Everything one chat run is allowed to touch.

    `subscriber_id` is the scope boundary. A run constructed with `None` can
    answer general questions but cannot read anyone's alerts — which is what an
    anonymous session gets.
    """

    subscriber_id: str | None
    language: str = "en"
    #: Citations accumulated across the run, so the reply can carry its
    #: provenance back to the caller. Mutated by the search tool.
    sources: list[SourceCitation] = field(default_factory=list)


_INSTRUCTIONS = """\
You are SHELTER's assistant. SHELTER is a satellite early-warning service for \
climate hazards in Nigeria and Sub-Saharan Africa. You help subscribers \
understand alerts they have received.

Your readers are smallholder farmers, cooperative officers, district emergency \
staff and public-health officers. Many read on a basic handset.

WHERE FACTS COME FROM — this is absolute:

- All figures about a subscriber's hazard — inundated area, crop stress, \
rainfall, population, severity, confidence — come ONLY from the get_my_alerts \
tool. That is measured satellite data.
- search_web gives you BACKGROUND CONTEXT ONLY: what a hazard does, what \
authorities have announced, general agronomic or health guidance.
- NEVER use a number from search_web to describe the subscriber's hazard. Never \
say "other sources report X% flooded". If web sources disagree with the \
assessment, say the assessment is what SHELTER measured and leave it there.
- If you did not retrieve something, say you do not know. Never estimate.

HOW TO ANSWER:

- Call get_my_alerts first for any question about the subscriber's own situation.
- Plain words. No NDVI, no decibels, no model names, no jargon.
- Lead with the answer, then the reason.
- When you cite outside reporting, name the source in the sentence.
- Advice must be doable with what a smallholder farm or district office has.
- Never invent a phone number, URL, agency name, deadline, or price.
- This is a forecast service. Do not promise certainty.
- Answer in the subscriber's language when it is not English.
"""


def build_agent():
    """Construct the chat agent.

    Built per call rather than at import time so the module stays importable with
    no provider configured — `provider.build_model()` raises when `LLM_BASE_URL`
    is unset, and callers check `provider.available()` first.
    """
    agent = Agent[ChatDeps, str](
        provider.build_model(),
        deps_type=ChatDeps,
        output_type=str,
        # `instructions` rather than `system_prompt`: instructions are re-sent per
        # run and are not carried in message history, so replaying retrieved turns
        # as history cannot smuggle a stale copy of the grounding rule.
        instructions=_INSTRUCTIONS,
        model_settings=provider.model_settings(),
        # One retry: a tool that errors should get a second chance to be called
        # correctly, but a loop that keeps failing must end rather than burn quota.
        retries=1,
    )

    @agent.instructions
    def language_note(ctx: RunContext[ChatDeps]) -> str:
        """Dynamic instruction — re-evaluated per run, so the language is always
        the one this session was created with."""
        note = f"The subscriber's language is '{ctx.deps.language}'."
        if not ctx.deps.subscriber_id:
            note += (
                " They have no subscriber record, so you cannot read their alerts "
                "— say so plainly if asked about their own situation."
            )
        return note

    @agent.tool
    async def get_my_alerts(ctx: RunContext[ChatDeps], limit: int = 5) -> dict:
        """Get this subscriber's recent SHELTER alerts with the measured evidence
        behind each.

        This is the ONLY authoritative source for figures about their hazard.
        Call this first for any question about their own alert.
        """
        if not ctx.deps.subscriber_id:
            return {"error": "no subscriber attached to this session"}

        alerts = await repository.list_alerts(
            ctx.deps.subscriber_id, limit=min(limit, 10)
        )
        if not alerts:
            return {"alerts": [], "note": "no alerts on record for this subscriber"}

        return {
            "alerts": [
                {
                    "alert_id": a.id,
                    "area": a.assessment.aoi_name,
                    "hazard": a.assessment.hazard.value,
                    "severity": a.assessment.severity.value,
                    "confidence": round(a.assessment.confidence, 2),
                    "issued": a.created_at.isoformat(),
                    "headline": a.advisory.headline,
                    "actions": a.advisory.actions,
                    # The authoritative figures — the same list the advisory
                    # generator was restricted to.
                    "evidence": a.assessment.evidence,
                    "expected_next": [h.value for h in a.assessment.cascade],
                    "data_sources": a.assessment.data_sources,
                }
                for a in alerts
            ]
        }

    @agent.tool
    async def get_area_history(
        ctx: RunContext[ChatDeps], area_name: str, days: int = 30
    ) -> dict:
        """Risk and severity over time for one of this subscriber's areas.

        Use for "is this getting worse?" or "how does this compare to last month?".
        """
        if not ctx.deps.subscriber_id:
            return {"error": "no subscriber attached to this session"}

        subscriber = await repository.get_subscriber(ctx.deps.subscriber_id)
        if subscriber is None:
            return {"error": "subscriber not found"}

        owned = {a.name.lower(): a.id for a in subscriber.areas}
        aoi_id = owned.get(area_name.strip().lower())
        if aoi_id is None:
            # Return the caller's own area list so the model can recover, without
            # disclosing whether the requested name exists for anyone else.
            return {"error": "unknown area", "your_areas": sorted(owned)}

        series = await repository.assessment_history(aoi_id, days=min(days, 180))
        return {
            "area": area_name,
            "days": min(days, 180),
            "assessments": [
                {
                    "assessed_at": a.assessed_at.isoformat(),
                    "hazard": a.hazard.value,
                    "severity": a.severity.value,
                    "score": round(a.score, 2),
                    "confidence": round(a.confidence, 2),
                }
                for a in series
            ],
        }

    if search.available():

        @agent.tool
        async def search_web(
            ctx: RunContext[ChatDeps], query: str, days: int | None = None
        ) -> dict:
            """Search the web for BACKGROUND CONTEXT: what a hazard does to crops
            or health, official announcements, general guidance.

            NOT a source of figures about the subscriber's own hazard — those come
            only from get_my_alerts.
            """
            response = await search.search(
                query, max_results=settings.chat_search_max_results, days=days
            )
            if not response.searched:
                # Tell the model the tool failed so it says "I could not check",
                # rather than reading an empty list as "nothing exists".
                return {"error": "web search is unavailable right now"}

            for result in response.results:
                ctx.deps.sources.append(
                    SourceCitation(
                        url=result.url,
                        title=result.title,
                        snippet=result.snippet[:400],
                        tier=result.tier,
                        published=result.published,
                    )
                )

            return {
                "results": [
                    {
                        "title": r.title,
                        "url": r.url,
                        "snippet": r.snippet[:600],
                        "source_type": r.tier,
                    }
                    for r in response.results
                ],
                "reminder": (
                    "Background context only. Do not use any number from these "
                    "results to describe the subscriber's hazard."
                ),
            }

    return agent
