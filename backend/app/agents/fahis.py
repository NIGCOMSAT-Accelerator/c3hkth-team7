"""Agent 5 — Fahis: did it actually happen?

The other four agents run forward: discover, measure, decide, deliver. Fahis runs
**backward**, days later, and asks the question nothing else in the system asks —
*were we right?*

That question has no answer today. `ml/weights/*.pt` are gitignored and absent, so
`ml/inference.py` falls back to documented threshold heuristics at 0.55
confidence. There is no labelled dataset, no precision figure, and no way to know
whether a WARNING meant anything. Fahis is the only path to one.

**Why it is off the main line.** Scout → Analyst → Oracle → Herald must not
depend on it and must not be influenced by it. Fahis writes to `verifications` and
`agent_memory` and nowhere else. It never touches an advisory, never adjusts a
severity, never feeds a score. The direction of data flow is the safety property:
if verification could reach the advisory path, unattributed web prose would be one
hop from a number a farmer acts on — exactly the failure the grounding rule in
`advisory/generator.py` exists to prevent, and which was already violated twice in
this codebase's history.

**Why the verdict taxonomy has five values.** The naive design is
confirmed/refuted/unknown, and it is wrong. A flood in a remote LGA may never be
reported by any indexable source. Reading that silence as REFUTED would record
correct warnings as false alarms and train the system on noise. So:

    CONFIRMED       independent sources describe this hazard, here, then
    PARTIAL         right area, wrong hazard or wrong severity
    REFUTED         a source affirmatively says it did NOT happen  (rare)
    UNVERIFIED      nothing found either way          (the common rural case)
    NOT_ATTEMPTED   search was unavailable            (an outage, not a finding)

Only CONFIRMED and REFUTED are trainable. The rest are recorded and excluded from
metrics, because computing precision over UNVERIFIED rows would count unreported
real floods as false positives.

**Bias toward UNVERIFIED is deliberate and enforced twice** — in the prompt, and
in `_guard_verdict` after the model replies. A verification agent that is eager to
conclude is worse than none: it manufactures a ground truth that is really just
search-index coverage.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.agentic import provider as llm_provider
from app.agentic import verdict_agent
from app.agents.base import Agent
from app.config import settings
from app.logging_config import get_logger
from app.models.enums import JobStage, Severity, Verdict
from app.models.schemas import (
    RiskAssessment,
    SourceCitation,
    Verification,
)
from app.search import client as search
from app.store import memory

log = get_logger(__name__)

#: Severity floor for spending a verification budget. Below WATCH the finding was
#: never dispatched (see herald.DISPATCH_FLOOR) or is too routine for anyone to
#: have reported, so a search would return noise and consume quota.
VERIFY_FLOOR = Severity.WATCH

#: The adjudication prompt now lives in `app/agentic/verdict_agent.py`,
#: beside the `VerdictOutput` schema it describes. They belong together: the
#: prompt names the four verdicts and the schema constrains them, and a copy
#: kept here would drift.


class FahisAgent(Agent[RiskAssessment, Verification]):
    """Verifies one assessment against the outside world."""

    stage = JobStage.FAHIS
    #: End of the line. Fahis enqueues nothing — by design, since anything
    #: downstream of it could carry web-sourced text back toward an advisory.
    next_stage = None

    async def run(self, payload: RiskAssessment) -> Verification:
        assessment = payload

        verification = Verification(
            assessment_id=assessment.id,
            aoi_id=assessment.aoi_id,
            claimed_hazard=assessment.hazard,
            claimed_severity=assessment.severity,
            assessed_at=assessment.assessed_at,
        )

        # No search backend means no attempt — recorded as such, never as a
        # non-finding. This distinction is why NOT_ATTEMPTED exists.
        if not search.available():
            verification.verdict = Verdict.NOT_ATTEMPTED
            verification.rationale = "No search backend configured."
            return verification

        queries = self._build_queries(assessment)
        verification.queries = queries

        results = await self._gather(queries, assessment)
        verification.sources = [
            SourceCitation(
                url=r.url,
                title=r.title,
                snippet=r.snippet[: settings.fahis_snippet_max_chars],
                tier=r.tier,
                published=r.published,
            )
            for r in results
        ]

        if not results:
            verification.verdict = Verdict.UNVERIFIED
            verification.rationale = (
                f"Searched {len(queries)} queries; no sources describing this area "
                "and period were found. This is common for rural areas and does "
                "not indicate the hazard did not occur."
            )
            self.log.info(
                "verification found nothing",
                extra={"aoi_id": assessment.aoi_id, "queries": len(queries)},
            )
            return verification

        # Reasoning needs an LLM. Without one we still record what was found, so
        # an operator can adjudicate manually — better than discarding the search.
        if not llm_provider.available():
            verification.verdict = Verdict.UNVERIFIED
            verification.rationale = (
                f"Found {len(results)} candidate sources but no inference endpoint "
                "is configured to adjudicate them. Recorded for manual review."
            )
            return verification

        await self._adjudicate(verification, assessment, results)
        await self._file_memory(verification, assessment)

        self.log.info(
            "verification complete",
            extra={
                "aoi_id": assessment.aoi_id,
                "claimed": assessment.hazard.value,
                "verdict": verification.verdict.value,
                "confidence": round(verification.confidence, 2),
                "sources": len(verification.sources),
            },
        )
        return verification

    # ------------------------------------------------------------------ #
    # Shared memory
    # ------------------------------------------------------------------ #

    async def _file_memory(
        self, verification: Verification, assessment: RiskAssessment
    ) -> None:
        """Record the outcome so later runs over this area can see it.

        Only the conclusive verdicts are filed. `unverified` means nobody reported
        it — writing that as a memory would fill the store with non-findings and,
        worse, a future agent recalling "unverified" could read it as evidence the
        hazard did not occur. That is the same trap the verdict taxonomy exists to
        avoid, one layer down.

        A `refuted` verdict is filed as a **correction**, not an outcome: it is the
        system being told it was wrong, and it is the only ground truth available
        for evaluating an untrained deployment.
        """
        if verification.verdict not in (Verdict.CONFIRMED, Verdict.REFUTED):
            return

        was_right = verification.verdict is Verdict.CONFIRMED
        kind = memory.KIND_OUTCOME if was_right else memory.KIND_CORRECTION

        content = (
            f"A {assessment.severity.value} {assessment.hazard.value.replace('_', ' ')} "
            f"warning for {assessment.aoi_name} on "
            f"{assessment.assessed_at:%Y-%m-%d} was "
            f"{'confirmed by' if was_right else 'REFUTED by'} independent reporting. "
            f"{verification.rationale}"
        )

        await memory.remember(
            agent="fahis",
            kind=kind,
            content=content,
            aoi_id=assessment.aoi_id,
            metadata={
                "verdict": verification.verdict.value,
                "hazard": assessment.hazard.value,
                "severity": assessment.severity.value,
                "score": round(assessment.score, 3),
                "assessment_confidence": round(assessment.confidence, 3),
                "verdict_confidence": round(verification.confidence, 3),
                "source_count": len(verification.sources),
            },
        )

    # ------------------------------------------------------------------ #
    # Query construction
    # ------------------------------------------------------------------ #

    def _build_queries(self, assessment: RiskAssessment) -> list[str]:
        """Queries for one assessment.

        Several angles rather than one, because a single phrasing that misses tells
        you nothing about whether the event occurred — only that your wording was
        wrong. Place name plus hazard words, plus a bare place-and-month query that
        would surface a report using vocabulary we did not anticipate.
        """
        place = self._searchable_place(assessment)
        month = assessment.assessed_at.strftime("%B %Y")
        terms = _HAZARD_SEARCH_TERMS.get(assessment.hazard, ["disaster"])

        queries = [f"{place} {term} {month}" for term in terms[:2]]
        # Deliberately vague: catches a report phrased in words our hazard
        # vocabulary does not contain.
        queries.append(f"{place} {month} flooding OR drought OR crop loss")
        return queries[: settings.fahis_max_queries]

    @staticmethod
    def _searchable_place(assessment: RiskAssessment) -> str:
        """A place name that could plausibly appear in a news report.

        **Never `aoi_name`.** That is the subscriber's own label — "My Irri Palm Fruit Plantation" —
        and it exists nowhere outside their account, so a query built from it cannot corroborate
        anything. Verified live before this fix: three queries, ten sources, none about the place,
        and an `unverified` verdict reached for entirely the wrong reason.

        Administrative names are what reporting uses: "Isoko South, Delta State". Preferring the
        local government area over the state narrows the search to where the hazard actually was —
        a state-wide query would surface a flood four hours away and risk confirming the wrong
        event.

        Falls back to `aoi_name` only when no administrative name was resolved. That query is
        near-useless, but a useless query yields UNVERIFIED, which is the correct and honest verdict
        when we cannot look properly — never REFUTED.
        """
        parts = [p for p in (assessment.admin2, assessment.admin1) if p]
        if parts:
            return ", ".join(parts)
        return assessment.aoi_name

    async def _gather(
        self, queries: list[str], assessment: RiskAssessment
    ) -> list[search.SearchResult]:
        """Run the queries and merge, de-duplicated by URL.

        Sequential rather than concurrent: this is a self-hosted SearXNG instance
        that proxies upstream engines, and firing several queries at once is the
        fastest way to get rate-limited by those upstreams. Fahis runs days after
        the fact — nothing is waiting on it.
        """
        window_days = settings.fahis_search_window_days
        seen: set[str] = set()
        merged: list[search.SearchResult] = []

        for query in queries:
            response = await search.search(
                query,
                max_results=settings.fahis_results_per_query,
                days=window_days,
            )
            for result in response.results:
                if result.url in seen:
                    continue
                seen.add(result.url)
                merged.append(result)

        # Official first, then media. `other`-tier results are kept for the audit
        # trail but the prompt is told they cannot carry a `confirmed` on their own.
        order = {"official": 0, "media": 1, "other": 2}
        merged.sort(key=lambda r: order.get(r.tier, 3))
        return merged[: settings.fahis_max_sources]

    # ------------------------------------------------------------------ #
    # Adjudication
    # ------------------------------------------------------------------ #

    async def _adjudicate(
        self,
        verification: Verification,
        assessment: RiskAssessment,
        results: list[search.SearchResult],
    ) -> None:
        """Ask the model for a verdict, then check its work."""
        window_start = assessment.assessed_at - timedelta(days=1)
        window_end = assessment.assessed_at + timedelta(
            days=assessment.lead_time_days
        )

        numbered = "\n\n".join(
            f"[{i}] ({r.tier}, {_recency(r.published, window_start, window_end)}) "
            f"{r.title}\n{r.url}\n{r.snippet[:600]}"
            for i, r in enumerate(results)
        )

        prompt = f"""\
