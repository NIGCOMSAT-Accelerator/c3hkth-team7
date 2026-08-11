"""An area must be somewhere SHELTER can actually monitor.

## The incident

A subscriber registering "Alspecs Farms in Kobape, Ogun State, Nigeria" ended up with an area
**activated at -2.58, 53.41 — Warrington, England**, at 2 hectares instead of 140.

Three failures compounded, and none of them raised:

  1. Place search returned nothing. OSM has no entry for "Kobape" at all — verified directly
     against Nominatim, with and without country hints. Its LGA resolves fine.
  2. Browser geolocation supplied coordinates instead, and the subscriber's network placed them
     in the UK.
  3. **Nothing anywhere checked which continent the result was on.**

The third is the one that made it dangerous. Everything downstream then behaves plausibly:
Scout finds Sentinel scenes over Cheshire, the Analyst measures them, and the subscriber
receives crop-stress readings for a field 2,000 km from their own. There is no error to notice.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.area_input import normalise_area
from app.models.schemas import AreaOfInterest, BBox


def _aoi(west: float, south: float, east: float, north: float) -> AreaOfInterest:
    return AreaOfInterest(
        id="aoi_test",
        name="test plot",
        bbox=BBox(west=west, south=south, east=east, north=north),
        country="NG",
    )


@pytest.mark.parametrize(
    ("label", "bounds"),
    [
        # The exact coordinates from the incident.
        ("Warrington, England", (-2.5828, 53.4090, -2.5807, 53.4103)),
        ("Mumbai, India", (72.85, 19.05, 72.90, 19.10)),
        ("São Paulo, Brazil", (-46.65, -23.56, -46.62, -23.53)),
        ("null island drift", (0.0, 40.0, 0.01, 40.01)),
    ],
)
def test_areas_outside_africa_are_refused(label, bounds):
    """**The guard the incident needed.** A wrong continent must fail loudly, not silently."""
    with pytest.raises(HTTPException) as raised:
        normalise_area(_aoi(*bounds))

    assert raised.value.status_code == 422
    detail = str(raised.value.detail)
    assert "outside the region SHELTER monitors" in detail, f"{label}: unhelpful message"
    # The message must name the offending coordinates and the likely cause, because the
    # subscriber did not type them — a device did.
    assert "current location" in detail, (
        "the message should point at geolocation, which is how a wrong position usually arrives"
    )


@pytest.mark.parametrize(
    ("label", "bounds"),
    [
        # The plot from the incident, at its REAL location.
        ("Kobape, Ogun State", (3.4650, 7.1450, 3.4780, 7.1560)),
        ("Kano, Nigeria", (8.46, 11.92, 8.56, 12.02)),
        ("Yenagoa, Niger Delta", (6.28, 4.88, 6.38, 4.98)),
        # Not Nigeria: the service covers Africa and the Sahel, and the guard must not
        # quietly become a Nigeria-only check.
        ("Nairobi, Kenya", (36.80, -1.30, 36.85, -1.26)),
        ("Dakar, Senegal", (-17.48, 14.68, -17.44, 14.72)),
        ("Cape Town, South Africa", (18.40, -33.94, 18.44, -33.90)),
    ],
)
def test_areas_inside_the_service_region_are_accepted(label, bounds):
    """A legitimate area anywhere in the covered region must pass.

    Over-tightening this is the failure mode that matters more than the leak it prevents: a
    farmer refused at signup has no workaround, while an area 200 km outside our focus is
    merely unhelpful.
    """
    result = normalise_area(_aoi(*bounds))

    assert result.hectares and result.hectares > 0, f"{label}: no area computed"


def test_the_check_is_on_the_centroid_not_the_corners():
    """An area straddling a coastline or a border must not be refused.

    The centroid is the one point guaranteed to be inside the area, so it is the correct thing
    to test. A corners-based check would reject a legitimate coastal plot in Lagos.
    """
    from app.api.area_input import _within_service_area

    # Straddling the prime meridian at the Ghana/Togo latitude — corners on both sides.
    straddling = _aoi(-0.05, 6.10, 0.05, 6.20)
    assert _within_service_area(straddling)


def test_the_service_bounds_are_generous_rather_than_a_border_check():
    """This is a "wrong continent" guard, not a customs post.

    Pinning the intent: the box must comfortably contain the whole continent, because a tight
    fit would refuse legitimate areas at the edges — and the cost of a false refusal at signup
    is far higher than the cost of accepting an area slightly outside our focus.
    """
    from app.api.area_input import (
        SERVICE_EAST,
        SERVICE_NORTH,
        SERVICE_SOUTH,
        SERVICE_WEST,
    )

    # Extremes of the SERVED region, which is Nigeria and Sub-Saharan Africa plus the Sahel —
    # not the whole continent. The northern edge sits at 28N by design: SHELTER's hazard model
    # is built on West African monsoon rainfall, Sahelian crop calendars and malaria endemicity,
    # none of which describes Tunis or Cairo. Including the Maghreb would let someone register
    # an area the pipeline would assess with inapplicable assumptions and no warning.
    for label, lon, lat in (
        ("Cape Verde", -24.0, 15.0),
        ("Horn of Africa", 51.0, 11.5),
        ("Cape Agulhas", 20.0, -34.8),
        ("Sahel, northern Niger", 8.0, 20.0),
    ):
        assert SERVICE_WEST <= lon <= SERVICE_EAST, f"{label} longitude excluded"
        assert SERVICE_SOUTH <= lat <= SERVICE_NORTH, f"{label} latitude excluded"

    # And the deliberate exclusions, so a future widening is a conscious edit rather than drift.
    assert SERVICE_NORTH < 36.8, (
        "the northern bound now includes the Mediterranean coast; SHELTER's rainfall, crop and "
        "malaria models do not apply there, so admitting it would produce confident nonsense"
    )
