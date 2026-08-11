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
}


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
