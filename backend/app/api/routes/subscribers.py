"""Subscriber registration — the endpoint the frontend's activation flow calls."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.agents import pipeline
from app.api.area_input import normalise_area, reject_unavailable_channels
from app.api.audience import Audience, resolve_audience
from app.eo import geometry
from app.iam import attribution, mailer
from app.iam import store as iam_store
from app.iam.models import ApiKeyScope
from app.iam.platform import require_platform_scope
from app.logging_config import get_logger
from app.models.enums import DeliveryMode
from app.models.schemas import (
    AreaOfInterest,
    BBox,
    ChannelBinding,
    Subscriber,
    SubscriberCreate,
)
from app.store import repository

log = get_logger(__name__)
router = APIRouter(prefix="/subscribers", tags=["subscribers"])


@router.post(
    "",
    response_model=Subscriber,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_platform_scope(ApiKeyScope.PLATFORM_SUBSCRIBERS))],
)
async def create_subscriber(payload: SubscriberCreate) -> Subscriber:
    """Register a subscriber and immediately queue a first scan.

    Queueing on registration matters for the product: someone who signs up
    during a developing flood should not wait up to six hours for the next
    scheduled cycle to find out they are in it.
    """
    if not payload.areas:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one area of interest is required",
        )
    reject_unavailable_channels(payload.channels)

    if not payload.channels:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one delivery channel is required",
        )

    subscriber = Subscriber(**payload.model_dump())
    await repository.save_subscriber(subscriber)

    for area in subscriber.areas:
        # Deferred: `agents.pipeline` reaches `eo/cog` and therefore rasterio, so a
        # module-scope import would drag GDAL into every consumer of this router —
        # including `app/openapi_export.py`, which builds the schema in a lightweight CI
        # job with no geospatial stack. Same reasoning as `eo/exposure.py`.
        from app.agents import pipeline
        await pipeline.enqueue_scan(subscriber, area)

    log.info(
        "subscriber registered",
        extra={
            "subscriber_id": subscriber.id,
            "areas": len(subscriber.areas),
            "channels": [c.channel.value for c in subscriber.channels],
        },
    )
    return subscriber


@router.get("", response_model=list[Subscriber])
async def list_subscribers(
    caller: Audience = Depends(resolve_audience), active_only: bool = False
) -> list[Subscriber]:
    """The subscribers this caller may see.

    ## What this used to return

    Everything, to anyone. There was no dependency at all, so a bare curl with no credentials got
    every subscriber's full name, email address or phone number, and the exact bounding box of every
    plot they monitor. Verified during triage:

        MDN4D9KSEV  'Lionel Okeoghene Orishane'  contacts=['lionel@…']
        'My Rice plantation' bbox={'west': 4.412…, 'south': 12.412…}

    A farm's coordinates plus its owner's phone number is the most identifying pair the platform
    holds, and it was a public endpoint.

    An **individual** now sees exactly their own subscriber — the plot they activated themselves. An
    **aggregator** sees the customers their workspace serves, resolved through the membership edge,
    and never the global list.
    """
    permitted = caller.permitted_subscriber_ids

    if permitted is None:
        # Platform surface only — the operations dashboard. Never reachable anonymously.
        return await repository.list_subscribers(active_only=active_only)

    if not permitted:
        # No plot bound yet, or an aggregator with no customers. Empty is the honest answer; the
        # global list is not. `is None` above and `not` here are deliberately separate — see
        # `Audience`.
        return []

    # Fetched by id rather than filtering a global read, so another tenant's record is never
    # loaded into this process at all. The same reasoning as the session filter living inside the
    # chat-retrieval query rather than being applied to its results.
    found = [await repository.get_subscriber(sid) for sid in sorted(permitted)]
    subscribers = [s for s in found if s is not None]
    if active_only:
        subscribers = [s for s in subscribers if s.active]
    return subscribers


@router.get("/{subscriber_id}", response_model=Subscriber)
async def get_subscriber(
    subscriber_id: str, caller: Audience = Depends(resolve_audience)
) -> Subscriber:
    """One subscriber, if this caller may see them.

    **404 rather than 403** for someone else's record: a 403 confirms the id exists, which turns
    this into an enumeration oracle over subscriber ids. Same rule as `get_customer` and `get_alert`.
    """
    subscriber = await repository.get_subscriber(subscriber_id)
    if subscriber is None:
        raise HTTPException(status_code=404, detail="Subscriber not found")

    if not caller.may_see(subscriber_id):
        log.warning(
            "cross-tenant subscriber read refused",
            extra={"subscriber_id": subscriber_id, "audience": caller.label},
        )
        raise HTTPException(status_code=404, detail="Subscriber not found")

    return subscriber


class ChannelUpdate(BaseModel):
    """The channels a subscriber wants, replacing what is stored.

    Sent as the **full desired set**, not a diff. That matches how the create path works and how
    the UI presents it, and it makes "remove this channel" expressible — a diff-based API needs a
    separate delete verb, and a partial update cannot say "stop using WhatsApp".
    """

    channels: list[ChannelBinding] = Field(
        min_length=1,
        description=(
            "At least one. A subscriber with no channels is monitored and unreachable, which is "
            "the one configuration the platform must not accept."
        ),
    )


@router.put("/{subscriber_id}/channels", response_model=Subscriber)
async def replace_channels(
    request: Request,
    subscriber_id: str,
    payload: ChannelUpdate,
    background: BackgroundTasks,
    caller: Audience = Depends(resolve_audience),
) -> Subscriber:
    """Change where alerts go, and from which severity — per area or for all of them.

    ## Why this endpoint did not exist before

    Channels were settable **only at signup**. There was no way to correct a mistyped phone
    number, switch from email to WhatsApp, or raise a threshold after too many advisories — the
    portal's Settings page could display the preferred channel but not change it. For a farmer who
    entered the wrong number, alerts went nowhere and nothing could be done about it.

    ## Per-area overrides

    A binding with `aoi_id` set applies to that plot only; one with `aoi_id: null` applies to every
    plot. **Specific overrides general** rather than adding to it — see `Subscriber.channels_for`
    for why a union would be wrong. So a subscriber can route flood alerts for the riverside plot
    to SMS while everything else stays on email.

    An `aoi_id` naming an area this subscriber does not own is rejected: it would create a binding
    that can never fire, and silently accepting it would look like a save that worked.

    ## Scoping

    An individual may change their own. An aggregator may change their customers' — they usually
    entered the contact details during onboarding, so they must be able to correct them — and the
    customer is emailed naming who did it. A silent change to where someone's flood warnings go is
    not acceptable even when it is legitimate.
    """
    subscriber = await repository.get_subscriber(subscriber_id)
    if subscriber is None:
        raise HTTPException(status_code=404, detail="Subscriber not found")

    if not caller.may_see(subscriber_id):
        log.warning(
            "cross-tenant channel update refused",
            extra={"subscriber_id": subscriber_id, "audience": caller.label},
        )
        # 404, not 403 — see `get_subscriber`. A 403 confirms the id exists.
        raise HTTPException(status_code=404, detail="Subscriber not found")

    reject_unavailable_channels(payload.channels)

    owned = {area.id for area in subscriber.areas}
    for binding in payload.channels:
        if binding.aoi_id and binding.aoi_id not in owned:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"{binding.aoi_id} is not one of this subscriber's areas. A channel bound to "
                    f"an area they do not monitor could never deliver anything."
                ),
            )

    # `min_score` and `min_severity` are part of the comparison key, not just the address set.
    #
    # Without them, changing only a threshold would compare equal and send no notice — and a raised
    # dial is precisely the change worth announcing, because it makes a subscriber harder to reach.
    # An aggregator quietly narrowing a farmer's delivery is the case this notice exists for.
    previous = {
        (b.channel.value, b.address, b.aoi_id, b.min_severity.value, b.min_score)
        for b in subscriber.channels
    }
    subscriber.channels = payload.channels
    saved = await repository.save_subscriber(subscriber)

    # Tell the subscriber their delivery configuration changed — but only when it ACTUALLY did.
    #
    # A no-op save (the UI re-submitting an unchanged form) must not send mail: an email saying
    # "your alert settings changed" when nothing changed teaches people to ignore the ones that
    # matter, and this is exactly the notice that matters if somebody else made the change.
    current = {
        (b.channel.value, b.address, b.aoi_id, b.min_severity.value, b.min_score)
        for b in saved.channels
    }
    if current != previous:
        # Built HERE, not inside the task. A `Request` must not outlive its response — the
        # background task runs after the connection is released, so `request.client` may
        # already be gone. A plain value is safe to carry across that boundary.
        background.add_task(
            _confirm_channels_changed,
            subscriber_id,
            saved,
            caller.label,
            mailer.request_context(request),
        )

    log.info(
        "channels replaced",
        extra={"subscriber_id": subscriber_id, "channels": len(saved.channels)},
    )
    return saved


async def _confirm_channels_changed(
    subscriber_id: str,
    subscriber: Subscriber,
    audience: str,
    context: mailer.RequestContext | None = None,
) -> None:
    """Email the subscriber that their alert delivery changed. Never raises.

    `audience` is the resolver's label — `account:XYZ` for the subscriber themselves,
    `aggregator:ABC` for a partner. Only the aggregator case names an actor, because "you changed
    your own settings" is noise while "someone else changed where your flood alerts go" is the
    whole point of the message.
    """
    try:
        account = await iam_store.account_for_subscriber(subscriber_id)
        if account is None or not account.email:
            return

        changed_by = None
        if audience.startswith("aggregator:"):
            actor = await iam_store.get_account(audience.split(":", 1)[1])
            changed_by = (actor.organisation or actor.first_name) if actor else "your provider"

        await mailer.send_channels_changed(
            account.email,
            account.first_name,
            channels=[
                {
                    "channel": b.channel.value,
                    "address": b.address,
                    "min_severity": b.min_severity.value,
                    # Passed through so the notice states the dial.
                    "min_score": b.min_score,
                    "area": next(
                        (a.name for a in subscriber.areas if a.id == b.aoi_id), None
                    ),
                }
                for b in subscriber.channels
                if b.enabled
            ],
            changed_by=changed_by,
            context=context,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "channel-change confirmation could not be sent",
            extra={"subscriber_id": subscriber_id, "error": str(exc)},
        )


@router.patch(
    "/{subscriber_id}/active",
    response_model=Subscriber,
    dependencies=[Depends(require_platform_scope(ApiKeyScope.PLATFORM_SUBSCRIBERS))],
)
async def set_active(subscriber_id: str, active: bool) -> Subscriber:
    """Pause or resume alerts without losing the configuration."""
    subscriber = await repository.get_subscriber(subscriber_id)
    if subscriber is None:
        raise HTTPException(status_code=404, detail="Subscriber not found")

    subscriber.active = active
    await repository.save_subscriber(subscriber)
    return subscriber


@router.delete(
    "/{subscriber_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # `response_model=None` is REQUIRED, not tidiness. FastAPI infers the response
    # model from the `-> None` return annotation, and under pydantic 2.12+ that
    # inference yields a truthy model instead of being skipped — which trips
    # `assert is_body_allowed_for_status_code(204)` at import time and makes the
    # whole app fail to start. Stating it explicitly bypasses the inference.
    response_model=None,
    dependencies=[Depends(require_platform_scope(ApiKeyScope.PLATFORM_SUBSCRIBERS))],
)
async def delete_subscriber(subscriber_id: str) -> None:
    if not await repository.delete_subscriber(subscriber_id):
        raise HTTPException(status_code=404, detail="Subscriber not found")


# --------------------------------------------------------------------------- #
# Monitored areas — the full lifecycle
#
# A subscriber could previously add areas at registration and never change them: there was no
# PATCH and no DELETE anywhere in the API, so a mistyped plot name or a wrongly placed pin was
# permanent, and stopping monitoring on one plot meant deleting the whole subscription.
#
# These act on ONE area by id rather than re-sending the subscriber's whole set. That is the
# load-bearing difference: `save_subscriber` replaces areas wholesale, so a caller who resends
# the set with a freshly minted id silently orphans that plot's entire assessment history —
# the timeline resets to empty and nothing reports an error.
# --------------------------------------------------------------------------- #


async def _inherit_attribution(subscriber_id: str, aoi_id: str) -> None:
    """Copy the billing owner from one of this subscriber's existing areas.

    Every area of one subscriber has the same owner by construction — the model has no case
    where two plots of the same farmer are billed to different parties. So the first attributed
    area is authoritative, and a new one joins it.

    Silent when nothing is found: a subscriber with no attributed area predates this feature, and
    `reconcile_attribution` repairs those in bulk rather than blocking an area from being added.
    """
    subscriber = await repository.get_subscriber(subscriber_id)
    if subscriber is None:
        return

    for area in subscriber.areas:
        if area.id == aoi_id:
            continue
        existing = await iam_store.attribution_for(area.id)
        if existing is None:
            continue
        await iam_store.record_attribution(
            aoi_id=aoi_id,
            owner_kind=attribution.OwnerKind(existing["owner_kind"]),
            owner_id=existing["owner_id"],
            subscriber_id=subscriber_id,
            subject_account_id=existing.get("subject_account_id"),
            external_ref=existing.get("external_ref"),
            # The project comes along too. A new plot for a farmer already in the Kano rollout
            # belongs to that rollout — dropping it here would leave the area billed to the right
            # aggregator but under no project, so the per-project totals would quietly under-sum.
            workspace_id=existing.get("workspace_id"),
        )
        return

    log.warning(
        "area added with no attribution to inherit; it will not be billed until reconciled",
        extra={"subscriber_id": subscriber_id, "aoi_id": aoi_id},
    )


class AreaPatch(BaseModel):
    """Fields that may be changed on a monitored area.

    All optional: a PATCH states only what changes. Sending `{"name": "..."}` must not clear
    the crop, which is what a full-object PUT would do to any field the client omitted.

    ## Two classes of edit, and only one is fully safe

    **`name` and `crop` are safe.** The update is in place, the `aoi_id` survives, and every past
    assessment stays attached AND stays meaningful — the ground being described is unchanged.

    **`bbox` and `hectares` change what is being measured.** The history is preserved (those
    readings were true when taken, and discarding them would erase the record of a flood that
    happened) and a re-scan runs immediately so the current reading matches the current shape.
    But older points then describe a different footprint under the same name: a "65% flooded"
    reading from last week may be land the subscriber no longer monitors.

    Supported, because a mis-dropped pin has to be correctable. Not recommended for a genuinely
    different plot — add a new area instead and each keeps its own clean history. The route says
    so in its description, and `geometry_changed` is audited so the discontinuity is traceable.
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    crop: str | None = Field(default=None, max_length=60)
    #: Changing either re-measures different ground. See the class docstring.
    bbox: BBox | None = None
    hectares: float | None = Field(default=None, gt=0, le=250_000)
    #: Who contacts the subscriber about this plot. **A third class of edit**, and the safest of
    #: the three: it changes nothing about the measurement or the history, only the delivery.
    #:
    #: `webhook` means SHELTER dispatches nothing directly and the aggregator's webhook is the
    #: delivery — so it is refused for an area with no aggregator behind it, because that would
    #: silence the subscriber's alerts entirely with nobody to relay them.
    delivery_mode: DeliveryMode | None = None


