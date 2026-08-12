"""Type-ahead (Photon) and GeoParquet reads (Overture) — the two new upstreams.

## Why these are tested together

Both were added for the same review feedback and both are **optional by construction**: absent
configuration disables the feature and every caller degrades. That property is the thing most
likely to rot, because the failure is invisible — a disabled feature and a broken one both return
an empty list, and the difference only shows up as a user being told "no results" for a query that
would have matched.

So the assertions here are mostly about *distinguishing absence from emptiness*, which is the same
"absent is not zero" discipline `_exposure_term` enforces on the risk side.
"""

from __future__ import annotations

import httpx
import pytest

from app.eo import suggest, vector
from app.models.schemas import BBox

KANO = BBox(west=8.47, south=11.95, east=8.57, north=12.05)

#: A real Photon response shape, captured from a live instance.
PHOTON_BODY = {
    "features": [
        {
            "properties": {
                "name": "Argungu",
                "osm_value": "town",
                "state": "Kebbi",
                "county": "Argungu",
                "countrycode": "NG",
            },
            "geometry": {"type": "Point", "coordinates": [4.5266, 12.7477]},
        },
        {
            "properties": {
                "name": "Argungu Road",
                "osm_value": "secondary",
                "city": "Argungu",
                "state": "Kebbi",
                "countrycode": "NG",
            },
            "geometry": {"type": "Point", "coordinates": [4.53, 12.75]},
        },
        {
            "properties": {
                "osm_value": "farmland",
                "city": "Argungu",
                "state": "Kebbi",
                "countrycode": "NG",
            },
            "geometry": {"type": "Point", "coordinates": [4.54, 12.76]},
        },
    ]
}


def _mock_photon(monkeypatch, body, *, status: int = 200):
    async def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, text="error")
        return httpx.Response(200, json=body)

    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(suggest.httpx, "AsyncClient", factory)
    monkeypatch.setattr(suggest.settings, "photon_url", "http://photon.test", raising=False)

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(suggest.cache, "get_text", _none)
    monkeypatch.setattr(suggest.cache, "set_text", _none)


# --------------------------------------------------------------------------- #
# Photon — off by default, and honest about being off
# --------------------------------------------------------------------------- #


def test_type_ahead_is_disabled_without_a_url(monkeypatch):
    """**Ships dark.** No `PHOTON_URL` means no feature, not a broken form.

    The public Photon instance measured **23 seconds** per query, so self-hosting is mandatory —
    which means the deployment may legitimately not have one yet. The UI falls back to the
    existing debounced Nominatim search.
    """
    monkeypatch.setattr(suggest.settings, "photon_url", "", raising=False)
    assert suggest.available() is False


async def test_a_disabled_instance_makes_no_request(monkeypatch):
    """Not merely empty — it must not call out at all.

    Two traps here, both of which made an earlier version of this test pass against a *removed*
    guard — found by mutation testing, not by reading:

      * **The cache answers first.** `cache.get_text` returned a value from a prior run before the
        client was ever constructed, so the test asserted the cache was warm rather than that the
        guard existed. Both cache calls must be stubbed.
      * **`suggest` catches `Exception`, which includes `AssertionError`.** Raising from a patched
        `AsyncClient` therefore proves nothing: the adapter swallows it and returns `[]`, which is
        exactly the expected value. So the fact of construction is *recorded* and asserted
        afterwards, outside the adapter's reach.
    """
    monkeypatch.setattr(suggest.settings, "photon_url", "", raising=False)

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(suggest.cache, "get_text", _none)
    monkeypatch.setattr(suggest.cache, "set_text", _none)

    constructed: list[bool] = []
    real = httpx.AsyncClient

    def record(*args, **kwargs):
        constructed.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(suggest.httpx, "AsyncClient", record)

    assert await suggest.suggest("Argun") == []
    assert not constructed, "a disabled suggester must not open a client at all"


