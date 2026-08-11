"""Shared validation for an incoming area of interest.

Lives here rather than in `eo/geometry.py` because it raises `HTTPException`, and `geometry` is
imported by the Oracle — the risk layer must not acquire a FastAPI dependency, which is the same
reason `exposure.py` keeps its rasterio imports function-local.

Used by every write path that accepts geometry: activation, the aggregator customer routes, and
the area lifecycle routes. Duplicating it per router is how one path ends up missing the check.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from app.eo import geometry
from app.eo.geometry import GeometryError
from app.models.enums import MVP_CHANNELS
from app.models.schemas import AreaOfInterest

#: The geography SHELTER can actually monitor, as a generous bounding box.
#:
#: Sub-Saharan Africa plus the Sahel and a margin: roughly Cape Verde to the Horn, and the
#: Mediterranean coast to the Cape. Deliberately loose — this is a "you are on the wrong
#: continent" guard, not a border check, and refusing a legitimate area is worse than
#: accepting one 200 km outside the service's focus.
SERVICE_WEST, SERVICE_EAST = -26.0, 52.0
SERVICE_SOUTH, SERVICE_NORTH = -36.0, 28.0


def _within_service_area(area: AreaOfInterest) -> bool:
    """Whether the AOI centroid falls inside the region SHELTER monitors."""
    longitude, latitude = area.bbox.centroid
    return (
        SERVICE_WEST <= longitude <= SERVICE_EAST
        and SERVICE_SOUTH <= latitude <= SERVICE_NORTH
    )


def normalise_area(area: AreaOfInterest) -> AreaOfInterest:
    """Validate and complete an incoming AOI, or 422 with a readable reason.

    Without this a malformed ring reaches the `aoi_outline_within_bbox` CHECK constraint and
    surfaces as a 500 — and a mismatched bbox/ring pair that happens to satisfy the constraint
    produces an all-NaN mask and a silent 0% reading, which the Oracle reads as "no hazard".
    That is the worst failure mode in the product: a flooded plot reported as safe.

    ## The geography check, and the incident that added it

    A farm described as "Alspecs Farms in Kobape, Ogun State" was created and **activated** at
    `-2.58, 53.41` — Warrington, England. Browser geolocation supplied the coordinates (the
    subscriber's network placed them in the UK), place search had returned nothing for "Kobape"
    because OSM has no entry for it, and nothing anywhere asked whether the resolved location was
    on the right continent.

    Everything downstream then behaves plausibly and uselessly: Scout finds Sentinel scenes over
    Cheshire, the Analyst measures them, and the subscriber gets crop-stress readings for a field
    2,000 km from their own. There is no error to notice, which is what makes an unchecked
    location worse than a rejected one.

    Checked on the **centroid**, not the corners: an AOI legitimately straddling a coastline or a
    national border should pass, and a centroid is the one point guaranteed to be inside the area.

    `GeometryError` messages are written for the subscriber, so they are surfaced unchanged
    rather than wrapped.
    """
    try:
        normalised = geometry.normalise_aoi(area)
    except GeometryError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    if not _within_service_area(normalised):
        longitude, latitude = normalised.bbox.centroid
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            (
                f"That location ({latitude:.4f}, {longitude:.4f}) is outside the region SHELTER "
                f"monitors, which is Africa and the Sahel. If you used 'Use my current "
                f"location', your device or network may have reported the wrong position — "
                f"search for the place by name or drop a pin on the map instead."
            ),
        )

    return normalised


def reject_unavailable_channels(channels: list) -> None:
    """422 when a binding names a channel this deployment cannot deliver on.

    ## Why the write path is where this belongs

    Every non-MVP channel already degrades honestly at dispatch: no credentials means
    `Dispatcher.available` is False and the receipt comes back SKIPPED with "credentials not
    configured". Nothing crashes.

    But that happens *hours later*, in a log nobody reads. A subscriber who bound WhatsApp saw the
    save succeed and then received nothing, with no way to discover why — and for a service whose
    whole promise is "we contact you", a channel that accepts an address and silently delivers
    nothing is worse than one that is absent.

    So the refusal moves to the moment of choosing, where the person is still present and can pick
    something that works.

    Lives here beside `normalise_area` for the same reason that does: every write path needs it, and
    duplicating it per router is how one path ends up missing the check.
    """
    unsupported = sorted(
        {b.channel.value for b in channels if b.channel not in MVP_CHANNELS}
    )
    if not unsupported:
        return

    _refuse(unsupported)


def reject_unavailable_channel(channel) -> None:
    """The single-channel form, for the Partner API's `preferred_channel`.

    `POST /partner/customers` and `POST /workspaces/{id}/customers` take one `Channel` rather than a
    list of bindings, and turn it into the customer's only binding. They therefore need the same
    refusal, and they did not have it: an aggregator could onboard a farmer with
    `preferred_channel: "whatsapp"`, receive a 200, and that farmer would never be contacted —
    a silent non-delivery reaching a third party who never chose the channel and cannot see the
    setting. Worse than the same mistake on one's own account.
    """
    if channel is not None and channel not in MVP_CHANNELS:
        _refuse([channel.value])


def _refuse(unsupported: list[str]) -> None:
    """The shared 422, so both forms word it identically."""
    offered = ", ".join(sorted(c.value for c in MVP_CHANNELS))
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        (
            f"{' and '.join(unsupported)} delivery is not available yet. SHELTER currently "
            f"delivers on: {offered}. Those other channels are built but have never delivered a "
            f"real message, so binding one would accept your address and send you nothing."
        ),
    )
