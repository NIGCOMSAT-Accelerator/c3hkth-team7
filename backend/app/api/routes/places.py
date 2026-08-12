"""Place search and AOI geometry helpers — `/shelter/v1/api/places/*`.

Nine endpoints in service of one thing: **nobody should need to know what a bounding box is**
to start monitoring a field — not a farmer on a phone, not a partner writing an importer.

| Endpoint | Answers |
|---|---|
| `POST /places/resolve` | **"Argungu, 5 hectares" → a submittable area.** The one to build against |
| `GET /places/suggest` | "…as I type" — prefix matches for an address box |
| `GET /places/search` | "where is Argungu, and what shape is it?" — a name to coordinates **and its outline** |
| `GET /places/reverse` | "what is here?" — a pin to country/state/LGA |
| `POST /places/preview` | "is this area monitorable, and how big is it?" |
| `GET /places/admin/{states,lgas,wards,extent}` | "browse to it instead" — for when a name finds nothing |

## Address- and building-level precision, and the floor that bounds it

`search` returns the feature's own `ring`, so "Wuse Market, Abuja" resolves the market's real
perimeter rather than a rectangle around it. That outline is **confirmation**: a rectangle looks
identical whether we found the right market or one a kilometre away sharing its name.

It is not always a monitoring footprint, and the reason is physical. Sentinel reads 10 m pixels, so
a building is ~14 of them — measured, Kano Central Mosque is a 17-vertex ring of **0.1473 ha**
against `MIN_AOI_HECTARES = 0.5`. Below ~50 pixels a "flooded fraction" is edge noise.

So the answer is neither to fake the precision nor to refuse the address: `monitoring` carries the
exact location **and** the resolution separately, in a sentence written for an end user. The same
applies at the top end — Kano State's boundary is 2,035,580 ha, well over the 250,000 ha ceiling,
and comes back as a viewport rather than an area.

## `suggest` and `search` are different engines on purpose

Nominatim is full-text: it resolves "Argungu, Kebbi" well, the prefix "Argun" poorly, and its usage
policy is one request per second — which forbids a request per keystroke on arithmetic alone. So
type-ahead is a **self-hosted Photon** prefix index, and `suggest` reports `available: false` when
none is deployed rather than returning an empty list that reads as "no such place".

Suggestions carry **no geometry**, deliberately: a half-typed string must not be able to become a
monitored plot. Choosing one leads back to `search`, which is where the outline comes from.

## Why `preview` exists, and why it is the one that matters most

Registration is the only place a subscriber can get an AOI *wrong* in a way that produces
plausible-looking but useless intelligence — an area too small to measure, too large to
locate anything in, or an outline that crosses itself. Discovering that from a 422 after
submitting a form is a bad experience; discovering it while still drawing is a good one.

So `preview` is the same validation the write path runs, exposed as a read. The UI calls it
as the shape changes and shows the area and any problem inline. A partner's bulk importer
can call it per row before committing a batch, which turns "37 of 400 rows failed" into a
pre-flight report.

## Gated by `X-SHELTER-API-Key`

An earlier version of this module was public, on the reasoning that the signup form needed
these before an account existed. **That reasoning was wrong.** The portal calls them through
Next.js Server Actions, and `frontend/lib/api.ts` already attaches the platform
service-account key to every request — so gating them costs the signup flow nothing.

Gating is right for three reasons that have nothing to do with the data being secret:

  * **These consume a rate-limited third party on our behalf.** `search` and `reverse` proxy
    Nominatim under a one-request-per-second policy enforced process-wide. An open endpoint
    in front of that is one anyone can exhaust, and the consequence lands on every
    subscriber — a block earned by an abuser degrades place search for real farmers.
  * **Consumption must be attributable.** "Which aggregator resolved 40,000 areas last month"
    is a question the platform has to be able to answer, for capacity and for commercial
    terms. An unauthenticated endpoint produces no such record.
  * **Authorised use should be legally scoped.** A partner consuming the API programmatically
    does so under agreed terms; a credential is what ties a request to that agreement.

`current_key_holder` accepts an aggregator key **or** a platform service key, because both
audiences use these for the same purpose — a partner's importer and the portal both turn a
village name into an area. It does not require a specific scope: there is nothing
tenant-owned to protect in "where is Argungu?", so the gate is for attribution and rate
control rather than authorisation, and it says so rather than implying a permission model it
does not have.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.config import settings
from app.eo import admin, geometry, human, places, suggest
from app.eo.geometry import GeometryError
from app.iam.deps import KeyHolder, current_key_holder
from app.logging_config import get_logger
from app.models.schemas import BBox
from app.store import cache

log = get_logger(__name__)
router = APIRouter(prefix="/places", tags=["places"])


class MonitoringNote(BaseModel):
    """Whether a resolved outline is monitorable as-is, and what to tell the user.

    Mirrors `eo/human.MonitoringNote` onto the wire. Separate from the dataclass for the usual
    reason: the dataclass is the internal contract and this is the published one, so an internal
    field cannot leak into the API by being added to a shared type.
    """

    outline_is_monitorable: bool
    monitored_hectares: float
    note: str
    #: True when the monitored area is larger than what the user outlined — a building, mostly.
    enlarged: bool

class PlaceResult(BaseModel):
    """One candidate place."""

    label: str
    lat: float
    lon: float
    #: `[west, south, east, north]` when upstream provided one. Lets a client frame the
    #: result at a sensible zoom — a state and a village need very different viewports.
    bbox: list[float] | None = None
    country: str | None = None
    admin1: str | None = None
    admin2: str | None = None
    kind: str | None = None

    #: **The feature's real outline**, as a closed `[[lon, lat], …]` ring.
    #:
    #: The market's perimeter, the mosque's walls, the LGA's boundary. Draw this to show the
    #: user *which* place was found — a rectangle over their district proves nothing, whereas
    #: their own compound outlined on the map is unambiguous confirmation.
    #:
    #: **Null is normal, not an error.** Streets are lines and most villages are single nodes,
    #: so neither has an outline; `bbox` still frames them. Measured over Nigeria: markets,
    #: named buildings and administrative areas have rings; roads and settlement nodes do not.
    #:
    #: This is for **display and confirmation**, not a monitoring footprint. Submitting it
    #: verbatim is legitimate only when `monitoring.outline_is_monitorable` is true — a building
    #: footprint is below the measurement floor and a state boundary is above the ceiling, and
    #: the write path refuses both.
    ring: list[list[float]] | None = None
    #: True area of `ring` in hectares. Null when there is no ring.
    ring_hectares: float | None = None
    #: What the pipeline can actually do with `ring`, in a sentence fit to display verbatim.
    #:
    #: Present whenever a ring is. This is the field that answers "can you monitor my shop?"
    #: honestly: it reports the exact location *and* the resolution separately, rather than
    #: rejecting an address for being precise. See `human.MonitoringNote`.
    monitoring: MonitoringNote | None = None




class PlaceSearchResponse(BaseModel):
    results: list[PlaceResult]
    #: ODbL licence condition. Clients MUST display this wherever results appear; it is
    #: returned in the payload rather than documented only, so a client cannot omit it by
    #: not having read the docs.
    attribution: str


@router.get("/search", response_model=PlaceSearchResponse)
async def search_places(
    q: str = Query(min_length=3, max_length=120, description="Place name to find"),
    country: str | None = Query(
        default="ng",
        max_length=2,
        description=(
            "ISO-3166 alpha-2 bias. Defaults to Nigeria — 'Kano' exists in several "
            "countries, and a Nigerian subscriber offered a Japanese result concludes the "
            "search is broken. Pass an empty string to search globally."
        ),
    ),
    limit: int = Query(default=6, ge=1, le=20),
    holder: KeyHolder = Depends(current_key_holder),
) -> PlaceSearchResponse:
    """Find a place by name.

    Returns an empty list rather than an error when the geocoder is unreachable: a
    subscriber who cannot search can still drop a pin or use GPS, and those are the more
    accurate paths anyway. A 503 here would make a degraded convenience look like a broken
    signup.
    """
    found = await places.search(q, limit=limit, country=country or None)
    return PlaceSearchResponse(
        results=[_to_result(p) for p in found],
        attribution=places.ATTRIBUTION,
    )


def _to_result(place: places.Place) -> PlaceResult:
    """`Place` → wire model, attaching the monitoring verdict when there is an outline.

    Computed on read rather than stored on `Place`, for the same reason `Alert.tracks` is
    derived in `alerts._attach_tracks`: the verdict depends on `MIN_AOI_HECTARES` and
    `MAX_AOI_HECTARES`, and a cached `Place` carrying a note computed under an older floor would
    tell a subscriber something the write path no longer agrees with. `eo/places` caches
    upstream payloads for a week, so that is a real window, not a theoretical one.
    """
    fields = vars(place)
    note = None
    if place.ring is not None:
        verdict = human.monitoring_note(
            place.ring_hectares,
            # The place's own name, so the sentence reads "We located Wuse Market exactly"
            # rather than "that outline". `label` is a full display name, so take the leading
            # component — the rest is administrative context the user can already see.
            label=place.label.split(",")[0].strip() or "that outline",
        )
        note = MonitoringNote(**vars(verdict))
    return PlaceResult(**fields, monitoring=note)


# --------------------------------------------------------------------------- #
# Type-ahead
# --------------------------------------------------------------------------- #


class SuggestionResult(BaseModel):
    """One type-ahead row.

    Carries **no geometry** — deliberately. A suggestion is a label plus enough coordinates to
    frame a map, and nothing here is sufficient to build an AOI. Selecting one is expected to
    call `GET /places/search` with the label, which is where the outline and the administrative
    hierarchy come from.

    That separation is a safety property, not tidiness: if this response could produce an AOI, a
    half-typed string could too.
    """

    #: The name the eye lands on — "Argungu".
    label: str
    #: The context that disambiguates it — "Kebbi, Nigeria". Nigeria has a Kajola in several
    #: states, and one combined string makes them look identical at a glance.
    detail: str
    lat: float
    lon: float
    kind: str | None = None
    country: str | None = None


class SuggestResponse(BaseModel):
    results: list[SuggestionResult]
    #: False when no Photon instance is configured. **Check this before showing "no matches"** —
    #: an empty list means the same thing either way, and telling a user their query found
    #: nothing because the feature is switched off is a lie. When false, fall back to the
    #: debounced `GET /places/search`.
    available: bool
    attribution: str


@router.get("/suggest", response_model=SuggestResponse)
async def suggest_places(
    q: str = Query(min_length=2, max_length=120, description="Partially typed place name"),
    limit: int = Query(default=8, ge=1, le=10),
    holder: KeyHolder = Depends(current_key_holder),
) -> SuggestResponse:
    """Prefix suggestions for a partially typed place — the search-as-you-type surface.

    Separate from `GET /places/search` because the two answer different questions with different
    engines. Nominatim is full-text: it resolves "Argungu, Kebbi" well and the prefix "Argun"
    poorly, and its one-request-per-second policy forbids a request per keystroke outright. This
    route queries a **self-hosted Photon** prefix index, which is built for exactly this.

    `min_length=2` against search's 3: a prefix index can rank "Ka" usefully where full-text
    matching cannot, and arriving before the user finishes typing is the entire point.

    ## Rate limited per credential, and why that is not optional here

    A per-keystroke endpoint is a far cheaper enumeration surface than a 500 ms-debounced one:
    an attacker with a valid key could walk the alphabet and harvest a substantial slice of
    Nigeria's address graph, which is a licence and reputational problem even though the
    underlying data is public. The ceiling is generous — real typing produces a few hundred
    requests an hour at most, and it is per *credential* rather than per area, because the
    resource being protected is our own Photon instance.

    Fails **open** on an unreachable cache, consistent with every other limiter here: refusing
    to help someone find their farm because a Redis key is missing is the worse failure.
    """
    limit_per_hour = settings.place_suggest_rate_limit_per_hour
    if limit_per_hour > 0:
        # `holder.label` — "aggregator:<id>" or "platform:service-account". The identity the
        # audit trail already uses, and deliberately not the key or a hash of it: this value
        # lands in a cache key that is visible to anyone with datastore access, and a credential
        # must not be reconstructible from monitoring data.
        #
        # One consequence to know: every portal request shares "platform:service-account", so
        # the portal's own typing is limited **in aggregate** across all subscribers, while each
        # aggregator gets its own bucket. That is the right shape — the portal is one client of
        # our Photon instance and the ceiling protects the instance — but it means the limit must
        # be generous enough for concurrent signups, which is why the default is high.
        used = await cache.incr(cache.key("place-suggest", holder.label), 3_600)
        if used > limit_per_hour:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"This credential has made {limit_per_hour} suggestion requests this hour, "
                f"which is far above interactive typing. Use `GET /places/search` for "
                f"programmatic resolution — it is cached and intended for bulk use.",
                headers={"Retry-After": "3600"},
            )

    found = await suggest.suggest(q, limit=limit)
    return SuggestResponse(
        results=[SuggestionResult(**s.as_dict()) for s in found],
        available=suggest.available(),
        attribution=suggest.ATTRIBUTION,
    )


class AdminAreasResponse(BaseModel):
    """State or LGA names for a cascading picker."""

    #: Alphabetical. Empty when the upstream is unreachable — the caller falls back to search.
    names: list[str]
    attribution: str = "GRID3 Nigeria / geoBoundaries"


class AdminExtentResponse(BaseModel):
    """One LGA's bounding box, as a map starting position."""

    bbox: BBox | None
    #: Centre of that box, so a client can place a pin without doing the arithmetic.
    centroid_lon: float | None = None
    centroid_lat: float | None = None
    #: **Read this before submitting.** An LGA is tens of kilometres across; it is where to put
    #: the map, never a monitoring footprint. See the field docs on `lga_extent`.
    is_monitorable_area: bool = False
    note: str = ""
    attribution: str = "GRID3 Nigeria"


