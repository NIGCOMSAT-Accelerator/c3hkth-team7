"""Token guardrail contracts.

Two failure modes to guard against, pulling in opposite directions:

* **Over-spending** — the deterministic path stops matching, every question hits
  the LLM, and cost grows silently because nothing looks broken.
* **Over-matching** — the deterministic path answers a specific question with a
  canned reply. Cheap, and wrong, and a farmer acts on it.

The second is worse, so `classify()` is deliberately conservative and most of
these tests pin that conservatism.
"""

from __future__ import annotations

import pathlib

import pytest

from app.chat import answers
from app.config import Settings, settings
from app.llm import budget
from app.models.enums import HazardType, Severity
from app.models.schemas import Advisory, Alert, RiskAssessment


def _alert(
    severity: Severity = Severity.WARNING,
    hazard: HazardType = HazardType.CROP_WATERLOGGING,
    *,
    actions: list[str] | None = None,
    evidence: list[str] | None = None,
    cascade: list[HazardType] | None = None,
    confidence: float = 0.8,
) -> Alert:
    return Alert(
        subscriber_id="sub_1",
        assessment=RiskAssessment(
            aoi_id="aoi_1",
            aoi_name="Argungu",
            hazard=hazard,
            severity=severity,
            score=0.7,
            confidence=confidence,
            evidence=evidence if evidence is not None else ["31% of cropland flooded"],
            cascade=cascade or [],
            data_sources=["sentinel-1", "climateserv-gefs"],
        ),
        advisory=Advisory(
            headline="Waterlogged cropland — Argungu. Act within 48 hours.",
            body="Standing water is sitting on your plots.",
            actions=actions if actions is not None else ["Open drainage furrows."],
        ),
    )


# --------------------------------------------------------------------------- #
# Deterministic intent matching — must FIRE on routine questions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What should I do?", "actions"),
        ("what do i do now", "actions"),
        ("How sure are you about this?", "confidence"),
        ("how confident are you", "confidence"),
        ("How long will this last?", "timing"),
        ("Why did you send me this?", "evidence"),
        ("what evidence do you have", "evidence"),
        ("What does this mean?", "meaning"),
        ("What happens next?", "cascade"),
        ("Do I have any alerts?", "status"),
    ],
)
def test_routine_questions_are_matched(question, expected):
    """These must not cost tokens — the answers already exist in the assessment."""
    assert answers.classify(question) == expected


def test_matching_is_punctuation_and_case_insensitive():
    for variant in ("WHAT SHOULD I DO", "what should i do?!", "  What should I do  "):
        assert answers.classify(variant) == "actions"


# --------------------------------------------------------------------------- #
# ...and must NOT fire on anything specific
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "question",
    [
        # Comparison and history — needs data a template does not have.
        "How does this compare to last season?",
        "Is this worse than last year?",
        "What is the trend over the past month?",
        # Causal reasoning.
        "Why does standing water kill rice roots?",
        "How does waterlogging affect nitrogen uptake?",
        # Hypotheticals.
        "What if I harvest early instead?",
        "Should I still apply fertiliser?",
        # External claims.
        "My neighbour said the flood is over, is it true?",
        "I heard the government is giving compensation",
        # Off-domain.
        "What is the market price for rice now?",
        "Can I get insurance for this?",
        # Long and specific — carries detail no template addresses.
        "What should I do about the three low-lying plots near the river that I "
        "planted late in the season",
    ],
)
def test_specific_questions_fall_through_to_the_llm(question):
    """A canned answer to a specific question is worse than paying for a real one."""
    assert answers.classify(question) is None


def test_unknown_questions_fall_through():
    assert answers.classify("Tell me about soil chemistry") is None
    assert answers.classify("hello") is None


def test_long_questions_always_fall_through():
    """Length is a proxy for specificity — a 15-word question carries detail."""
    long = " ".join(["what should i do"] + ["extra"] * 15)
    assert answers.classify(long) is None


# --------------------------------------------------------------------------- #
# Answer construction — assembled from data, never generated
# --------------------------------------------------------------------------- #


def test_actions_answer_quotes_the_advisory_verbatim():
    """No paraphrasing: paraphrasing is where invented numbers come from."""
    alert = _alert(actions=["Open drainage furrows.", "Delay fertiliser."])
    result = answers.try_answer("What should I do?", alert)

    assert result is not None
    reply, intent = result
    assert intent == "actions"
    assert "Open drainage furrows." in reply
    assert "Delay fertiliser." in reply


def test_confidence_answer_states_the_watch_cap_when_it_applies():
    """The cap is the honest part of the answer — a low-confidence read cannot
    raise an emergency, and the subscriber should know that."""
    low = answers.try_answer("How sure are you?", _alert(confidence=0.5))
    assert low is not None
    assert "capped at Watch" in low[0]

    high = answers.try_answer("How sure are you?", _alert(confidence=0.9))
    assert high is not None
    assert "capped at Watch" not in high[0]


def test_evidence_answer_lists_only_the_evidence():
    alert = _alert(evidence=["31% of cropland flooded", "48 mm of rain forecast"])
    result = answers.try_answer("Why did you send this?", alert)

    assert result is not None
    assert "31% of cropland flooded" in result[0]
    assert "48 mm of rain forecast" in result[0]
    assert "Nothing else was used" in result[0]