async def test_available_is_reported_separately_from_the_results(monkeypatch):
    """**The distinction the route exists to publish.**

    An empty list means the same thing whether the feature is off or the query matched nothing.
    Telling a user their query found nothing because Photon is not deployed is a lie, so
    `SuggestResponse.available` carries the difference and the UI branches on it.
    """
    monkeypatch.setattr(suggest.settings, "photon_url", "", raising=False)
    assert suggest.available() is False

    monkeypatch.setattr(suggest.settings, "photon_url", "http://photon.test", raising=False)
    assert suggest.available() is True


async def test_suggestions_are_ranked_by_what_someone_would_monitor(monkeypatch):
    """Farmland and settlements above a road that shares the name.

    Ranking only — an unranked kind sorts last rather than vanishing. Guessing wrong about what
    someone wants to monitor should cost them a scroll, not a result.
    """
    _mock_photon(monkeypatch, PHOTON_BODY)
    results = await suggest.suggest("Argun")

    kinds = [s.kind for s in results]
    assert kinds.index("farmland") < kinds.index("town") < kinds.index("secondary")


async def test_label_and_detail_are_separate_and_deduplicated(monkeypatch):
    """Nigeria has a Kajola in several states.

    One combined string makes them look identical at a glance, which is precisely when someone
    picks the wrong one and monitors a field 300 km away. Photon also repeats a value across
    `city` and `county` for small settlements, so "Argungu, Argungu, Kebbi" reads like a bug.
    """
    _mock_photon(monkeypatch, PHOTON_BODY)
    results = await suggest.suggest("Argun")

    town = next(s for s in results if s.kind == "town")
    assert town.label == "Argungu"
    assert town.detail == "Kebbi", "the name must not repeat inside its own context"


async def test_an_unnamed_feature_still_renders(monkeypatch):
    """Photon returns nameless features (a numbered building). A blank row is not acceptable."""
    _mock_photon(monkeypatch, PHOTON_BODY)
    results = await suggest.suggest("Argun")

    nameless = next(s for s in results if s.kind == "farmland")
    assert nameless.label, "must fall back to the most specific context available"
    assert nameless.label != ""


async def test_a_malformed_feature_does_not_discard_the_others(monkeypatch):
    """One bad row in a list of ten must cost one row."""
    body = {"features": [*PHOTON_BODY["features"], {"properties": {}, "geometry": {}}]}
    _mock_photon(monkeypatch, body)

    results = await suggest.suggest("Argun")
    assert len(results) == 3


async def test_an_unreachable_photon_degrades_silently(monkeypatch):
    """Logged at DEBUG, not WARNING, and this is deliberate.

    This sits behind a keystroke, so one outage would emit a log line per character typed by every
    user on the platform — a log that drowns the ones that matter.
    """
    _mock_photon(monkeypatch, None, status=503)
    assert await suggest.suggest("Argun") == []


@pytest.mark.parametrize("query", ["", "A", " "])
async def test_a_too_short_query_costs_nothing(monkeypatch, query):
    """Two characters, against three for full search — a prefix index can rank "Ka" usefully."""
    _mock_photon(monkeypatch, PHOTON_BODY)
    assert await suggest.suggest(query) == []


async def test_the_result_count_is_capped_by_config(monkeypatch):
    """A caller cannot ask for more than the deployment allows.

    `limit=50` from a client must not become 50 upstream requests' worth of payload — the ceiling
    belongs to the operator, not the caller.
    """
    captured: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.url.params)
        return httpx.Response(200, json=PHOTON_BODY)

    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(suggest.httpx, "AsyncClient", factory)
    monkeypatch.setattr(suggest.settings, "photon_url", "http://photon.test", raising=False)
    monkeypatch.setattr(suggest.settings, "photon_max_results", 2, raising=False)
    monkeypatch.setattr(suggest.cache, "get_text", _none)
    monkeypatch.setattr(suggest.cache, "set_text", _none)

    await suggest.suggest("Argun", limit=50)
    assert captured["limit"] == "2"


