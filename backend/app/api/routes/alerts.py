"""Alert history and manual dispatch."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.audience import Audience, resolve_audience
from app.dispatch.tracks import tracks
from app.iam.models import ApiKeyScope
from app.iam.platform import require_platform_scope
from app.logging_config import get_logger
from app.models.enums import TRAINABLE_VERDICTS
from app.models.schemas import (
    Alert,
    AssessmentTrack,
    CitedSource,
    RiskAssessment,
    VerdictSummary,
)
from app.store import repository

log = get_logger(__name__)
router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("", response_model=list[Alert])
async def list_alerts(
    caller: Audience = Depends(resolve_audience),
    subscriber_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[Alert]:
    """Recent alerts, newest first, each carrying Fahis's verdict once it has run.

    ## Scoping is derived from the CREDENTIAL, never from `subscriber_id`

    This endpoint previously took `subscriber_id` as an optional query parameter with **no
    authentication at all**, and `None` meant "every alert on the platform". Reproduced with a bare
    curl and no credentials: another subscriber's plot names, advisories and delivery receipts.

    A farmer's alerts say where their field is, what is wrong with it, and which number was
    messaged. That is exactly the data the whole tenancy model exists to isolate.

    So the audience is now resolved from whoever is calling (`alert_audience`), and
    `subscriber_id` narrows *within* that audience — it can never widen it. An individual asking
    for someone else's id gets their own alerts, not a 403: a 403 would confirm the id exists,
    which is the enumeration leak `authenticate` and `get_customer` are both careful to avoid.

    The verdict is attached here rather than stored on the alert: it is recorded days later, and an
    alert is the immutable record of what a subscriber was told. Joining on read keeps the two
    independent — the alert is what we said, the verdict is what turned out to be true.

    One batch query for every verdict rather than one per alert, so a twenty-alert queue is two
    round trips instead of twenty-one.
    """
    permitted = caller.permitted_subscriber_ids

    # Platform callers (the frontend's own machine-to-machine key) keep the global feed, because
    # the operations dashboard is a platform surface. `None` here means "unrestricted" and is
    # reachable ONLY with `platform:read` — never from an unauthenticated request.
    if permitted is None:
        alerts = await repository.list_alerts(subscriber_id, limit=limit)
        return _attach_tracks(await _attach_verdicts(alerts))

    if not permitted:
        # A signed-in account with no subscriber bound yet, or an aggregator with no customers.
        # An empty list is the honest answer; the global feed is not.
        return []

    # A supplied id must be one this caller may already see. Intersection rather than trust.
    targets = [subscriber_id] if subscriber_id in permitted else sorted(permitted)

    collected: list[Alert] = []
    for target in targets:
        collected.extend(await repository.list_alerts(target, limit=limit))
    collected.sort(key=lambda a: a.created_at, reverse=True)
    return _attach_tracks(await _attach_verdicts(collected[:limit]))


@router.get("/{alert_id}", response_model=Alert)
async def get_alert(
    alert_id: str, caller: Audience = Depends(resolve_audience)
) -> Alert:
    """One alert, if it belongs to this caller.

    **404 rather than 403 for someone else's alert**, for the same reason as `get_customer`: a 403
    confirms the id exists and turns this into an enumeration oracle. Alert ids are not secrets, so
    the distinction has to come from the response.
    """
    alert = await repository.get_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")

    permitted = caller.permitted_subscriber_ids
    if permitted is not None and alert.subscriber_id not in permitted:
        log.warning(
            "cross-tenant alert read refused",
            extra={"alert_id": alert_id, "audience": caller.label},
        )
        raise HTTPException(status_code=404, detail="Alert not found")

    return _attach_tracks(await _attach_verdicts([alert]))[0]


async def _attach_verdicts(alerts: list[Alert]) -> list[Alert]:
    """Populate `alert.verdict` from the verifications table.

    Summarised rather than passed through whole: the queries issued and the raw source snippets are
    provenance for an operator, and putting outside prose on a subscriber's alert card is the
    adjacency the grounding rule exists to avoid. The rationale cites only its listed sources, so it
    travels; the snippets do not.

    Never raises — a verification read failing must not cost a subscriber their alerts. An absent
    verdict renders as "not yet checked", which is the honest state for a fresh alert anyway.
    """
    if not alerts:
        return alerts

    try:
        found = await repository.verifications_for(
            [a.assessment.id for a in alerts]
        )
    except Exception:  # noqa: BLE001
        log.warning("could not read verdicts; alerts returned without them")
        return alerts

    out: list[Alert] = []
    for alert in alerts:
        verification = found.get(alert.assessment.id)
        if verification is None:
            out.append(alert)
            continue
        out.append(
            alert.model_copy(
                update={
                    "verdict": VerdictSummary(
                        verdict=verification.verdict,
                        confidence=verification.confidence,
                        rationale=verification.rationale,
                        source_count=len(verification.sources),
                        # The citations, so a subscriber can check the verdict instead of taking
                        # it on trust. `snippet` is deliberately NOT copied — see `CitedSource`.
                        sources=[
                            CitedSource(
                                url=source.url,
                                title=source.title,
                                tier=source.tier,
                                published=source.published,
                            )
                            for source in verification.sources
                        ],
                        trainable=verification.verdict in TRAINABLE_VERDICTS,
                        verified_at=verification.verified_at,
                    )
                }
            )
        )
    return out


def _attach_tracks(alerts: list[Alert]) -> list[Alert]:
    """Populate `alert.tracks` — the per-track modules the portal renders as cards.

    ## Derived on read, and derived HERE

    Nothing is stored: the tracks are a pure function of the assessment, and persisting them would
    create a second copy of the agronomy that could disagree with the email a subscriber already
    received. Read-time derivation is the same choice `_attach_verdicts` makes for a different
    reason, so both joins sit in one place.

    It happens at this edge rather than as a computed field on `Alert` because the derivation lives
    in `app/dispatch/tracks.py` and imports `app.models.schemas` — a computed field would be a
    circular import, and pointing the schemas at a delivery helper would be the wrong dependency
    direction anyway.

    **The thresholds therefore exist exactly once**, and the email and the portal cannot describe
    one plot differently. That is the whole point: `email/layout.track_modules` and the portal's
    cards are two renderings of this one list.

    Never raises. A track derivation failing must not cost a subscriber their alerts — an alert with
    no modules degrades to the card and the prose, which is what every alert looked like before this
    existed.
    """
    if not alerts:
        return alerts

    out: list[Alert] = []
    for alert in alerts:
        try:
            derived = [
                AssessmentTrack(
                    key=t.key,
                    label=t.label,
                    reading=t.reading,
                    meaning=t.meaning,
                    weight=t.weight,
                    sources=t.sources,
                    detail=t.detail,
                )
                for t in tracks(alert.assessment)
            ]
        except Exception:  # noqa: BLE001
            log.warning(
                "could not derive assessment tracks",
                extra={"alert_id": alert.id},
            )
            out.append(alert)
            continue
        out.append(alert.model_copy(update={"tracks": derived}))
    return out


@router.post(
    "/dispatch/{subscriber_id}",
    response_model=Alert,
    # PLATFORM_BROADCAST, not PLATFORM_SUBSCRIBERS. This endpoint reaches people
    # directly — including the NIGCOMSAT broadcast escalation when every terrestrial
    # channel fails. The frontend has no legitimate reason to page a district, so its
    # key does not carry this scope even though the old shared key granted it.
    dependencies=[Depends(require_platform_scope(ApiKeyScope.PLATFORM_BROADCAST))],
)
async def dispatch_assessment(
    subscriber_id: str, assessment: RiskAssessment
) -> Alert:
    """Generate and send an alert for an assessment.

    The escape hatch for an operator who has looked at a WATCH-grade finding
    and decided it warrants telling people anyway. Suppression rules still
    apply, so a duplicate returns 409 rather than silently sending nothing.
    """
    subscriber = await repository.get_subscriber(subscriber_id)
    if subscriber is None:
        raise HTTPException(status_code=404, detail="Subscriber not found")

    # Imported here, not at module scope. `agents.pipeline` reaches
    # `analyst` -> `eo.cog` -> rasterio, so a top-level import would drag GDAL into
    # every consumer of this router — including `app/openapi_export.py`, which must
    # build the schema in a lightweight CI job with no geospatial stack installed.
    # Same reasoning as the deferred imports in `eo/exposure.py`.
    from app.agents.pipeline import herald

    alert = await herald.execute((subscriber, assessment))
    if alert is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Alert suppressed: severity is below the dispatch floor, or an "
                "equal-or-higher alert for this hazard was already sent recently."
            ),
        )
    return alert
