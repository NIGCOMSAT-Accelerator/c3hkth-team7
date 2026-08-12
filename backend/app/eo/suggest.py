"""Type-ahead place suggestions, via Photon (OpenStreetMap).

## Why a second geocoder, when `places.py` already geocodes

They answer different questions and one cannot do the other's job.

**Nominatim is a full-text geocoder.** Given a complete phrase it finds the place, and it is
what `places.search` uses to resolve "Argungu, Kebbi" into coordinates and — since the
`polygon_geojson` change — an outline. Given a *prefix* it is much weaker: "Argun" is not a
name, so relevance ranking has little to work with. Its usage policy is also **one request per
second**, enforced process-wide in `places._get`, which forbids a request per keystroke on
arithmetic alone: a seven-character place name debounced to 400 ms would queue behind its own
predecessors.

**Photon is a prefix index over the same OSM data.** It exists specifically for
search-as-you-type, matches partial tokens, and answers from a local Elasticsearch in
single-digit milliseconds.

So this module is the *suggestion* layer and `places.py` remains the *resolution* layer. A
suggestion is a label the user recognises; the moment they choose one, resolution takes over
and produces the geometry. Keeping them separate is what stops a half-typed string ever
becoming an AOI.

## Self-hosted, and why that is not optional

Measured against the public instance at `photon.komoot.io`: **23 seconds** for a single query.
Behind a keystroke that is not a slow feature, it is a broken one — the user has typed three
more characters before the first suggestion lands. The same reasoning as SearXNG and MinIO:
this is infrastructure we run, not a third party we lean on.

It also carries the same privacy argument as `places.py`, more sharply. A suggestion stream is
a **character-by-character** record of someone typing their own address. That belongs in our
own logs or nowhere.

`PHOTON_URL` empty **disables the feature entirely** and callers fall back to the existing
debounced Nominatim search. That is deliberate: this must be shippable dark, so a deployment
without a Photon container is a deployment with slightly clumsier search rather than a
deployment with a broken form.

## Attribution

Photon serves OSM data, so the ODbL condition is identical — `places.ATTRIBUTION` is reused
rather than duplicated, because two copies of a licence string is how one of them goes stale.
"""

from __future__ import annotations

import json

import httpx

from app.config import settings

#: Re-exported so a caller needs one import for suggestions and their licence condition.
#: Reused rather than duplicated because two copies of a licence string is how one goes stale.
from app.eo.places import ATTRIBUTION
from app.logging_config import describe, get_logger
from app.store import cache

log = get_logger(__name__)

_PREFIX = "geo:suggest"

__all__ = ["ATTRIBUTION", "Suggestion", "available", "suggest"]

#: Nigeria's rough centre, used to bias results toward the service area.
#:
#: Photon's `lat`/`lon` are a *preference*, not a filter, so this improves ordering without
#: excluding anything — a partner searching Ghana still gets Ghanaian results, just lower. A
#: hard filter would be wrong: the platform's remit is Sub-Saharan Africa, not one country.
_BIAS_LAT = 9.0
_BIAS_LON = 8.0

#: OSM values that are places a subscriber might monitor, ordered by how likely that is.
#:
#: Used only for ranking, never to exclude — an unranked kind sorts last rather than vanishing,
#: because guessing wrong about what someone wants to monitor should cost them a scroll, not a
#: result. Farms and settlements lead; a road or a bus stop that shares a name comes after.
_KIND_RANK: dict[str, int] = {
    "farm": 0,
    "farmland": 0,
    "village": 1,
    "hamlet": 1,
    "town": 1,
    "city": 1,
    "suburb": 2,
    "neighbourhood": 2,
    "quarter": 2,
    "marketplace": 3,
    "commercial": 3,
    "industrial": 3,
    "school": 4,
    "hospital": 4,
    "clinic": 4,
}


class Suggestion:
    """One type-ahead row.

    Deliberately **not** a `Place`. A suggestion carries no geometry and no admin fields — it
    is a label plus enough coordinates to frame a map, and nothing here is sufficient to build
    an AOI. Selecting one triggers a real resolution through `places.search`, which is where
    the outline and the admin hierarchy come from.

    That separation is the safety property: if this type could produce an AOI, a partially typed
    string could too.
    """

    __slots__ = ("label", "detail", "lat", "lon", "kind", "country")

    def __init__(
        self,
        *,
        label: str,
        detail: str,
        lat: float,
        lon: float,
        kind: str | None,
        country: str | None,
    ) -> None:
        self.label = label
        self.detail = detail
        self.lat = lat
        self.lon = lon
        self.kind = kind
        self.country = country

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "detail": self.detail,
            "lat": self.lat,
            "lon": self.lon,
            "kind": self.kind,
            "country": self.country,
        }


