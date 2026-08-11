"""Search client and chat grounding contracts.

Two things under test:

* **Search** must never let us verify ourselves, and must distinguish "searched
  and found nothing" from "could not search".
* **Chat** must not be able to replace a measured figure with a web-sourced one,
  and must not be able to read a subscriber it wasn't scoped to.
"""

from __future__ import annotations

import inspect
import pathlib

from app.config import settings
from app.llm import client as llm
from app.search import client as search

# --------------------------------------------------------------------------- #
# Source tiering
# --------------------------------------------------------------------------- #


def test_official_domains_outrank_media():
    assert search._tier("nema.gov.ng") == "official"
    assert search._tier("reliefweb.int") == "official"
    assert search._tier("premiumtimesng.com") == "media"
    assert search._tier("some-random-blog.xyz") == "other"


def test_tier_matches_subdomains():
    """`www.` and department subdomains must classify the same as the parent."""
    assert search._tier("www.nema.gov.ng") == "official"
    assert search._tier("alerts.nema.gov.ng") == "official"


def test_tier_does_not_match_lookalike_domains():
    """A suffix check must not let `nema.gov.ng.evil.com` claim official status."""
    assert search._tier("nema.gov.ng.evil.com") == "other"
    assert search._tier("fake-nema.gov.ng.attacker.io") == "other"


# --------------------------------------------------------------------------- #
# Self-exclusion — the circular verification guard
# --------------------------------------------------------------------------- #


def test_own_domain_is_excluded(monkeypatch):
    """Without this, verification could 'confirm' a SHELTER alert by finding
    SHELTER's own published alert. Circular, and the most likely way this feature
    produces a confident wrong answer."""
    monkeypatch.setattr(settings, "public_site_url", "https://shelter.zerorate.io")

    assert search._is_self("shelter.zerorate.io") is True
    assert search._is_self("www.shelter.zerorate.io") is True
    assert search._is_self("nema.gov.ng") is False


def test_extra_excluded_domains_are_honoured(monkeypatch):
    monkeypatch.setattr(settings, "search_exclude_domains", "mirror.example.org")
    assert search._is_self("mirror.example.org") is True


# --------------------------------------------------------------------------- #
# Search degradation
# --------------------------------------------------------------------------- #


def test_search_unavailable_without_url(monkeypatch):
    monkeypatch.setattr(settings, "search_provider", "none")
    monkeypatch.setattr(settings, "searxng_url", None)
    assert search.available() is False


async def test_search_returns_searched_false_when_unconfigured(monkeypatch):
    """`searched=False` is what lets Fahis tell an outage from a non-finding."""
    monkeypatch.setattr(settings, "search_provider", "none")
    monkeypatch.setattr(settings, "searxng_url", None)

    response = await search.search("Kebbi flooding")

    assert response.searched is False
    assert response.results == []


async def test_search_never_raises_on_backend_failure(monkeypatch):
    """Unreachable backend must degrade, not propagate."""
    # Provider AND its config: availability is both, since a URL alone no longer implies a backend.
    monkeypatch.setattr(settings, "search_provider", "searxng")
    monkeypatch.setattr(settings, "searxng_url", "http://127.0.0.1:1")

    response = await search.search("Kebbi flooding")

    assert response.searched is False
    assert response.results == []


# --------------------------------------------------------------------------- #
# LLM transport
# --------------------------------------------------------------------------- #


def test_llm_available_needs_only_base_url(monkeypatch):
    """A self-hosted vLLM needs no key — requiring one would make on-premise
    inference impossible, which defeats the sovereignty reason for supporting it."""
    monkeypatch.setattr(settings, "llm_base_url", "http://localhost:8000/v1")
    monkeypatch.setattr(settings, "llm_api_key", None)
    assert llm.available() is True

    monkeypatch.setattr(settings, "llm_base_url", None)
    assert llm.available() is False


