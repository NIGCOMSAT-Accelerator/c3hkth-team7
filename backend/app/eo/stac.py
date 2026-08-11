"""STAC search.

Talks the STAC API `/search` endpoint over httpx directly rather than through
pystac-client, which is synchronous and would block the event loop on every
catalogue round trip.

Nothing here downloads pixels — it returns hrefs to Cloud-Optimized GeoTIFFs
that `cog.py` reads by byte range. Where a catalogue requires it, those hrefs
are SAS-signed before being handed on.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings
from app.eo import auth
from app.eo.catalogs import AuthMode, Catalog, Product, chain_for
from app.logging_config import get_logger
from app.models.schemas import AssetRef, BBox, SceneRef

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(45.0, connect=10.0)


class NoImageryError(RuntimeError):
    """No scene matched in any catalogue — the caller decides what to do."""


def _iso_range(days_back: int) -> str:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days_back)
    return f"{start.isoformat()}/{end.isoformat()}"


async def _headers_for(catalog: Catalog) -> dict[str, str]:
    if catalog.auth is AuthMode.OAUTH:
        token = await auth.copernicus_token()
        if token:
            return {"Authorization": f"Bearer {token}"}
    return {}


async def _search_one(
    client: httpx.AsyncClient,
    catalog: Catalog,
    *,
    collection: str,
    bbox: BBox,
    days_back: int | None,
    limit: int,
    query: dict | None = None,
) -> list[dict]:
    """POST /search against a single catalogue. Returns raw STAC features."""
    body: dict = {
        "collections": [collection],
        "bbox": bbox.as_list(),
        "limit": limit,
    }
    # Static products (DEM, land cover) have no meaningful time dimension;
    # sending a recent datetime range would return nothing.
    if days_back is not None:
        body["datetime"] = _iso_range(days_back)
        body["sortby"] = [{"field": "properties.datetime", "direction": "desc"}]
    if query:
        body["query"] = query

    response = await client.post(
        f"{catalog.url.rstrip('/')}/search",
        json=body,
        headers=await _headers_for(catalog),
    )
    response.raise_for_status()
    return response.json().get("features", [])


async def _to_scene(feature: dict, catalog: Catalog) -> SceneRef | None:
    """Map a STAC feature onto our narrower SceneRef, keeping known bands only.

    Signs asset hrefs when the catalogue requires it — this is what makes
    Planetary Computer assets actually readable.
    """
    props = feature.get("properties", {})
    assets = feature.get("assets", {})
    collection = feature.get("collection") or "unknown"

    # SAS tokens are per CONTAINER, not per collection.
    #
    # This used to be `sas_token(collection)`, which hits `/token/{collection_id}`. That endpoint
    # answers for every collection we use, but the token it returns does not cover every container:
    # measured, `cop-dem-glo-30` and `jrc-gsw` both returned **403** on a real asset read while an
    # `{account}/{container}` token returned 206. Both are Planetary-only, so there was no fallback —
    # the Analyst logged "dem read failed" and "permanent-water read failed" on every cycle.
    #
    # The token is resolved per-asset below, from the href, and cached per container by `sas_token`
    # so a scene with twenty assets in one container still costs one request.
    sas_needed = catalog.auth is AuthMode.SAS_SIGNED

    refs: list[AssetRef] = []
    for logical, asset_key in catalog.band_map.items():
        asset = assets.get(asset_key)
        if not asset or not asset.get("href"):
            continue

        href = asset["href"]
        if sas_needed:
            # Keyed on the container the asset actually lives in — see `auth.container_key`.
            # Falls back to the collection id when the href is not a recognisable blob URL, which
            # preserves the previous behaviour for anything unexpected rather than skipping it.
            key = auth.container_key(href) or collection
            href = auth.sign_href(href, await auth.sas_token(key))

        refs.append(
            AssetRef(
                band=logical,
                href=href,
                nodata=asset.get("nodata"),
            )
        )

    if not refs:
        return None

    bbox_values = feature.get("bbox")
    if not bbox_values or len(bbox_values) < 4:
        return None

    # Static products carry no `datetime`; fall back to the range start, then
    # to epoch, rather than dropping an otherwise usable scene.
    when = (
        props.get("datetime")
        or props.get("start_datetime")
        or props.get("end_datetime")
    )
    try:
        moment = (
            datetime.fromisoformat(when.replace("Z", "+00:00"))
            if when
            else datetime(1970, 1, 1, tzinfo=timezone.utc)
        )
    except ValueError:
        moment = datetime(1970, 1, 1, tzinfo=timezone.utc)

    return SceneRef(
        item_id=feature.get("id", "unknown"),
        collection=collection,
        datetime=moment,
        cloud_cover=props.get("eo:cloud_cover"),
        bbox=BBox(
            west=bbox_values[0],
            south=bbox_values[1],
            east=bbox_values[2],
            north=bbox_values[3],
        ),
        assets=refs,
    )


async def search(
    product: Product,
    bbox: BBox,
    *,
    days_back: int | None = None,
    limit: int = 4,
    query: dict | None = None,
) -> list[SceneRef]:
    """Search one product across its catalogue chain; first hit wins."""
    catalogs = chain_for(product)
    if not catalogs:
        log.warning("no catalogue serves product", extra={"product": product.value})
        return []

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        for catalog in catalogs:
            collection = catalog.collection_for(product)
            if not collection:
                continue
            try:
                features = await _search_one(
                    client,
                    catalog,
                    collection=collection,
                    bbox=bbox,
                    days_back=days_back,
                    limit=limit,
                    query=query,
                )
            except Exception as exc:
                log.warning(
                    "catalogue search failed, trying next",
                    extra={
                        "catalog": catalog.name,
                        "collection": collection,
                        "error": str(exc),
                    },
                )
                continue

            scenes = [
                scene for f in features if (scene := await _to_scene(f, catalog))
            ]
            if scenes:
                log.info(
                    "imagery found",
                    extra={
                        "catalog": catalog.name,
                        "collection": collection,
                        "count": len(scenes),
                    },
                )
                return scenes

            log.info(
                "catalogue returned no usable features",
                extra={"catalog": catalog.name, "collection": collection},
            )

    return []


async def search_optical(
    bbox: BBox,
    *,
    days_back: int | None = None,
    max_cloud: float = 60.0,
    limit: int = 4,
) -> list[SceneRef]:
    """Recent Sentinel-2 scenes, cloudiest filtered out server-side.

    A high `max_cloud` is intentional: partial cloud still yields usable pixels
    once the SCL mask is applied, and rejecting them early is how optical-only
    systems end up with nothing during the rainy season.
    """
    return await search(
        Product.S2,
        bbox,
        days_back=days_back or settings.max_scene_age_days,
        limit=limit,
        query={"eo:cloud_cover": {"lt": max_cloud}},
    )


async def search_landsat(
    bbox: BBox,
    *,
    days_back: int | None = None,
    max_cloud: float = 60.0,
    limit: int = 4,
) -> list[SceneRef]:
    """Recent Landsat 8/9 Level-2 scenes. **The second optical sensor.**

    ## Why a second optical source exists at all

    The cloud survey (docs/eo-smoke-test-2026-08-10.md) measured 90 days of rainy season over three
    Nigerian AOIs. Sentinel-2 returned **zero** scenes under 40% cloud at Ikorodu and Yenagoa — the
    southern AOIs, during precisely the months when flood and crop stress matter. Landsat returned
    three usable scenes at Ikorodu in the same window.

    Different platform, different orbit, different overpass time. So this is not redundancy for its
    own sake; it is the only way the optical track sees anything in the south during the season the
    product exists for.

    ## Why it is a separate function rather than merged into `search_optical`

    The band conventions differ (`nir08` against `nir`/`B08`), so one catalogue's `band_map` cannot
    serve both. Keeping them separate also keeps the *preference* explicit: Sentinel-2 is 10 m on a
    5-day revisit and Landsat is 30 m on 16, so S2 is searched first and Landsat supplements it.

    Same `max_cloud` as `search_optical`, for the same reason — partial cloud still yields usable
    pixels, and rejecting early is how an optical system ends up blind in the rainy season.
    """
    return await search(
        Product.LANDSAT,
        bbox,
        days_back=days_back or settings.max_scene_age_days,
        limit=limit,
        query={"eo:cloud_cover": {"lt": max_cloud}},
    )


async def search_radar(
    bbox: BBox, *, days_back: int | None = None, limit: int = 4
) -> list[SceneRef]:
    """Recent Sentinel-1 GRD scenes. No cloud filter — SAR doesn't care."""
    return await search(
        Product.S1,
        bbox,
        days_back=days_back or settings.max_scene_age_days,
        limit=limit,
    )


