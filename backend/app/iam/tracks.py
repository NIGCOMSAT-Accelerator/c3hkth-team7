"""Intelligence tracks — what a workspace can activate, and what that actually changes.

## The three tracks are not equally real, and this module says so

The product is presented as three tracks. The risk model backs them very unequally, and
pretending otherwise would let an aggregator activate something that delivers nothing:

| Track | Primary hazards `OracleAgent._classify` can return | Status |
|---|---|---|
| Agricultural | `crop_waterlogging`, `crop_drought_stress`, `crop_vegetation_anomaly` | **live** |
| Environmental | `flood_inundation`, `flood_forecast` | **live** |
| Public Health | *none* | **not deliverable** |

`malaria_risk` exists in `HazardType` but `_classify` never returns it — it appears only in
`RiskAssessment.cascade`, gated on `health.endemic`. So a Public Health track activated today
would produce zero alerts, forever, while reading as enabled.

This is the finding `docs/frontend-journey-review.md` §2.2 recorded, and it is why activation
is modelled with a `deliverable` flag rather than a boolean. An aggregator may express interest
in a track that is not yet deliverable — that is a useful demand signal and the roadmap depends
on it — but the portal and the API must both say plainly that nothing will arrive yet.

## Why Environmental is live here when the UI called it "next phase"

Because the hazards are the same ones the flood engine already produces. `flood_inundation`
and `flood_forecast` are returned today, measured from the same SAR water mask. Calling that
track "coming soon" understated a capability that exists — the README notes Track A runs "on
Track B's SAR flood engine underneath".

What Environmental does *not* yet have is its own distinct output (vulnerability mapping,
infrastructure exposure), so it is live as an alerting track and thin as a product surface.
That distinction is in `notes` rather than hidden.
"""

from __future__ import annotations

from enum import Enum

from app.models.enums import HazardType


class Track(str, Enum):
    """An intelligence track. Stored on the workspace document, so renaming one is a migration."""

    AGRICULTURAL = "agricultural"
    ENVIRONMENTAL = "environmental"
    PUBLIC_HEALTH = "public_health"
    FINANCIAL = "financial"


#: Primary hazards each track delivers.
#:
#: Derived from reading `OracleAgent._classify` rather than from the marketing taxonomy, which
#: is the only way this table can be trusted: it is what decides whether an activated track
#: produces alerts.
TRACK_HAZARDS: dict[Track, frozenset[HazardType]] = {
    Track.AGRICULTURAL: frozenset(
        {
            HazardType.CROP_WATERLOGGING,
            HazardType.CROP_DROUGHT_STRESS,
            HazardType.CROP_VEGETATION_ANOMALY,
        }
    ),
    Track.ENVIRONMENTAL: frozenset(
        {HazardType.FLOOD_INUNDATION, HazardType.FLOOD_FORECAST}
    ),
    # Empty, and that is the point. `malaria_risk` is cascade-only — `_classify` never returns
    # it — so this track has no primary hazard to alert on.
    Track.PUBLIC_HEALTH: frozenset(),
    # Empty for a DIFFERENT reason from Public Health, and the difference matters.
    #
    # Public Health has a hazard (`malaria_risk`) that `_classify` does not yet return. Financial
    # has no hazard at all, and should not: a credit signal is not a hazard. Nothing about a
    # neighbourhood's commercial density is an *event* to warn a farmer about, and modelling it as
    # one would put a wealth proxy into the same pipeline that sends flood warnings.
    #
    # What this track will deliver is a **score with attribution**, on request, for a place a
    # lender named — closer to `POST /risk/assess` than to the watch loop. So an empty hazard set
    # here is the correct long-term state, not a gap waiting to be filled.
    Track.FINANCIAL: frozenset(),
}


#: Whether activating the track produces alerts today.
#:
#: A separate flag rather than `bool(TRACK_HAZARDS[track])` so the reason is explicit at the
#: point of use, and so a track could be marked undeliverable for a reason other than an empty
#: hazard set (an upstream data source that has not been wired, say).
TRACK_DELIVERABLE: dict[Track, bool] = {
    Track.AGRICULTURAL: True,
    Track.ENVIRONMENTAL: True,
    Track.PUBLIC_HEALTH: False,
    Track.FINANCIAL: False,
}


