"""Catalogue registry.

Three things differ between STAC catalogues serving the *same* satellite, and
getting any of them wrong fails silently:

1. **Collection IDs.** Sentinel-2 L2A is `sentinel-2-l2a` on Element84 and
   Planetary Computer, but `SENTINEL-2` on Copernicus Data Space. A search with
   the wrong ID returns an empty feature list, not an error.
2. **Asset keys.** The red band is `red` on Element84 and `B04` on the others.
3. **Access.** Planetary Computer hrefs need SAS signing; Copernicus needs an
   OAuth token for some collections. Element84 is anonymous.

Ordering is the failover order: Element84 first because it is anonymous and
fast, Copernicus second as the authoritative source, Planetary Computer last
because signing costs an extra round trip.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from app.config import settings


class Product(str, Enum):
    """Logical dataset, independent of which catalogue serves it."""

    S2 = "s2"
    S1 = "s1"
    DEM = "dem"
    WORLDCOVER = "worldcover"
    #: JRC Global Surface Water — the permanent-water baseline. Planetary-only,
    #: keyless. Without it, a river that is always there reads as new inundation
    #: on every single pass.
    SURFACE_WATER = "surface_water"
    #: Landsat 8/9 Collection-2 Level-2. A SECOND optical sensor, and the cloud survey is why.
    #:
    #: Over 90 days of rainy season, Sentinel-2 returned **zero** scenes under 40% cloud at
    #: Ikorodu and Yenagoa — during exactly the months when flood and crop stress matter. Landsat
    #: returned three usable scenes at Ikorodu in the same window. Different platform, different
    #: orbit, different overpass time, so it is genuinely complementary rather than redundant.
    #:
    #: 30 m against Sentinel-2's 10 m, and a 16-day revisit against 5. So it is the fallback, not
    #: the primary — `CATALOG_CHAIN` orders it after S2 and `Product.S2` is still searched first.
    LANDSAT = "landsat"


class AuthMode(str, Enum):
    ANONYMOUS = "anonymous"
    #: Assets need a per-request SAS token appended (Planetary Computer).
    SAS_SIGNED = "sas_signed"
    #: Search needs a bearer token (Copernicus Data Space).
    OAUTH = "oauth"


@dataclass(frozen=True)
class Catalog:
    name: str
    url: str
    auth: AuthMode = AuthMode.ANONYMOUS
    #: Product -> this catalogue's collection ID. A product absent from the map
    #: is simply not searched here.
    collections: dict[Product, str] = field(default_factory=dict)
    #: logical band -> asset key in this catalogue
    band_map: dict[str, str] = field(default_factory=dict)

    def collection_for(self, product: Product) -> str | None:
        return self.collections.get(product)


# Sentinel-2 asset naming differs; these two maps cover every catalogue below.
_S2_BANDS_LOWER = {
    "blue": "blue",
    "green": "green",
    "red": "red",
    "nir": "nir",
    "swir16": "swir16",
    "scl": "scl",
}
_S2_BANDS_UPPER = {
    "blue": "B02",
    "green": "B03",
    "red": "B04",
    "nir": "B08",
    "swir16": "B11",
    "scl": "SCL",
}
_S1_BANDS = {"vv": "vv", "vh": "vh"}

#: Landsat Collection-2 Level-2 asset keys.
#:
#: **`nir08`, not `nir`** — the single most likely thing to get wrong here, and it fails silently:
#: `scene.asset("nir")` returns None, the Analyst's `"nir" not in hrefs` check trips, and the leg
#: returns `{}`. That reads as "no optical scene" rather than "the band map is wrong".
#:
#: `qa_pixel` is deliberately NOT mapped to `scl`. Both mask cloud but they are different
#: encodings: SCL is a class CODE per pixel (4 = vegetation, 9 = cloud-high), while `qa_pixel` is a
#: 16-bit BITMASK (bit 3 = cloud, bit 4 = cloud shadow). Feeding a bitmask to
#: `indices.apply_scl_mask`, which tests membership of `SCL_INVALID`, would mask essentially at
#: random — some bitmask values happen to equal an invalid SCL code and most do not. Landsat scenes
#: therefore go through the same path as a Sentinel-2 scene with no SCL asset: cloud filtering
#: relies on the catalogue's own `eo:cloud_cover`, which the STAC query already bounds.
#:
#: A proper `qa_pixel` decoder is the right follow-up. Mapping it to `scl` to look complete would
#: be worse than leaving it out, because it would corrupt the mask while appearing to apply one.
_LANDSAT_BANDS = {
    "blue": "blue",
    "green": "green",
    "red": "red",
    "nir": "nir08",
    "swir16": "swir16",
}


ELEMENT84 = Catalog(
    name="element84",
    url=settings.stac_earth_search_url,
    auth=AuthMode.ANONYMOUS,
    collections={
        Product.S2: settings.collection_s2,
        Product.S1: settings.collection_s1,
        Product.DEM: settings.collection_dem,
    },
    band_map={**_S2_BANDS_LOWER, **_S1_BANDS, "data": "data", "map": "map"},
)

COPERNICUS = Catalog(
    name="copernicus",
    url=settings.stac_copernicus_url,
    auth=AuthMode.OAUTH,
    collections={
        # Copernicus Data Space uses upper-case mission names, not the
        # Element84 product slugs. This is the fix for that catalogue
        # previously returning nothing.
        Product.S2: settings.collection_s2_copernicus,
        Product.S1: settings.collection_s1_copernicus,
    },
    band_map={**_S2_BANDS_UPPER, **_S1_BANDS},
)

PLANETARY = Catalog(
    name="planetary",
    url=settings.stac_planetary_url,
    auth=AuthMode.SAS_SIGNED,
    collections={
        Product.S2: settings.collection_s2,
        Product.S1: settings.collection_s1,
        Product.DEM: settings.collection_dem,
        Product.WORLDCOVER: settings.collection_worldcover,
        Product.SURFACE_WATER: settings.collection_surface_water,
        Product.LANDSAT: settings.collection_landsat,
    },
    band_map={
        **_S2_BANDS_UPPER,
        **_S1_BANDS,
        # Landsat's own keys. Merged AFTER the Sentinel-2 upper-case map so `red`/`green`/`blue`
        # resolve to Landsat's lower-case names — which is correct here because Planetary serves
        # S2 with upper-case B0x keys, so the two sets do not collide on any key that matters.
        # `nir` is the one that would: it maps to `B08` for S2 and `nir08` for Landsat.
        #
        # This is why `search_landsat` exists as its own function rather than being folded into
        # `search_optical`: one band_map cannot serve two conventions for the same logical band.
        **_LANDSAT_BANDS,
        "data": "data",
        "map": "map",
        # jrc-gsw publishes one asset per statistic; `occurrence` is the share of
        # observations in which a pixel was water (0-100), which is exactly the
        # permanent-water question. `extent` would say "ever wet", including a
        # one-off flood -- subtracting that would hide real events.
        "occurrence": "occurrence",
    },
)


#: Failover order per product. WorldCover is Planetary-only in practice, so it
#: has a single entry rather than a fake fallback chain.
CATALOG_CHAIN: dict[Product, tuple[Catalog, ...]] = {
    Product.S2: (ELEMENT84, COPERNICUS, PLANETARY),
    # PLANETARY first for radar, deliberately out of step with S2 above.
    #
    # Element84's `sentinel-1-grd` assets are `s3://sentinel-s1-l1c/...` URIs on a
    # REQUESTER-PAYS bucket, so rasterio cannot open them without AWS credentials we do not
    # have. The search succeeds and returns scenes, so the failover chain never advanced — a
    # catalogue that answers with unreadable hrefs looks identical to a working one until the
    # read fails, and the read failure was being reported as "no usable imagery".
    #
    # Planetary Computer serves `sentinel-1-rtc` as SAS-signed HTTPS COGs with a real CRS,
    # which is both readable and the correct product (see `collection_s1`).
    Product.S1: (PLANETARY, ELEMENT84, COPERNICUS),
    Product.DEM: (PLANETARY, ELEMENT84),
    Product.WORLDCOVER: (PLANETARY,),
    Product.SURFACE_WATER: (PLANETARY,),
    # Landsat is PLANETARY-only, and that is a deliberate omission of Element84.
    #
    # Element84 does serve `landsat-c2-l2`, but its assets are `s3://usgs-landsat/...` on a
    # REQUESTER-PAYS bucket — the same trap that made Sentinel-1 unreadable there: search succeeds,
    # so the chain never advances, and the failure surfaces as "no usable imagery" at read time.
    # `.env.example` already warns against creating an AWS account for this.
    #
    # Planetary serves the same collection as SAS-signed HTTPS COGs with free egress.
    Product.LANDSAT: (PLANETARY,),
}


def chain_for(product: Product) -> tuple[Catalog, ...]:
    """Catalogues to try for a product, in order, skipping ones that don't
    serve it."""
    return tuple(
        c for c in CATALOG_CHAIN.get(product, ()) if c.collection_for(product)
    )