def available() -> bool:
    """True when a Photon instance is configured.

    Callers use this to decide whether to offer type-ahead at all, rather than calling and
    getting an empty list — an empty list is indistinguishable from "no matches", and showing
    "no results" for a healthy query because the feature is switched off is a lie.
    """
    return bool((settings.photon_url or "").strip())


def _label(props: dict) -> tuple[str, str]:
    """`(label, detail)` from Photon's properties.

    Two strings rather than one, because a suggestion list needs a **name** the eye lands on
    and a **context** that disambiguates it. Nigeria has a Kajola in several states; one
    combined string makes them look identical at a glance, which is precisely when someone
    picks the wrong one and monitors a field 300 km away.
    """
    name = (props.get("name") or "").strip()

    parts = [
        props.get("street"),
        props.get("district"),
        props.get("city"),
        props.get("county"),
        props.get("state"),
        props.get("country"),
    ]
    # Deduplicate while preserving order: Photon frequently repeats a value across `city` and
    # `county` for a small settlement, and "Argungu, Argungu, Kebbi" reads like a bug.
    seen: set[str] = set()
    detail: list[str] = []
    for part in parts:
        text = (part or "").strip()
        if not text or text == name or text in seen:
            continue
        seen.add(text)
        detail.append(text)

    if not name:
        # Photon can return an unnamed feature (a numbered building). Fall back to the most
        # specific context available rather than showing a blank row.
        name = detail.pop(0) if detail else "Unnamed place"

    return name, ", ".join(detail)


def _to_suggestion(feature: dict) -> Suggestion | None:
    """One GeoJSON feature → `Suggestion`, or None if unusable.

    None rather than raising: one malformed feature must not discard the rest of the list.
    """
    try:
        props = feature["properties"]
        lon, lat = feature["geometry"]["coordinates"][:2]
        lat = float(lat)
        lon = float(lon)
    except (KeyError, TypeError, ValueError, IndexError):
        return None

    label, detail = _label(props)
    return Suggestion(
        label=label,
        detail=detail,
        lat=lat,
        lon=lon,
        kind=props.get("osm_value") or props.get("osm_key"),
        country=(props.get("countrycode") or "").upper() or None,
    )


async def suggest(query: str, *, limit: int | None = None) -> list[Suggestion]:
    """Prefix suggestions for a partially typed place. Never raises.

    Returns an empty list for a disabled or unreachable Photon, a too-short query, or no
    matches. The caller cannot distinguish those and does not need to — in every case the user
    keeps typing and the existing full search still works on the complete string.

    **No rate limiter here, unlike `places._get`.** That lock exists to honour a third party's
    published policy; this queries our own instance, where the constraint is our CPU rather than
    someone else's goodwill. The protection that *is* needed is per-credential, at the route —
    a per-keystroke endpoint is a far cheaper address-enumeration surface than a 500 ms-debounced
    one, and rate limiting belongs where the caller's identity is known.
    """
    if not available():
        return []

    query = (query or "").strip()
    # Two characters, against three for full search. A prefix index genuinely can rank "Ka"
    # usefully where full-text matching cannot, and the whole value of type-ahead is arriving
    # before the user finishes typing.
    if len(query) < 2:
        return []

    count = max(1, min(limit or settings.photon_max_results, settings.photon_max_results))
    params = {
        "q": query,
        "limit": count,
        "lat": _BIAS_LAT,
        "lon": _BIAS_LON,
    }

    cache_key = f"{_PREFIX}:{json.dumps(params, sort_keys=True)}"
    try:
        cached = await cache.get_text(cache_key)
        if cached:
            payload = json.loads(cached)
        else:
            payload = None
    except Exception:  # noqa: BLE001 — a cache miss must never block a keystroke
        payload = None

    if payload is None:
        try:
            async with httpx.AsyncClient(
                timeout=settings.photon_timeout_seconds
            ) as client:
                response = await client.get(
                    f"{settings.photon_url.rstrip('/')}/api", params=params
                )
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:  # noqa: BLE001
            # Debug, not warning. This sits behind a keystroke, so a single outage would emit
            # one line per character typed by every user on the platform — a log that drowns
            # the ones that matter. `GET /places/suggest/health` is how an operator checks it.
            log.debug("photon suggest failed", extra={"error": describe(exc)})
            return []

        try:
            await cache.set_text(
                cache_key,
                json.dumps(payload),
                ttl_seconds=settings.photon_cache_ttl_seconds,
            )
        except Exception:  # noqa: BLE001
            pass

    if not isinstance(payload, dict):
        return []

    features = payload.get("features")
    if not isinstance(features, list):
        return []

    out = [s for s in (_to_suggestion(f) for f in features) if s is not None]
    out.sort(key=lambda s: _KIND_RANK.get(s.kind or "", 99))
    return out