@router.get("/admin/states", response_model=AdminAreasResponse)
async def list_admin_states(
    holder: KeyHolder = Depends(current_key_holder),
) -> AdminAreasResponse:
    """Every Nigerian state plus the FCT.

    ## Why this endpoint exists

    A subscriber tried to register a farm in **Kobape, Ogun State** and place search returned
    nothing. Verified against Nominatim directly, with and without country hints: OSM has no
    entry for Kobape at all. Its LGA resolves, and GRID3 places the coordinates correctly — the
    platform could always locate the area, there was simply no way to *ask* for it by
    administrative name.

    That is the common rural case rather than an edge case. OSM's Nigerian coverage is good for
    cities and thin for villages, and most subscribers are not in cities. So "search for your
    village" needs a companion path: pick the state, pick the LGA, then refine with a pin.

    Cached for a day upstream. An LGA list changes when Nigeria creates an LGA.
    """
    return AdminAreasResponse(names=await admin.list_states())


@router.get("/admin/lgas", response_model=AdminAreasResponse)
async def list_admin_lgas(
    state: str = Query(min_length=2, max_length=60, description="State name from /admin/states"),
    holder: KeyHolder = Depends(current_key_holder),
) -> AdminAreasResponse:
    """Every LGA in one state, alphabetically.

    An unknown state returns an empty list rather than a 404: the picker's job is to offer the
    next choice, and a 404 mid-cascade reads as a broken form rather than a typo.
    """
    return AdminAreasResponse(names=await admin.list_lgas(state))