@router.get("/{subscriber_id}/areas", response_model=list[AreaOfInterest])
async def list_areas(
    subscriber_id: str, caller: Audience = Depends(resolve_audience)
) -> list[AreaOfInterest]:
    """Every area this subscriber has under monitoring.

    Scoped for the same reason as `get_subscriber`, and more urgently: an `AreaOfInterest` carries
    the plot's **exact bounding box**. Anonymous callers were being handed the coordinates of
    identifiable smallholder farms.

    404, not 403 — see `get_subscriber`.
    """
    subscriber = await repository.get_subscriber(subscriber_id)
    if subscriber is None:
        raise HTTPException(status_code=404, detail="Subscriber not found")

    if not caller.may_see(subscriber_id):
        log.warning(
            "cross-tenant area read refused",
            extra={"subscriber_id": subscriber_id, "audience": caller.label},
        )
        raise HTTPException(status_code=404, detail="Subscriber not found")

    return subscriber.areas


@router.post(
    "/{subscriber_id}/areas",
    response_model=AreaOfInterest,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_platform_scope(ApiKeyScope.PLATFORM_SUBSCRIBERS))],
)
async def create_area(
    request: Request,
    subscriber_id: str,
    payload: AreaOfInterest,
    background: BackgroundTasks,
) -> AreaOfInterest:
    """Add an area to an existing subscription, and scan it immediately.

    Scanned on creation for the same reason registration is: someone adding a plot during a
    developing flood should not wait up to six hours to learn they are in it.
    """
    normalised = normalise_area(payload)

    # Fetched rather than trusted from the path: `enqueue_scan` needs the subscriber for the
    # job envelope, and this doubles as the existence check.
    subscriber = await repository.get_subscriber(subscriber_id)
    if subscriber is None:
        raise HTTPException(status_code=404, detail="Subscriber not found")

    try:
        created = await repository.add_area(subscriber_id, normalised)
    except repository.DuplicateAreaError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    if created is None:
        raise HTTPException(status_code=404, detail="Subscriber not found")

    # Attribution is INHERITED from the subscriber's other areas, not re-derived.
    #
    # A subscriber is either an aggregator's customer or a direct individual, and adding a plot
    # does not change which. Looking it up from an existing area is what keeps a new area on the
    # same invoice as the rest — deriving it here from `memberships` would put an aggregator's
    # farmer on a personal subscription the moment the edge was read at a different time.
    #
    # Best-effort: an unattributed area is a billing gap, not a reason to refuse monitoring, and
    # the reconciliation sweep below can repair it.
    await _inherit_attribution(subscriber_id, created.id)

    await pipeline.enqueue_scan(subscriber, created)

    # Confirm it, to the person whose land it is.
    #
    # Reported by an aggregator on the customer path, and this one had the identical gap: an area
    # was created, queued and logged with nobody told, so the only confirmation was the HTTP 201
    # the portal received. Someone who has just asked for 24/7 monitoring of their field should be
    # told it started — and should get one last chance to notice a wrong location before advisories
    # begin arriving about the wrong ground.
    #
    # Backgrounded, and non-fatal: the area is already durable and already queued, so a slow mail
    # provider must not turn a successful creation into an error.
    background.add_task(
        _confirm_area_added, subscriber_id, created, mailer.request_context(request)
    )

    log.info("area added", extra={"subscriber_id": subscriber_id, "aoi_id": created.id})
    return created


