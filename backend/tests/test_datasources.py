"""Data-source plumbing: catalogues, signing, geometry and classification.

Everything here is pure logic — no network. The point is that the parts which
silently produce wrong answers (a mismatched collection ID, an unsigned href,
a fabricated area) now fail loudly in a test instead.
"""

from __future__ import annotations

import json

import pytest

from app.eo import auth, soil
from app.eo.catalogs import COPERNICUS, ELEMENT84, PLANETARY, AuthMode, Product, chain_for
from app.eo.geometry import area_hectares, bbox_geojson
from app.models.schemas import BBox, SoilProfile

# Roughly the Argungu rice plots in Kebbi State.
KEBBI = BBox(west=4.42, south=12.65, east=4.63, north=12.84)


# --------------------------------------------------------------------------- #
# Catalogues — the silent-failure bug
# --------------------------------------------------------------------------- #


def test_copernicus_serves_no_sentinel_data():
    """**Verified live 2026-08-10, and it contradicts what this file used to assert.**

    `catalogue.dataspace.copernicus.eu/stac/collections` returns ten collections —
    `ccm-optical`, `ccm-sar` and eight CLMS burnt-area products. A Sentinel search returns
    `HTTP 400 CollectionInQuerryDoesNotExist`. There is no upper-case `SENTINEL-2` collection;
    the previous assertion encoded a mapping that never worked.

    So the Copernicus Sentinel IDs are empty, which makes `chain_for` skip the rung — removing a
    guaranteed-failing request from every scan while leaving the rung declared and configurable.
    The test asserts the SKIP, because that is the behaviour that matters.

    Sentinel access at Copernicus is via OData/OpenSearch — a different protocol, separate
    adapter, not built. See docs/eo-smoke-test-2026-08-10.md.
    """
    assert not COPERNICUS.collection_for(Product.S2)
    assert not COPERNICUS.collection_for(Product.S1)
    # And therefore it is absent from the chains it cannot answer.
    assert COPERNICUS not in chain_for(Product.S2)
    assert COPERNICUS not in chain_for(Product.S1)
    # The catalogues that DO answer are unaffected.
    assert ELEMENT84.collection_for(Product.S2) == "sentinel-2-l2a"


def test_the_imagery_chain_still_has_redundancy_without_copernicus():
    """Dropping a dead rung must not leave a single point of failure.

    Element84 and Planetary both answer for Sentinel-2 (verified: HTTP 200, scenes returned), so
    the chain keeps two working links. If this ever falls to one, imagery discovery — the only
    non-degrading stage in the pipeline — has no fallback at all.
    """
    for product in (Product.S2, Product.S1):
        chain = chain_for(product)
        assert len(chain) >= 2, (
            f"{product.value} must keep at least two catalogues; imagery discovery cannot degrade"
        )


def test_every_product_has_at_least_one_catalogue():
    for product in Product:
        assert chain_for(product), f"no catalogue serves {product.value}"


def test_chain_skips_catalogues_that_do_not_serve_the_product():
    """WorldCover is Planetary-only; the chain must not include catalogues
    that would 404 on it."""
    chain = chain_for(Product.WORLDCOVER)
    assert all(c.collection_for(Product.WORLDCOVER) for c in chain)
    assert PLANETARY in chain


def test_band_maps_differ_between_catalogues():
    """`red` on Element84 is `B04` elsewhere — reading the wrong key yields no
    assets rather than an error."""
    assert ELEMENT84.band_map["red"] == "red"
    assert COPERNICUS.band_map["red"] == "B04"


def test_planetary_is_marked_as_needing_signing():
    assert PLANETARY.auth is AuthMode.SAS_SIGNED
    assert ELEMENT84.auth is AuthMode.ANONYMOUS


# --------------------------------------------------------------------------- #
# SAS signing — the 403 bug
# --------------------------------------------------------------------------- #