#: What the portal shows, and the honest caveat for each.
TRACK_INFO: dict[Track, dict[str, str]] = {
    Track.AGRICULTURAL: {
        "label": "Agricultural Intelligence",
        "summary": (
            "Crop health monitoring, soil-moisture analysis, crop-stress scoring and "
            "irrigation guidance."
        ),
        "notes": (
            "Fully live. Sentinel-2 optical indices with a Sentinel-1 radar fallback, so "
            "monitoring continues through the cloud that blinds optical-only services."
        ),
    },
    Track.ENVIRONMENTAL: {
        "label": "Environmental Intelligence",
        "summary": (
            "Flood inundation and 7-day flood outlook, with population and cropland "
            "exposure for the affected footprint."
        ),
        "notes": (
            "Alerting is live — it runs on the same SAR water mask as the agricultural "
            "track. Vulnerability mapping and infrastructure exposure are the next "
            "additions, so today this delivers flood alerts rather than a full "
            "environmental console."
        ),
    },
    Track.PUBLIC_HEALTH: {
        "label": "Public Health Intelligence",
        "summary": (
            "Malaria environmental risk, standing-water detection and climate-health "
            "monitoring."
        ),
        "notes": (
            "NOT YET DELIVERABLE. Malaria risk is currently reported only as a cascade "
            "consequence of a flood or waterlogging alert, never as a hazard in its own "
            "right — so activating this track records your interest and changes what you "
            "receive not at all. It is listed rather than hidden because the demand signal "
            "is what sequences the work."
        ),
    },
    Track.FINANCIAL: {
        "label": "Financial & Credit Risk Intelligence",
        "summary": (
            "Geospatial KYC/KYB and PoS terminal anchor verification, neighbourhood commercial "
            "and demographic risk scoring, asset and activity validation, and lender portfolio "
            "exposure — from the same satellite pipeline."
        ),
        "notes": (
            "NOT YET DELIVERABLE, and it is the track where saying so matters most: a credit "
            "decision that declines someone is harder to appeal than a flood alert that did not "
            "arrive. Activating this records interest and changes what you receive not at all.\n\n"
            "Two capabilities are already close. Portfolio exposure monitoring IS the existing "
            "Oracle — severity per area, scored, timelined and verified — pointed at a loan book "
            "instead of a farm. Neighbourhood scoring runs on WorldPop, WorldCover built-up "
            "fraction and Copernicus DEM, all wired today. Both need a lender-facing surface "
            "rather than new science.\n\n"
            "Two are genuinely missing. Address verification can answer 'inside a mapped "
            "settlement, near a road, in a built-up cell' but cannot yet answer 'is this the "
            "declared building' — that needs building footprints, and Overture's African address "
            "layer is empty (measured: 39 countries, none African). Residency and mobility "
            "timelines need consented GPS, and there is NO CONSENT PRIMITIVE in this platform "
            "yet — no consent record, no lawful basis, no purpose limitation, no per-subject "
            "retention. That layer is built FIRST, before any location stream is ingested; "
            "ingest-then-harden produces a compliance problem that cannot be retrofitted.\n\n"
            "One limit is physical and will not move: MIN_AOI_HECTARES is 0.5 ha because below "
            "~50 Sentinel pixels a fraction is edge noise. A typical urban plot is 0.02-0.05 ha, "
            "so 'verify this house' is not a pixel measurement — it is a vector and contextual "
            "question, which is why this track relocates to footprints, POI and settlement data "
            "rather than forcing the imagery pipeline to produce a precise-looking number that "
            "means nothing."
        ),
    },
}