def test_tool_loop_is_bounded():
    """An unbounded loop is a runaway cost and latency risk on a service that has
    to answer during a flood."""
    source = inspect.getsource(llm.run_tool_loop)
    assert "max_rounds" in source
    assert "for round_index in range(rounds)" in source
    assert settings.llm_max_tool_rounds > 0


def test_tool_spec_serialises_to_openai_shape():
    spec = llm.ToolSpec(
        name="get_thing",
        description="Gets a thing.",
        parameters={"type": "object", "properties": {}},
        handler=lambda args: None,
    )
    wire = spec.as_wire()

    assert wire["type"] == "function"
    assert wire["function"]["name"] == "get_thing"
    assert "parameters" in wire["function"]


# --------------------------------------------------------------------------- #
# Chat grounding — the rule that keeps web prose out of hazard figures
# --------------------------------------------------------------------------- #


def _tool_schemas(agent) -> dict:
    """Tool name -> JSON schema, as the model actually receives them.

    Reads the agent's own registry rather than a list we built, so a tool added
    without a matching test is visible here.
    """
    return {
        name: tool.function_schema.json_schema
        for name, tool in agent._function_toolset.tools.items()
    }


def _chat_agent(monkeypatch, *, with_search: bool = True):
    """A chat agent wired for offline testing."""
    monkeypatch.setattr(settings, "llm_base_url", "http://localhost:9999/v1")
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(
        settings, "search_provider", "searxng" if with_search else "none"
    )
    monkeypatch.setattr(
        settings, "searxng_url", "http://localhost:8888" if with_search else None
    )

    from app.agentic.chat_agent import build_agent

    return build_agent()


def test_chat_instructions_forbid_web_sourced_figures():
    """The instructions must state the rule explicitly. Tool design and typed deps
    enforce it, but a model that has not been told will still try."""
    from app.agentic import chat_agent

    prompt = chat_agent._INSTRUCTIONS

    assert "get_my_alerts" in prompt
    assert "BACKGROUND CONTEXT ONLY" in prompt
    assert "NEVER use a number from search_web" in prompt


def test_search_tool_description_marks_it_as_context_only(monkeypatch):
    """The description is what the model reads when choosing a tool."""
    agent = _chat_agent(monkeypatch)
    tool = agent._function_toolset.tools["search_web"]

    description = tool.function_schema.description or ""
    assert "BACKGROUND CONTEXT" in description
    assert "NOT a source of figures" in description


def test_search_tool_absent_when_unconfigured(monkeypatch):
    """No search backend should mean no search tool offered — better than a tool
    that always errors."""
    agent = _chat_agent(monkeypatch, with_search=False)
    names = set(_tool_schemas(agent))

    assert "search_web" not in names
    # The subscriber's own data is still reachable, so chat still works.
    assert "get_my_alerts" in names


def test_subscriber_id_is_not_a_tool_parameter(monkeypatch):
    """Scope must travel through typed deps, never as an argument.

    If `subscriber_id` were a parameter the model could ask for anyone's alerts —
    and since GET /alerts is currently unauthenticated, that would make chat a
    convenient exfiltration interface. `RunContext[ChatDeps]` is excluded from the
    generated schema, which is exactly the property under test.
    """
    for name, schema in _tool_schemas(monkeypatch and _chat_agent(monkeypatch)).items():
        properties = schema.get("properties", {})
        assert "subscriber_id" not in properties, (
            f"tool {name} exposes subscriber_id as an argument — scope must "
            "travel through ChatDeps instead"
        )


async def test_area_history_rejects_unowned_areas(monkeypatch):
    """Asking for an area the subscriber doesn't own must not probe the database.

    Returns the caller's own area list so the model can recover, without
    disclosing whether the requested name exists elsewhere.
    """
    from app.agentic.chat_agent import ChatDeps

    agent = _chat_agent(monkeypatch)
    result = await _call_tool(
        agent, "get_area_history", ChatDeps(subscriber_id=None),
        {"area_name": "somewhere else"},
    )

    assert "error" in result