def test_sign_href_appends_token():
    signed = auth.sign_href("https://x.blob.core.windows.net/a/b.tif", "se=2026&sig=ab")
    assert signed == "https://x.blob.core.windows.net/a/b.tif?se=2026&sig=ab"


def test_sign_href_preserves_existing_query():
    signed = auth.sign_href("https://x/b.tif?foo=1", "se=2026&sig=ab")
    assert signed == "https://x/b.tif?foo=1&se=2026&sig=ab"


def test_sign_href_does_not_double_sign():
    already = "https://x/b.tif?se=2026&sig=ab"
    assert auth.sign_href(already, "se=2027&sig=cd") == already


def test_sign_href_without_token_is_identity():
    assert auth.sign_href("https://x/b.tif", None) == "https://x/b.tif"


# --------------------------------------------------------------------------- #
# Geometry — replacing the fabricated area
# --------------------------------------------------------------------------- #


def test_area_hectares_is_physically_plausible():
    """~0.21° x ~0.19° near 12.7°N is roughly 22.8 km x 21.0 km ≈ 48,000 ha."""
    hectares = area_hectares(KEBBI)
    assert 40_000 < hectares < 56_000


def test_area_shrinks_with_latitude():
    """Longitude degrees narrow towards the poles; a naive degree-squared area
    would report these two as identical."""
    equator = BBox(west=0, south=0, east=1, north=1)
    high = BBox(west=0, south=60, east=1, north=61)
    assert area_hectares(high) < area_hectares(equator) * 0.6


def test_area_of_degenerate_bbox_is_not_negative():
    tiny = BBox(west=0, south=0, east=0.0001, north=0.0001)
    assert area_hectares(tiny) >= 0


def test_bbox_geojson_is_valid_closed_polygon():
    parsed = json.loads(bbox_geojson(KEBBI))
    ring = parsed["geometry"]["coordinates"][0]
    assert parsed["type"] == "Feature"
    assert parsed["geometry"]["type"] == "Polygon"
    assert len(ring) == 5
    assert ring[0] == ring[-1], "polygon ring must close"


# --------------------------------------------------------------------------- #
# Soil classification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("clay", "sand", "expected"),
    [
        (420.0, 200.0, "impeded"),
        (150.0, 700.0, "free"),
        (250.0, 400.0, "moderate"),
    ],
)
def test_drainage_classification(clay, sand, expected):
    assert soil._classify(clay, sand) == expected


def test_impeded_soil_prolongs_waterlogging():
    assert SoilProfile(drainage="impeded").waterlogging_multiplier > 1.0
    assert SoilProfile(drainage="free").waterlogging_multiplier < 1.0


def test_unknown_soil_multiplier_is_exactly_neutral():
    """A missing lookup must not nudge the score in either direction."""
    assert SoilProfile().waterlogging_multiplier == 1.0


def test_soilgrids_extract_applies_scaling_factor():
    """SoilGrids returns integers scaled by d_factor; forgetting to divide
    reports clay 10x too high and misclassifies every soil as impeded."""
    payload = {
        "properties": {
            "layers": [
                {
                    "name": "clay",
                    "unit_measure": {"d_factor": 10},
                    "depths": [{"values": {"mean": 3200}}],
                }
            ]
        }
    }
    assert soil._extract(payload, "clay") == pytest.approx(320.0)


def test_soilgrids_extract_missing_property_is_none():
    assert soil._extract({"properties": {"layers": []}}, "clay") is None


# --------------------------------------------------------------------------- #
# Source registry contract
#
# `app/eo/sources.py` declares every upstream as data so Scout can decide what is
# due and /health can report what is live. These tests keep that declaration from
# drifting from the code and config it describes — the same failure mode
# test_config.py eliminated for settings and test_schema_contract.py for tables.
# --------------------------------------------------------------------------- #