@router.get("/admin/wards", response_model=AdminAreasResponse)
async def list_admin_wards(
    state: str = Query(min_length=2, max_length=60),
    lga: str = Query(min_length=2, max_length=80),
    holder: KeyHolder = Depends(current_key_holder),
) -> AdminAreasResponse:
    """Wards in one LGA — the third and most useful tier of the cascade.

    ## Why a ward step earns its place

    Measured on the reported case. Obafemi Owode LGA is 58 x 63 km (3,671 km squared) and the
    farm sat **22.4 km from the LGA centre**, so centring there left the plot off-screen. Kajola
    ward, which contains it, is 18 x 16 km — a **12.9x smaller** search area, 5.9 km from centre.
    That is the difference between recognising your own land and panning around guessing.

    ## An empty list is a normal answer

    GRID3's ward layer covers **24 of 37 states** (5,872 wards). Verified absent: Lagos, Rivers,
    FCT, Anambra, Edo, Ondo, Ekiti, Imo, Benue, Plateau, Taraba, Akwa Ibom, Cross River, Ebonyi.
    And geoBoundaries — the broader-Africa fallback — publishes only ADM1 and ADM2 for Nigeria;
    ADM3 returns 404, so there is no ward source to fall back to.

    So a client must treat empty as "skip this step and go to the pin", never as an error. The
    alternative would make the picker unusable in Lagos, which is the opposite of the problem the
    cascade exists to solve.
    """
    return AdminAreasResponse(names=await admin.list_wards(state, lga))


