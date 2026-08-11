"""Verification results — was the warning right?

The operator-facing view of Fahis. `GET /verification/metrics` is the number that
tells you whether the pipeline works at all, and it is the only such number the
system has: model weights are absent by default, so inference runs on threshold
heuristics with no labelled evaluation set behind it.

Read the `note` field on the metrics response before quoting the precision figure.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.audience import Audience, resolve_audience
from app.config import settings
from app.iam.models import ApiKeyScope
from app.iam.platform import require_platform_scope
from app.logging_config import get_logger
from app.models.schemas import Verification
from app.store import repository

log = get_logger(__name__)
router = APIRouter(prefix="/verification", tags=["verification"])


@router.get("/metrics")
async def metrics(days: int = Query(default=90, ge=1, le=365)) -> dict:
    """Verdict counts and precision over the window.

    `precision` counts CONFIRMED against CONFIRMED+REFUTED only, and is `null`
    when neither exists rather than 0 — a displayed 0% would read as "always
    wrong" instead of "not yet measurable".

    `coverage` is the share of verdicts that were conclusive at all. It is usually
    low, and that is expected: most rural areas are not covered by indexed news.
    Precision without coverage beside it overstates what is known.
    """
    return await repository.verification_metrics(days=days)


@router.get("/training-set")
async def training_set(days: int = Query(default=365, ge=1, le=1095)) -> dict:
    """Whether Fahis has accumulated enough verified outcomes to retrain against.

    ## Why this endpoint exists

    The classic-ML additions (LightGBM regression, RandomForest on soil covariates) were deliberately
    NOT built, for one reason: **there was no target**. Fitting them against a derived target would
    have taught them to reproduce the Oracle's existing rules, and they would have inherited
    `CONFIDENCE_TRAINED = 0.88` — escalation authority for a fitted constant.

    Fahis is what removes that objection. It searches outside reporting days after each alert and
    records whether the hazard actually happened, so it accumulates the real labels those models need.
    This reports how far along that is, so the decision to fit is a measurement rather than a guess.

    `ready` stays False until there are enough rows AND both classes are present. A set of 40
    CONFIRMED and 0 REFUTED can only learn "always yes", which scores perfectly on its own data and
    is worthless in the field.

    Ungated deliberately: it exposes counts and feature NAMES, never a subscriber's assessment.
    """
    return await repository.training_set_readiness(days=days)


_NO_VERDICT = (
    "No verification for this assessment. It may not be due yet, or "
    "may be below the severity floor for verification."
)


@router.get("/{assessment_id}", response_model=Verification)
async def get_verification(
    assessment_id: str, caller: Audience = Depends(resolve_audience)
) -> Verification:
    """One verdict, with the sources it rested on — if the caller owns the area it judged.

    Scoped through `aoi_id`, because a `Verification` carries the claimed hazard and severity for a
    named plot. `/metrics` and `/training-set` stay open by contrast: they are aggregates with no
    subscriber, area or contact in them, and the accuracy of the service is something we want
    readable without a credential.

    404 for another tenant's verdict, and the **same message** as a verdict that does not exist —
    otherwise the difference between the two answers confirms the assessment id is real.
    """
    verification = await repository.get_verification(assessment_id)
    if verification is None:
        raise HTTPException(status_code=404, detail=_NO_VERDICT)

    owner = await repository.owner_of_area(verification.aoi_id)
    if not caller.may_see(owner):
        log.warning(
            "cross-tenant verdict read refused",
            extra={"assessment_id": assessment_id, "audience": caller.label},
        )
        raise HTTPException(status_code=404, detail=_NO_VERDICT)

    return verification


@router.post("/sweep", dependencies=[Depends(require_platform_scope(ApiKeyScope.PLATFORM_OPERATE))])
async def sweep(limit: int | None = None) -> dict:
    """Queue every assessment now past its verification date.

    The scheduler does this each cycle; this is the manual trigger for a demo or
    for draining a backlog after the feature is first enabled.
    """
    if not settings.fahis_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Fahis is disabled on this deployment.",
        )
    # Deferred: `agents.pipeline` reaches `eo/cog` and therefore rasterio, so a
    # module-scope import would drag GDAL into every consumer of this router —
    # including `app/openapi_export.py`, which builds the schema in a lightweight CI
    # job with no geospatial stack. Same reasoning as `eo/exposure.py`.
    from app.agents import pipeline
    queued = await pipeline.enqueue_due_verifications(limit)
    return {"queued": queued}