#: Per-capability status for the Financial track, in the order a lender would ask about them.
#:
#: ## Why this exists rather than one "coming soon" flag
#:
#: Following the same honesty rule as `TRACK_DELIVERABLE`, one step finer. A single "next phase"
#: label on a track this broad is less credible to a reviewer than a list that is defensible line
#: by line — and it is also less useful to us, because the sequencing argument lives in the gaps.
#:
#: `blocked_by` is deliberately a sentence rather than a code. The blocker is the interesting part:
#: three of these are waiting on a *decision* (a consent model, a lender surface) and two on data
#: that has been checked and found absent, which are very different kinds of "not yet".
FINANCIAL_CAPABILITIES: tuple[dict[str, str], ...] = (
    {
        "key": "portfolio_exposure",
        "label": "Lender portfolio exposure",
        "status": "ready",
        "detail": (
            "Loan exposure mapped against flood risk, agricultural stress and settlement "
            "context. This IS the existing Oracle: severity per area, already scored, already "
            "timelined in `assessments`, already verified by Fahis."
        ),
        "blocked_by": "A lender-facing surface. No new measurement is required.",
    },
    {
        "key": "neighbourhood_scoring",
        "label": "Neighbourhood demographic & commercial scoring",
        "status": "ready",
        "detail": (
            "Population density (WorldPop 100 m), built-up fraction (ESA WorldCover), terrain "
            "(Copernicus DEM), and POI/commercial density (Overture `places` — 53,865 records "
            "measured over a Lagos bbox)."
        ),
        "blocked_by": (
            "Composition and weighting. Every input is wired; none is yet combined into a score "
            "a lender could act on — and that combination is the part that needs review, not the "
            "plumbing."
        ),
    },
    {
        "key": "agent_merchant_network",
        "label": "Agent, merchant & MSME network intelligence",
        "status": "feasible",
        "detail": (
            "Agent viability and merchant catchment from POI density, WorldPop catchment and "
            "road proximity."
        ),
        "blocked_by": (
            "Two more Overpass queries. `eo/exposure.py` runs exactly two today (`place` and "
            "`amenity`), so `shop=*`, `office=*` and `highway=*` are cheap additions on the same "
            "helper — the fastest credibility win in this track."
        ),
    },
    {
        "key": "pos_terminal_anchor",
        "label": "PoS terminal anchor verification (CBN geo-fencing)",
        "status": "feasible",
        "detail": (
            "Verify that a PoS agent's REGISTERED business address is real and a plausible place "
            "of trade: does it resolve to a mapped feature, is the 70 m disc built-up rather than "
            "water or cropland, is there commercial activity in the catchment, and is it near a "
            "road. Returned with per-input attribution so a declined registration can be "
            "explained and challenged.\n\n"
            "THE 70 M RADIUS IS WHAT MAKES THIS MEASURABLE, and the arithmetic is the whole "
            "reason this capability is `feasible` rather than blocked. The CBN's original 10 m "
            "geofence is a disc of 314 m2 = 0.0314 ha, about THREE Sentinel pixels — far below "
            "MIN_AOI_HECTARES and therefore unmeasurable, edge noise by definition. The radius "
            "relaxed to 70 m after operator feedback gives 15,394 m2 = 1.5394 ha, about 153 "
            "pixels, comfortably above the 0.5 ha floor. A regulatory concession made for "
            "practical reasons is what moved this from impossible to buildable."
        ),
        "blocked_by": (
            "A surface and a scoring composition; every input is already wired (WorldCover "
            "built-up fraction, Overture POI density, Overpass road proximity, and "
            "/places/resolve for the address itself).\n\n"
            "SCOPE LIMIT, stated because overclaiming here is a regulatory risk for the "
            "operator rather than for us: this verifies the ANCHOR, not the transaction. The "
            "directive requires the terminal to transmit dual-frequency GPS at transaction time "
            "and the PSP's switch to flag, decline or disable outside the radius — that is "
            "hardware we do not make and a payment path we must not sit in. The live "
            "within-radius check is a haversine any PSP writes themselves.\n\n"
            "What nobody else answers at national scale is whether the anchor is genuine: "
            "geofencing a terminal against a FAKE address enforces nothing, because the terminal "
            "sits happily within 70 m of a coordinate in the middle of a swamp. Ghost and cloned "
            "terminals are the stated reason for the directive, so the anchor is the gap. "
            "Enforcement begins 2026-08-01."
        ),
    },
    {
        "key": "kyc_kyb_address",
        "label": "KYC/KYB address verification",
        "status": "partial",
        "detail": (
            "Can answer today: is this point inside a mapped settlement, near a road, in a "
            "built-up cell, and does the declared place resolve to a real feature with an "
            "outline (`/places/resolve` returns the footprint and its true area)."
        ),
        "blocked_by": (
            "Cannot yet answer 'is this the declared building'. Needs building footprints — "
            "Microsoft `ms-buildings` (Parquet, has `meanHeight`) is reachable and unbuilt. "
            "Overture's address layer does NOT help: measured, it covers 39 countries and none "
            "of them are in Africa."
        ),
    },
    {
        "key": "fraud_synthetic_location",
        "label": "Fraud & synthetic-location detection",
        "status": "partial",
        "detail": (
            "Settlement, road and land-use cross-checks are feasible now — a declared shop in "
            "the middle of unmapped scrub is detectable with data already ingested."
        ),
        "blocked_by": (
            "Impossible-movement and duplicate-agent-cluster detection need consented GPS. See "
            "`residency_timeline`; the same consent layer gates both."
        ),
    },
    {
        "key": "residency_timeline",
        "label": "Residency & business-permanence timelines",
        "status": "blocked",
        "detail": (
            "Consented GPS streams, check-ins or field-visit records turned into a location-"
            "consistency timeline."
        ),
        "blocked_by": (
            "THE CONSENT LAYER, which does not exist. Verified: no consent record, no lawful-"
            "basis field, no purpose limitation, no per-subject retention anywhere in this "
            "codebase. The nearest machinery is `audit.py`'s `expires_at` TTL index — right "
            "bones, wrong subject. Under the NDPR this is built before ingest, not after."
        ),
    },
    {
        "key": "hr_background_support",
        "label": "HR-tech background-verification support",
        "status": "blocked",
        "detail": (
            "Complementary address-consistency checks for hiring workflows, where lawful and "
            "candidate-consented."
        ),
        "blocked_by": (
            "The consent layer, plus a governance decision this platform has not made. The "
            "safeguards are not optional extras: explicit consent, minimum necessary data, no "
            "inference of sensitive characteristics, explainable and challengeable outputs, "
            "human review of every anomaly, time-limited retention, and a fairness and "
            "data-protection assessment before deployment. These signals must never be a "
            "standalone basis for a hiring, exclusion, pay or disciplinary decision. Listed here "
            "so the constraint is recorded with the capability rather than in a separate document "
            "nobody reads at build time."
        ),
    },
)