async def _confirm_area_added(
    subscriber_id: str,
    area: AreaOfInterest,
    context: mailer.RequestContext | None = None,
) -> None:
    """Email the subscriber that this plot is being watched. Never raises.

    Resolves the account for the name and address rather than reading the subscriber's email
    channel binding, for one reason: a subscriber may have chosen WhatsApp or SMS for *alerts*
    while their account address is where account mail belongs. Confirming an area is account mail —
    it is about the configuration, not about a hazard.

    Silent when there is no account behind the subscription. That is a real state for a subscriber
    created directly through the platform API, and it means "nobody to email" rather than an error.
    """
    try:
        account = await iam_store.account_for_subscriber(subscriber_id)
        if account is None or not account.email:
            return

        await mailer.send_area_added(
            account.email,
            account.first_name,
            area_name=area.name,
            hectares=area.hectares,
            admin1=area.admin1,
            admin2=area.admin2,
            country=area.country,
            context=context,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "area confirmation could not be sent",
            extra={"subscriber_id": subscriber_id, "error": str(exc)},
        )


@router.patch(
    "/{subscriber_id}/areas/{aoi_id}",
    response_model=AreaOfInterest,
    dependencies=[Depends(require_platform_scope(ApiKeyScope.PLATFORM_SUBSCRIBERS))],
)
async def patch_area(
    subscriber_id: str, aoi_id: str, payload: AreaPatch
) -> AreaOfInterest:
    """Rename, move, resize or re-crop one area. The id — and its history — survive.

    ## Renaming is safe; moving is a judgement call

    `name` and `crop` are in-place edits: the plot is the same ground, so its whole assessment
    history remains both attached and meaningful.

    `bbox` or `hectares` re-measure **different ground**. The history is kept and a fresh scan
    runs immediately, but earlier readings then describe the old footprint under the same name.
    For correcting a mis-dropped pin that is what you want; for a genuinely different plot, prefer
    `POST /subscribers/{id}/areas` — there is no limit on areas, and a new one keeps a clean
    history of its own.

    ## Ownership is checked, not assumed

    The area id is in the path and so is the subscriber id, and they must agree. Without that
    check, knowing an AOI id would be enough to edit somebody else's monitored plot — 404
    rather than 403 for a mismatch, so the id space cannot be probed.

    A geometry change re-scans immediately: the previous assessment describes different ground,
    so leaving it as the current reading would show a subscriber a measurement of a plot they
    no longer monitor.
    """
    owned = await repository.get_area(aoi_id)
    if owned is None or owned[0] != subscriber_id:
        raise HTTPException(status_code=404, detail="Area not found")

    moved = payload.bbox is not None
    if moved:
        # Validated before the write, so a refused geometry does not half-apply an edit that
        # also renamed the plot.
        check = geometry.check_monitorable(payload.bbox)
        if not check.ok:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=check.reason
            )

    # `webhook` mode needs an aggregator to relay. Refused otherwise, because it would silence
    # the subscriber's alerts entirely with nobody to deliver them — a setting that looks like a
    # preference and behaves like an off switch.
    if payload.delivery_mode is DeliveryMode.WEBHOOK:
        attribution_row = await iam_store.attribution_for(aoi_id)
        owner_kind = (attribution_row or {}).get("owner_kind")
        if owner_kind != attribution.OwnerKind.AGGREGATOR.value:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Only an aggregator-managed area can be set to `webhook` delivery — there "
                    "would be nobody to relay the alert. Use `direct` or `both`."
                ),
            )

    updated = await repository.update_area(
        aoi_id,
        name=payload.name,
        bbox=payload.bbox,
        crop=payload.crop,
        hectares=payload.hectares,
        delivery_mode=payload.delivery_mode,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Area not found")

    if moved:
        subscriber = await repository.get_subscriber(subscriber_id)
        if subscriber is not None:
            await pipeline.enqueue_scan(subscriber, updated)

    log.info(
        "area updated",
        extra={"subscriber_id": subscriber_id, "aoi_id": aoi_id, "geometry_changed": moved},
    )
    return updated


@router.delete(
    "/{subscriber_id}/areas/{aoi_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # Required alongside a 204 — see `delete_subscriber` above for why.
    response_model=None,
    dependencies=[Depends(require_platform_scope(ApiKeyScope.PLATFORM_SUBSCRIBERS))],
)
async def remove_area(subscriber_id: str, aoi_id: str) -> None:
    """Stop monitoring one area.

    **Past assessments are kept.** An assessment records what a satellite measured on a date;
    removing the area does not make that untrue, and a subscriber who drops a plot after a
    flood season must not thereby erase the record that they were warned. Those rows become
    unreachable from their view — which is what "stop monitoring" means — and remain available
    to an operator investigating a complaint.

    Refuses to remove the last area: a subscription with none is active but watching nowhere,
    which reads as working while delivering nothing. 409 with an explanation, not a silent
    success.
    """
    owned = await repository.get_area(aoi_id)
    if owned is None or owned[0] != subscriber_id:
        raise HTTPException(status_code=404, detail="Area not found")

    try:
        removed = await repository.delete_area(aoi_id)
    except repository.LastAreaError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    if not removed:
        raise HTTPException(status_code=404, detail="Area not found")

    # Closes the billable period; the record is kept so a past invoice stays explainable.
    await iam_store.end_attribution(aoi_id)

    log.info("area removed", extra={"subscriber_id": subscriber_id, "aoi_id": aoi_id})
