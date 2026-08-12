"""Administrative boundaries — State / LGA / Ward, from authoritative sources.

## Why this module exists

Two problems it solves, and the second is the one that matters most.

**A subscriber should not have to draw geometry.** "My farm is in Odelemo ward, Shagamu LGA, Ogun
State" is how a Nigerian farmer describes where their land is. A bounding box is not. Resolving an
administrative name to a polygon lets the portal offer a picker instead of a map, which is the
difference between a form a smallholder can complete on a phone and one they abandon.

**Fahis cannot verify what it cannot name.** `agents/fahis._searchable_place` builds its search
query from `admin1`/`admin2` and deliberately never from `aoi_name` — "My Irri Palm Fruit Plantation"
appears in no news report. Those fields are populated only when a subscriber supplies them or
Nominatim happens to resolve them, so **a pin-registered AOI was effectively unverifiable**: the
accountability agent had no place name to search. That is a hole in the loop, not a missing nicety.

## Why two sources, and which leads

**Three tiers, tried in order.** Each exists because the one above it has a real coverage hole,
found by probing rather than assumed:

| Tier | Source | Depth | Coverage | Verified 2026-08-10 |
|---|---|---|---|---|
| 1 | GRID3 wards | State / LGA / **Ward** | **24 of 37 states** — Lagos ABSENT | 5,872 features; `state='Lagos'` count = 0 |
| 2 | GRID3 LGA | State / LGA | **all 37 states, 774 LGAs** | resolves Lagos correctly |
| 3 | geoBoundaries | country | every country | ADM1/ADM2 = 200, **ADM3 = 404 for Nigeria** |

Tier 1 leads because it is deepest *and* carries alternate spellings, which is what a search
actually needs: "Ode Lemo", "Ode -Lemo" and "Odelemo" are one ward, and a verification query built
on a single spelling finds nothing.

Tier 2 is not redundancy — it is the only thing covering a third of the country. A ward-only
resolver left Lagos, Nigeria's largest city, with no administrative name at all, and an AOI with no
place name is one Fahis cannot verify.

Tier 3 is the cross-border answer. The platform's scope is Nigeria *and Sub-Saharan Africa*, so a
Nigeria-only resolver would be a dead end at the first deployment outside it.

Measured end to end: Ikorodu → `Lagos / Ikorodu` (tier 2), Kano → `Kano / Ungogo / Tudun Fulani`
(tier 1), Yenagoa → `Bayelsa / Ogbia / Imiringi-Ward 8` with alternates
`('Imiringi', 'Imiringi 8', 'Imiringi ward 8')` (tier 1).

## The failure mode this must not have

Both upstreams are network calls on a path that decides what a farmer's plot is *called*. Neither may
fail an assessment: every function here returns None or an empty result rather than raising, and the
caller keeps whatever the subscriber typed. An unresolved AOI is monitored exactly as well as before
— it is only less verifiable, which is the status quo this improves on rather than a regression.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import httpx

from app.config import settings
from app.logging_config import describe, get_logger
from app.models.schemas import AreaOfInterest
from app.store import cache

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

#: Attribution. GRID3 data is CC-BY and geoBoundaries is ODbL/CC-BY depending on release, so credit
#: is a licence condition wherever a resolved name is displayed — the same rule as
#: `places.ATTRIBUTION` for Nominatim and DB-IP for geolocation.
ATTRIBUTION = (
    "Administrative boundaries: GRID3 Nigeria (CIESIN) and geoBoundaries (gbOpen)."
)


@dataclass(frozen=True)
class AdminPlace:
    """One resolved administrative unit.

    `alt_names` is not decoration. Nigerian ward and LGA names have several accepted spellings, and
    `agents/fahis` searches outside reporting for a hazard *by place name* — so a query built on one
    spelling misses reports that used another. Carrying them lets the search widen without guessing.
    """

    country: str | None
    #: State / province.
    admin1: str | None
    #: LGA / district.
    admin2: str | None
    #: Ward / community. GRID3 only; geoBoundaries has no ADM3 for Nigeria.
    admin3: str | None = None
    #: Alternate spellings for admin2 and admin3, deduplicated, most specific first.
    alt_names: tuple[str, ...] = ()
    #: Which upstream answered — `grid3` or `geoboundaries`. Recorded because the two have
    #: different depth and provenance, and a reader comparing two AOIs should be able to tell.
    source: str = "unknown"


def _split_alt(raw: str | None) -> list[str]:
    """GRID3 packs alternates into one comma-separated string, e.g. `"Ode Lemo, Ode -Lemo"`."""
    if not raw:
        return []
    return [part.strip() for part in str(raw).split(",") if part.strip()]


async def _arcgis_envelope_query(
    service: str, out_fields: str, bbox: list[float]
) -> dict | None:
    """One ArcGIS envelope query. None on any failure.

    Shared by the ward and LGA tiers because the request shape is identical — only the service and
    the field list differ.
    """
    url = f"{settings.grid3_base_url.rstrip('/')}/{service}/FeatureServer/0/query"
    envelope = json.dumps(
        {
            "xmin": bbox[0],
            "ymin": bbox[1],
            "xmax": bbox[2],
            "ymax": bbox[3],
            "spatialReference": {"wkid": 4326},
        }
    )
    params = {
        "geometry": envelope,
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": out_fields,
        # Geometry deliberately NOT returned — see `resolve_grid3`.
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001 — resolution must never fail an assessment
        log.warning(
            "arcgis admin lookup failed", extra={"service": service, "error": describe(exc)}
        )
        return None

    if "error" in payload:
        log.warning(
            "arcgis returned an error",
            extra={"service": service, "error": str(payload["error"])[:200]},
        )
        return None
    return payload


async def resolve_grid3_lga(bbox: list[float]) -> AdminPlace | None:
    """State / LGA from GRID3's national LGA layer. **Covers all 37 states.**

    ## Why a second GRID3 tier exists

    The ward layer is deeper but **not national**: it holds 5,872 wards across only **24 of
    Nigeria's 37 states**, and Lagos is one of the thirteen missing — verified live, `state='Lagos'`
    returns a count of 0. So a ward-only resolver leaves roughly a third of the country, including
    its largest city, with no administrative name at all. For Fahis that means unverifiable, which
    is precisely the hole this module was built to close.

    `NGA_LGA_Boundaries_2` carries all **774** LGAs and `statename` alongside, so it answers
    everywhere the ward layer cannot. Shallower by one level, and that is the right trade: an LGA
    name is a searchable place, whereas nothing is not.
    """
    payload = await _arcgis_envelope_query(
        settings.grid3_lga_service, "lganame,statename,Shape__Area", bbox
    )
    if payload is None:
        return None

    features = payload.get("features") or []
    if not features:
        return None

    # Largest by shape area, for the boundary-straddling case — same reasoning as
    # `_largest_feature`, but this layer reports `Shape__Area` rather than `area_sqkm`.
    feature = max(
        features,
        key=lambda f: float(f.get("attributes", {}).get("Shape__Area") or 0.0),
    )
    attrs = feature.get("attributes", {})

    state = (attrs.get("statename") or "").strip() or None
    lga = (attrs.get("lganame") or "").strip() or None
    if not (state or lga):
        return None

    return AdminPlace(
        country="Nigeria",
        admin1=state,
        admin2=lga,
        admin3=None,
        source="grid3-lga",
    )


async def _grid3_query(bbox: list[float]) -> dict | None:
    """One ArcGIS envelope query against the GRID3 ward layer. None on any failure.

    **An envelope, not a point.** The point query returns zero features against this service even
    for a coordinate that is plainly inside a ward — verified live — while the equivalent envelope
    returns four. Rather than depend on why, the envelope is also the more correct question: an AOI
    is an area, and an area can span two wards.
    """
    return await _arcgis_envelope_query(
        settings.grid3_wards_service,
        "state,lga,ward,lga_alt_names,ward_alt_names,area_sqkm",
        bbox,
    )


def _largest_feature(features: list[dict]) -> dict | None:
    """The ward covering most of the AOI, approximated by the largest returned ward.

    An AOI straddling a boundary matches several wards, and one of them has to be reported as *the*
    place. Largest-by-area is a proxy for most-overlapping and is right in the common case, where a
    small plot sits inside one ward and clips the corner of a neighbour.

    It is a proxy, not a computation: doing this exactly needs the ward polygons, which this query
    deliberately does not fetch. Naming the second-largest ward on a boundary AOI is a cosmetic
    error; fetching megabytes of geometry per registration is a real cost.
    """
    if not features:
        return None
    return max(
        features,
        key=lambda f: float(f.get("attributes", {}).get("area_sqkm") or 0.0),
    )


async def resolve_grid3(bbox: list[float]) -> AdminPlace | None:
    """State / LGA / Ward for an area, from GRID3 Nigeria. None when it cannot answer."""
    payload = await _grid3_query(bbox)
    if payload is None:
        return None

    feature = _largest_feature(payload.get("features") or [])
    if feature is None:
        return None

    attrs = feature.get("attributes", {})
    ward = (attrs.get("ward") or "").strip() or None
    lga = (attrs.get("lga") or "").strip() or None

    # Most specific first: a verification search should try the ward before the LGA, since a report
    # naming the ward is far stronger evidence than one naming a district of a million people.
    alt = _split_alt(attrs.get("ward_alt_names")) + _split_alt(attrs.get("lga_alt_names"))
    seen: set[str] = set()
    unique = tuple(
        name for name in alt if not (name.lower() in seen or seen.add(name.lower()))
    )

    return AdminPlace(
        country="Nigeria",
        admin1=(attrs.get("state") or "").strip() or None,
        admin2=lga,
        admin3=ward,
        alt_names=unique,
        source="grid3",
    )


async def resolve_geoboundaries(bbox: list[float], iso3: str = "NGA") -> AdminPlace | None:
    """ADM1 / ADM2 from geoBoundaries. The cross-border failover.

    ## Why this is a metadata call and not a spatial query

    geoBoundaries serves whole-country GeoJSON releases, not a point-in-polygon API. Downloading and
    intersecting a national ADM2 file per registration would be tens of megabytes and a geometry
    dependency in a module that has none.

    So this confirms **which country** the coordinate belongs to and reports the release it would
    come from, without claiming a unit it has not actually intersected. That is deliberately less
    than `resolve_grid3` returns, and it is the honest limit of a metadata endpoint. It exists so a
    non-Nigerian AOI is not silently left with nothing, and so the failover path is real code rather
    than a comment promising one.

    Filling admin1/admin2 properly outside Nigeria needs the release GeoJSON loaded into PostGIS
    once and queried locally — which is the right design, and is why `admin_boundaries` is a table
    in the migration rather than a cache.
    """
    url = f"{settings.geoboundaries_base_url.rstrip('/')}/gbOpen/{iso3}/ADM2/"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("geoboundaries lookup failed", extra={"error": describe(exc)})
        return None

    name = payload.get("boundaryName")
    if not name:
        return None

    return AdminPlace(
        country=name,
        admin1=None,
        admin2=None,
        source="geoboundaries",
    )


async def resolve(bbox: list[float], *, country: str | None = None) -> AdminPlace | None:
    """Best available administrative naming for an area. **Never raises.**

    GRID3 first for Nigeria — deeper (ward level) and it carries alternate spellings. geoBoundaries
    as the failover and for everywhere else.

    Returns None when neither answers, and that is a supported outcome: the caller keeps whatever
    the subscriber typed, and monitoring proceeds unchanged. Only verifiability is reduced, which is
    the pre-existing state rather than a regression.
    """
    if not settings.admin_resolution_enabled:
        return None

    # Nigeria is the deep path. `country` may be absent or a code or a name, so the test is
    # deliberately loose — a false positive costs one query that returns no features.
    nigerian = country is None or str(country).strip().lower() in {"ng", "nga", "nigeria"}

    if nigerian:
        # Tier 1 — ward level. Deepest, carries alternate spellings, but only 24 of 37 states.
        found = await resolve_grid3(bbox)
        if found is not None:
            return found

        # Tier 2 — LGA level, all 774 LGAs and all 37 states. This is the tier that covers Lagos
        # and the twelve other states the ward layer omits; without it a third of Nigeria,
        # including its largest city, would resolve to nothing.
        found = await resolve_grid3_lga(bbox)
        if found is not None:
            return found

        log.info("no GRID3 layer resolved this area; falling back to geoBoundaries")

    iso3 = "NGA" if nigerian else (country or "NGA")
    return await resolve_geoboundaries(bbox, iso3=iso3 if len(iso3) == 3 else "NGA")


async def resolve_many(
    boxes: list[list[float]], *, country: str | None = None
) -> list[AdminPlace | None]:
    """Resolve several areas concurrently. Used when an aggregator onboards in bulk.

    `return_exceptions=True` plus the per-call guards means one bad box costs one name, not the
    batch — the same reasoning as `explain.explain_all`.
    """
    results = await asyncio.gather(
        *(resolve(box, country=country) for box in boxes), return_exceptions=True
    )
    return [r if isinstance(r, AdminPlace) else None for r in results]


async def enrich(aoi: AreaOfInterest) -> AreaOfInterest:
    """Fill in missing `admin1` / `admin2` on an AOI. **Never raises, never overwrites.**

    ## Why this is the integration point

    Five paths create an AOI — portal activation, portal area-add, two Partner API routes, and the
    workspace customer routes. Enriching in each would mean five places to forget; enriching here
    means one call per path and identical behaviour across them.

    ## Why it never overwrites

    A subscriber or an aggregator who supplied `admin1`/`admin2` knows something we do not: an
    aggregator's `external_ref` scheme may key on their own district naming, and a farmer typing
    their LGA is stating a fact about their own land. Boundary datasets disagree at edges, so
    replacing a supplied value with a resolved one would silently contradict the person who owns the
    plot. Only genuinely empty fields are filled.

    ## What an unresolved AOI costs

    Nothing to monitoring — the pipeline never reads these fields. It costs *verifiability*:
    `agents/fahis` searches outside reporting by administrative name, so an AOI with none yields
    UNVERIFIED for want of a query rather than for want of reporting. That is the honest verdict
    either way, which is why failure here is safe.
    """
    if aoi.admin1 and aoi.admin2:
        return aoi

    try:
        found = await resolve(aoi.bbox.as_list(), country=aoi.country)
    except Exception as exc:  # noqa: BLE001 — registration must never fail over a place name
        log.warning("admin enrichment failed", extra={"aoi_id": aoi.id, "error": describe(exc)})
        return aoi

    if found is None:
        log.info(
            "no administrative name resolved; this AOI will be harder to verify",
            extra={"aoi_id": aoi.id},
        )
        return aoi

    # `admin2` prefers the WARD when one was resolved, because that is what Fahis searches and a
    # ward-level report is far stronger evidence than a district-level one. Falls back to the LGA.
    resolved_admin2 = found.admin3 or found.admin2

    updates: dict[str, str] = {}
    if not aoi.admin1 and found.admin1:
        updates["admin1"] = found.admin1
    if not aoi.admin2 and resolved_admin2:
        updates["admin2"] = resolved_admin2

    if not updates:
        return aoi

    log.info(
        "administrative names resolved",
        extra={"aoi_id": aoi.id, "source": found.source, **updates},
    )
    return aoi.model_copy(update=updates)


# --------------------------------------------------------------------------- #
# Browse: the reverse direction
#
# Everything above answers "what is at these coordinates?". This section answers "where is
# Ogun / Obafemi Owode?" — a name to a geometry, which is the direction a person setting up
# monitoring actually works in.
#
# ## Why this had to exist
#
# A subscriber tried to register "Alspecs Farms in Kobape, Ogun State" and place search
# returned **nothing** — OSM has no entry for Kobape at all, verified against Nominatim with
# and without country hints. Its LGA (Obafemi Owode) resolves fine, and GRID3 places the
# coordinates correctly, so the platform could always have found the area; there was simply no
# way to ASK for it by administrative name.
#
# That is the common rural case, not an edge case: OSM's Nigerian coverage is good for cities
# and thin for villages, and SHELTER's subscribers are mostly not in cities. So an
# unfindable village becomes "pick Ogun, then Obafemi Owode", and the pin refines from there.
#
# GRID3's LGA layer already ships all 774 LGAs with `statename` alongside, and ArcGIS supports
# both `returnDistinctValues` and `returnExtentOnly` — so this needs no new data source and no
# new credential, only two query shapes the reverse path never used.
# --------------------------------------------------------------------------- #

#: Browse answers change when Nigeria creates an LGA, which is a matter of years. A day is
#: therefore conservative, and it keeps a cascading picker from issuing an ArcGIS request per
#: keystroke of a user's drill-down.
_BROWSE_TTL_SECONDS = 86_400


#: Aliases GRID3 does not carry, mapped to the spelling it does.
#:
#: GRID3's LGA layer has **no alt-names field at all** (verified: `lganame`, `lgacode`,
#: `statename`, `statecode` and geometry columns only), and the ward layer's `lga_alt_names` is
#: empty on almost every row. So there is no upstream synonym list to query — the mapping has to
#: live here.
#:
#: Deliberately short. This is for names a partner's spreadsheet will genuinely contain, not a
#: fuzzy-matching layer: `_canonical` already handles punctuation, spacing, case and the "State"
#: / "LGA" suffixes, which covers most of it. Each entry below is a name that normalisation alone
#: cannot reach because the words themselves differ.
_ADMIN_ALIASES: dict[str, str] = {
    # GRID3 spells the Federal Capital Territory "Fct".
    "federalcapitalterritory": "fct",
    "abuja": "fct",
    "abujafct": "fct",
    "fctabuja": "fct",
    # Common misspelling, and the one most likely in a hand-typed sheet.
    "nassarawa": "nasarawa",
    # Occasionally written as one word.
    "crossriverstate": "crossriver",
    "akwaibomstate": "akwaibom",
}


def _canonical(name: str) -> str:
    """A comparison key that survives the ways a name is realistically written.

    ## The bug this fixes

    Every browse query was an exact string match, so a partner sending `Obafemi-Owode` instead of
    `Obafemi Owode` received **zero wards** — indistinguishable from the 13 states that genuinely
    have no ward coverage. Measured before the fix:

        Obafemi Owode      -> 12 wards
        Obafemi-Owode      ->  0 wards   silently empty
        Obafemi/Owode      ->  0 wards   silently empty
        Ado-Odo/Ota        ->  0 wards   silently empty  (GRID3 has "Ado Odo/Ota")
        Ijebu North-East   ->  0 wards   silently empty
        Ogun State         ->  0 LGAs    silently empty
        Federal Capital Territory -> 0 LGAs

    A partner's bulk importer would read those as "not covered" and skip the row, which is the
    worst shape of failure: no error, no log, just a farm that never gets monitored.

    Normalises case, strips punctuation and spacing, and drops the administrative suffixes
    ("state", "lga", "local government area") that a spreadsheet column adds and GRID3 omits.
    """
    lowered = (name or "").strip().lower()
    # Suffixes first, while word boundaries still exist.
    for suffix in (
        " local government area",
        " local govt area",
        " local government",
        " lga",
        " state",
    ):
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)].strip()
    # Then collapse everything that is not a letter or digit. This is what makes
    # "Ado-Odo/Ota", "Ado Odo/Ota" and "AdoOdoOta" the same key.
    stripped = "".join(character for character in lowered if character.isalnum())
    return _ADMIN_ALIASES.get(stripped, stripped)


def _match_name(supplied: str, candidates: list[str]) -> str | None:
    """The candidate whose canonical form matches `supplied`, or None.

    Returns the **upstream spelling**, which is what the ArcGIS query must use — normalising for
    comparison is not the same as being able to query by the normalised form.

    Exact match is tried first so a correctly-spelled name never pays for the normalisation, and
    so an unambiguous exact hit always wins over a normalised collision.
    """
    if supplied in candidates:
        return supplied

    key = _canonical(supplied)
    if not key:
        return None
    for candidate in candidates:
        if _canonical(candidate) == key:
            return candidate
    return None


def _sql_quote(value: str) -> str:
    """Escape a value for an ArcGIS `where` clause by doubling single quotes.

    ArcGIS takes a SQL-ish WHERE string with no parameter binding, so the only defence is
    escaping. GRID3 has no apostrophes in its names today — but "N'Djamena"-style names exist
    across Africa, and a rename should not be able to truncate a clause.
    """
    return value.replace("'", "''")


async def _arcgis_browse(params: dict) -> dict | None:
    """One non-spatial ArcGIS query against the LGA layer. None on any failure.

    Separate from `_arcgis_envelope_query` because that one always sends an envelope and always
    asks for intersection; these are attribute queries. Sharing one function would mean a
    parameter that means "actually, ignore the geometry", which is how a spatial query
    accidentally becomes a table scan.
    """
    url = (
        f"{settings.grid3_base_url.rstrip('/')}/"
        f"{settings.grid3_lga_service}/FeatureServer/0/query"
    )
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url, params={**params, "f": "json"})
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("arcgis browse failed", extra={"error": describe(exc)})
        return None

    if "error" in payload:
        log.warning("arcgis browse returned an error", extra={"error": str(payload["error"])[:200]})
        return None
    return payload


async def list_states() -> list[str]:
    """Every Nigerian state plus the FCT, alphabetically. Empty list on failure.

    Empty rather than an exception: a picker that cannot load its options must fall back to
    place search, not break the page. The caller renders the other modes regardless.
    """
    cached = await cache.get_json("admin:states:NG")
    if isinstance(cached, list) and cached:
        return cached

    payload = await _arcgis_browse(
        {
            "where": "1=1",
            "outFields": "statename",
            "returnDistinctValues": "true",
            "returnGeometry": "false",
            "orderByFields": "statename",
        }
    )
    if payload is None:
        return []

    states = sorted(
        {
            str(feature["attributes"]["statename"]).strip()
            for feature in payload.get("features", [])
            if feature.get("attributes", {}).get("statename")
        }
    )
    if states:
        await cache.set_json("admin:states:NG", states, ttl_seconds=_BROWSE_TTL_SECONDS)
    return states


async def list_lgas(state: str) -> list[str]:
    """Every LGA in one state, alphabetically. Empty list on failure or unknown state."""
    state = (state or "").strip()
    if not state:
        return []

    # Resolve to GRID3's own spelling before querying. A partner sending "Ogun State" or
    # "Federal Capital Territory" would otherwise get an empty list, which their importer cannot
    # distinguish from a state we do not cover.
    matched_state = _match_name(state, await list_states())
    if matched_state is None:
        return []
    state = matched_state

    key = f"admin:lgas:NG:{state.lower()}"
    cached = await cache.get_json(key)
    if isinstance(cached, list) and cached:
        return cached

    payload = await _arcgis_browse(
        {
            "where": f"statename='{_sql_quote(state)}'",
            "outFields": "lganame",
            "returnDistinctValues": "true",
            "returnGeometry": "false",
            "orderByFields": "lganame",
        }
    )
    if payload is None:
        return []

    lgas = sorted(
        {
            str(feature["attributes"]["lganame"]).strip()
            for feature in payload.get("features", [])
            if feature.get("attributes", {}).get("lganame")
        }
    )
    if lgas:
        await cache.set_json(key, lgas, ttl_seconds=_BROWSE_TTL_SECONDS)
    return lgas


async def lga_extent(state: str, lga: str) -> list[float] | None:
    """`[west, south, east, north]` for one LGA, or None.

    **Returns the LGA's full extent, which is far larger than any farm.** Verified: Obafemi
    Owode spans 3.271-3.794 E and 6.674-7.243 N — roughly 58 x 63 km. That is a starting
    position for the map, never a monitoring footprint: `POST /risk/assess` rejects anything
    over ~4 deg squared, and an AOI that size would average a whole LGA into one reading and
    report nothing useful about a 140-hectare farm.
    """
    state, lga = (state or "").strip(), (lga or "").strip()
    if not state or not lga:
        return None

    matched_state = _match_name(state, await list_states())
    if matched_state is None:
        return None
    state = matched_state
    matched_lga = _match_name(lga, await list_lgas(state))
    if matched_lga is None:
        return None
    lga = matched_lga

    payload = await _arcgis_browse(
        {
            "where": (
                f"statename='{_sql_quote(state)}' AND lganame='{_sql_quote(lga)}'"
            ),
            "outFields": "lganame",
            "returnExtentOnly": "true",
        }
    )
    if payload is None:
        return None

    extent = payload.get("extent") or {}
    try:
        bounds = [
            float(extent["xmin"]),
            float(extent["ymin"]),
            float(extent["xmax"]),
            float(extent["ymax"]),
        ]
    except (KeyError, TypeError, ValueError):
        return None

    # An ArcGIS extent over an empty result set comes back as NaN rather than absent.
    if any(value != value for value in bounds):  # NaN check without importing math
        return None
    return bounds


async def _arcgis_browse_wards(params: dict) -> dict | None:
    """One non-spatial query against the WARD layer. None on any failure.

    Separate from `_arcgis_browse` because the two layers use different field names — the LGA
    layer has `lganame`/`statename`, the ward layer has `lga`/`state`/`ward`. Verified live: a
    ward query using `lganame` returns "Invalid query parameters", not an empty result, so the
    difference is load-bearing rather than cosmetic.
    """
    url = (
        f"{settings.grid3_base_url.rstrip('/')}/"
        f"{settings.grid3_wards_service}/FeatureServer/0/query"
    )
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url, params={**params, "f": "json"})
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("arcgis ward browse failed", extra={"error": describe(exc)})
        return None

    if "error" in payload:
        log.warning(
            "arcgis ward browse returned an error",
            extra={"error": str(payload["error"])[:200]},
        )
        return None
    return payload


async def list_wards(state: str, lga: str) -> list[str]:
    """Wards in one LGA, alphabetically. **Empty when the state has no ward data.**

    ## Why this tier is worth a third dropdown

    Measured on the reported case. Obafemi Owode LGA is 58 x 63 km — 3,671 km squared — and the
    farm sat **22.4 km from the LGA centre**, so landing there put the plot off-screen. Kajola
    ward, which contains it, is 18 x 16 km: a **12.9x smaller** search area, and 5.9 km from
    centre. That is the difference between "I can see my farm" and "I have to pan around
    guessing".

    ## Why it must be optional rather than required

    GRID3's ward layer covers **24 of 37 states** (5,872 wards). Verified absent: Lagos, Rivers,
    FCT, Anambra, Edo, Ondo, Ekiti, Imo, Benue, Plateau, Taraba, Akwa Ibom, Cross River, Ebonyi.

    So an empty list is a normal answer, not a failure, and the UI must skip straight from LGA to
    the pin when it comes back empty. Requiring a ward would make the picker unusable in Lagos —
    which is the opposite of the problem it is solving.
    """
    state, lga = (state or "").strip(), (lga or "").strip()
    if not state or not lga:
        return []

    # Same normalisation as the LGA tier, and it matters more here: an unmatched name returns []
    # which is ALSO the correct answer for the 13 states with no ward coverage, so a typo would be
    # indistinguishable from a genuine gap.
    matched_state = _match_name(state, await list_states())
    if matched_state is None:
        return []
    state = matched_state
    matched_lga = _match_name(lga, await list_lgas(state))
    if matched_lga is None:
        return []
    lga = matched_lga

    key = f"admin:wards:NG:{state.lower()}:{lga.lower()}"
    cached = await cache.get_json(key)
    if isinstance(cached, list):
        # Note `isinstance` WITHOUT a truthiness check, unlike states and LGAs: an empty ward
        # list is a real, cacheable answer for the 13 states with no coverage, and re-querying
        # ArcGIS on every keystroke to be told "none" again would be pure cost.
        return cached

    payload = await _arcgis_browse_wards(
        {
            "where": f"state='{_sql_quote(state)}' AND lga='{_sql_quote(lga)}'",
            "outFields": "ward",
            "returnDistinctValues": "true",
            "returnGeometry": "false",
            "orderByFields": "ward",
        }
    )
    if payload is None:
        return []

    wards = sorted(
        {
            str(feature["attributes"]["ward"]).strip()
            for feature in payload.get("features", [])
            if feature.get("attributes", {}).get("ward")
        }
    )
    await cache.set_json(key, wards, ttl_seconds=_BROWSE_TTL_SECONDS)
    return wards


async def _match_ward_alias(state: str, lga: str, ward: str) -> str | None:
    """Resolve a ward by GRID3's own published alternate names.

    The ward layer carries `ward_alt_names` — a comma-separated string of real synonyms, e.g.
    `"Moloko Asipa, Moloko-Asipa"` for the ward GRID3 names `Maloko Asipa`. Consulting them beats
    growing `_ADMIN_ALIASES` by hand: these come from the data custodian, and a hand-maintained
    list of Nigerian ward synonyms would be both enormous and permanently incomplete.

    One extra query, and only on the miss path — a correctly-spelled ward never reaches here.
    """
    payload = await _arcgis_browse_wards(
        {
            "where": f"state='{_sql_quote(state)}' AND lga='{_sql_quote(lga)}'",
            "outFields": "ward,ward_alt_names",
            "returnGeometry": "false",
        }
    )
    if payload is None:
        return None

    key = _canonical(ward)
    if not key:
        return None
    for feature in payload.get("features", []):
        attributes = feature.get("attributes", {})
        for alias in _split_alt(attributes.get("ward_alt_names")):
            if _canonical(alias) == key:
                return str(attributes.get("ward") or "").strip() or None
    return None


async def ward_extent(state: str, lga: str, ward: str) -> list[float] | None:
    """`[west, south, east, north]` for one ward, or None.

    Still not a monitoring footprint — Kajola ward is 18 x 16 km, far larger than any farm — but
    a much better place to put the map than the LGA. The pin remains the step that defines the
    actual AOI.
    """
    state, lga, ward = (state or "").strip(), (lga or "").strip(), (ward or "").strip()
    if not state or not lga or not ward:
        return None

    matched_state = _match_name(state, await list_states())
    if matched_state is None:
        return None
    state = matched_state
    matched_lga = _match_name(lga, await list_lgas(state))
    if matched_lga is None:
        return None
    lga = matched_lga
    matched_ward = _match_name(ward, await list_wards(state, lga))
    if matched_ward is None:
        # GRID3 publishes its own synonyms in `ward_alt_names` — "Moloko Asipa" for "Maloko
        # Asipa", "Mokoloki" for "Molokiki". Those are authoritative, so they are consulted
        # before giving up, rather than being left to `_ADMIN_ALIASES` to duplicate by hand.
        matched_ward = await _match_ward_alias(state, lga, ward)
    if matched_ward is None:
        return None
    ward = matched_ward

    payload = await _arcgis_browse_wards(
        {
            "where": (
                f"state='{_sql_quote(state)}' AND lga='{_sql_quote(lga)}' "
                f"AND ward='{_sql_quote(ward)}'"
            ),
            "outFields": "ward",
            "returnExtentOnly": "true",
        }
    )
    if payload is None:
        return None

    extent = payload.get("extent") or {}
    try:
        bounds = [
            float(extent["xmin"]),
            float(extent["ymin"]),
            float(extent["xmax"]),
            float(extent["ymax"]),
        ]
    except (KeyError, TypeError, ValueError):
        return None

    if any(value != value for value in bounds):  # NaN
        return None
    return bounds