@router.get("/admin/extent", response_model=AdminExtentResponse)
async def admin_extent(
    state: str = Query(min_length=2, max_length=60),
    lga: str = Query(min_length=2, max_length=80),
    ward: str | None = Query(
        default=None,
        max_length=80,
        description=(
            "Optional, and much more useful when available. A ward narrows the search area "
            "~13x against its LGA — measured: Kajola ward is 18x16 km inside Obafemi Owode's "
            "58x63 km. Omit it, or pass nothing, for the LGA extent."
        ),
    ),
    holder: KeyHolder = Depends(current_key_holder),
) -> AdminExtentResponse:
    """Where to point the map, at the finest administrative tier available.

    **This is never a monitoring area, and the response says so.** Measured: Obafemi Owode LGA
    spans about 58 x 63 km and Kajola ward 18 x 16 km — both far larger than any farm.
    Submitting either as an AOI would average a whole district into one reading, and
    `POST /risk/assess` rejects anything over ~4 deg squared outright.

    So `is_monitorable_area` is always False and `note` names the next step. The honest shape for
    this endpoint is "here is where to look"; letting a client mistake it for an area is exactly
    how a plausible-but-useless assessment gets produced.

    Falls back to the LGA when a ward is given but has no boundary — 13 states have no ward
    coverage at all, so a missing ward is an expected state rather than a caller error.
    """
    tier = "LGA"
    bounds = None
    if ward:
        bounds = await admin.ward_extent(state, lga, ward)
        if bounds is not None:
            tier = "ward"

    if bounds is None:
        bounds = await admin.lga_extent(state, lga)

    if bounds is None:
        return AdminExtentResponse(
            bbox=None,
            note=f"No boundary found for {lga}, {state}. Check the name against /admin/lgas.",
        )

    west, south, east, north = bounds
    label = f"{ward} ward" if tier == "ward" else f"the whole of {lga} LGA"
    return AdminExtentResponse(
        bbox=BBox(west=west, south=south, east=east, north=north),
        centroid_lon=(west + east) / 2,
        centroid_lat=(south + north) / 2,
        is_monitorable_area=False,
        note=(
            f"This is {label} — roughly "
            f"{abs(east - west) * 111:.0f} x {abs(north - south) * 111:.0f} km. Use it to "
            f"position the map, then drop a pin on the plot itself or outline it."
        ),
    )


