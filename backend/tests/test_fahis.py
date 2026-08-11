"""Fahis contracts.

The verdict logic is where this feature is most dangerous, so it gets the most
tests. A verification agent that concludes too eagerly manufactures a ground truth
that is really just search-index coverage — and because the output looks like data,
nobody notices until a model has been trained on it.

The rule under test throughout: **absence of evidence is not evidence of absence.**
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from app.agents.fahis import VERIFY_FLOOR, FahisAgent, is_due, verify_after_for
from app.config import settings
from app.models.enums import (
    SEVERITY_ORDER,
    TRAINABLE_VERDICTS,
    HazardType,
    JobStage,
    Severity,
    Verdict,
)
from app.models.schemas import RiskAssessment, Verification
from app.search.client import SearchResult


def _assessment(
    severity: Severity = Severity.WARNING,
    hazard: HazardType = HazardType.FLOOD_INUNDATION,
    **kwargs,
) -> RiskAssessment:
    return RiskAssessment(
        aoi_id="aoi_test",
        aoi_name="Argungu, Kebbi",
        hazard=hazard,
        severity=severity,
        score=0.7,
        confidence=0.8,
        **kwargs,
    )


def _result(tier: str, url: str = "https://example.org/a") -> SearchResult:
    return SearchResult(url=url, title="t", snippet="s", tier=tier)


# --------------------------------------------------------------------------- #
# Verdict taxonomy
# --------------------------------------------------------------------------- #


def test_unverified_is_distinct_from_refuted():
    """The central rule. Collapsing these would record correct warnings — for
    floods nobody happened to report — as false alarms."""
    assert Verdict.UNVERIFIED != Verdict.REFUTED
    assert Verdict.NOT_ATTEMPTED != Verdict.UNVERIFIED


def test_only_confirmed_and_refuted_are_trainable():
    """Computing precision over UNVERIFIED rows would count every unreported real
    flood as a false positive — measuring news coverage, not model accuracy."""
    assert TRAINABLE_VERDICTS == {Verdict.CONFIRMED, Verdict.REFUTED}
    for verdict in (Verdict.UNVERIFIED, Verdict.PARTIAL, Verdict.NOT_ATTEMPTED):
        assert verdict not in TRAINABLE_VERDICTS


def test_verification_defaults_to_unverified():
    """The default must be the cautious verdict, not an optimistic one."""
    verification = Verification(
        assessment_id="risk_1",
        aoi_id="aoi_1",
        claimed_hazard=HazardType.FLOOD_INUNDATION,
        claimed_severity=Severity.WARNING,
        assessed_at=datetime.now(timezone.utc),
    )
    assert verification.verdict is Verdict.UNVERIFIED
    assert verification.is_trainable is False


# --------------------------------------------------------------------------- #
# _guard_verdict — enforcing caution after the model replies
# --------------------------------------------------------------------------- #


def test_refuted_without_credible_source_is_downgraded():
    """Models reach for 'refuted' when a search comes back thin. Refuting needs a
    source affirmatively saying it did not happen."""
    agent = FahisAgent()
    verdict = agent._guard_verdict({"verdict": "refuted"}, [_result("other")])
    assert verdict is Verdict.UNVERIFIED


def test_refuted_with_no_sources_at_all_is_downgraded():
    agent = FahisAgent()
    assert agent._guard_verdict({"verdict": "refuted"}, []) is Verdict.UNVERIFIED


def test_confirmed_on_low_tier_sources_only_is_downgraded():
    """A content farm re-publishing a rumour is not corroboration."""
    agent = FahisAgent()
    verdict = agent._guard_verdict(
        {"verdict": "confirmed"}, [_result("other"), _result("other", "https://x.io/b")]
    )
    assert verdict is Verdict.PARTIAL


def test_confirmed_with_official_source_is_kept():
    agent = FahisAgent()
    verdict = agent._guard_verdict({"verdict": "confirmed"}, [_result("official")])
    assert verdict is Verdict.CONFIRMED


def test_confirmed_with_media_source_is_kept():
    agent = FahisAgent()
    verdict = agent._guard_verdict({"verdict": "confirmed"}, [_result("media")])
    assert verdict is Verdict.CONFIRMED


def test_refuted_with_official_source_is_kept():
    """A genuine refutation must survive — the guard is a floor, not a ceiling."""
    agent = FahisAgent()
    verdict = agent._guard_verdict({"verdict": "refuted"}, [_result("official")])
    assert verdict is Verdict.REFUTED


def test_unknown_verdict_falls_back_to_unverified():
    agent = FahisAgent()
    for bad in ({"verdict": "probably"}, {"verdict": ""}, {}):
        assert agent._guard_verdict(bad, [_result("official")]) is Verdict.UNVERIFIED


def test_model_cannot_claim_not_attempted():
    """NOT_ATTEMPTED means the search never ran, which only we know. A model
    returning it would otherwise disguise a real non-finding as an outage."""
    agent = FahisAgent()
    verdict = agent._guard_verdict(
        {"verdict": "not_attempted"}, [_result("official")]
    )
    assert verdict is Verdict.UNVERIFIED


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #


def test_verification_is_deferred_past_the_forecast_window():
    """Verifying on day 0 is meaningless — the window has not closed and nothing
    has been reported."""
    assessment = _assessment(lead_time_days=7)
    due = verify_after_for(assessment)

    assert due is not None
    expected = assessment.assessed_at + timedelta(
        days=7 + settings.fahis_reporting_lag_days
    )
    assert due == expected
    assert due > assessment.assessed_at + timedelta(days=7)


def test_low_severity_is_never_verified():
    """Below the floor, findings were never dispatched or are too routine for
    anyone to report — searching burns quota to learn nothing."""
    for severity in (Severity.INFO, Severity.ADVISORY):
        assert verify_after_for(_assessment(severity=severity)) is None


def test_at_and_above_floor_is_scheduled():
    for severity in (Severity.WATCH, Severity.WARNING, Severity.EMERGENCY):
        assert verify_after_for(_assessment(severity=severity)) is not None


def test_verify_floor_is_at_or_above_dispatch_floor():
    """Verifying something never sent would measure alerts nobody received."""
    from app.agents.herald import DISPATCH_FLOOR

    assert SEVERITY_ORDER[VERIFY_FLOOR] >= SEVERITY_ORDER[DISPATCH_FLOOR]


def test_is_due_handles_none_and_future():
    assert is_due(None) is False
    assert is_due(datetime.now(timezone.utc) + timedelta(days=1)) is False
    assert is_due(datetime.now(timezone.utc) - timedelta(days=1)) is True


# --------------------------------------------------------------------------- #
# Degradation
# --------------------------------------------------------------------------- #


async def test_no_search_backend_records_not_attempted(monkeypatch):
    """An outage must never become a verdict about the world."""
    monkeypatch.setattr(settings, "searxng_url", None)

    verification = await FahisAgent().run(_assessment())

    assert verification.verdict is Verdict.NOT_ATTEMPTED
    assert verification.is_trainable is False


async def test_search_returning_nothing_is_unverified_not_refuted(monkeypatch):
    """The single most important behaviour in this module."""
    # Availability is `SEARCH_PROVIDER` plus that provider's own configuration, so both are set.
    # A URL alone no longer implies a backend — a deployment that set one and forgot the provider
    # gets NOT_ATTEMPTED, which is the honest state rather than a silent partial configuration.
    monkeypatch.setattr(settings, "search_provider", "searxng")
    monkeypatch.setattr(settings, "searxng_url", "http://searx.local")

    async def _empty(query, **kwargs):
        from app.search.client import SearchResponse

        return SearchResponse(query=query, results=[], searched=True)

    monkeypatch.setattr("app.search.client.search", _empty)

    verification = await FahisAgent().run(_assessment())

    assert verification.verdict is Verdict.UNVERIFIED
    assert verification.verdict is not Verdict.REFUTED
    # The rationale must say why, so an operator reading it doesn't infer safety.
    assert "does not indicate" in verification.rationale
    assert verification.queries, "queries should be recorded even when fruitless"


# --------------------------------------------------------------------------- #
# Isolation from the advisory path
# --------------------------------------------------------------------------- #


def test_fahis_is_terminal():
    """Enqueueing anything downstream could carry web-sourced text back toward an
    advisory, which is the failure the grounding rule prevents."""
    assert FahisAgent.next_stage is None
    assert FahisAgent.stage is JobStage.FAHIS


def _code_only(path: str) -> str:
    """Source with comments and docstrings stripped.

    These checks are about what the module can *do*, so prose that merely
    describes the rule must not trip them — the docstrings here deliberately
    discuss the advisory path at length.
    """
    import ast

    tree = ast.parse(pathlib.Path(path).read_text())
    for node in ast.walk(tree):
        # Drop docstring expression statements, leaving executable code.
        if isinstance(
            node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_fahis_never_writes_advisories_or_dispatches():
    """Structural check on the safety boundary.

    Fahis writes verifications and agent memory. If it ever gains the ability to
    save an assessment, generate an advisory, or dispatch, unattributed web prose
    is one hop from a number a farmer acts on.
    """
    code = _code_only("app/agents/fahis.py")

    for forbidden in (
        "save_assessment",
        "save_alert",
        "save_alert",
        "generator",
        "dispatch",
        "deliver",
    ):
        assert forbidden not in code, (
            f"fahis.py calls {forbidden!r} — verification must not be able to "
            "reach the advisory or dispatch path"
        )


def test_oracle_and_analyst_never_import_search():
    """The risk model must stay reproducible.

    Web results differ between two calls an hour apart; a score derived from them
    could not be defended to a state agriculture officer, and test_oracle.py
    could not exist.
    """
    for module in ("app/agents/oracle.py", "app/agents/analyst.py", "app/agents/scout.py"):
        code = _code_only(module)
        assert "app.search" not in code, f"{module} must not import web search"
        assert "searxng" not in code.lower(), f"{module} must not reach a search engine"


def test_eo_layer_never_imports_search():
    """Same rule for the data adapters — every number in an assessment must come
    from a typed, reproducible source."""
    for path in pathlib.Path("app/eo").glob("*.py"):
        code = _code_only(str(path))
        assert "app.search" not in code, f"{path} must not import web search"


# --------------------------------------------------------------------------- #
# Query construction
# --------------------------------------------------------------------------- #


def test_queries_use_reporting_vocabulary_not_enum_names():
    """Nobody writes 'crop_waterlogging'. Searching for our internal name would
    miss every real report."""
    agent = FahisAgent()
    queries = agent._build_queries(
        _assessment(hazard=HazardType.CROP_WATERLOGGING)
    )

    assert queries
    joined = " ".join(queries)
    assert "crop_waterlogging" not in joined
    assert "waterlogged" in joined or "submerged" in joined
    # Place and month anchor the search in space and time.
    assert all("Argungu" in q for q in queries)


def test_every_hazard_has_search_terms():
    """A hazard with no vocabulary would silently never be verifiable."""
    from app.agents.fahis import _HAZARD_SEARCH_TERMS

    for hazard in HazardType:
        assert hazard in _HAZARD_SEARCH_TERMS, f"no search terms for {hazard.value}"
        assert _HAZARD_SEARCH_TERMS[hazard], f"empty search terms for {hazard.value}"


def test_query_count_is_bounded():
    agent = FahisAgent()
    assert len(agent._build_queries(_assessment())) <= settings.fahis_max_queries


@pytest.mark.parametrize("hazard", list(HazardType))
def test_queries_build_for_every_hazard(hazard):
    assert FahisAgent()._build_queries(_assessment(hazard=hazard))


# --------------------------------------------------------------------------- #
# Recency — dates as evidence weight
# --------------------------------------------------------------------------- #


def test_recency_classifies_a_source_against_the_window():
    """The window comparison is done HERE, deterministically, not by the model.

    Asking an LLM to compare ISO timestamps invites a silent arithmetic error with no way to catch
    it. The model receives the conclusion — "published INSIDE the window" — and reasons about the
    relationship instead.
    """
    from datetime import datetime, timezone

    from app.agents.fahis import _recency

    start = datetime(2026, 8, 9, tzinfo=timezone.utc)
    end = datetime(2026, 8, 17, tzinfo=timezone.utc)

    assert "INSIDE" in _recency("2026-08-12T10:00:00Z", start, end)
    assert "BEFORE" in _recency("2019-09-01T10:00:00Z", start, end)
    assert "after" in _recency("2026-08-19T10:00:00Z", start, end)

    # A naive timestamp is read as UTC rather than crashing — SearXNG returns both forms.
    assert "INSIDE" in _recency("2026-08-12T10:00:00", start, end)


def test_an_undated_or_malformed_source_is_reported_as_unknown():
    """"unknown date" is stated explicitly, never omitted.

    An absent tag would read as "no date issue", when it is the case most needing caution — and on a
    general-category engine it is the common one.
    """
    from datetime import datetime, timezone

    from app.agents.fahis import _recency

    start = datetime(2026, 8, 9, tzinfo=timezone.utc)
    end = datetime(2026, 8, 17, tzinfo=timezone.utc)

    assert _recency(None, start, end) == "unknown date"
    assert _recency("not-a-date", start, end) == "unknown date"
    assert _recency("", start, end) == "unknown date"


def test_days_before_is_quantified_not_just_flagged():
    """"2 days before" and "7 years before" warrant very different scepticism.

    A bare "before" would collapse them, and a model cannot recover the difference.
    """
    from datetime import datetime, timezone

    from app.agents.fahis import _recency

    start = datetime(2026, 8, 9, tzinfo=timezone.utc)
    end = datetime(2026, 8, 17, tzinfo=timezone.utc)

    near = _recency("2026-08-07T00:00:00Z", start, end)
    far = _recency("2019-09-01T00:00:00Z", start, end)

    # Asserting the RELATIONSHIP, not exact day counts. Hardcoding "2d" and "2534d" would pin
    # arithmetic that is not the point of this test and would break on a boundary change — what
    # matters is that a near miss and a seven-year gap are distinguishable.
    near_days = int(near.split("published ")[1].split("d ")[0])
    far_days = int(far.split("published ")[1].split("d ")[0])
    assert near_days < 30, f"a two-day gap should read as small, got {near}"
    assert far_days > 2000, f"a seven-year gap should read as large, got {far}"
    assert far_days > near_days * 100


def test_confirmed_is_downgraded_when_every_dated_source_predates_the_window():
    """**The failure this exists to prevent.**

    A model matching on place and hazard words alone will confirm a 2026 warning with a 2019 flood
    report. That inflates precision with a false positive nobody audits — worse than no verdict,
    because the whole accountability claim is that CONFIRMED means something.

    Verified live: before the dates reached the prompt, six Reuters articles from 2021-2025 about
    Brazil and Indonesia produced `confirmed`; after, the same shape produced `unverified` with the
    reason stated.
    """
    from datetime import datetime, timezone

    from app.search.client import SearchResult

    start = datetime(2026, 8, 9, tzinfo=timezone.utc)
    end = datetime(2026, 8, 17, tzinfo=timezone.utc)

    stale = [
        SearchResult(
            url="https://nema.gov.ng/2019",
            title="Flooding in 2019",
            snippet="s",
            tier="official",
            published="2019-09-01T00:00:00Z",
        )
    ]
    verdict = FahisAgent()._guard_verdict(
        {"verdict": "confirmed"}, stale, window_start=start, window_end=end
    )
    assert verdict is Verdict.PARTIAL, (
        "a confirmation resting only on pre-window sources must be downgraded"
    )


def test_one_source_inside_the_window_keeps_the_confirmation():
    """The guard must not punish a mixed result set.

    Historical context alongside a genuine in-window report is normal, and downgrading it would make
    CONFIRMED unreachable for any well-covered area.
    """
    from datetime import datetime, timezone

    from app.search.client import SearchResult

    start = datetime(2026, 8, 9, tzinfo=timezone.utc)
    end = datetime(2026, 8, 17, tzinfo=timezone.utc)

    mixed = [
        SearchResult(url="https://a/1", title="old", snippet="s", tier="official",
                     published="2019-09-01T00:00:00Z"),
        SearchResult(url="https://a/2", title="now", snippet="s", tier="official",
                     published="2026-08-12T00:00:00Z"),
    ]
    assert (
        FahisAgent()._guard_verdict(
            {"verdict": "confirmed"}, mixed, window_start=start, window_end=end
        )
        is Verdict.CONFIRMED
    )


def test_undated_sources_never_trigger_the_recency_downgrade():
    """Most of rural Nigeria is covered by engines that report no dates.

    Firing on undated results would penalise an area for its coverage rather than for its evidence,
    and would make the guard indistinguishable from "we found little".
    """
    from datetime import datetime, timezone

    from app.search.client import SearchResult

    start = datetime(2026, 8, 9, tzinfo=timezone.utc)
    end = datetime(2026, 8, 17, tzinfo=timezone.utc)

    undated = [
        SearchResult(url="https://a/1", title="t", snippet="s", tier="official", published=None)
    ]
    assert (
        FahisAgent()._guard_verdict(
            {"verdict": "confirmed"}, undated, window_start=start, window_end=end
        )
        is Verdict.CONFIRMED
    )


def test_the_prompt_tells_the_model_how_to_weigh_dates():
    """The tag is useless if the instructions do not say what to do with it."""
    import inspect

    source = inspect.getsource(FahisAgent._adjudicate)

    assert "_recency(" in source, "each source must be tagged with its recency"
    assert "INSIDE the window" in source, "the prompt must explain what to do with the tag"
    assert "cannot describe what happened" in source, (
        "the prompt must state that a pre-window source is not corroboration"
    )


def test_the_worker_resolves_the_alert_id_for_correlation():
    """`verifications.alert_id` is the key the webhook contract tells partners to join on.

    Fahis is handed only the assessment and cannot know the alert, so this column was written NULL
    on every row — dead since creation, while the published contract documented it as the way to
    close the loop on an alert already received. Resolved in the worker, which has repository
    access; not in the agent, which must keep reaching nothing but the search backend.
    """
    import pathlib

    source = pathlib.Path("app/queue/worker.py").read_text()
    start = source.index("async def _handle_fahis(")
    body = source[start : source.index("\n_HANDLERS", start)]

    assert "alert_id_for_assessment" in body, (
        "the worker must resolve the alert this verdict judges, or partners cannot correlate"
    )
    # Only when Fahis did not already set one — never overwriting a known link.
    assert "if verification.alert_id is None:" in body


def test_a_verdict_survives_without_an_alert():
    """An assessment below the dispatch floor is still verified.

    Accuracy is measured on what we CONCLUDED, not on what we chose to send — so `alert_id` staying
    None must not prevent the verdict being recorded. Those correlate on `assessment_id`, which is
    always present.
    """
    from app.models.schemas import Verification

    v = Verification(
        assessment_id="risk_x",
        aoi_id="aoi_x",
        claimed_hazard=HazardType.FLOOD_INUNDATION,
        claimed_severity=Severity.WATCH,
        assessed_at=datetime.now(timezone.utc),
    )
    assert v.alert_id is None
    assert v.assessment_id, "assessment_id is the fallback correlation key and must always be set"


# --------------------------------------------------------------------------- #
# The retraining set — Fahis accumulating real labels
#
# This is the sequencing that makes LightGBM/RandomForest honest. Those models were deliberately not
# built because there was NO TARGET: fitting them against a derived target teaches them to reproduce
# the Oracle's own rules, and they would inherit CONFIDENCE_TRAINED = 0.88 for it.
# --------------------------------------------------------------------------- #


def test_the_training_set_carries_features_not_just_outcomes():
    """`verification_outcomes` yields `(confidence, outcome)` — enough to CALIBRATE, not to RETRAIN.

    A model needs the inputs that produced a prediction. A CONFIRMED flood should teach that
    "65% inundated, on impeded clay, with 48 mm forecast" was correct; with only the confidence, all
    it can teach is "the pipeline was right at 0.88" — the same scalar for every hazard.
    """
    import pathlib

    source = pathlib.Path("app/store/repository.py").read_text()
    start = source.index("async def training_rows(")
    body = source[start : source.index("\nasync def training_set_readiness(", start)]

    for feature in (
        "inundated_fraction",
        "stressed_crop_fraction",
        "score_drivers",
        "stress_attribution",
        "method_flood",
    ):
        assert feature in body, f"the training set must carry {feature}"


def test_only_trainable_verdicts_enter_the_training_set():
    """UNVERIFIED means nobody reported it — the COMMON case for a remote Nigerian LGA.

    Training on it would fit news coverage rather than model accuracy. PARTIAL is genuinely
    ambiguous (right area, wrong hazard or severity) and forcing it to 0 or 1 would inject the
    judgement the five-value taxonomy deliberately refuses to make.
    """
    import pathlib

    source = pathlib.Path("app/store/repository.py").read_text()
    start = source.index("async def training_rows(")
    body = source[start : source.index("\nasync def training_set_readiness(", start)]

    assert "IN ('confirmed', 'refuted')" in body
    for excluded in ("'unverified'", "'partial'", "'not_attempted'"):
        assert excluded not in body, f"{excluded} must not enter the training set"


def test_an_unmeasured_leg_persists_as_null_not_zero():
    """**The defect this would otherwise reintroduce.**

    A training row asserting "0% water, and that was CONFIRMED" teaches the model that a blind cycle
    is evidence of dry ground. That conflation is what made a radar failure classify as drought, and
    a training set is the worst possible place to repeat it.
    """
    import pathlib

    source = pathlib.Path("app/agents/oracle.py").read_text()
    start = source.index("inundated_fraction=(")
    block = source[start : start + 320]

    assert "if analysis.flood_measured else None" in block
    assert "if analysis.stress_measured else None" in block


def test_readiness_requires_both_classes_present():
    """40 CONFIRMED and 0 REFUTED can only learn "always yes".

    That scores perfectly on its own data and is worthless in the field. A count alone would report
    such a set as ready.
    """
    import pathlib

    source = pathlib.Path("app/store/repository.py").read_text()
    start = source.index("async def training_set_readiness(")
    body = source[start : source.index("\nasync def verification_outcomes(", start)]

    assert "MIN_PER_CLASS" in body
    assert "confirmed < MIN_PER_CLASS" in body
    assert "refuted < MIN_PER_CLASS" in body
    # And it must say WHY refuted is the binding constraint.
    assert "rare by design" in body


def test_readiness_is_reported_rather_than_assumed():
    """A fitting script that runs on 3 rows produces a model, and that model is noise.

    The failure is silent: nothing errors, the weights load, and the pipeline gains escalation
    authority for a fitted constant. So the decision to fit is an explicit, inspectable measurement.
    """
    import pathlib

    routes = pathlib.Path("app/api/routes/verification.py").read_text()
    assert "training_set_readiness" in routes, (
        "readiness must be observable, or nobody can tell when fitting became legitimate"
    )