def test_every_declared_setting_key_exists():
    """A registry naming a setting that does not exist is worse than no registry:
    it reads as configured and silently never applies."""
    from app.config import Settings
    from app.eo import sources

    unknown: list[str] = []
    for source in sources.SOURCES:
        for key in source.settings_keys:
            if key not in Settings.model_fields:
                unknown.append(f"{source.key} -> {key}")
        if source.credential_key and source.credential_key not in Settings.model_fields:
            unknown.append(f"{source.key} -> {source.credential_key} (credential)")

    assert not unknown, f"registry names settings that do not exist: {unknown}"


def test_credential_keys_are_actually_credentials():
    """Guards a subtle mislabel: pointing `credential_key` at a non-secret would
    make a keyless source look gated, and it would be skipped for no reason."""
    from app.config import Settings
    from app.eo import sources

    suffixes = ("_key", "_token", "_secret", "_password")
    for source in sources.SOURCES:
        if source.credential_key is None:
            continue
        assert source.credential_key.endswith(suffixes), (
            f"{source.key} declares {source.credential_key!r} as its credential, "
            "but that name does not look like a secret"
        )
        # And a credential must default to None, or "configured" is always true.
        assert Settings.model_fields[source.credential_key].default is None


def test_every_chain_has_a_keyless_primary():
    """The architecture's central promise: the pipeline runs end-to-end on
    `.env.example` unchanged.

    If any chain's only live link needed a credential, a default install would
    lose that whole signal — for rainfall that means the Oracle's entire
    forward-looking term.
    """
    from app.eo import sources

    for kind in sources.Kind:
        chain = sources.for_kind(kind)
        assert chain, f"no sources declared for {kind.value}"
        keyless = [s for s in chain if s.credential_key is None]
        assert keyless, (
            f"the {kind.value} chain has no keyless source — a default install "
            "would lose this signal entirely"
        )


def test_failover_targets_exist_and_do_not_cycle():
    """A `falls_back_to` naming a missing source, or pointing back up its own
    chain, would loop or dead-end at runtime."""
    from app.eo import sources

    for source in sources.SOURCES:
        target = source.falls_back_to
        if target is None:
            continue
        assert target in sources.BY_KEY, (
            f"{source.key} falls back to {target!r}, which is not a declared source"
        )
        # Walk the chain to a terminus, bounded by the source count.
        seen = {source.key}
        cursor = target
        for _ in range(len(sources.SOURCES)):
            if cursor is None:
                break
            assert cursor not in seen, f"failover cycle through {cursor!r}"
            seen.add(cursor)
            cursor = sources.BY_KEY[cursor].falls_back_to


def test_static_products_are_not_repolled_every_cycle():
    """Terrain, soil and land cover do not change between satellite passes.

    Re-reading them on a 6-hour cadence is pure waste — it is the cost the
    stateful Scout exists to avoid, so the intent has to be recorded here.
    """
    from app.eo import sources

    for key in ("copernicus-dem", "soilgrids", "isda-soil-texture", "worldcover", "malaria-atlas"):
        source = sources.BY_KEY[key]
        assert source.min_interval_hours >= 168, (
            f"{key} is effectively static but declares a "
            f"{source.min_interval_hours}h re-poll floor"
        )


def test_registry_covers_every_documented_data_source():
    """The registry must not lag the adapters.

    Each of these has a read path in `app/eo/`; a new adapter without a registry entry would be
    invisible to Scout's scheduling and to /health.

    The list is EXPLICIT rather than derived, because the point is to notice when someone adds an
    adapter — a self-deriving test would approve the drift it exists to catch. It failed to do that
    once already: JRC Global Surface Water was read on every radar leg for weeks while this test
    passed, because the hardcoded set simply did not mention it and set-equality against a stale
    list is only ever as good as the list.

    `test_every_stac_product_has_a_registry_entry` below closes that specific hole from the other
    direction — it derives from `Product`, so a new *product* cannot be forgotten.
    """
    from app.eo import sources

    expected = {
        "element84", "copernicus", "planetary", "landsat",
        "gfs-forecast", "gpm-imerg", "climateserv-chirps", "era5",
        "worldpop", "worldcover", "copernicus-dem", "jrc-gsw", "openstreetmap",
        "soilgrids", "isda-soil-texture", "smap-l3", "malaria-atlas",
    }
    assert set(sources.BY_KEY) == expected