async def test_suggestions_are_restricted_to_the_configured_countries(monkeypatch):
    """**The proximity bias alone is not enough**, measured against the live instance.

    Photon's `lat`/`lon` are a preference, not a filter. Typing "Argun" returned a village in
    Uzbekistan, two in Türkiye and a town in Chechnya — all ahead of Argungu, Kebbi — because exact
    name-similarity outranks proximity. A farmer offered four foreign places concludes the search is
    broken.

    `countrycode=ng` fixes it outright: the same query then returns Argungu / Argungu Native /
    Argungu Road. Sent as a request parameter rather than filtered locally, so the limit is spent on
    results that can actually be used.
    """
    captured: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.url.params)
        return httpx.Response(200, json=PHOTON_BODY)

    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(suggest.httpx, "AsyncClient", factory)
    monkeypatch.setattr(suggest.settings, "photon_url", "http://photon.test", raising=False)
    monkeypatch.setattr(suggest.settings, "photon_countries", "ng", raising=False)
    monkeypatch.setattr(suggest.cache, "get_text", _none)
    monkeypatch.setattr(suggest.cache, "set_text", _none)

    await suggest.suggest("Argun")
    assert captured.get("countrycode") == "ng"

    # Empty must search globally — the escape hatch for an aggregator with cross-border customers.
    captured.clear()
    monkeypatch.setattr(suggest.settings, "photon_countries", "", raising=False)
    await suggest.suggest("Argun")
    assert "countrycode" not in captured


def test_suggestions_carry_no_geometry():
    """**A safety property, not tidiness.**

    A `Suggestion` is a label plus coordinates to frame a map. Nothing on it is sufficient to build
    an AOI — selecting one triggers a real resolution through `places.search`, which is where the
    outline and admin hierarchy come from. If this type could produce an AOI, a partially typed
    string could too.
    """
    fields = set(suggest.Suggestion.__slots__)
    assert not fields & {"ring", "bbox", "geometry", "hectares"}


def test_attribution_is_shared_with_places_not_duplicated():
    """Photon serves OSM data, so the ODbL condition is identical.

    Two copies of a licence string is how one of them goes stale.
    """
    from app.eo import places

    assert suggest.ATTRIBUTION is places.ATTRIBUTION


# --------------------------------------------------------------------------- #
# Overture / GeoParquet — the first vector source
# --------------------------------------------------------------------------- #


def test_vector_reads_need_both_a_url_and_duckdb(monkeypatch):
    """Either alone is a misconfiguration that would otherwise look like an empty result.

    A URL with no DuckDB reads nothing; DuckDB with no URL has nothing to read.
    """
    monkeypatch.setattr(vector.settings, "overture_release_url", "", raising=False)
    assert vector.available() is False

    monkeypatch.setattr(
        vector.settings, "overture_release_url", "s3://x/release/y", raising=False
    )
    # DuckDB is in requirements, so this should now be true; if it is ever made truly optional
    # and removed, this asserts the check is the reason rather than the URL.
    from importlib.util import find_spec

    assert vector.available() is (find_spec("duckdb") is not None)


async def test_a_disabled_vector_source_returns_unknown_not_zero(monkeypatch):
    """**The "absent is not zero" invariant, at its most consequential.**

    `CommercialProfile()` defaults `available=False` with `total=0`. An empty POI count read as
    "no commercial activity here" would be a fabricated input to a credit decision — a wrongly
    declined loan. The flag is what keeps unknown distinguishable from measured-zero.
    """
    monkeypatch.setattr(vector.settings, "overture_release_url", "", raising=False)

    profile = await vector.commercial_profile(KANO)
    assert profile.available is False
    assert profile.total == 0
    assert profile.categories == {}


async def test_a_genuine_zero_is_distinguishable_from_an_outage(monkeypatch):
    """Farmland with no businesses is a real measurement and must report `available=True`.

    `available` keys on the query SUCCEEDING, never on finding anything — which is why it is
    carried rather than inferred from `total > 0`.
    """
    monkeypatch.setattr(vector, "available", lambda: True)
    monkeypatch.setattr(vector, "_run", lambda query: [])

    profile = await vector.commercial_profile(KANO)
    assert profile.available is True
    assert profile.total == 0


