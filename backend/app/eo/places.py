"""Place search and reverse geocoding, via Nominatim (OpenStreetMap).

## Why this is proxied rather than called from the browser

The k-anonymity argument does not apply here — a place query is not a secret — but three
others do:

  * **Subscriber IPs stay off a third party.** Every query names a Nigerian district and is
    attached to someone setting up flood monitoring for their own farm. A direct browser
    call would put that pairing in OSM's logs with the subscriber's own address.
  * **The 1 req/sec policy is per-IP, and type-ahead breaks it instantly.** Typing
    "Argungu" is seven keystrokes; debounced to 400ms it is still several requests inside a
    second, and Nominatim's response to that is a 429 or a block. Serialising server-side is
    the only way to be a good citizen and still offer search-as-you-type.
  * **One cache serves everyone.** "Kano" is looked up by every subscriber in Kano. Caching
    per-deployment rather than per-browser turns thousands of upstream calls into one.

## Attribution is a licence condition, not a courtesy

Nominatim data is ODbL. The UI must display "© OpenStreetMap contributors" wherever these
results appear — `ATTRIBUTION` is exported for that, so there is one string to change.

## Everything degrades rather than failing

Unreachable, rate-limited, or malformed all return an empty list. A subscriber who cannot
search can still drop a pin or use GPS, which are the more accurate paths anyway — so a
geocoder outage costs convenience, not the ability to register.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

import httpx

from app.config import settings
from app.logging_config import get_logger
from app.store import cache

log = get_logger(__name__)

#: Required wherever results are displayed. ODbL condition.
ATTRIBUTION = "© OpenStreetMap contributors"

_PREFIX = "geo:places"

#: Nominatim requires a real, identifying User-Agent and blocks generic ones.
_HEADERS = {
    "User-Agent": "SHELTER-EarlyWarning/1.0 (+https://shelter.zerorate.io)",
    # Ask for English names, falling back to local. A farmer searching in Hausa still gets a
    # result; the label just prefers the transliteration they are likely to recognise.
    "Accept-Language": "en",
}

#: Serialises upstream calls across the whole process.
#:
#: A lock plus a timestamp rather than a token bucket: the policy is one request per second,
#: which is exactly "wait until a second has passed since the last one". A bucket would allow
#: bursts, which is what the policy forbids.
_lock = asyncio.Lock()
_last_call = 0.0


@dataclass(frozen=True)
class Place:
    """One search result, reduced to what the AOI form needs.

    Nominatim returns dozens of fields; carrying them all would mean caching kilobytes per
    query and coupling the frontend to an upstream schema. These are the ones that either
    place a pin or fill an AOI field.
    """

    label: str
    lat: float
    lon: float
    #: The upstream bounding box, when given. Lets the UI frame the result at a sensible
    #: zoom — a city needs a different viewport from a village, and guessing from the point
    #: alone gets both wrong.
    bbox: list[float] | None
    country: str | None
    admin1: str | None
    admin2: str | None
    #: "city", "village", "farm", "administrative"… Used to order results, since a
    #: subscriber searching a place name almost always wants the settlement, not a road
    #: that shares its name.
    kind: str | None


def _pick_admin(address: dict) -> tuple[str | None, str | None]:
    """(admin1, admin2) from Nominatim's `address` object.

    The keys vary by country and by feature type, which is why this is a fallback chain
    rather than two lookups: a Nigerian result may carry `state` + `local_government_area`,
    or `state` + `county`, or only `state`. Guessing wrong leaves the AOI's admin fields
    empty, which is recoverable — inventing a value is not.
    """
    admin1 = address.get("state") or address.get("region") or address.get("province")
    admin2 = (
        address.get("local_government_area")
        or address.get("county")
        or address.get("city_district")
        or address.get("suburb")
    )
    return admin1, admin2


def _to_place(raw: dict) -> Place | None:
    """One Nominatim result → `Place`, or None if it is unusable.

    Returns None rather than raising: one malformed entry in a list of ten should not
    discard the other nine.
    """
    try:
        lat = float(raw["lat"])
        lon = float(raw["lon"])
    except (KeyError, TypeError, ValueError):
        return None

    address = raw.get("address") or {}
    admin1, admin2 = _pick_admin(address)

    bbox = None
    raw_bb = raw.get("boundingbox")
    if isinstance(raw_bb, list) and len(raw_bb) == 4:
        try:
            # Nominatim order is [south, north, west, east]. Converted to the
            # west/south/east/north order every other surface in this codebase uses —
            # the mismatch is a classic source of a map framing a different continent.
            south, north, west, east = (float(v) for v in raw_bb)
            bbox = [west, south, east, north]
        except (TypeError, ValueError):
            bbox = None

    return Place(
        label=raw.get("display_name") or raw.get("name") or f"{lat:.4f}, {lon:.4f}",
        lat=lat,
        lon=lon,
        bbox=bbox,
        country=(address.get("country_code") or "").upper() or None,
        admin1=admin1,
        admin2=admin2,
        kind=raw.get("type") or raw.get("class"),
    )


async def _get(path: str, params: dict) -> list[dict] | dict | None:
    """One rate-limited, cached upstream call. Never raises."""
    cache_key = f"{_PREFIX}:{path}:{json.dumps(params, sort_keys=True)}"

    try:
        cached = await cache.get_text(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:  # noqa: BLE001 — a cache miss must never block the request
        pass

    global _last_call
    async with _lock:
        # Honour one-request-per-second across the whole process, including concurrent
        # subscribers. Inside the lock so two coroutines cannot both decide they are clear.
        wait = settings.nominatim_min_interval_seconds - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)

        try:
            async with httpx.AsyncClient(
                timeout=settings.nominatim_timeout_seconds
            ) as client:
                response = await client.get(
                    f"{settings.nominatim_url.rstrip('/')}/{path}",
                    params={**params, "format": "jsonv2", "addressdetails": 1},
                    headers=_HEADERS,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("place lookup unreachable (%s)", exc)
            return None
        finally:
            _last_call = time.monotonic()

    if response.status_code == 429:
        # Being throttled means we are misbehaving or sharing an IP with someone who is.
        # Logged at warning so it is visible, but the caller still degrades quietly.
        log.warning("Nominatim rate-limited this deployment; search degraded")
        return None
    if response.status_code != 200:
        log.warning("place lookup returned %s", response.status_code)
        return None

    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        return None

    try:
        # A long TTL because place names do not move. This is the setting that keeps a
        # deployment inside the usage policy under real load.
        await cache.set_text(
            cache_key,
            json.dumps(payload),
            ttl_seconds=settings.nominatim_cache_ttl_seconds,
        )
    except Exception:  # noqa: BLE001
        pass

    return payload


async def search(query: str, *, limit: int = 6, country: str | None = None) -> list[Place]:
    """Forward geocode: "Argungu, Kebbi" → candidate places.

    `country` biases results to one ISO-3166 code. Worth passing: "Kano" exists in several
    countries, and a Nigerian subscriber offered a Japanese result concludes the search is
    broken.

    Results are ordered settlements-first, because someone typing a place name wants the
    town rather than a road or a bus stop that shares its name — and Nominatim's own
    ordering is by importance, which does not encode that preference.
    """
    query = (query or "").strip()
    if len(query) < 3:
        # Below three characters every query matches thousands of places, so the request is
        # pure cost with no useful answer.
        return []

    params: dict[str, object] = {"q": query, "limit": max(1, min(limit, 20))}
    if country:
        params["countrycodes"] = country.lower()

    payload = await _get("search", params)
    if not isinstance(payload, list):
        return []

    places = [p for p in (_to_place(item) for item in payload) if p is not None]

    settlement = {"city", "town", "village", "hamlet", "suburb", "municipality"}
    places.sort(key=lambda p: 0 if (p.kind or "") in settlement else 1)
    return places


async def reverse(lat: float, lon: float) -> Place | None:
    """Reverse geocode: a dropped pin → country, state and LGA.

    This is what makes the map worth having on the API side as well as the UI: a partner
    posting `{lat, lon}` gets `admin1`/`admin2`/`country` filled without knowing Nigerian
    administrative structure. Those fields are not decorative — `AreaOfInterest.admin1` and
    `admin2` are what an operator filters by when a flood crosses several LGAs.

    None on any failure. The AOI is still fully monitorable without them, so this must never
    be the reason a registration fails.
    """
    payload = await _get("reverse", {"lat": lat, "lon": lon, "zoom": 12})
    if not isinstance(payload, dict) or "lat" not in payload:
        return None
    return _to_place(payload)