#: What a workspace gets when it is first created.
#:
#: Agricultural only. Not "everything deliverable": activating a track is a commercial
#: decision, and defaulting an organisation into flood alerting they did not ask for would
#: send warnings about hazards outside what they signed up for.
DEFAULT_TRACKS: tuple[Track, ...] = (Track.AGRICULTURAL,)


def hazards_for(tracks: list[str] | tuple[str, ...]) -> frozenset[HazardType]:
    """Every primary hazard the given tracks deliver.

    This is the enforcement point: the Herald can filter an alert against it, so a workspace
    with only the agricultural track does not receive flood alerts it never activated.

    Unknown track names are ignored rather than raising. A workspace document written by a
    later build must not break alerting for the tracks this build does understand — dropping
    an unknown one degrades coverage, whereas raising would stop delivery entirely.
    """
    out: set[HazardType] = set()
    for name in tracks:
        try:
            out |= TRACK_HAZARDS[Track(name)]
        except (ValueError, KeyError):
            continue
    return frozenset(out)


def undeliverable(tracks: list[str] | tuple[str, ...]) -> list[Track]:
    """Activated tracks that will produce nothing yet.

    Returned so the API can say so in its response and the portal can label the switch. An
    activation that silently delivers nothing is the failure this whole module exists to
    prevent.
    """
    out: list[Track] = []
    for name in tracks:
        try:
            track = Track(name)
        except ValueError:
            continue
        if not TRACK_DELIVERABLE.get(track, False):
            out.append(track)
    return out