WARNING TO VERIFY
Area: {assessment.aoi_name}
Hazard claimed: {assessment.hazard.value.replace("_", " ")}
Severity claimed: {assessment.severity.value}
Issued: {assessment.assessed_at:%Y-%m-%d}
Window it covered: {window_start:%Y-%m-%d} to {window_end:%Y-%m-%d}

SEARCH RESULTS ({len(results)} sources)
Each is tagged with its source tier and how its publication date relates to the window.
{numbered}

Did this hazard occur in this area during that window?

Weigh the dates. A source published INSIDE the window is describing events as they happened and is
the strongest evidence available. A source published shortly AFTER is reporting on the window and is
nearly as strong. A source published BEFORE the window cannot describe what happened during it — it
may establish that the area is flood-prone, but it is not corroboration, and a verdict resting on it
alone should be `unverified` rather than `confirmed`. A source with an UNKNOWN date may support
`partial` but cannot on its own support `confirmed`, because you cannot tell whether it describes
this event or a different one years earlier."""

        try:
            agent = verdict_agent.build_agent()
            run = await agent.run(prompt)
            output = run.output
            data = {
                "verdict": output.verdict,
                "confidence": output.confidence,
                "rationale": output.rationale,
                "source_indices": output.source_indices,
            }
        except Exception as exc:
            # A failed adjudication must not become a verdict. Record the sources
            # and leave it UNVERIFIED for manual review.
            self.log.warning(
                "adjudication failed; leaving unverified",
                extra={"aoi_id": assessment.aoi_id, "error": str(exc)},
            )
            verification.rationale = (
                f"Found {len(results)} sources but adjudication failed: {exc}. "
                "Recorded for manual review."
            )
            return

        # The schema guarantees the SHAPE; `_guard_verdict` checks the verdict is
        # actually SUPPORTED by the sources. Validation cannot do the second job,
        # which is why this survives the migration unchanged.
        verdict = self._guard_verdict(
            data, results, window_start=window_start, window_end=window_end
        )
        verification.verdict = verdict
        verification.confidence = _clamp01(float(data.get("confidence") or 0.0))
        verification.rationale = str(data.get("rationale") or "")[:2000]

        # Narrow the stored sources to the ones actually cited. The rest were
        # searched but not relied upon, and keeping them all would overstate the
        # basis of the verdict.
        indices = data.get("source_indices")
        if isinstance(indices, list) and indices:
            cited = [
                verification.sources[i]
                for i in indices
                if isinstance(i, int) and 0 <= i < len(verification.sources)
            ]
            if cited:
                verification.sources = cited

    def _guard_verdict(
        self,
        data: dict,
        results: list[search.SearchResult],
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> Verdict:
        """Validate the model's verdict against the evidence it actually had.

        The prompt asks for caution; this enforces it. Three specific corrections, each for a
        failure mode an eager model reliably exhibits:

        1. **REFUTED without an affirmative source.** Models reach for "refuted"
           when a search comes back thin. Refuting needs a positive statement, so
           with no official or media source present it is downgraded to UNVERIFIED.
        2. **CONFIRMED on `other`-tier sources alone.** A content farm or an
           aggregator re-publishing a rumour is not corroboration. Downgraded to
           PARTIAL.
        3. **CONFIRMED where every dated source predates the window.** An article
           published before the event cannot describe it. A model matching on place and hazard
           words alone will happily confirm a 2026 warning with a 2019 flood report — and that
           inflates precision with a false positive nobody will audit, which is worse than having
           no verdict at all. Downgraded to PARTIAL.

           Only DATED sources count against this. Where nothing carries a date the check cannot
           fire, so it never turns a thin result set into a downgrade on its own — that would
           penalise areas whose coverage happens to lack timestamps, which is most of rural
           Nigeria.
        """
        raw = str(data.get("verdict") or "").strip().lower()
        try:
            verdict = Verdict(raw)
        except ValueError:
            log.warning("model returned unknown verdict", extra={"verdict": raw})
            return Verdict.UNVERIFIED

        # NOT_ATTEMPTED is ours to set, never the model's — it means the search
        # never ran, which the model is in no position to know.
        if verdict is Verdict.NOT_ATTEMPTED:
            return Verdict.UNVERIFIED

        credible = [r for r in results if r.tier in ("official", "media")]

        if verdict is Verdict.REFUTED and not credible:
            log.info("downgrading refuted -> unverified: no credible source")
            return Verdict.UNVERIFIED

        if verdict is Verdict.CONFIRMED and not credible:
            log.info("downgrading confirmed -> partial: no credible source")
            return Verdict.PARTIAL

        if (
            verdict is Verdict.CONFIRMED
            and window_start is not None
            and window_end is not None
        ):
            dated = [
                r
                for r in results
                if _recency(r.published, window_start, window_end) != "unknown date"
            ]
            # Only fires when we HAVE dates and all of them predate the window. An undated result
            # set leaves this untouched, deliberately.
            if dated and all("BEFORE" in _recency(r.published, window_start, window_end) for r in dated):
                log.info(
                    "downgrading confirmed -> partial: every dated source predates the window",
                    extra={"dated_sources": len(dated)},
                )
                return Verdict.PARTIAL

        return verdict


def _recency(
    published: str | None, window_start: datetime, window_end: datetime
) -> str:
    """How a source's publication date relates to the window being verified.

    ## Why this is in the prompt at all

    Fahis asks "did this hazard occur in this window?" — a question about time. Before this, the
    prompt carried the window and the sources but **not the sources' dates**, so the model was asked
    to judge timing with no timing information. It could only match on place and hazard words, which
    is how a 2019 flood article corroborates a 2026 warning.

    That failure mode is specific and dangerous: this codebase's whole accountability claim is that
    a CONFIRMED verdict means something. A confirmation resting on an article predating the event is
    worse than no verdict, because it inflates precision with a false positive that nobody will
    audit.

    ## Why a phrase rather than the raw date

    The model has to reason about the relationship, not do date arithmetic — and asking an LLM to
    compare ISO timestamps invites exactly the kind of silent error there is no way to catch. The
    comparison happens here, deterministically; the model receives the conclusion.

    "unknown date" is stated explicitly rather than omitted. An absent tag would read as "no date
    issue", when in fact it is the case that most needs caution — and on this deployment it is
    common, since general-category engines return no dates at all.
    """
    if not published:
        return "unknown date"

    try:
        # SearXNG returns ISO-8601, sometimes with a trailing Z rather than an offset.
        when = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return "unknown date"

    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    if when < window_start:
        days = (window_start - when).days
        # Named in days rather than "before", because "2 days before" and "7 years before" warrant
        # very different scepticism and the model should be able to tell them apart.
        return f"published {days}d BEFORE the window"
    if when <= window_end:
        return "published INSIDE the window"

    days = (when - window_end).days
    return f"published {days}d after the window"


# --------------------------------------------------------------------------- #
# Hazard vocabulary
# --------------------------------------------------------------------------- #

#: How each hazard is likely to be *reported*, which is not how we name it.
#: Nobody writes "crop_waterlogging" — they write "waterlogged farmland" or
#: "submerged farms". Getting this wrong makes every search miss.
_HAZARD_SEARCH_TERMS: dict = {}


def _init_search_terms() -> None:
    from app.models.enums import HazardType

    _HAZARD_SEARCH_TERMS.update(
        {
            HazardType.FLOOD_INUNDATION: ["flood", "flooding submerged"],
            HazardType.FLOOD_FORECAST: ["flood warning", "heavy rain flooding"],
            HazardType.CROP_WATERLOGGING: [
                "waterlogged farmland",
                "submerged farms crops destroyed",
            ],
            HazardType.CROP_DROUGHT_STRESS: [
                "drought crop failure",
                "dry spell farmers harvest",
            ],
            HazardType.CROP_VEGETATION_ANOMALY: [
                "crop failure",
                "poor harvest farmers",
            ],
            HazardType.MALARIA_RISK: [
                "malaria outbreak cases",
                "malaria surge health",
            ],
        }
    )


_init_search_terms()


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #


def verify_after_for(assessment: RiskAssessment) -> datetime | None:
    """When this assessment becomes verifiable, or None if it never does.

    The delay is the point. A 7-day forecast cannot be checked on day 0 — the
    window has not closed and nothing has been reported yet. We wait for the
    window plus a reporting lag, because local news and agency bulletins trail
    events by days.

    Below `VERIFY_FLOOR` returns None: those findings were never dispatched, or
    are too routine for anyone to have written about, so searching burns quota to
    learn nothing.
    """
    from app.models.enums import SEVERITY_ORDER

    if SEVERITY_ORDER[assessment.severity] < SEVERITY_ORDER[VERIFY_FLOOR]:
        return None

    delay = timedelta(
        days=assessment.lead_time_days + settings.fahis_reporting_lag_days
    )
    return assessment.assessed_at + delay


def is_due(verify_after: datetime | None) -> bool:
    if verify_after is None:
        return False
    return datetime.now(timezone.utc) >= verify_after