async def search_dem(bbox: BBox) -> list[SceneRef]:
    """Copernicus DEM GLO-30. Static — no time filter."""
    return await search(Product.DEM, bbox, days_back=None, limit=2)


async def search_worldcover(bbox: BBox) -> list[SceneRef]:
    """ESA WorldCover land classification. Static annual product."""
    return await search(Product.WORLDCOVER, bbox, days_back=None, limit=2)


async def search_surface_water(bbox: BBox) -> list[SceneRef]:
    """JRC Global Surface Water. Static — a 1984–2021 occurrence summary.

    No time filter, for the same reason as the DEM: this is a historical baseline,
    not an observation. Asking for recent scenes would return nothing.
    """
    return await search(Product.SURFACE_WATER, bbox, days_back=None, limit=2)


async def search_both(
    bbox: BBox, *, days_back: int | None = None
) -> tuple[list[SceneRef], list[SceneRef]]:
    """Optical and radar concurrently — they hit different catalogue chains."""
    # THREE searches, two results. Landsat joins the optical list rather than becoming a third
    # sensor category, because the Analyst's optical leg treats it identically — NDVI from
    # surface reflectance is NDVI, whichever platform measured it.
    #
    # `_best_optical` then picks the least-cloudy scene across BOTH sensors, which is exactly the
    # behaviour the cloud survey calls for: at Ikorodu every Sentinel-2 scene in 90 days was over
    # 40% cloud while Landsat had three under it, so the selector should reach for Landsat there
    # and stay on the higher-resolution Sentinel-2 wherever it can.
    optical, landsat, radar = await asyncio.gather(
        search_optical(bbox, days_back=days_back),
        search_landsat(bbox, days_back=days_back),
        search_radar(bbox, days_back=days_back),
        return_exceptions=True,
    )
    combined = (optical if isinstance(optical, list) else []) + (
        landsat if isinstance(landsat, list) else []
    )
    return (
        combined,
        radar if isinstance(radar, list) else [],
    )
