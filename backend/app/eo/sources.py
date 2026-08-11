"""Dataset source registry — every feed, declared once.

The adapters in this package each know how to *call* one upstream. Nothing knew
the set of them as data: which env keys configure a source, whether it needs a
credential, how often it is worth re-polling, or what a poll actually costs. That
made three things impossible — telling an operator at a glance which feeds are
live, letting Scout decide what is due, and failing the build when a source is
added to `.env.example` and never wired.

This module is that missing table. It is **declaration only**: no HTTP, no
rasterio, no imports from `cog`/`stac`. `tests/test_datasources.py` asserts every
declared env key exists as a setting, so the registry cannot drift from config.

**Cadence is derived from the upstream's own refresh rate, not guessed.**
Re-polling faster than the satellite revisits buys nothing but rate-limit risk:

    Sentinel-1 revisits West Africa ~6 days, Sentinel-2 ~5.
    CHIRPS/GEFS publish daily. WorldCover is annual. DEM is effectively static.

So `min_interval_hours` is a floor on *usefulness*, and Scout uses it to skip a
poll whose answer cannot have changed. That is the whole reason the stateful Scout
is cheaper than the stateless one: most sources, most cycles, have nothing new.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Kind(str, Enum):
    """What a source contributes. Determines who consumes the poll result."""

    #: Scene discovery + windowed pixel reads. Consumed by Analyst.
    IMAGERY = "imagery"
    #: Forward-looking or antecedent precipitation. Consumed by Oracle.
    RAINFALL = "rainfall"
    #: Who and what is inside the footprint. Consumed by Oracle.
    EXPOSURE = "exposure"
    #: Soil drainage / health baseline. Consumed by Oracle.
    REFERENCE = "reference"


class Poll(str, Enum):
    """How the source is reached — the shape of one poll."""

    #: STAC POST /search, then windowed COG reads by HTTP range request.
    STAC_COG = "stac_cog"
    #: Submit a job, poll for completion, fetch the result.
    SUBMIT_POLL = "submit_poll"
    #: One request, one JSON answer.
    REST = "rest"
    #: OPeNDAP ASCII subset.
    OPENDAP = "opendap"
    #: Overpass QL POST.
    OVERPASS = "overpass"
    #: WMS GetFeatureInfo point query.
    WMS = "wms"


@dataclass(frozen=True)
class Source:
    """One upstream dataset, as configured.

    `settings_keys` are `Settings` field names (snake_case), which is what the
    contract test checks against — `.env.example` uses the SCREAMING_CASE form of
    the same names, and `tests/test_config.py` already keeps those two aligned.
    """

    key: str
    label: str
    kind: Kind
    poll: Poll
    #: Settings that configure this source. Every one must exist.
    settings_keys: tuple[str, ...]
    #: Setting holding the credential, when one is needed. None means keyless.
    credential_key: str | None = None
    #: Floor on re-poll frequency, from the upstream's own publication cadence.
    #: 0 means "every cycle" (nothing is cheaper to re-check than to cache).
    min_interval_hours: int = 0
    #: True when a failure drops the pipeline to a documented fallback rather than
    #: stopping it. Every source here is optional except imagery discovery.
    degrades: bool = True
    #: Which fallback runs when this one fails, for the operator console.
    falls_back_to: str | None = None
    #: Cacheable to the object store. Windowed crops and small JSON payloads only —
    #: never a whole scene, which would defeat the range-read design.
    cacheable: bool = False
    notes: str = ""
    #: Datasets this source is grouped with in `.env.example`, for cross-reference.
    env_section: str = ""


#: Every source, in the order `.env.example` declares them.
#:
#: Ordering within a `kind` is failover order — `chain_for()` relies on it, and it
#: matches the chains documented in `README.md` §3.
SOURCES: tuple[Source, ...] = (
    # ---------------- Imagery ----------------
    Source(
        key="element84",
        label="Element84 Earth Search (STAC)",
        kind=Kind.IMAGERY,
        poll=Poll.STAC_COG,
        settings_keys=(
            "stac_earth_search_url",
            "collection_s2",
            "collection_s1",
            "collection_dem",
        ),
        credential_key=None,
        min_interval_hours=6,
        falls_back_to="copernicus",
        cacheable=True,
        env_section="SATELLITE IMAGERY",
        notes="Anonymous and fast, so it leads the chain.",
    ),
    Source(
        key="copernicus",
        label="Copernicus Data Space (STAC)",
        kind=Kind.IMAGERY,
        poll=Poll.STAC_COG,
        settings_keys=(
            "stac_copernicus_url",
            "collection_s2_copernicus",
            "collection_s1_copernicus",
            "copernicus_token_url",
        ),
        credential_key="copernicus_client_secret",
        min_interval_hours=6,
        falls_back_to="planetary",
        cacheable=True,
        env_section="SATELLITE IMAGERY",
        notes=(
            "NO SENTINEL DATA on this STAC endpoint. Verified 2026-08-10: /stac/collections "
            "returns 10 collections — ccm-optical, ccm-sar and eight CLMS burnt-area products. "
            "A Sentinel search returns HTTP 400 CollectionInQuerryDoesNotExist. Sentinel access "
            "at Copernicus is via OData/OpenSearch, which is a different adapter. Kept as a "
            "declared-but-unreachable rung so the gap is visible rather than forgotten; "
            "stac.search logs and advances, so it costs one failed request per scan."
        ),
    ),
    Source(
        key="planetary",
        label="Microsoft Planetary Computer (STAC)",
        kind=Kind.IMAGERY,
        poll=Poll.STAC_COG,
        settings_keys=("stac_planetary_url", "planetary_sas_url"),
        # KEYLESS. Verified live: anonymous search returns 200, the SAS token endpoint issues a
        # token without a subscription key, and a signed range read returns 206.
        #
        # This was `credential_key="planetary_computer_api_key"`, which made `configured()` False on
        # a default install — so `/health` reported imagery as `live=['element84']` alone,
        # understating redundancy on the pipeline's single most critical chain. The flag gates only
        # reporting, never fetching, so the rung worked while the console said it could not.
        #
        # `PLANETARY_COMPUTER_API_KEY` remains supported and is still sent when set
        # (`eo/auth.py`): it raises the rate limit and unlocks some restricted collections. It is an
        # enhancement, not a requirement, and the registry now says so.
        credential_key=None,
        min_interval_hours=6,
        cacheable=True,
        env_section="SATELLITE IMAGERY",
        notes=(
            "Keyless. Assets need SAS signing or every read 403s — the token is anonymous. "
            "Sole WorldCover, DEM and surface-water source."
        ),
    ),
    Source(
        key="landsat",
        label="Landsat 8/9 Collection-2 L2 (Planetary Computer)",
        kind=Kind.IMAGERY,
        poll=Poll.STAC_COG,
        settings_keys=("stac_planetary_url", "collection_landsat", "planetary_sas_url"),
        credential_key=None,
        # 16-day revisit, so re-searching more often than every 12 hours cannot find anything
        # new. Sentinel-2's 6 reflects its 5-day revisit; this is the same reasoning at Landsat's
        # cadence.
        min_interval_hours=12,
        cacheable=True,
        env_section="SATELLITE IMAGERY",
        notes=(
            "SECOND optical sensor, and it is load-bearing in the south. Over 90 days of rainy "
            "season Sentinel-2 returned ZERO scenes under 40% cloud at Ikorodu and Yenagoa, "
            "while Landsat returned three usable ones at Ikorodu. 30 m / 16-day, so it "
            "supplements S2 rather than replacing it. Planetary-only: Element84's assets are "
            "s3://usgs-landsat requester-pays. qa_pixel is a BITMASK, not SCL codes, so it is "
            "deliberately unmapped — cloud filtering relies on eo:cloud_cover."
        ),
    ),
    Source(
        key="jrc-gsw",
        label="JRC Global Surface Water (Planetary Computer)",
        kind=Kind.EXPOSURE,
        poll=Poll.STAC_COG,
        settings_keys=("stac_planetary_url", "collection_surface_water"),
        credential_key=None,
        # A 1984-2021 historical summary. It does not change, so the only reason to re-read is a
        # cache miss — `terrain.permanent_water_mask` holds it for 30 days per AOI.
        min_interval_hours=720,
        cacheable=True,
        env_section="SATELLITE IMAGERY",
        notes=(
            "WAS QUERIED BUT UNREGISTERED until 2026-08-10 — the exact drift this registry "
            "exists to prevent, and `test_registry_covers_every_documented_data_source` passed "
            "throughout because it hardcoded the 13 keys it expected. Read on EVERY radar leg "
            "(PERMANENT_WATER_MASKING=true) by terrain.permanent_water_mask: without it a river "
            "that is always there reads as new inundation on every pass."
        ),
    ),
    # ---------------- Rainfall ----------------
    Source(
        key="gfs-forecast",
        label="NOAA GFS/GEFS short-range rainfall forecast",
        kind=Kind.RAINFALL,
        poll=Poll.REST,
        settings_keys=("forecast_api_url", "forecast_model"),
        credential_key=None,
        # GFS runs four times daily; re-asking more often than every 6 hours cannot find a new run.
        min_interval_hours=6,
        falls_back_to="gpm-imerg",
        cacheable=True,
        env_section="RAINFALL & CLIMATE",
        notes=(
            "THE ONLY FORWARD-LOOKING RUNG. Replaces ClimateSERV datatype 35, which was labelled "
            "GEFS but does not exist: getClimateScenarioInfo lists only CCSM4/CFSv2 and 35 is "
            "rejected with progress -1, so forecast_available was permanently False and every "
            "assessment took the 0.75 no-forecast confidence penalty. Reached as JSON rather than "
            "GRIB2 so eo/rainfall.py stays free of cfgrib/eccodes. Point sample at the AOI "
            "centroid, which is right for a plot smaller than a ~25 km GFS cell."
        ),
    ),
    Source(
        key="gpm-imerg",
        label="NASA GPM IMERG (observed)",
        kind=Kind.RAINFALL,
        poll=Poll.OPENDAP,
        settings_keys=(
            "cmr_search_url",
            "gpm_imerg_short_name",
            "gpm_imerg_version",
            "imerg_opendap_host",
        ),
        credential_key="nasa_earthdata_token",
        min_interval_hours=12,
        falls_back_to="climateserv-chirps",
        cacheable=True,
        env_section="RAINFALL",
        notes=(
            "Validated live 2026-08-11: 6.2 mm/week over Ikorodu. Reached via CMR -> "
            "opendap.earthdata.nasa.gov as `.dap.csv`; the documented GES DISC host 404s and "
            "`.ascii`/`.dods`/`.dap.json` all fail there. Needs the NASA GESDISC DATA ARCHIVE "
            "application authorised on the Earthdata profile, not just a token."
        ),
    ),
    Source(
        key="smap-l3",
        label="NASA SMAP L3 enhanced soil moisture (9 km)",
        kind=Kind.REFERENCE,
        poll=Poll.OPENDAP,
        settings_keys=(
            "cmr_search_url",
            "smap_short_name",
            "imerg_opendap_host",
        ),
        credential_key="nasa_earthdata_token",
        # SMAP has a ~2-3 day global revisit and publishes daily with a ~2-day lag, so twice a day
        # is already faster than the answer can change.
        min_interval_hours=12,
        # No fallback, deliberately: nothing else measures soil water content. Rainfall is a
        # different quantity, and substituting it would be the "absent data must not become an
        # implied claim" violation — see `SoilMoisture.available`.
        falls_back_to=None,
        cacheable=True,
        env_section="SOIL & TERRAIN",
        notes=(
            "THE IRRIGATION SIGNAL. Validated live 2026-08-11 on the 2026-08-09 granule: Kano "
            "0.311, Ikorodu 0.364, Yenagoa 0.593 m3/m3 — a monotonic north-south wetness gradient "
            "on one day, which is the physical check that the read is real. Grid is EASE-Grid 2.0 "
            "(EPSG:6933, equal-AREA), so row is NOT linear in latitude: a linear index put Kano "
            "480 km away and still returned a plausible number. Projected via pyproj and then "
            "proven per-request against the granule's own latitude/longitude arrays."
        ),
    ),
    Source(
        key="climateserv-chirps",
        label="ClimateSERV CHIRPS (observed)",
        kind=Kind.RAINFALL,
        poll=Poll.SUBMIT_POLL,
        settings_keys=("climateserv_url", "climateserv_datatype"),
        credential_key=None,
        min_interval_hours=12,
        falls_back_to="era5",
        cacheable=True,
        env_section="RAINFALL",
        notes="Antecedent wetness, not a prediction.",
    ),
    Source(
        key="era5",
        label="Copernicus ERA5 (reanalysis)",
        kind=Kind.RAINFALL,
        poll=Poll.SUBMIT_POLL,
        settings_keys=("era5_cds_url", "era5_dataset"),
        credential_key="era5_cds_key",
        min_interval_hours=24,
        cacheable=True,
        env_section="RAINFALL",
        notes="Last resort; the CDS queue can be slow. Never validated live.",
    ),
    # ---------------- Exposure ----------------
    Source(
        key="worldpop",
        label="WorldPop (population)",
        kind=Kind.EXPOSURE,
        poll=Poll.REST,
        settings_keys=("worldpop_api_url", "worldpop_dataset", "worldpop_year"),
        credential_key=None,
        # A gridded annual product: the answer for a fixed bbox does not change
        # within a season, so a weekly floor is generous.
        min_interval_hours=168,
        cacheable=True,
        env_section="EXPOSURE",
    ),
    Source(
        key="worldcover",
        label="ESA WorldCover (land cover)",
        kind=Kind.EXPOSURE,
        poll=Poll.STAC_COG,
        settings_keys=(
            "collection_worldcover",
            "worldcover_cropland_class",
            "worldcover_water_class",
            "worldcover_builtup_class",
            "exposure_tile_size",
        ),
        credential_key=None,
        # Annual product. Re-reading the same classified pixels weekly is waste.
        min_interval_hours=720,
        cacheable=True,
        env_section="EXPOSURE",
        notes="Planetary-only, so an unsigned read breaks exposure entirely.",
    ),
    Source(
        key="copernicus-dem",
        label="Copernicus DEM GLO-30 (terrain)",
        kind=Kind.EXPOSURE,
        poll=Poll.STAC_COG,
        settings_keys=("collection_dem", "dem_lowland_offset_m"),
        credential_key=None,
        # Terrain does not move. Cache once and effectively never re-poll.
        min_interval_hours=8760,
        cacheable=True,
        env_section="EXPOSURE",
    ),
    Source(
        key="openstreetmap",
        label="OpenStreetMap Overpass (settlements, facilities)",
        kind=Kind.EXPOSURE,
        poll=Poll.OVERPASS,
        settings_keys=("osm_overpass_url",),
        credential_key=None,
        min_interval_hours=168,
        cacheable=True,
        env_section="EXPOSURE",
    ),
    # ---------------- Reference ----------------
    Source(
        key="soilgrids",
        label="ISRIC SoilGrids (drainage)",
        kind=Kind.REFERENCE,
        poll=Poll.REST,
        settings_keys=(
            "soilgrids_base_url",
            "soilgrids_depth",
            "soilgrids_heavy_clay_threshold",
        ),
        credential_key=None,
        # Soil texture is a physical constant at this timescale.
        min_interval_hours=8760,
        cacheable=True,
        env_section="SOIL",
    ),
    Source(
        key="malaria-atlas",
        label="Malaria Atlas Project (PfPR baseline)",
        kind=Kind.REFERENCE,
        poll=Poll.WMS,
        settings_keys=(
            "malaria_atlas_url",
            "malaria_atlas_workspace",
            "malaria_atlas_layer",
            "malaria_endemic_threshold",
        ),
        credential_key=None,
        min_interval_hours=8760,
        cacheable=True,
        env_section="HEALTH",
        notes="Written from the GeoServer WMS contract; never validated live.",
    ),
)


BY_KEY: dict[str, Source] = {s.key: s for s in SOURCES}


def for_kind(kind: Kind) -> tuple[Source, ...]:
    """Sources of one kind, in failover order."""
    return tuple(s for s in SOURCES if s.kind is kind)


def configured(source: Source) -> bool:
    """Whether this source can be polled right now.

    Keyless sources are always configured — which is the point of the architecture:
    the pipeline runs end-to-end on `.env.example` unchanged. A credentialled source
    that lacks its credential is *skipped*, not an error; it simply drops out of its
    failover chain.
    """
    from app.config import settings

    if source.credential_key is None:
        return True
    return bool(getattr(settings, source.credential_key, None))


def summary() -> list[dict]:
    """Registry as JSON, for `/health` and the operator console.

    Lets an operator see which feeds are live on a calm Tuesday rather than
    discovering a missing Earthdata token during a flood.
    """
    return [
        {
            "key": s.key,
            "label": s.label,
            "kind": s.kind.value,
            "poll": s.poll.value,
            "configured": configured(s),
            "keyless": s.credential_key is None,
            "min_interval_hours": s.min_interval_hours,
            "falls_back_to": s.falls_back_to,
            "cacheable": s.cacheable,
        }
        for s in SOURCES
    ]


def chain_status(kind: Kind) -> dict:
    """Whether a whole failover chain can answer, and which links are live.

    A chain with no configured link is the failure worth alerting on — for
    rainfall that means the Oracle loses its entire forward-looking term.
    """
    chain = for_kind(kind)
    live = [s.key for s in chain if configured(s)]
    return {
        "kind": kind.value,
        "chain": [s.key for s in chain],
        "live": live,
        # Imagery and rainfall each have a keyless primary, so this should never
        # be empty on a default install. If it is, something was mis-set.
        "operational": bool(live),
    }