async def test_data_tools_refuse_without_a_subscriber(monkeypatch):
    """An anonymous session must not read anyone's alerts."""
    from app.agentic.chat_agent import ChatDeps

    agent = _chat_agent(monkeypatch)
    deps = ChatDeps(subscriber_id=None)

    assert "error" in await _call_tool(agent, "get_my_alerts", deps, {})
    assert "error" in await _call_tool(
        agent, "get_area_history", deps, {"area_name": "x"}
    )


async def _call_tool(agent, name: str, deps, args: dict):
    """Invoke one registered tool directly, with a minimal RunContext.

    Lets a single tool's guard be tested without a model in the loop — the
    behavioural tests in `test_agentic_chat.py` cover the full run.
    """
    from pydantic_ai import RunContext
    from pydantic_ai.usage import RunUsage

    tool = agent._function_toolset.tools[name]
    ctx = RunContext(deps=deps, model=agent.model, usage=RunUsage())
    return await tool.function_schema.call(args, ctx)


def test_advisory_generator_never_reaches_web_search():
    """The generator shares `app/llm/` transport with chat — that is intended, and
    is what makes the provider swappable — but it must NEVER gain search access.

    Transport is neutral; a data source is not. Chat may cite outside reporting
    because a human asked and can see the citation. An autonomous advisory has no
    such check, so web prose reaching it would put an unattributed number in a
    message a farmer acts on. That is the failure the grounding rule exists to
    prevent, and which this codebase has violated twice.
    """
    from tests.test_fahis import _code_only

    code = _code_only("app/advisory/generator.py")

    assert "app.search" not in code
    assert "searxng" not in code.lower()
    assert "search_web" not in code


def test_advisory_generator_keeps_its_template_fallback():
    """Rule 2 must survive any provider refactor: no configuration may leave a
    subscriber with no advisory."""
    from tests.test_fahis import _code_only

    code = _code_only("app/advisory/generator.py")

    assert "_template" in code
    # Every failure path returns the template rather than propagating.
    assert "except Exception" in code


def test_generator_has_no_tool_mechanism():
    """The advisory path must have no way to retrieve anything.

    Chat and Fahis moved to Pydantic-AI agents; the generator deliberately did
    not. It is one call with no tools, so there is no mechanism by which web prose
    could reach an autonomous advisory — the property this asserts is the *absence*
    of a retrieval path, which survives whatever transport either side uses.
    """
    from tests.test_fahis import _code_only

    code = _code_only("app/advisory/generator.py")

    # No tool loop, from either transport.
    assert "run_tool_loop" not in code
    assert "ToolSpec" not in code
    assert "@agent.tool" not in code
    # And no agent at all — the generator calls a model directly.
    assert "app.agentic" not in code


def test_agents_share_one_provider_factory():
    """Provider portability must stay expressible in one place.

    Chat and Fahis both build their model through `app.agentic.provider`, so
    `LLM_BASE_URL` + `LLM_API_KEY` remains the whole provider switch. A direct
    `OpenAIChatModel(...)` or a `'openai:gpt-4o'` string shorthand anywhere else
    would read the vendor's own env vars and silently bypass our settings.
    """
    from tests.test_fahis import _code_only

    for module in ("app/agentic/chat_agent.py", "app/agentic/verdict_agent.py"):
        code = _code_only(module)
        assert "provider.build_model()" in code, f"{module} must use the factory"
        assert "OpenAIChatModel(" not in code, (
            f"{module} constructs a model directly — it must go through "
            "app/agentic/provider.py, which injects LLM_BASE_URL/LLM_API_KEY"
        )
        # The string shorthand is the specific trap: it looks portable and isn't.
        assert "'openai:" not in code and '"openai:' not in code


def test_chat_has_no_template_fallback():
    """Unlike advisory generation, chat should fail honestly.

    A canned reply to a free-text question is worse than "the assistant is
    unavailable" — it looks like an answer.
    """
    source = pathlib.Path("app/api/routes/chat.py").read_text()
    assert "503" in source
    assert "_template" not in source