def test_every_stac_product_has_a_registry_entry():
    """**Derived, so it cannot go stale.**

    `test_registry_covers_every_documented_data_source` above is a hardcoded list, and a hardcoded
    list is exactly how `jrc-gsw` stayed unregistered while being read on every radar leg. This
    asserts the same property from the direction that cannot be forgotten: every `Product` the STAC
    layer can search must be reachable from some registered source's `settings_keys`.

    A new `Product` with no registry entry fails here without anyone remembering to edit a set.
    """
    from app.config import Settings
    from app.eo import catalogs, sources

    declared_settings = {k for src in sources.SOURCES for k in src.settings_keys}
    real_settings = set(Settings.model_fields)

    for product in catalogs.Product:
        # The setting that names this product's collection, by convention.
        candidates = {f"collection_{product.value.lower()}"} & real_settings
        if not candidates:
            # Not every product is configured by a `collection_*` setting — skip those rather than
            # asserting a naming convention the code does not actually follow.
            continue
        assert candidates & declared_settings, (
            f"{product.value} is searchable but no registered source declares its collection "
            "setting — it would be invisible to Scout's scheduling and to /health, which is how "
            "jrc-gsw went unregistered while being read on every radar leg"
        )


def test_registry_declares_no_network_dependency():
    """Declaration only — no HTTP, no rasterio.

    Importing the registry must be free, because `/health`, the tests and Scout's
    scheduler all read it, and `app/eo/geometry.py` exists for exactly this reason:
    to keep the risk layer importable without GDAL.
    """
    import pathlib

    source = pathlib.Path("app/eo/sources.py").read_text()

    for forbidden in ("import httpx", "import rasterio", "from app.eo import cog"):
        assert forbidden not in source, f"sources.py must not {forbidden!r}"


def test_gdal_cachemax_is_an_int_not_a_string():
    """**The bug that made every COG read fail.**

    rasterio special-cases `GDAL_CACHEMAX` and passes it to GDAL as a number, so a string
    raises `TypeError: an integer is required` inside `rasterio.Env(**_GDAL_ENV)` — before any
    network call, and therefore on every band of every scene.

    The symptom was maximally misleading: the Analyst caught the failure and reported "No usable
    satellite imagery was available this cycle", which is its correct behaviour for a genuinely
    cloudy or unavailable scene. So a totally broken read path looked like a working pipeline
    finding nothing, and every assessment came back score 0.0 / confidence 0.0.

    Every other key in that dict is a real string option, which is why it read as uniform.
    """
    from app.eo.cog import _GDAL_ENV

    assert isinstance(_GDAL_ENV["GDAL_CACHEMAX"], int), (
        "GDAL_CACHEMAX must be an int — rasterio.Env rejects a string and every read fails"
    )

    # And the whole dict must actually be accepted by rasterio, which is the real contract.
    # Skipped rather than failed where rasterio is absent: the exporter and most unit tests
    # deliberately run without GDAL installed.
    rasterio = pytest.importorskip("rasterio")
    with rasterio.Env(**_GDAL_ENV):
        pass


def test_sentinel1_uses_the_terrain_corrected_collection():
    """RTC, not GRD — `sentinel-1-grd` cannot be read by a windowed COG reader at all.

    Level-1 GRD measurement TIFFs are georeferenced by ground control points: `src.crs` is None
    and the transform is identity, so converting a WGS84 bbox to pixel space raises
    `CRSError: CRS is invalid: None`. Verified against a live scene — 231 GCPs, no CRS.

    `sentinel-1-rtc` is projected to UTM with overviews, and is also the scientifically correct
    input: terrain correction removes the slope-induced brightness variation that would
    otherwise read as standing water on a hillside.
    """
    from app.config import settings

    assert settings.collection_s1 == "sentinel-1-rtc", (
        "Sentinel-1 must come from the RTC collection; GRD has no CRS and cannot be windowed"
    )