class ReverseResponse(BaseModel):
    place: PlaceResult | None
    attribution: str


@router.get("/reverse", response_model=ReverseResponse)
async def reverse_place(
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    holder: KeyHolder = Depends(current_key_holder),
) -> ReverseResponse:
    """Identify a dropped pin: country, state and LGA.

    This is what lets a partner post `{lat, lon}` and get `admin1`/`admin2`/`country`
    filled without knowing Nigerian administrative structure. Those fields are not
    cosmetic — they are what an operator filters by when one flood crosses several LGAs.

    `place: null` when nothing is known about the point (open ocean, or the geocoder is
    down). The AOI is fully monitorable without them, so this must never block registration.
    """
    found = await places.reverse(lat, lon)
    return ReverseResponse(
        place=PlaceResult(**vars(found)) if found else None,
        attribution=places.ATTRIBUTION,
    )


class AoiPreviewRequest(BaseModel):
    """An area to check before committing it.

    Exactly one of the two shapes:

      * `ring` — the drawn outline, `[[lon, lat], ...]`
      * `lat`/`lon`/`radius_km` — a pin and a radius

    The pin form is offered because it is what a farmer on a phone can actually produce,
    and it is fully supported end to end: the resulting square's envelope *is* its geometry,
    so masking is a no-op and nothing is lost.
    """

    ring: list[list[float]] | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    radius_km: float | None = Field(
        default=None,
        gt=0,
        le=50,
        description="Half-width of the square around the pin. Capped at 50 km.",
    )


class AoiPreviewResponse(BaseModel):
    """What the pipeline will actually monitor, and whether it can."""

    monitorable: bool
    #: Present when `monitorable` is false. Written for the subscriber, not the developer —
    #: the UI shows it verbatim.
    reason: str | None = None

    #: The envelope. What STAC search and the COG window use.
    bbox: BBox | None = None
    #: The normalised ring — closed, counter-clockwise, as it will be stored. Returned so a
    #: client can render exactly what was accepted rather than what it sent.
    ring: list[list[float]] | None = None

    #: True area of the shape. What a subscriber recognises as their farm size.
    hectares: float | None = None
    #: Area of the envelope. Equal to `hectares` for a pin AOI.
    envelope_hectares: float | None = None
    #: `envelope_hectares / hectares`. Above ~1.5 the UI should say that masking is doing
    #: real work, because that is the case where drawing the outline changed the answer.
    envelope_ratio: float | None = None
    #: Roughly how many Sentinel 10 m pixels fall inside. Under a few hundred, a "fraction"
    #: is dominated by edge effects — which is what `MIN_AOI_HECTARES` encodes.
    approx_pixels: int | None = None


@router.post("/preview", response_model=AoiPreviewResponse)
async def preview_aoi(
    payload: AoiPreviewRequest,
    holder: KeyHolder = Depends(current_key_holder),
) -> AoiPreviewResponse:
    """Validate and measure an area without saving it.

    Runs the *same* checks as the write path, so a preview that passes cannot be refused at
    registration — a preview with looser rules would be worse than none, because it would
    teach the user their shape is fine and then reject it.

    Returns `monitorable: false` with a readable reason rather than a 4xx for geometry
    problems, because those are an expected part of drawing rather than a client error, and
    a 422 mid-draw is noise. Genuine input errors — neither shape supplied — are still 422.
    """
    if payload.ring:
        try:
            ring = geometry.validate_ring(payload.ring)
        except GeometryError as exc:
            return AoiPreviewResponse(monitorable=False, reason=str(exc))

        bbox = geometry.polygon_bbox(ring)
        hectares = geometry.polygon_area_hectares(ring)

    elif payload.lat is not None and payload.lon is not None:
        radius = payload.radius_km or 2.0
        bbox = _square_around(payload.lat, payload.lon, radius)
        ring = None
        hectares = geometry.area_hectares(bbox)

    else:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Send either `ring` (a drawn outline) or `lat`/`lon` with an optional "
            "`radius_km`.",
        )

    envelope = geometry.area_hectares(bbox)

    try:
        geometry.check_monitorable(hectares)
    except GeometryError as exc:
        return AoiPreviewResponse(
            monitorable=False,
            reason=str(exc),
            bbox=bbox,
            ring=ring,
            hectares=round(hectares, 2),
            envelope_hectares=round(envelope, 2),
        )

    return AoiPreviewResponse(
        monitorable=True,
        bbox=bbox,
        ring=ring,
        hectares=round(hectares, 2),
        envelope_hectares=round(envelope, 2),
        envelope_ratio=round(envelope / hectares, 2) if hectares > 0 else None,
        # One hectare is 10,000 m²; a Sentinel-2 pixel is 100 m². Reported so the size
        # limits read as physics rather than as an arbitrary policy.
        approx_pixels=int(hectares * 100),
    )


