"""Behavioural tests for the two agent surfaces.

**This file is the reason the migration was worth doing.** Every other test in
this suite is *structural* — it greps for a forbidden import, asserts an enum
label matches, checks a phrase appears in a prompt. None of them can answer the
question that actually matters:

    "When a subscriber asks about their alert, does the model consult their own
     assessments — or does it answer from a web search?"

`FunctionModel` answers it. It substitutes a plain Python function for the model,
so a full agent run executes offline and deterministically: real tools, real
`RunContext`, real scoping, no network and no tokens. What the "model" decides to
call is scripted by the test, which means the *consequences* of a tool call
become assertable.

Two things to hold onto when reading these:

* A `FunctionModel` can only exercise the machinery — tools, deps, scoping,
  output validation. It cannot prove a *real* model obeys the grounding rule; only
  that the mechanism it would have to defeat is in place, and that obeying it is
  possible.
* Where a test scripts a *misbehaving* model (asks for another subscriber's data,
  returns an unsupported verdict), it is asserting our guard holds — not that the
  model would do that.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.config import settings
from app.models.enums import HazardType, Severity, Verdict
from app.models.schemas import Advisory, Alert, RiskAssessment


def _tool_returns(messages: list[ModelMessage]) -> str:
    """Every tool result in the conversation so far, as one searchable string.

    `ToolReturnPart.content` is whatever the tool returned — a dict here, not a
    string — so it has to be serialised before it can be asserted against. Doing
    that in one place keeps the tests readable and stops each of them
    reimplementing the traversal slightly differently.
    """
    import json

    from pydantic_ai.messages import ToolReturnPart

    chunks: list[str] = []
    for message in messages:
        for part in getattr(message, "parts", []):
            if isinstance(part, ToolReturnPart):
                chunks.append(json.dumps(part.content, default=str))
    return " ".join(chunks)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """Point the provider at a URL that is never dialled.

    `FunctionModel` replaces the model, so no request leaves the process — but
    `provider.available()` gates chat, and `build_agent()` constructs a real model
    before `override()` swaps it out.
    """
    monkeypatch.setattr(settings, "llm_base_url", "http://localhost:9999/v1")
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    # Provider AND its config — see `search.client.provider()`.
    monkeypatch.setattr(settings, "search_provider", "searxng")
    monkeypatch.setattr(settings, "searxng_url", "http://localhost:8888")


def _alert(**kw) -> Alert:
    return Alert(
        subscriber_id="sub_1",
        assessment=RiskAssessment(
            aoi_id="aoi_1",
            aoi_name="Argungu",
            hazard=kw.get("hazard", HazardType.CROP_WATERLOGGING),
            severity=kw.get("severity", Severity.WARNING),
            score=0.7,
            confidence=0.82,
            evidence=kw.get("evidence", ["31% of cropland is under standing water"]),
            data_sources=["sentinel-1", "climateserv-gefs"],
        ),
        advisory=Advisory(
            headline="Waterlogged cropland — Argungu. Act within 48 hours.",
            body="Standing water is sitting on your plots.",
            actions=["Open drainage furrows on the worst-affected plots."],
        ),
    )


@pytest.fixture
def one_alert(monkeypatch):
    """Stub the repository so tools return data without a database."""

    async def _list_alerts(subscriber_id=None, *, limit=50):
        return [_alert()] if subscriber_id == "sub_1" else []

    monkeypatch.setattr("app.store.repository.list_alerts", _list_alerts)


# --------------------------------------------------------------------------- #
# Chat — the grounding rule, tested behaviourally
# --------------------------------------------------------------------------- #


async def test_model_can_reach_the_subscribers_own_evidence(monkeypatch, one_alert):
    """The happy path: a scripted model calls `get_my_alerts` and the measured
    evidence reaches it.

    This is the assertion no structural test could make — that the tool actually
    returns the Oracle's `evidence` list to the model, rather than merely existing.
    """
    from app.agentic.chat_agent import ChatDeps, build_agent

    seen: list[str] = []

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        # First turn: ask for the subscriber's own alerts.
        if len(messages) == 1:
            return ModelResponse(
                parts=[ToolCallPart("get_my_alerts", {"limit": 3})]
            )
        # Second turn: the tool result is now in history — record what we got.
        seen.append(_tool_returns(messages))
        return ModelResponse(parts=[TextPart("Your plots are waterlogged.")])

    agent = build_agent()
    with agent.override(model=FunctionModel(script)):
        result = await agent.run("What does my alert mean?", deps=ChatDeps("sub_1"))

    assert result.output == "Your plots are waterlogged."
    assert seen, "the tool result never reached the model"
    assert "standing water" in seen[0], "the evidence list was not returned"
    assert "31%" in seen[0], "the Oracle's own figure was not what was returned"


async def test_anonymous_session_cannot_read_any_subscriber(monkeypatch, one_alert):
    """A run with no subscriber gets an error from every data tool — even though
    an alert for `sub_1` exists and the model asks for it.

    The scoping guarantee: `ChatDeps.subscriber_id` is the only way in, and the
    model has no parameter with which to supply a different one.
    """
    from app.agentic.chat_agent import ChatDeps, build_agent

    tool_results: list[str] = []

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("get_my_alerts", {})])
        tool_results.append(_tool_returns(messages))
        return ModelResponse(parts=[TextPart("I cannot see your alerts.")])

    agent = build_agent()
    with agent.override(model=FunctionModel(script)):
        await agent.run("Show me my alerts", deps=ChatDeps(subscriber_id=None))

    joined = " ".join(tool_results)
    assert "error" in joined
    # The critical negative: sub_1's data must not appear in an anonymous run.
    assert "31%" not in joined
    assert "Argungu" not in joined


async def test_scope_is_not_forgeable_through_tool_arguments(monkeypatch, one_alert):
    """A model that *tries* to pass `subscriber_id` cannot widen its scope.

    Scripts the misbehaviour deliberately. The argument is not in the schema, so
    it is rejected as unexpected — and the run still cannot read `sub_1`.
    """
    from app.agentic.chat_agent import ChatDeps, build_agent

    leaked: list[str] = []

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            # A forged argument, of exactly the kind the old closure design relied
            # on a grep to prevent.
            return ModelResponse(
                parts=[ToolCallPart("get_my_alerts", {"subscriber_id": "sub_1"})]
            )
        leaked.append(_tool_returns(messages))
        return ModelResponse(parts=[TextPart("done")])

    agent = build_agent()
    with agent.override(model=FunctionModel(script)):
        await agent.run("anything", deps=ChatDeps(subscriber_id=None))

    joined = " ".join(leaked)
    assert "31%" not in joined, "a forged tool argument widened the run's scope"
    assert "Argungu" not in joined


async def test_search_results_are_labelled_context_only(monkeypatch):
    """When the model calls `search_web`, the payload it receives carries the
    reminder that these results may not supply hazard figures.

    Structural tests could assert the reminder exists in the source. This asserts
    it is actually *delivered* alongside the results.
    """
    from app.agentic.chat_agent import ChatDeps, build_agent
    from app.search.client import SearchResponse, SearchResult

    async def _search(query, **kwargs):
        return SearchResponse(
            query=query,
            results=[
                SearchResult(
                    url="https://nema.gov.ng/x",
                    title="Flooding reported",
                    snippet="Widespread flooding affected 60% of farmland.",
                    tier="official",
                )
            ],
            searched=True,
        )

    monkeypatch.setattr("app.search.client.search", _search)

    payloads: list[str] = []

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(
                parts=[ToolCallPart("search_web", {"query": "waterlogging rice"})]
            )
        payloads.append(_tool_returns(messages))
        return ModelResponse(parts=[TextPart("Waterlogging starves roots of oxygen.")])

    deps = ChatDeps(subscriber_id="sub_1")
    agent = build_agent()
    with agent.override(model=FunctionModel(script)):
        await agent.run("what does waterlogging do?", deps=deps)

    assert payloads, "search results arrived without the context-only reminder"
    assert "Do not use any number from these" in payloads[0]
    # And the citation was captured for provenance on the stored turn.
    assert len(deps.sources) == 1
    assert deps.sources[0].tier == "official"


async def test_search_outage_is_reported_as_a_failure_not_an_absence(monkeypatch):
    """An unreachable backend must tell the model the tool *failed*.

    An empty result list would read as "nothing exists", which is the same
    absence-of-evidence trap the Fahis verdict taxonomy exists to avoid.
    """
    from app.agentic.chat_agent import ChatDeps, build_agent
    from app.search.client import SearchResponse

    async def _down(query, **kwargs):
        return SearchResponse(query=query, results=[], searched=False)

    monkeypatch.setattr("app.search.client.search", _down)

    payloads: list[str] = []

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("search_web", {"query": "x"})])
        payloads.append(_tool_returns(messages))
        return ModelResponse(parts=[TextPart("I could not check that.")])

    agent = build_agent()
    with agent.override(model=FunctionModel(script)):
        await agent.run("q", deps=ChatDeps("sub_1"))

    joined = " ".join(payloads)
    assert "unavailable" in joined
    # The distinction that matters: an outage must not look like an empty result.
    assert "error" in joined


async def test_tool_provenance_is_recorded_from_the_run(monkeypatch, one_alert):
    """`_tools_called` reads the framework's message trace.

    Provenance is what lets an operator answer "how did it know that?", so it must
    reflect what actually ran rather than a hand-kept list.
    """
    from app.agentic.chat_agent import ChatDeps, build_agent
    from app.chat.service import _tools_called

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("get_my_alerts", {})])
        return ModelResponse(parts=[TextPart("ok")])

    agent = build_agent()
    with agent.override(model=FunctionModel(script)):
        result = await agent.run("q", deps=ChatDeps("sub_1"))

    assert _tools_called(result) == ["get_my_alerts"]


async def test_the_agent_sees_every_registered_tool(monkeypatch):
    """`AgentInfo` reports what the model was actually offered.

    Guards against a tool being defined but never reaching the model — a failure
    that would otherwise be invisible until someone noticed chat could not answer
    a whole class of question.
    """
    from app.agentic.chat_agent import ChatDeps, build_agent

    offered: list[str] = []

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        offered.extend(t.name for t in info.function_tools)
        return ModelResponse(parts=[TextPart("done")])

    agent = build_agent()
    with agent.override(model=FunctionModel(script)):
        await agent.run("q", deps=ChatDeps("sub_1"))

    assert set(offered) == {"get_my_alerts", "get_area_history", "search_web"}


# --------------------------------------------------------------------------- #
# Fahis — verdict validation and the guard behind it
# --------------------------------------------------------------------------- #


def _assessment(severity: Severity = Severity.WARNING) -> RiskAssessment:
    return RiskAssessment(
        aoi_id="aoi_1",
        aoi_name="Argungu, Kebbi",
        hazard=HazardType.FLOOD_INUNDATION,
        severity=severity,
        score=0.7,
        confidence=0.8,
        assessed_at=datetime.now(timezone.utc) - timedelta(days=10),
    )


def _verdict_script(payload: dict):
    """A model that returns `payload` as the structured output.

    Emits JSON TEXT, not a tool call, because the verdict agent uses `NativeOutput` — the schema
    goes to the provider as its own response format and no tool is sent.

    That is not a test convenience: Gemini's OpenAI-compatible endpoint rejects function calling
    combined with a JSON response mime type outright (400, "Function calling with a response mime
    type: 'application/json' is unsupported"), and returns that error as a JSON list Pydantic-AI
    cannot parse — so every adjudication silently fell back to UNVERIFIED with a misleading
    `finish_reason` error in the logs.

    Asserting `not info.output_tools` pins the fix: if someone reverts to tool-based output, this
    fails here rather than in production against one specific provider.
    """

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert not info.output_tools, (
            "adjudication must use NativeOutput, not a tool — Gemini rejects tools combined with "
            "a JSON response format"
        )
        return ModelResponse(parts=[TextPart(json.dumps(payload))])

    return script


async def test_verdict_output_is_validated_and_applied(monkeypatch):
    """A well-formed verdict over a credible source survives end to end."""
    from app.agentic import verdict_agent
    from app.agents.fahis import FahisAgent
    from app.models.schemas import Verification
    from app.search.client import SearchResult

    agent = verdict_agent.build_agent()
    monkeypatch.setattr(verdict_agent, "build_agent", lambda: agent)

    assessment = _assessment()
    verification = Verification(
        assessment_id=assessment.id,
        aoi_id=assessment.aoi_id,
        claimed_hazard=assessment.hazard,
        claimed_severity=assessment.severity,
        assessed_at=assessment.assessed_at,
    )
    results = [
        SearchResult(
            url="https://nema.gov.ng/flood",
            title="NEMA reports flooding in Kebbi",
            snippet="Flooding submerged farmland across Argungu.",
            tier="official",
        )
    ]
    verification.sources = [
        __import__("app.models.schemas", fromlist=["SourceCitation"]).SourceCitation(
            url=r.url, title=r.title, snippet=r.snippet, tier=r.tier
        )
        for r in results
    ]

    with agent.override(
        model=FunctionModel(
            _verdict_script(
                {
                    "verdict": "confirmed",
                    "confidence": 0.9,
                    "rationale": "NEMA reported flooding in this LGA in the window.",
                    "source_indices": [0],
                }
            )
        )
    ):
        await FahisAgent()._adjudicate(verification, assessment, results)

    assert verification.verdict is Verdict.CONFIRMED
    assert verification.confidence == pytest.approx(0.9)
    assert "NEMA" in verification.rationale


async def test_guard_downgrades_confirmed_without_a_credible_source(monkeypatch):
    """Schema validation cannot check *support* — the guard still must.

    The scripted model returns a perfectly well-formed `confirmed` over a single
    low-tier source. Validation passes; `_guard_verdict` downgrades it to
    `partial`, because a content farm re-publishing a rumour is not corroboration.
    """
    from app.agentic import verdict_agent
    from app.agents.fahis import FahisAgent
    from app.models.schemas import Verification
    from app.search.client import SearchResult

    agent = verdict_agent.build_agent()
    monkeypatch.setattr(verdict_agent, "build_agent", lambda: agent)

    assessment = _assessment()
    verification = Verification(
        assessment_id=assessment.id,
        aoi_id=assessment.aoi_id,
        claimed_hazard=assessment.hazard,
        claimed_severity=assessment.severity,
        assessed_at=assessment.assessed_at,
    )
    results = [
        SearchResult(url="https://blog.xyz/a", title="t", snippet="s", tier="other")
    ]

    with agent.override(
        model=FunctionModel(
            _verdict_script(
                {
                    "verdict": "confirmed",
                    "confidence": 0.95,
                    "rationale": "A blog says so.",
                    "source_indices": [0],
                }
            )
        )
    ):
        await FahisAgent()._adjudicate(verification, assessment, results)

    assert verification.verdict is Verdict.PARTIAL


async def test_guard_downgrades_refuted_without_a_credible_source(monkeypatch):
    """The most important single behaviour in Fahis.

    Absence of evidence is not evidence of absence: a `refuted` unsupported by any
    credible source becomes `unverified`, never a recorded false alarm.
    """
    from app.agentic import verdict_agent
    from app.agents.fahis import FahisAgent
    from app.models.schemas import Verification
    from app.search.client import SearchResult

    agent = verdict_agent.build_agent()
    monkeypatch.setattr(verdict_agent, "build_agent", lambda: agent)

    assessment = _assessment()
    verification = Verification(
        assessment_id=assessment.id,
        aoi_id=assessment.aoi_id,
        claimed_hazard=assessment.hazard,
        claimed_severity=assessment.severity,
        assessed_at=assessment.assessed_at,
    )
    results = [
        SearchResult(url="https://blog.xyz/a", title="t", snippet="s", tier="other")
    ]

    with agent.override(
        model=FunctionModel(
            _verdict_script(
                {
                    "verdict": "refuted",
                    "confidence": 0.9,
                    "rationale": "Found nothing, so presumably it did not happen.",
                    "source_indices": [0],
                }
            )
        )
    ):
        await FahisAgent()._adjudicate(verification, assessment, results)

    assert verification.verdict is Verdict.UNVERIFIED
    assert verification.verdict is not Verdict.REFUTED


async def test_unknown_verdict_string_falls_back_to_unverified(monkeypatch):
    """A verdict outside the four values must not become a conclusion.

    `not_attempted` is the specific trap: it means the search never ran, which the
    model is in no position to know, so claiming it would disguise a real
    non-finding as an outage.
    """
    from app.agentic import verdict_agent
    from app.agents.fahis import FahisAgent
    from app.models.schemas import Verification
    from app.search.client import SearchResult

    agent = verdict_agent.build_agent()
    monkeypatch.setattr(verdict_agent, "build_agent", lambda: agent)

    results = [
        SearchResult(url="https://nema.gov.ng/a", title="t", snippet="s", tier="official")
    ]

    for bad in ("not_attempted", "probably", ""):
        assessment = _assessment()
        verification = Verification(
            assessment_id=assessment.id,
            aoi_id=assessment.aoi_id,
            claimed_hazard=assessment.hazard,
            claimed_severity=assessment.severity,
            assessed_at=assessment.assessed_at,
        )
        with agent.override(
            model=FunctionModel(
                _verdict_script(
                    {
                        "verdict": bad,
                        "confidence": 0.9,
                        "rationale": "x",
                        "source_indices": [],
                    }
                )
            )
        ):
            await FahisAgent()._adjudicate(verification, assessment, results)

        assert verification.verdict is Verdict.UNVERIFIED, f"verdict={bad!r}"


async def test_adjudication_failure_leaves_the_verdict_unverified(monkeypatch):
    """A crashed adjudication must not become a verdict about the world."""
    from app.agentic import verdict_agent
    from app.agents.fahis import FahisAgent
    from app.models.schemas import Verification
    from app.search.client import SearchResult

    agent = verdict_agent.build_agent()
    monkeypatch.setattr(verdict_agent, "build_agent", lambda: agent)

    def explode(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("provider exploded")

    assessment = _assessment()
    verification = Verification(
        assessment_id=assessment.id,
        aoi_id=assessment.aoi_id,
        claimed_hazard=assessment.hazard,
        claimed_severity=assessment.severity,
        assessed_at=assessment.assessed_at,
    )

    with agent.override(model=FunctionModel(explode)):
        await FahisAgent()._adjudicate(
            verification,
            assessment,
            [SearchResult(url="https://a/b", title="t", snippet="s", tier="official")],
        )

    assert verification.verdict is Verdict.UNVERIFIED
    assert "adjudication failed" in verification.rationale


async def test_adjudication_runs_at_zero_temperature(monkeypatch):
    """Variance between runs over identical evidence would make the ground truth
    itself irreproducible, which defeats the point of recording it."""
    from app.agentic import verdict_agent

    monkeypatch.setattr(settings, "llm_temperature", 0.7)
    monkeypatch.setattr(settings, "llm_supports_temperature", True)

    agent = verdict_agent.build_agent()

    assert agent.model_settings is not None
    assert agent.model_settings.get("temperature") == 0.0


# --------------------------------------------------------------------------- #
# The scope boundary
# --------------------------------------------------------------------------- #


def test_the_deterministic_pipeline_never_imports_the_framework():
    """Scout, Analyst and Oracle do classic ML and arithmetic locally.

    They make no model call, so a framework has nothing to offer them — and
    keeping it out is what keeps `score`, `confidence` and `severity`
    deterministic, reproducible run-to-run, and testable with no provider
    configured. `test_oracle.py` depends on exactly that.

    The same applies to `app/eo/` (typed geospatial and meteorological adapters)
    and `app/ml/` (PyTorch forward passes).
    """
    import pathlib

    from tests.test_fahis import _code_only

    targets = [
        "app/agents/scout.py",
        "app/agents/analyst.py",
        "app/agents/oracle.py",
        *[str(p) for p in pathlib.Path("app/eo").glob("*.py")],
        *[str(p) for p in pathlib.Path("app/ml").glob("*.py")],
    ]

    for module in targets:
        code = _code_only(module)
        assert "pydantic_ai" not in code, f"{module} imports pydantic-ai"
        assert "app.agentic" not in code, f"{module} imports the agent package"


def test_only_two_surfaces_use_the_framework():
    """The migration's scope, asserted.

    If a third caller appears, this fails — which is the point: the framework was
    adopted for two genuinely agentic surfaces, and spreading it further should be
    a deliberate decision rather than a drift.
    """
    import pathlib

    from tests.test_fahis import _code_only

    users = sorted(
        str(p)
        for p in pathlib.Path("app").rglob("*.py")
        if "app.agentic" in _code_only(str(p)) and "app/agentic/" not in str(p)
    )

    assert users == [
        # Fahis adjudication.
        "app/agents/fahis.py",
        # Reports whether the two surfaces are operational.
        "app/api/routes/chat.py",
        "app/api/routes/health.py",
        # Herald's chat.
        "app/chat/service.py",
    ], f"unexpected app.agentic consumers: {users}"
