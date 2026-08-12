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

    #: **The actual outline of the feature**, as a closed `[[lon, lat], …]` ring — the
    #: mosque's walls, the market's perimeter, the ward's boundary.
    #:
    #: ## Why this was worth adding
    #:
    #: Nominatim has always been able to return this; we simply never asked for it
    #: (`polygon_geojson=1`). So a search for "Wuse Market, Abuja" resolved a real 17-vertex
    #: perimeter upstream and we kept **four numbers** — the envelope — and threw the shape
    #: away. The subscriber then saw a rectangle over their market and had no way to tell
    #: whether we had found *their* market or something a kilometre away with a similar name.
    #:
    #: That is the whole confirmation problem: the pipeline was locating places correctly and
    #: was unable to show its work.
    #:
    #: None for a point result (a village node) or a line (a street), which is not a failure —
    #: most OSM features genuinely have no polygon, and `bbox` still frames them. Callers must
    #: treat this as a bonus, never as required.
    ring: list[list[float]] | None = None
    #: True area of `ring` in hectares, or None when there is no ring.
    #:
    #: Carried here rather than recomputed by every caller because it is what decides whether
    #: the outline can be monitored *as drawn* — and a building almost never can. Measured:
    #: Kano Central Mosque is **0.147 ha**, about 14 Sentinel pixels, against a
    #: `MIN_AOI_HECTARES` floor of 0.5. See `human.monitoring_note`.
    ring_hectares: float | None = None


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

    ring, ring_hectares = _outline(raw.get("geojson"))

    return Place(
        label=raw.get("display_name") or raw.get("name") or f"{lat:.4f}, {lon:.4f}",
        lat=lat,
        lon=lon,
        bbox=bbox,
        country=(address.get("country_code") or "").upper() or None,
        admin1=admin1,
        admin2=admin2,
        kind=raw.get("type") or raw.get("class"),
        ring=ring,
        ring_hectares=ring_hectares,
    )


def _outline(geojson: object) -> tuple[list[list[float]] | None, float | None]:
    """The feature's own outline as a normalised ring, plus its true area.

    ## What upstream sends, measured

    `polygon_geojson=1` returns a real GeoJSON geometry whose type depends entirely on what
    the feature *is*, and all four of these occur in ordinary Nigerian searches:

        Wuse Market, Abuja           Polygon         -> a ring, usable
        Kano Central Mosque          Polygon         -> a ring, 17 vertices, 0.147 ha
        Argungu                      Polygon         -> the LGA boundary, usable
        Adeola Odeku Street, Lagos   LineString      -> no ring; a street has no area
        Argungu (the town node)      Point           -> no ring
        a multi-part district        MultiPolygon    -> take the largest part

    So returning None is the *common* case, not an error path. A caller that treats a missing
    ring as failure would reject street and settlement results, which are most of them.

    **MultiPolygon takes the largest part rather than merging.** Merging would need a real
    union and would produce a shape spanning the gap between two disjoint pieces of a
    district — which is not ground the subscriber owns, and every fraction measured over it
    would be diluted by whatever lies between.

    Any malformed geometry yields `(None, None)`: the `bbox` still frames the result, so a bad
    outline must degrade to the old behaviour rather than lose the search hit.
    """
    if not isinstance(geojson, dict):
        return None, None

    kind = geojson.get("type")
    coords = geojson.get("coordinates")

    if kind == "Polygon":
        outer = coords[0] if isinstance(coords, list) and coords else None
    elif kind == "MultiPolygon":
        if not isinstance(coords, list) or not coords:
            return None, None
        # Largest by vertex count — a proxy for area that cannot raise on a degenerate part,
        # and the choice only has to be *stable*, since a district's main body is also its
        # most densely digitised.
        try:
            outer = max(
                (p[0] for p in coords if isinstance(p, list) and p),
                key=len,
                default=None,
            )
        except (TypeError, ValueError):
            return None, None
    else:
        # Point, LineString, GeometryCollection — no area to monitor.
        return None, None

    if not isinstance(outer, list) or len(outer) < 3:
        return None, None

    try:
        ring = [[float(x), float(y)] for x, y in outer]
    except (TypeError, ValueError):
        return None, None

    # Close it, so what we return is valid GeoJSON a map can draw directly.
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))

    # **Deliberately NOT `geometry.validate_ring`**, and this distinction is the point.
    #
    # `validate_ring` guards the *write* path: it caps vertices at 200 because its
    # self-intersection check is O(n²), and it is the gate on a ring that will become a
    # rasterised monitoring mask. Kano State's boundary is 1,371 vertices, so routing this
    # through it returned **no outline at all for any state** — the search would resolve the
    # boundary upstream and then silently discard it, which is the exact bug this whole change
    # set out to fix, reintroduced one layer down.
    #
    # An outline here is *cartography*: something to draw so the user can confirm we found the
    # right place. It is never rasterised and never monitored — `/places/preview` and the write
    # path still run the full validation on whatever the user actually submits. A bow-tie in an
    # OSM administrative boundary would render slightly oddly and mislead nobody, so paying
    # O(1371²) to reject it buys nothing.
    #
    # `ring_hectares` is likewise informational: it is what `human.monitoring_note` uses to say
    # "this outline is smaller than we can measure", which is a *sentence*, not a mask.
    from app.eo import geometry

    # `ArithmeticError`/`TypeError`/`ValueError` only — deliberately NOT a bare `Exception`.
    #
    # A broad handler here silently absorbed a `GeometryError`, so a future edit routing this
    # through `validate_ring` would still return the ring and no test would notice. Caught by
    # mutation-testing the guard: the mutant escaped, which means the narrow `except` is doing
    # load-bearing work rather than being style.
    #
    # `polygon_area_hectares` is pure shoelace arithmetic over floats, so these three are the
    # only failures it can produce, and anything else IS a bug worth surfacing.
    try:
        return ring, round(geometry.polygon_area_hectares(ring), 4)
    except (ArithmeticError, TypeError, ValueError):
        log.debug("could not measure an upstream outline", exc_info=True)
        return ring, None


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
                    params={
                        **params,
                        "format": "jsonv2",
                        "addressdetails": 1,
                        # **The feature's real outline.** Costs one parameter and was simply
                        # never asked for, so every search discarded the geometry upstream had
                        # already computed and kept only the four-number envelope. See
                        # `_outline` for what actually comes back.
                        "polygon_geojson": 1,
                        # **Deliberately NOT simplified**, and this was measured rather than
                        # assumed. `polygon_threshold=0.0005` (~55 m, finer than a Sentinel
                        # pixel, so it looked free) cut Kano Central Mosque from 17 vertices to
                        # 4 and its area from 0.147 ha to 0.066 ha — a **55% error on the very
                        # features this change exists to resolve**. Simplification tolerance is
                        # absolute while feature sizes here span five orders of magnitude, so
                        # one threshold cannot serve both a building and a state.
                        #
                        # The cost it was meant to avoid does not exist: unsimplified, the
                        # largest realistic response is a whole state boundary at 1,371
                        # vertices and **33 KB**. That is a rounding error in a cache holding
                        # week-long place entries.
                    },
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
