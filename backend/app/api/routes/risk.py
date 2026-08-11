"""On-demand assessment and pipeline control.

`POST /risk/assess` runs Scout → Analyst → Oracle synchronously and returns the
assessment. It bypasses the queue on purpose: a caller is waiting.

It is the slow endpoint in the API — it does real satellite reads and forward
passes, typically 10–40 seconds depending on AOI size and catalogue latency.

## Who actually calls it

Two callers, and the distinction is the whole reason both this and the queued path
exist:

  * **The portal's "Check now"** on `/portal/areas`
    (`frontend/app/portal/areas/actions.ts:reassessArea`) — a subscriber asking what
    the satellite says about their own plot *now*, rather than waiting for the ~6-hour
    watch loop. That action re-reads the plot from the account's own record and passes
    it here verbatim; the `aoi_id` from the form is only ever a lookup key, because
    this endpoint assesses whatever geometry it is handed.
  * **Manual exercise of the EO stack** — the fastest way to drive the STAC/COG,
    rainfall and inference paths against live endpoints without waiting for a cycle.
    The worked call is in `USAGE.md` §8.2.

**Not the operations dashboard.** `/dashboard` calls `GET /alerts` and renders what the
watch loop already found; it has no assessment-triggering control. This docstring
previously claimed a dashboard "scan now" button, which never existed — recorded rather
than silently deleted, because the wrong version sent a reader looking for a caller that
was not there, and reading as though the endpoint were already wired to a UI is what let
it sit unused.

## Why partners get a *different* endpoint for the same intent

`POST /iam/customers/{id}/areas/{aoi_id}/scan` queues instead of blocking, and is
scoped to the caller's own customers. That one takes an **area id**; this one takes
a **geometry**, which is what makes it usable for ground that is not registered at
all — and equally what makes it unscopeable to a tenant. Hence the separate
`platform:assess` scope: an unscoped synchronous assessment is a capability for a
service account, not for a partner key.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.audience import Audience, resolve_audience
from app.config import settings
from app.iam.models import ApiKeyScope
from app.iam.platform import require_platform_scope
from app.logging_config import get_logger
from app.models.schemas import AreaOfInterest, RiskAssessment
from app.store import repository

log = get_logger(__name__)
router = APIRouter(prefix="/risk", tags=["risk"])


@router.post(
    "/assess",
    response_model=RiskAssessment,
    # Separate from PLATFORM_READ because this spends real catalogue quota and takes
    # 10-40s of COG reads. A read-only dashboard key should not be able to run it.
    dependencies=[Depends(require_platform_scope(ApiKeyScope.PLATFORM_ASSESS))],
)
async def assess_area(aoi: AreaOfInterest) -> RiskAssessment:
    """Assess one area now. Nothing is dispatched."""
    if aoi.bbox.area_deg2 > 4.0:
        # ~4 deg² is already a large state. Beyond that the windowed COG reads
        # stop being windowed and the request will time out rather than fail
        # cleanly, so reject it with an explanation instead.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Area of interest is too large for a synchronous assessment. "
                "Split it into smaller areas, or register it as a subscriber "
                "area to have it processed on the queue."
            ),
        )

    # Deferred: `agents.pipeline` reaches `eo/cog` and therefore rasterio, so a
    # module-scope import would drag GDAL into every consumer of this router —
    # including `app/openapi_export.py`, which builds the schema in a lightweight CI
    # job with no geospatial stack. Same reasoning as `eo/exposure.py`.
    from app.agents import pipeline
    assessment = await pipeline.assess(aoi)
    await repository.save_assessment(assessment)
    return assessment


@router.get("/areas/{aoi_id}", response_model=RiskAssessment)
async def latest_assessment(
    aoi_id: str, caller: Audience = Depends(resolve_audience)
) -> RiskAssessment:
    """Most recent cached assessment for an area, if the caller may see that area.

    ## Why this needed a tenant join

    An assessment is keyed by `aoi_id`, and an `aoi_id` carries no tenant of its own — so this route
    had nothing to check against and returned any plot's full assessment to an anonymous caller.
    Verified during triage: `aoi_name='My Irri Palm Fruit Plantation'`, severity, score and the whole
    evidence list, with no credentials.

    `owner_of_area` supplies the missing edge. It is a second query, which is the honest cost of
    tenancy on a table that does not carry the tenant.

    404 for an area this caller may not see — same rule as `get_subscriber`: a 403 would confirm the
    `aoi_id` exists, and area ids appear in dashboard URLs.
    """
    assessment = await repository.get_assessment(aoi_id)
    if assessment is None:
        raise HTTPException(
            status_code=404,
            detail="No recent assessment for this area. Run POST /risk/assess.",
        )

    owner = await repository.owner_of_area(aoi_id)
    if not caller.may_see(owner):
        log.warning(
            "cross-tenant assessment read refused",
            extra={"aoi_id": aoi_id, "audience": caller.label},
        )
        raise HTTPException(
            status_code=404,
            detail="No recent assessment for this area. Run POST /risk/assess.",
        )

    return assessment


@router.post(
    "/scan",
    # PLATFORM_OPERATE, not PLATFORM_ASSESS: this queues a scan for EVERY active
    # subscriber, so its cost scales with the whole fleet rather than one area. That
    # is an operations action, not something the portal should be able to trigger.
    dependencies=[Depends(require_platform_scope(ApiKeyScope.PLATFORM_OPERATE))],
)
async def trigger_scan() -> dict:
    """Queue a full watch cycle across every active subscriber immediately."""
    from app import scheduler

    queued = await scheduler.trigger_now()
    return {"queued_jobs": queued, "horizon_days": settings.forecast_horizon_days}