async def test_categories_are_passed_through_unmapped(monkeypatch):
    """Overture's own taxonomy, not ours.

    Inventing a grouping would be a judgement call hidden inside a number; the raw category is
    what a reviewer can check. Null categories are counted in the total but not given a name —
    Overture leaves `basic_category` null on a substantial minority of records (452 of ~1,300 over
    the Kano test AOI, measured).
    """
    monkeypatch.setattr(vector, "available", lambda: True)
    monkeypatch.setattr(
        vector,
        "_run",
        lambda query: [("restaurant", 85, 40), (None, 452, 100), ("bank", 12, 12)],
    )

    profile = await vector.commercial_profile(KANO)
    assert profile.total == 549, "null-category rows still count toward the total"
    assert profile.categories == {"restaurant": 85, "bank": 12}
    assert profile.contactable == 152


async def test_a_slow_query_yields_unknown_rather_than_extending_a_scan(monkeypatch):
    """Buildings measured **~110 s** per AOI; places ~12 s.

    This is a *contextual* signal — nothing in the hazard path depends on it — so a stalled public
    bucket must not hold a scan open. The Analyst is already the slow stage.
    """
    import asyncio

    monkeypatch.setattr(vector, "available", lambda: True)
    monkeypatch.setattr(vector.settings, "overture_query_timeout_seconds", 0.05, raising=False)

    def slow(query):
        import time

        time.sleep(1.0)
        return [("restaurant", 1, 1)]

    monkeypatch.setattr(vector, "_run", slow)

    profile = await asyncio.wait_for(vector.commercial_profile(KANO), timeout=5.0)
    assert profile.available is False, "a timeout is unknown, not an empty district"


async def test_a_failing_query_degrades_rather_than_raising(monkeypatch):
    """Every adapter in `app/eo/` degrades. A missing POI count must never fail a scan."""
    monkeypatch.setattr(vector, "available", lambda: True)

    def boom(query):
        raise RuntimeError("parquet is on fire")

    monkeypatch.setattr(vector, "_run", boom)

    profile = await vector.commercial_profile(KANO)
    assert profile.available is False


def test_the_commercial_profile_is_not_an_exposure_field():
    """**A boundary, asserted structurally.**

    `ExposureSummary` feeds `_exposure_term` and therefore severity. A commercial-activity count
    is a Track 4 (credit) signal and must not silently become a risk-score input — the same reason
    `Track` and `HazardType` are separate enums. Keeping it in its own type is what makes that
    reviewable.
    """
    from app.models.schemas import ExposureSummary

    exposure_fields = set(ExposureSummary.model_fields)
    profile_fields = {"total", "categories", "contactable"}
    assert not exposure_fields & profile_fields


def test_buildings_are_kept_off_the_request_path():
    """Measured ~110 s per AOI, against ~12 s for places.

    Named as a constant rather than left as a comment so a future caller reaching for buildings
    inside `exposure_for` has to read the reason first.
    """
    assert vector.POI_ONLY_IN_REQUEST_PATH is True


def test_overture_addresses_are_not_used_anywhere():
    """**Measured: `theme=addresses` covers 39 countries and none are in Africa.**

    Zero records over a Lagos bbox, and the country list is US/BR/MX/FR/IT/JP/DE/CA/AU/ES/… So
    Overture cannot verify a Nigerian address, and a future change that wires it up for
    "address verification across Africa" would be building on nothing. Nominatim structured search
    plus the `polygon_geojson` outline is what actually serves that need.
    """
    import inspect

    source = inspect.getsource(vector)
    # Referenced in the docstring as a documented absence; must not appear in a query.
    assert 'theme=addresses' not in source.split('"""')[-1], (
        "Overture has no African address coverage; see the module docstring"
    )