def _square_around(lat: float, lon: float, radius_km: float) -> BBox:
    """A square bbox centred on a pin.

    The cosine-latitude correction on longitude is not optional: without it a 2 km radius at
    12°N would be ~2% too wide, and at higher latitudes badly wrong — the AOI would not be
    the square the subscriber was shown.

    Clamped to valid ranges so a pin near a pole or the antimeridian produces a legal box
    rather than a validation error the subscriber cannot interpret.
    """
    import math

    lat_delta = radius_km / (geometry.M_PER_DEG_LAT / 1000.0)
    lon_delta = radius_km / (
        (geometry.M_PER_DEG_LON_EQUATOR / 1000.0) * max(math.cos(math.radians(lat)), 0.01)
    )

    return BBox(
        west=max(-180.0, lon - lon_delta),
        south=max(-90.0, lat - lat_delta),
        east=min(180.0, lon + lon_delta),
        north=min(90.0, lat + lat_delta),
    )


# --------------------------------------------------------------------------- #
# Plain-language resolution
#
# The endpoint that means neither a subscriber nor a partner integration ever has to know
# what a bounding box is.
# --------------------------------------------------------------------------- #


class ResolveRequest(BaseModel):
    """A place and a size, described the way a person would describe them.

    ## Three ways to say where, in order of accuracy

      * `lat`/`lon` — the phone's own GPS, or a tapped map pin. Most accurate.
      * `place` — "Argungu", "Dikko, Kebbi". Geocoded. Accurate to the settlement.
      * neither — 422. There is no sensible default for "where".

    A partner importing a spreadsheet almost always has a place name and a size in a column,
    which is exactly this shape. A UI has GPS or a pin plus a typed size, also this shape.
    """

    #: Where, as a name. Ignored when `lat`/`lon` are given, since coordinates are strictly
    #: better and silently preferring the vaguer input would be surprising.
    place: str | None = Field(default=None, max_length=160)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)

    #: How big, in words: "5 hectares", "12 acres", "2 plots", "medium", or blank.
    #:
    #: Never a validation failure. An unrecognised string resolves to a documented default
    #: and comes back flagged `size_is_estimate`, because the response includes a map-ready
    #: area the user can check — whereas a 422 for "about five and a bit" is a dead end for
    #: someone who answered honestly.
    size: str | None = Field(default=None, max_length=60)

    #: What to call it. Defaults to the resolved place name, so a partner sending only a
    #: place and a size still gets a sensible label in every alert.
    name: str | None = Field(default=None, max_length=120)

    country: str | None = Field(
        default="ng", max_length=2, description="ISO-3166 bias for the place lookup."
    )


class ResolvedArea(BaseModel):
    """A ready-to-submit area, plus everything needed to confirm it in plain language.

    The `area` object can be posted verbatim to `POST /iam/activate` or included as the
    `area` field of `POST /iam/customers`. That is the point: one call turns words into a
    monitorable pipeline.
    """

    #: Post this straight back. Already validated and normalised — a preview that resolves
    #: cannot be refused on submission.
    area: dict

    # ---- confirmation, for a human ---- #

    #: "Dikko, Argungu, Kebbi State, Nigeria" — what we think they meant.
    resolved_place: str | None = None
    #: "about 7 football pitches". The check a farmer can actually perform: they cannot verify
    #: "5 hectares", but they can see whether the shape on the map looks like seven pitches.
    size_description: str
    hectares: float
    #: True when the size was guessed rather than stated. The UI should invite a correction.
    size_is_estimate: bool

    #: Filled from the coordinates, so a partner need not know Nigerian administrative
    #: structure to populate them.
    country: str | None = None
    admin1: str | None = None
    admin2: str | None = None

    #: How often the pipeline will look, in plain words. Included because "is it working?"
    #: is the first question after setup, and the honest answer is "every few days" rather
    #: than "continuously" — Sentinel-1 revisits every ~6 days and Sentinel-2 every ~5.
    monitoring_cadence: str

    #: The resolved feature's **own outline**, when the place had one — a closed `[[lon, lat], …]`
    #: ring. The market's perimeter, the compound's walls.
    #:
    #: **Informational, and not what `area` contains.** `area.bbox` is still the square derived from
    #: the stated size, because the caller told us how big the plot is and not what shape it is —
    #: inventing a shape would be fabricating data. This field exists so a partner can *confirm* we
    #: found the right feature: a square around a coordinate looks identical whether we resolved
    #: their customer's market or one a kilometre away sharing its name.
    #:
    #: Null is the common case. Streets are lines and most Nigerian villages are single nodes.
    place_ring: list[list[float]] | None = None
    #: True area of `place_ring` in hectares. Null when there is no ring.
    place_ring_hectares: float | None = None
    #: What the pipeline could do with `place_ring` if it were submitted as the geometry.
    #:
    #: Present whenever a ring is. Reports the measurement floor honestly rather than as a refusal:
    #: a building footprint is a fraction of a hectare against a 0.5 ha floor, so the answer is
    #: "located exactly, monitored slightly wider" — see `human.MonitoringNote`.
    place_monitoring: MonitoringNote | None = None

    attribution: str