def test_radar_prefers_planetary_over_element84():
    """Element84's Sentinel-1 assets are `s3://` URIs on a requester-pays bucket.

    rasterio cannot open those without AWS credentials, and the *search* succeeds — so the
    failover chain never advances. A catalogue that answers with unreadable hrefs is
    indistinguishable from a working one until the read fails, which is exactly how this hid.

    Planetary Computer serves SAS-signed HTTPS COGs, so it must be first for radar. S2 keeps
    Element84 first, where its assets ARE readable HTTPS.
    """
    from app.eo.catalogs import CATALOG_CHAIN, Product

    chain = [c.name for c in CATALOG_CHAIN[Product.S1]]
    assert chain[0] == "planetary", f"radar chain must start with planetary, got {chain}"

    optical = [c.name for c in CATALOG_CHAIN[Product.S2]]
    assert optical[0] == "element84", (
        f"optical should still prefer element84, got {optical}"
    )


# --------------------------------------------------------------------------- #
# Planetary Computer SAS signing — per CONTAINER, not per collection
# --------------------------------------------------------------------------- #


def test_sas_tokens_are_keyed_on_the_container_not_the_collection():
    """**Three Planetary-only sources were 403-ing on every cycle.**

    `sas_token(collection)` hits `/token/{collection_id}`, which answers for every collection we
    use — but the token it returns does not cover every container. Measured on a real
    `cop-dem-glo-30` asset:

        collection token      -> HTTP 403
        {account}/{container} -> HTTP 206
        /sign?href=...        -> HTTP 206

    `jrc-gsw` behaved identically. Both are Planetary-only, so there was no fallback: the Analyst
    logged "dem read failed" and "permanent-water read failed" on every single assessment, which the
    pipeline logs had been showing all along.
    """
    import pathlib

    source = pathlib.Path("app/eo/stac.py").read_text()
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )

    assert "auth.container_key(href)" in code, (
        "the token must be keyed on the container the asset actually lives in"
    )
    assert "sas_token(collection)" not in code, (
        "a collection-keyed token does not cover every container — measured 403 on DEM and JRC"
    )


def test_container_key_parses_a_planetary_blob_href():
    from app.eo.auth import container_key

    assert (
        container_key(
            "https://elevationeuwest.blob.core.windows.net/copernicus-dem/COP30_hh/x.tif"
        )
        == "elevationeuwest/copernicus-dem"
    )
    # An existing query string must not confuse the parse.
    assert (
        container_key("https://ai4edataeuwest.blob.core.windows.net/esa-worldcover/x.tif?sig=abc")
        == "ai4edataeuwest/esa-worldcover"
    )
    # Unparseable falls through to None so the caller can use the collection id instead.
    assert container_key("not-a-url") is None


def test_worldcover_tries_every_returned_tile():
    """WorldCover is a 3-degree tiled mosaic; the first tile may not cover the AOI.

    Measured over Kano: STAC returned two tiles and the FIRST computed a window at `row_off=36000`
    on a 36000-row raster — entirely outside it. Taking `scenes[0]` made tile order decide whether
    exposure worked at all, and when it lost, the second tile held the data.
    """
    import pathlib

    source = pathlib.Path("app/eo/exposure.py").read_text()
    start = source.index("async def _worldcover_fractions(")
    body = source[start : source.index("\nasync def ", start + 10)]

    code = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )
    assert "for scene in scenes:" in code, "must iterate tiles, not take the first"
    # Comments stripped: the fix names the old `scenes[0]` access while explaining itself.
    assert "scenes[0]" not in code
    # Land-cover CODES must not be interpolated between.
    assert 'band="map"' in body