def test_answer_falls_through_when_the_data_is_missing():
    """A partial answer that trails off is worse than paying for a real one."""
    assert answers.try_answer("What should I do?", _alert(actions=[])) is None
    assert answers.try_answer("Why did you send this?", _alert(evidence=[])) is None


def test_cascade_answer_handles_an_empty_cascade():
    """Must say so plainly rather than returning nothing."""
    result = answers.try_answer("What happens next?", _alert(cascade=[]))
    assert result is not None
    assert "No follow-on hazards" in result[0]


def test_no_alert_means_no_deterministic_answer():
    assert answers.try_answer("What should I do?", None) is None


def test_deterministic_answers_never_call_an_llm():
    """Structural: this module must have no inference dependency, or the
    zero-token guarantee is not a guarantee."""
    code = pathlib.Path("app/chat/answers.py").read_text()

    for forbidden in ("app.llm", "llm.complete", "run_tool_loop", "httpx"):
        assert forbidden not in code, f"answers.py references {forbidden!r}"


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #


def test_token_estimate_is_the_right_order_of_magnitude():
    """Approximate on purpose — a tokeniser dependency would still be wrong for
    Hausa and Yoruba, and `record()` corrects it with the provider's own figure."""
    assert budget.estimate_tokens("") >= 1
    assert 20 <= budget.estimate_tokens("x" * 100) <= 40


def test_budget_disabled_always_allows():
    assert Settings.model_fields["llm_budget_enabled"].default is True


async def test_budget_check_allows_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "llm_budget_enabled", False)

    allowed, reason = await budget.check("sub_1", 999_999_999)

    assert allowed is True
    assert reason == ""


async def test_budget_fails_open_when_cache_is_down(monkeypatch):
    """The core safety choice. If we cannot read the counter we cannot know the
    spend, and refusing to explain a flood warning over a missing Redis key is
    the worse failure."""
    monkeypatch.setattr(settings, "llm_budget_enabled", True)
    monkeypatch.setattr(settings, "llm_budget_fail_closed", False)

    async def _boom(*args, **kwargs):
        raise RuntimeError("cache unreachable")

    monkeypatch.setattr(budget, "spent_today", _boom)

    allowed, _ = await budget.check("sub_1", 100)

    assert allowed is True


async def test_budget_can_fail_closed_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "llm_budget_enabled", True)
    monkeypatch.setattr(settings, "llm_budget_fail_closed", True)

    async def _boom(*args, **kwargs):
        raise RuntimeError("cache unreachable")

    monkeypatch.setattr(budget, "spent_today", _boom)

    allowed, reason = await budget.check("sub_1", 100)

    assert allowed is False
    assert "cannot be verified" in reason


def test_budget_never_gates_advisory_generation():
    """Advisories are the product. They degrade to English templates on their own
    if a provider is unavailable; a token ceiling must never be the reason a
    farmer gets no warning.

    Checks code rather than raw text — `_truncate_bytes` legitimately documents a
    "byte budget", which is a different thing entirely.
    """
    from tests.test_fahis import _code_only

    code = _code_only("app/advisory/generator.py")

    assert "llm.budget" not in code
    assert "app.llm.budget" not in code
    assert "budget.check" not in code
    assert "BudgetExceeded" not in code


def test_budget_counters_expire_at_midnight():
    """Self-expiring rather than reset by a job: one less moving part, and a
    counter can never outlive its window."""
    seconds = budget._seconds_to_midnight()

    assert 60 <= seconds <= 86_400


def test_usage_parsing_handles_missing_and_partial_blocks():
    """vLLM and some proxies omit `usage`; others report parts but not the total."""
    assert budget.usage_from_response({}) == 0
    assert budget.usage_from_response({"usage": {}}) == 0
    assert budget.usage_from_response({"usage": {"total_tokens": 150}}) == 150
    assert (
        budget.usage_from_response(
            {"usage": {"prompt_tokens": 100, "completion_tokens": 40}}
        )
        == 140
    )


# --------------------------------------------------------------------------- #
# Context sizing
# --------------------------------------------------------------------------- #


def test_model_context_is_smaller_than_display_history():
    """Two different numbers for two different jobs. Conflating them is how a
    display-history bump silently multiplies token cost on every turn."""
    assert (
        Settings.model_fields["chat_context_turns"].default
        < Settings.model_fields["chat_history_turns"].default
    )


def test_retrieval_always_keeps_the_last_exchange():
    """A follow-up like "and the second one?" is unintelligible without its
    predecessor, and similarity search will not rank it highly — it shares no
    vocabulary with the question."""
    from app.chat import service

    source = pathlib.Path(service.__file__).read_text()

    assert "recent AS (" in source
    assert "UNION ALL" in source


def test_retrieval_is_scoped_by_session():
    """Similarity search must filter by session INSIDE the query, not after.
    Otherwise another subscriber's turn is a candidate before the filter drops it.
    """
    from app.chat import service

    source = pathlib.Path(service.__file__).read_text()

    similar = source[source.index("similar AS ("):]
    similar = similar[: similar.index("SELECT DISTINCT")]
    assert "WHERE session_id = $1" in similar


def test_retrieval_degrades_to_recency_without_embeddings():
    """Chat must work with no embedding provider configured."""
    from app.chat import service

    source = pathlib.Path(service.__file__).read_text()

    assert "if vector is None:" in source
    assert "return await history(session_id, limit=limit)" in source