@router.post("/resolve", response_model=ResolvedArea)
async def resolve_area(
    payload: ResolveRequest,
    holder: KeyHolder = Depends(current_key_holder),
) -> ResolvedArea:
    """Turn a place and a size in words into a validated, submittable area.

    A thin wrapper over `resolve(...)`, which is also what the partner route
    `POST /iam/customers/{account_id}/areas/resolve` calls. **One implementation, deliberately:**
    two would let the portal and an aggregator resolve the same address to different geometry, and
    nothing downstream could detect that they disagreed.

    ## Why this endpoint is the one to build a UI and an importer against

    Every other way in requires the caller to construct a bbox from a centre and a radius,
    with a cosine-latitude correction, in the right coordinate order. That is a reasonable
    ask of a geospatial engineer and an unreasonable one of everybody else — and getting it
    subtly wrong produces a monitored area that is not the field, which nothing downstream
    can detect.

    So: `{"place": "Argungu", "size": "5 hectares"}` in, a complete area out.

    ## The resulting area is a square, deliberately

    A square centred on the point, sized to the stated hectares. Not a polygon, because the
    caller has not told us the field's shape and inventing one would be fabricating data —
    the same rule the advisory generator follows about never adding a number it was not given.

    A subscriber who wants the true outline can draw it (the Web UI offers this) or a partner
    can send `geometry` directly. Both then get a masked reading over the actual ring, with
    pixels outside it excluded from the denominator rather than counted as unaffected — so a
    flooded riverside strip reads at its true fraction instead of being diluted by the
    surrounding box. This endpoint is the floor, not the ceiling.
    """
    return await resolve(payload, holder_label=holder.label, is_platform=holder.is_platform)


async def resolve(
    payload: ResolveRequest, *, holder_label: str, is_platform: bool
) -> ResolvedArea:
    """The shared implementation. Raises `HTTPException` exactly as the route does.

    Split out so the partner API can offer address resolution without duplicating any of the
    geocoding, size parsing, clamping or GRID3 admin enrichment below — see the note on the route.
    """
    # ---- where ---- #
    place = None
    # Which path resolved the place. Load-bearing further down: only a forward geocode identifies
    # the FEATURE the caller named — a reverse lookup returns the enclosing administrative area.
    by_place = payload.lat is None or payload.lon is None
    if payload.lat is not None and payload.lon is not None:
        lat, lon = payload.lat, payload.lon
        # Reverse-geocode for the label and admin fields. Best-effort: an unnamed point is
        # still perfectly monitorable, so this must never be the reason a setup fails.
        place = await places.reverse(lat, lon)
    elif payload.place:
        found = await places.search(payload.place, limit=1, country=payload.country or None)
        if not found:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"We could not find “{payload.place}”. Try a nearby town or district name, "
                f"or use your phone's location instead.",
            )
        place = found[0]
        lat, lon = place.lat, place.lon
    else:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Tell us where: send `place` (a town or district name) or `lat` and `lon`.",
        )

    # ---- how big ---- #
    size = human.parse_size(payload.size)

    # Attribution. The gate exists so consumption is answerable — "which aggregator resolved
    # 40,000 areas last month" is a capacity and commercial question the platform must be able
    # to answer — and a gate with no record answers nothing.
    #
    # Logged rather than written to the immutable audit log: `record_audit` is account-scoped
    # and a platform service key has no account, so half these calls would have nowhere to go.
    # An audit entry per place lookup would also swamp the collection that holds logins and
    # key creations, which is what an incident review actually needs.
    log.info(
        "place resolved",
        extra={
            "holder": holder_label,
            "is_platform": is_platform,
            "hectares": round(size.hectares, 2),
            "by_place": payload.place is not None,
        },
    )

    # Clamp into the monitorable band rather than rejecting.
    #
    # Someone who says "2 plots" (0.09 ha) means a real piece of land — refusing them because
    # it is under the 0.5 ha sensor floor would be technically correct and practically
    # useless. Raising it to the floor monitors a slightly larger area around their plot,
    # which is what a satellite can actually do, and `size_is_estimate` tells the UI to say so.
    hectares = min(
        max(size.hectares, geometry.MIN_AOI_HECTARES), geometry.MAX_AOI_HECTARES
    )
    clamped = abs(hectares - size.hectares) > 0.001

    lon_delta, lat_delta = human.square_for_hectares(lat, lon, hectares)
    bbox = BBox(
        west=max(-180.0, lon - lon_delta),
        south=max(-90.0, lat - lat_delta),
        east=min(180.0, lon + lon_delta),
        north=min(90.0, lat + lat_delta),
    )

    # ## Admin names come from GRID3 first, Nominatim second
    #
    # `places.reverse` is Nominatim, and for Nigerian LGAs it is the less accurate of the two:
    # measured at 3.470, 7.150 it returned **"Ogun State / Odeda"** where GRID3's national LGA
    # layer returns **"Ogun / Obafemi Owode"** — which is correct, and is what `eo/admin.py`
    # already gives every assessment through `ScoutAgent`.
    #
    # That mattered more than a cosmetic label: `admin1`/`admin2` are what Fahis searches on to
    # verify a warning, so a wrong LGA makes a correct alert unverifiable. It also made the
    # portal disagree with the alert about where the plot is.
    #
    # Nominatim stays as the fallback — it covers countries GRID3 does not — and its `country`
    # is still preferred because GRID3 is Nigeria-only.
    grid3_place = await admin.resolve(
        [bbox.west, bbox.south, bbox.east, bbox.north],
        country=(place.country if place else None) or (payload.country or "ng").upper(),
    )

    area = {
        "name": payload.name or (place.label.split(",")[0] if place else "My area"),
        "bbox": bbox.model_dump(),
        "country": (place.country if place else None) or (payload.country or "ng").upper(),
        "admin1": (grid3_place.admin1 if grid3_place else None)
        or (place.admin1 if place else None),
        "admin2": (grid3_place.admin2 if grid3_place else None)
        or (place.admin2 if place else None),
        "hectares": round(geometry.area_hectares(bbox), 2),
    }

    # `by_place` rather than `place is not None`: both paths populate `place`, but only a forward
    # geocode resolved the FEATURE the caller named. See the note on `place_ring` below.
    ring = place.ring if (place is not None and by_place) else None
    ring_hectares = place.ring_hectares if (place is not None and by_place) else None
    ring_note = (
        MonitoringNote(
            **vars(
                human.monitoring_note(
                    ring_hectares,
                    label=place.label.split(",")[0].strip() or "that outline",
                )
            )
        )
        if ring is not None and place is not None
        else None
    )

    return ResolvedArea(
        area=area,
        resolved_place=place.label if place else None,
        size_description=human.describe_area(area["hectares"]),
        hectares=area["hectares"],
        size_is_estimate=size.approximate or clamped,
        country=area["country"],
        admin1=area["admin1"],
        admin2=area["admin2"],
        # Stated in days rather than as "continuous". Sentinel-1 revisits every ~6 days and
        # Sentinel-2 every ~5, so the honest answer is "every few days" — and a subscriber
        # told "continuous" who then waits a week for an update concludes it is broken.
        monitoring_cadence=(
            "Checked on every satellite pass — usually every 5 to 6 days, and radar sees "
            "through cloud so monitoring continues during a storm."
        ),
        # Carried through from the geocoder so a caller can confirm WHICH feature matched, without
        # a second round trip to `/places/search`.
        #
        # **Only on the by-place path**, and this is not a simplification — it is a correctness
        # requirement found by checking. `places.reverse` (the lat/lon path) returns the enclosing
        # ADMINISTRATIVE AREA, not the feature at the point: measured at 6.9312, 3.4021 it returns
        # "Obafemi Owode, Ogun State" with a **206-vertex LGA boundary**. Passing that through as
        # `place_ring` would hand a partner a district outline labelled as their customer's plot —
        # far worse than no ring, because it looks like precision.
        #
        # A caller who sent coordinates already knows where they pointed, so there is nothing to
        # confirm anyway.
        place_ring=ring,
        place_ring_hectares=ring_hectares,
        place_monitoring=ring_note,
        attribution=places.ATTRIBUTION,
    )
