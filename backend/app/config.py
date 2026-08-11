"""Central configuration.

Everything is read from the environment (see `.env.example` at the repo root).
Nothing here has a secret as its default — a missing credential disables the
feature that needs it rather than silently falling back to something wrong.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- App ----------------
    app_name: str = "SHELTER"
    #: Advertised in the root document, the OpenAPI schema and every outbound
    #: webhook's `api_version`. Receivers key their parser off it, so it is a
    #: contract rather than a cosmetic string.
    app_version: str = "1.0.0"
    environment: str = "development"
    log_level: str = "INFO"
    #: Every route, the Swagger UI and the OpenAPI schema live under this prefix.
    #:
    #: One prefix for the whole service is what makes the reverse-proxy config a
    #: single `location /shelter/v1/api/` block, and lets SHELTER share a host with
    #: something else without colliding. It is string-concatenated by
    #: `frontend/lib/api.ts`, so `app/preflight.py` rejects a missing leading slash
    #: or a trailing one before the port opens.
    api_prefix: str = "/shelter/v1/api"

    #: Public base URL of the API itself, e.g. https://api.shelter.zerorate.io
    #:
    #: Distinct from `public_site_url`, which is the *portal*. In the target
    #: deployment the frontend is on Netlify and the backend behind its own reverse
    #: proxy on a VPS, so they are different hosts — and an onboarding email that
    #: points a partner at the portal's domain for the OpenAPI spec sends them
    #: somewhere the spec is not served.
    #:
    #: Falls back to `public_site_url` when unset, which is correct for a single-host
    #: install where the proxy serves both from one domain.
    api_base_url: str | None = None
    cors_origins: str = "http://localhost:3000,https://shelter.zerorate.io"
    public_site_url: str = "https://shelter.zerorate.io"

    # Shared secret the frontend sends as `X-SHELTER-Key` on write endpoints.
    api_key: str | None = None

    # ---------------- Redis / Dragonfly ----------------
    # Two logical databases on one instance, and the split is deliberate:
    #
    #   db0  job streams + dead-letter. Durable. Nothing here may be evicted —
    #        a dropped stream entry is a satellite scan that silently never ran.
    #   db1  cache. Every key is disposable and every write carries a TTL.
    #
    # NOTE: eviction policy is per-INSTANCE, not per-database, on both Redis and
    # Dragonfly. So this split does not protect db0 from an eviction policy set
    # for db1's benefit. That is why the deployment runs with eviction disabled
    # and `cache.py` makes `ttl_seconds` a required argument instead — bounded
    # growth is enforced by the caller, not by hoping the evictor picks well.
    redis_url: str = "redis://localhost:6379/0"
    #: Cache database. Defaults to db1 on the same instance as `redis_url`.
    cache_url: str = "redis://localhost:6379/1"
    #: Prefix for every cache key, so a shared instance stays inspectable.
    cache_prefix: str = "shelter:cache"
    #: Default TTL for cache entries whose caller has no better idea.
    cache_default_ttl_seconds: int = 900
    #: Latest-assessment cache TTL. Two days: older than that and showing
    #: nothing is more honest than showing it.
    cache_assessment_ttl_seconds: int = 172_800
    queue_stream_prefix: str = "shelter"
    queue_consumer_group: str = "shelter-workers"
    queue_block_ms: int = 5_000
    queue_max_retries: int = 3

    # ---------------- PostgreSQL (PostGIS + pgvector + TimescaleDB) ----------
    # The system of record. Redis is a cache and a queue in front of this, never
    # the durable copy.
    #
    # Three extensions, each load-bearing:
    #   postgis     GiST-indexed ST_Intersects answers "which subscribers are in
    #               this flood footprint?" without scanning every subscriber.
    #   vector      Advisory/evidence embeddings live beside the rows they
    #               describe, so a retrieval query can filter spatially AND
    #               semantically in one statement.
    #   timescaledb Assessments are an append-only time series; the hypertable
    #               partitions it by time so history is cheap to keep forever.
    postgres_dsn: str = "postgresql://shelter:shelter@localhost:5432/shelter"
    postgres_pool_min: int = 2
    postgres_pool_max: int = 10
    #: Seconds a query may run before asyncpg cancels it. Guards against a
    #: pathological spatial query holding a pool slot open indefinitely.
    postgres_command_timeout: float = 30.0
    #: Run pending migrations on startup. True is right for compose and for the
    #: hackathon; a multi-replica deployment should migrate as a release step so
    #: replicas don't race.
    postgres_auto_migrate: bool = True
    #: Chunk interval for the assessments hypertable. One week keeps a typical
    #: 6-hourly scan cadence to a few thousand rows per chunk.
    timescale_chunk_interval_days: int = 7
    #: Dimensionality of stored embeddings. Must match the embedding model —
    #: changing it needs a migration, not just an edit.
    embedding_dimensions: int = 1_536

    # ---------------- Object storage (MinIO / S3-compatible) ----------------
    # Unstructured blobs: advisory voice notes, cached imagery crops, exports.
    # Self-hosted MinIO keeps the sovereignty story the broadcast layer makes.
    s3_endpoint: str = "localhost:9000"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    #: False for local MinIO over plain HTTP; True everywhere else.
    s3_secure: bool = False
    s3_region: str = "us-east-1"
    #: Advisory audio (mp3). Private — served only via presigned URLs.
    s3_bucket_audio: str = "shelter-audio"
    #: Windowed COG crops and quicklooks, keyed by scene + AOI.
    s3_bucket_imagery: str = "shelter-imagery"
    #: Operator-generated CSV/GeoJSON exports.
    s3_bucket_exports: str = "shelter-exports"
    #: Presigned URL lifetime. Short: an audio link in a Telegram message only
    #: needs to survive the download, not the conversation.
    s3_presign_ttl_seconds: int = 3_600
    #: Create missing buckets on startup rather than failing the first write.
    s3_auto_create_buckets: bool = True

    # ---------------- STAC / Earth Observation ----------------
    # Primary hub. AWS Open Data mirrors are used as fallbacks so the pipeline
    # keeps running if one endpoint is degraded.
    stac_copernicus_url: str = "https://catalogue.dataspace.copernicus.eu/stac"
    stac_earth_search_url: str = "https://earth-search.aws.element84.com/v1"
    stac_planetary_url: str = "https://planetarycomputer.microsoft.com/api/stac/v1"

    # Collection ids. These genuinely differ between catalogues serving the
    # same satellite — Copernicus Data Space uses upper-case mission names —
    # and a wrong id returns an empty result set rather than an error.
    collection_s2: str = "sentinel-2-l2a"
    #: Sentinel-1 on Planetary Computer. **RTC, not GRD**, and this is load-bearing.
    #:
    #: `sentinel-1-grd` is raw Level-1: the measurement TIFFs carry 231 ground control points
    #: and NO CRS (`src.crs is None`, identity transform), because they are georeferenced by
    #: GCPs rather than projected. `cog._read_window_sync` needs a CRS to convert our WGS84
    #: bbox into pixel space, so every read raised `CRSError: CRS is invalid: None` — which the
    #: Analyst reported honestly as "no usable satellite imagery", making a broken read path
    #: look like a working pipeline finding nothing.
    #:
    #: `sentinel-1-rtc` is Radiometrically Terrain Corrected: projected to UTM (EPSG:326xx),
    #: float32, with overviews. Verified reading 4096/4096 finite pixels over a Lagos AOI.
    #: It is also the scientifically correct input — RTC removes the terrain-induced
    #: brightness variation that would otherwise read as water on a hillside.
    collection_s1: str = "sentinel-1-rtc"
    #: Copernicus Sentinel collection IDs. **Empty by default, deliberately.**
    #:
    #: This STAC endpoint serves NO Sentinel data. Verified live 2026-08-10:
    #: `/stac/collections` returns ten collections — `ccm-optical`, `ccm-sar` and eight CLMS
    #: burnt-area products — and a Sentinel search returns
    #: `HTTP 400 CollectionInQuerryDoesNotExist`. The previous values `SENTINEL-2` / `SENTINEL-1`
    #: were wrong, and so was the CLAUDE.md invariant built on them.
    #:
    #: Empty rather than deleted, for two reasons. `catalogs.chain_for` skips a catalogue whose
    #: `collection_for` is falsy, so an empty string removes the guaranteed-failing request from
    #: every scan — one wasted round trip per product per cycle — while leaving the rung *declared*.
    #: And it stays configurable: an operator who has the OData adapter, or for whom Copernicus
    #: later publishes Sentinel over STAC, sets these and the rung returns without a code change.
    #:
    #: Sentinel access at Copernicus today is via OData/OpenSearch, which is a different protocol
    #: and a separate adapter — see `docs/eo-smoke-test-2026-08-10.md`.
    collection_s2_copernicus: str = ""
    collection_s1_copernicus: str = ""
    collection_dem: str = "cop-dem-glo-30"
    collection_worldcover: str = "esa-worldcover"
    #: Landsat 8/9 Collection-2 Level-2, on Planetary Computer. Keyless.
    #:
    #: A SECOND optical sensor, added because the cloud survey showed the first one is blind when
    #: it matters most: over 90 days of rainy season, Sentinel-2 returned ZERO scenes under 40%
    #: cloud at Ikorodu and Yenagoa, while Landsat returned three usable scenes at Ikorodu. Same
    #: STAC+COG path, different platform and overpass time.
    #:
    #: Level-2 (surface reflectance), not Level-1: NDVI on top-of-atmosphere radiance is not
    #: comparable with the Sentinel-2 L2A values the rest of the pipeline is calibrated against.
    collection_landsat: str = "landsat-c2-l2"
    #: JRC Global Surface Water. Planetary Computer only, keyless — the
    #: permanent-water baseline that stops a river reading as new inundation.
    collection_surface_water: str = "jrc-gsw"

    # Copernicus Data Space: client-credentials grant. Optional — anonymous
    # search works for open collections.
    copernicus_client_id: str | None = None
    copernicus_client_secret: str | None = None
    copernicus_token_url: str = (
        "https://identity.dataspace.copernicus.eu"
        "/auth/realms/CDSE/protocol/openid-connect/token"
    )

    # Planetary Computer: asset hrefs point at Azure Blob and need a per-
    # collection SAS token appended, or every read returns 403.
    planetary_sas_url: str = "https://planetarycomputer.microsoft.com/api/sas/v1"
    planetary_computer_api_key: str | None = None

    # ---------------- Rainfall (tried in this order) ----------------
    # 1. SERVIR ClimateSERV — fronts CHIRPS + GEFS, keyless, forecast-capable.
    climateserv_url: str = "https://climateserv.servirglobal.net/api"
    climateserv_datatype: int = 0  # 0 = CHIRPS precipitation
    # NOTE: `climateserv_forecast_datatype` was REMOVED, not repointed.
    #
    # It defaulted to 35, labelled "GEFS ensemble precipitation". Verified live 2026-08-11:
    # `getClimateScenarioInfo` lists only CCSM4 and CFSv2 ensembles — **ClimateSERV serves no GEFS**
    # — and 35 is not a valid datatype. Submitting it returns progress `-1`, an explicit rejection,
    # which is why every cycle logged "GEFS forecast unavailable" and `forecast_available` was
    # permanently False. The CFSv2/CCSM4 ensembles that do exist are seasonal: asked for a 7-day
    # window they return zero entries (measured on 9, 43 and 51), so they cannot serve this either.
    #
    # Deleted rather than left pointing at a seasonal ensemble, because an unread setting that looks
    # like a forecast control is exactly how the original defect survived. Seasonal outlooks are a
    # future capability and will need their own adapter, not a repurposed constant.

    # ---------------- Short-range rainfall forecast ----------------
    #
    # ## Why a fourth rainfall provider exists
    #
    # `RainfallOutlook.forecast_available` gates the difference between "the ground is already wet"
    # and "more rain is coming", and only the second supports a FORWARD flood warning. That flag was
    # permanently False because the configured GEFS datatype does not exist (see above), so every
    # advisory was antecedent-only — the Oracle's `_forecast_term` contributed nothing and
    # `NO_FORECAST_CONFIDENCE` scaled every assessment down by 0.75.
    #
    # Open-Meteo is a keyless REST façade over **NOAA GFS/GEFS** (`models=gfs_seamless`), which is
    # the product the hackathon brief intends. Reaching it as JSON rather than raw GRIB is what keeps
    # `eo/rainfall.py` free of a GRIB decoder — the same constraint that blocks the ERA5 rung.
    #
    # Verified live 2026-08-11, 7-day daily precipitation in mm:
    #   Kano    [15.0, 2.3, 11.5, 7.5, 5.0, 6.9, 14.2]
    #   Yenagoa [6.3, 1.5, 17.1, 10.5, 8.6, 5.7, 5.1]
    #   Ikorodu [0.1, 0.8, 0.4, 0.1, 0.5, 1.2, 0.3]
    forecast_api_url: str = "https://api.open-meteo.com/v1/forecast"
    #: Underlying weather model. `gfs_seamless` is NOAA GFS/GEFS.
    forecast_model: str = "gfs_seamless"
    climateserv_poll_attempts: int = 12
    climateserv_poll_interval_seconds: float = 2.0

    # 2. NASA GPM IMERG — needs a free Earthdata token.
    #: CMR granule search. **This is how IMERG granules are located** — see `gpm_imerg_collection`.
    cmr_search_url: str = "https://cmr.earthdata.nasa.gov/search"
    #: IMERG daily late-run. `short_name` + `version` for the CMR query.
    #:
    #: The GRANULE FILENAME is not constructible. Verified live 2026-08-11: the current granule is
    #: `...20260809-S000000-E235959.V07C.nc4` — note **V07C**, where the previous adapter hard-coded
    #: `V07B`. That suffix is a processing-version letter that changes without notice, so every
    #: hand-built URL eventually 404s. CMR is asked for the real link instead.
    gpm_imerg_short_name: str = "GPM_3IMERGDL"
    gpm_imerg_version: str = "07"
    #: OPeNDAP host for IMERG reads.
    #:
    #: **Not `gpm1.gesdisc.eosdis.nasa.gov/opendap`** — verified 2026-08-11, that host 404s on the
    #: product path and times out at the root. Data is served from
    #: `opendap.earthdata.nasa.gov/collections/{concept_id}/granules/{granule}`, and CMR supplies the
    #: full href per granule, so this setting exists only so an operator can point at a mirror.
    #:
    #: Read by `_imerg_granules` as a guard: a granule href from a DIFFERENT host is rejected, so a
    #: CMR index change cannot silently redirect our reads somewhere unexpected.
    imerg_opendap_host: str = "opendap.earthdata.nasa.gov"
    nasa_earthdata_token: str | None = None

    #: NASA SMAP L3 enhanced soil moisture, 9 km daily. Reuses `cmr_search_url`,
    #: `imerg_opendap_host` and `nasa_earthdata_token` — same index, same OPeNDAP host, same token.
    #:
    #: `SPL3SMP_E` and not the 3 km `SPL2SMAP_S`: measured 2026-08-11, the 3 km product's newest
    #: granule over Nigeria was 2026-06-28 (six weeks stale) against two days for this one. See
    #: `eo/soil_moisture.py` for why currency beats resolution on an irrigation decision.
    smap_short_name: str = "SPL3SMP_E"

    # 3. Copernicus ERA5 reanalysis — needs a free CDS API key.
    era5_cds_url: str = "https://cds.climate.copernicus.eu/api"
    era5_cds_key: str | None = None
    era5_dataset: str = "reanalysis-era5-single-levels"

    # NOTE: CHIRPS has no setting of its own — it is reached *through*
    # ClimateSERV above (`climateserv_datatype`), which is the only keyless
    # way to query it per-geometry. A direct bulk-raster URL would be a
    # config key nothing reads.

    # ---------------- Exposure ----------------
    # The zonal-stats REST API, not the bulk raster host — this is the surface
    # that answers per-geometry population queries.
    worldpop_api_url: str = "https://api.worldpop.org/v1"
    worldpop_dataset: str = "wpgppop"
    worldpop_year: int = 2020
    osm_overpass_url: str = "https://overpass-api.de/api/interpreter"

    # ---------------- Administrative boundaries (State / LGA / Ward) ---------
    #
    # Two purposes, and the second is the important one:
    #
    #   1. A subscriber picks "Odelemo ward, Shagamu LGA, Ogun State" instead of drawing geometry.
    #   2. `agents/fahis` searches outside reporting BY PLACE NAME, built from admin1/admin2 and
    #      deliberately never from `aoi_name`. Unresolved, a pin-registered AOI is unverifiable —
    #      the accountability agent has nothing to search for.
    #
    # Both keyless. GRID3 leads for Nigeria (ward depth plus alternate spellings); geoBoundaries is
    # the failover and the answer for the rest of Sub-Saharan Africa.

    #: Master switch. False skips resolution entirely and the subscriber's own text is kept.
    admin_resolution_enabled: bool = True
    #: GRID3 Nigeria, ArcGIS Feature Service. Keyless, CC-BY.
    grid3_base_url: str = "https://services3.arcgis.com/BU6Aadhn6tbBEdyk/arcgis/rest/services"
    #: The operational ward layer — 5,872 features carrying `state`, `lga`, `ward` and both
    #: `*_alt_names` fields. Verified live 2026-08-10.
    grid3_wards_service: str = "GRID3_NGA_operational_wards_v3_0"
    #: National LGA layer — all 774 LGAs across all 37 states, with `statename` alongside.
    #:
    #: Needed because the ward layer is NOT national: it covers 24 of 37 states and Lagos is
    #: absent (verified: `state='Lagos'` returns count 0). Without this tier a third of Nigeria,
    #: including its largest city, would resolve to no administrative name — and an AOI with no
    #: place name is one Fahis cannot verify.
    grid3_lga_service: str = "NGA_LGA_Boundaries_2"
    #: geoBoundaries open release API. Keyless.
    #:
    #: ADM1 and ADM2 answer for Nigeria; **ADM3 returns 404**, which is why GRID3 is the ward
    #: source and this is the breadth source rather than the depth one.
    geoboundaries_base_url: str = "https://www.geoboundaries.org/api/current"

    # ESA WorldCover class codes. 40 = cropland, 80 = permanent water,
    # 50 = built-up. Used to count real hectares instead of estimating them.
    worldcover_cropland_class: int = 40
    worldcover_water_class: int = 80
    worldcover_builtup_class: int = 50
    # Pixel grid for the WorldCover / DEM reads. Smaller than the imagery tile
    # because these only need area statistics, not per-plot detail.
    exposure_tile_size: int = 256
    # A pixel this far below the AOI's median elevation is treated as
    # low-lying, i.e. where water collects first.
    dem_lowland_offset_m: float = 3.0

    # ---------------- Soil & agronomy ----------------
    soilgrids_base_url: str = "https://rest.isric.org/soilgrids/v2.0"
    soilgrids_depth: str = "0-5cm"
    # Clay above this (g/kg) drains slowly, so waterlogging persists and the
    # crop-damage window is longer.
    soilgrids_heavy_clay_threshold: float = 350.0

    # ---------------- Health reference ----------------
    malaria_atlas_url: str = "https://data.malariaatlas.org/geoserver"
    malaria_atlas_workspace: str = "Malaria"
    malaria_atlas_layer: str = "Global_Pf_Parasite_Rate"
    # Baseline PfPR below which a flood is not treated as a malaria trigger:
    # standing water raises transmission where the parasite already circulates.
    malaria_endemic_threshold: float = 0.05

    # ---------------- Inference ----------------
    torch_device: str = "cpu"

    # --- Derived-statistics layer (app/stats/, app/eo/terrain.py) -----------
    # Each of these gates one accuracy improvement that has a documented
    # non-ML fallback. Turning one off returns the pipeline to its previous
    # behaviour rather than breaking it, which is what makes them safe to
    # toggle on a live deployment.
    #
    #: Otsu per-scene SAR thresholding instead of the fixed -16 dB cut.
    #: Falls back automatically on any scene whose histogram is not bimodal.
    adaptive_sar_threshold: bool = True
    #: Subtract JRC Global Surface Water permanent water from inundation.
    #: The single largest false-positive reduction available.
    permanent_water_masking: bool = True
    #: HAND/TWI terrain analysis instead of the median-elevation proxy.
    #: The most expensive computation here, cached 30 days per AOI.
    terrain_analysis_enabled: bool = True
    #: SPI/API rainfall statistics alongside raw millimetres.
    rainfall_statistics_enabled: bool = True
    #: Apply the fitted confidence calibration map, when one exists.
    #: Default False: adopting a curve is an operator decision, not automatic.
    confidence_calibration_enabled: bool = False
    crop_stress_weights: str = "app/ml/weights/crop_stress.pt"
    sar_flood_weights: str = "app/ml/weights/sar_flood.pt"
    # Tile size for windowed COG reads, in pixels. 512 keeps peak RAM per tile
    # near ~1 MB/band at int16 while still amortising HTTP range overhead.
    tile_size: int = 512
    #: How far back a scene search reaches, in days. **Not a per-sensor value, and 12 was
    #: silently excluding Landsat entirely.**
    #:
    #: 12 fits Sentinel-2's 5-day and Sentinel-1's ~6-day revisit with a margin for a missed pass.
    #: Landsat revisits every **16 days**, so at 12 it could never return a scene: measured live at
    #: Ikorodu, `days_back=12` gave 0 Landsat scenes while `days_back=30` gave the 29.5%-cloud pass
    #: from 2026-07-23 and 90 gave a 16.6% one. Adding the sensor without widening this window
    #: would have been a source with no reachable data — worse than not adding it, because
    #: `/health` would report it live.
    #:
    #: 20 covers one full Landsat cycle plus a missed pass. Deliberately NOT larger: a scene's age
    #: is the honest limit on how current a reading is, and `_best_optical` sorts by cloud with
    #: recency only as a tie-break — so a wide window lets a clear-but-stale scene outrank a
    #: cloudier fresh one. The scene date now travels on the assessment (see `RiskAssessment`) so a
    #: reader can see which they got.
    max_scene_age_days: int = 20
    forecast_horizon_days: int = 7

    # ---------------- Advisory generation ----------------
    # Which path generates advisories:
    #   "auto"      portable if LLM_BASE_URL is set, else Anthropic SDK if
    #               ANTHROPIC_API_KEY is set, else deterministic template.
    #   "openai"    force the portable /v1/chat/completions path.
    #   "anthropic" force the native SDK — keeps server-side fallback and effort
    #               control, which the portable path cannot express.
    #   "template"  no generation. English template output.
    advisory_provider: str = "auto"

    # Native Anthropic path (optional).
    anthropic_api_key: str | None = None
    advisory_model: str = "claude-opus-5"
    advisory_effort: str = "medium"
    advisory_max_tokens: int = 2_000
    #: Model for the portable path. Blank falls through to `llm_model`, so a
    #: single-provider deployment configures one model name, not two.
    advisory_model_openai: str = ""

    # ---------------- Dispatch channels ----------------
    # WhatsApp Cloud API
    whatsapp_phone_number_id: str | None = None
    whatsapp_access_token: str | None = None
    whatsapp_api_version: str = "v21.0"
    # Outside a 24-hour conversation window Meta only delivers pre-approved
    # templates; free-form sends return 200 and are dropped. Must live here
    # rather than in os.getenv — os.getenv does NOT read the .env file, so a
    # local run would silently fall back to free-form and lose every message.
    whatsapp_template_name: str | None = None
    whatsapp_template_lang: str = "en"

    # Telegram Bot API
    telegram_bot_token: str | None = None

    # Signal — signal-cli-rest-api (self-hosted, no vendor lock-in)
    signal_api_url: str | None = None
    signal_sender_number: str | None = None

    # Email — SMTP
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "alerts@shelter.africa"
    smtp_use_tls: bool = True
    #: Display name on outbound mail. Brevo and most providers show this beside the
    #: address, and an unnamed sender is markedly more likely to be filed as spam.
    smtp_from_name: str = "SHELTER Early Warning"
    #: Where replies go. Brevo's SMTP relay address is not a mailbox, so without
    #: this a subscriber replying to an alert gets a bounce.
    smtp_reply_to: str | None = None

    # --- Transactional email transport (app/iam/mailer.py) ------------------
    #
    # Two ways to reach the same Brevo account, and they fail differently:
    #
    #   brevo_api  HTTPS POST to /v3/smtp/email. Authenticates with the API key
    #              alone, so it is unaffected by Brevo's sender-IP allow-list, and
    #              returns a real messageId for delivery auditing.
    #   smtp       The relay on :587. Needs a separate SMTP key AND the sending
    #              host's egress IP on Brevo's permit list — which makes it fragile
    #              on a laptop, a CI runner, or any VPS that changes address.
    #
    # `auto` prefers the API when a key is present, then SMTP, then nothing. This
    # mirrors ADVISORY_PROVIDER: a *forced* provider that is not configured degrades
    # to noop rather than silently routing elsewhere, because an explicit choice
    # quietly ignored is worse than mail not being sent.
    #: "auto" | "brevo_api" | "smtp" | "noop"
    notification_provider: str = "auto"

    brevo_api_key: str | None = None
    brevo_api_url: str = "https://api.brevo.com/v3/smtp/email"
    #: Sender for the API path. Must be a verified sender or Brevo rejects with 400.
    #: Separate from `smtp_from` because the two transports can legitimately send as
    #: different addresses — the relay is often domain-verified while the API sender
    #: is a single confirmed mailbox.
    brevo_sender_email: str | None = None
    brevo_sender_name: str = "SHELTER"
    brevo_reply_to_email: str | None = None
    #: Brevo tags on every message, so transactional onboarding mail is filterable
    #: from alert traffic in their dashboard.
    brevo_tag: str = "shelter-transactional"

    # --- IAM: identity, onboarding, API keys (app/iam/) ---------------------
    #
    # MongoDB Atlas is the identity store, deliberately separate from Postgres —
    # see app/iam/store.py for why. Absent a URI the IAM endpoints return 503 and
    # the rest of the pipeline is unaffected: identity is an onboarding surface,
    # not something the satellite path depends on.
    #: Atlas connection string. Named MONGO_URL to match the operator's existing
    #: .env rather than inventing MONGODB_URI — pydantic-settings runs with
    #: `extra="ignore"`, so a name mismatch would resolve to None silently and the
    #: IAM endpoints would report "not configured" with the credentials sitting
    #: right there in the file. `tests/test_config.py` guards the pairing.
    mongo_url: str | None = None
    mongo_database: str = "shelter_iam"
    #: Server-selection timeout. Short on purpose: a signup must fail fast with a
    #: clear error rather than hanging a browser for 30 seconds on a bad URI.
    mongo_timeout_ms: int = 5000

    #: Signing secret for portal session tokens. Falls back to API_KEY locally;
    #: preflight makes it a hard error in production, because a predictable signing
    #: key lets anyone mint a session for any account.
    iam_jwt_secret: str | None = None
    #: Portal session lifetime. 12 hours: long enough for a working day without a
    #: re-login, short enough that a token copied from a shared device expires.
    #:
    #: This is the ABSOLUTE ceiling. `iam_idle_timeout_minutes` below is the one that
    #: normally ends a session — the two answer different questions ("how long may a
    #: session ever live" vs "how long may it sit unattended").
    iam_session_minutes: int = 720
    #: Idle window. A session with no *real user activity* for this long is refused,
    #: server-side, regardless of the JWT's own expiry.
    #:
    #: Refreshed only by `POST /iam/session/activity`, which the browser calls in
    #: response to genuine input — never by request traffic, or a dashboard that polls
    #: its own status endpoint would keep itself alive forever with nobody present.
    iam_idle_timeout_minutes: int = 15
    #: How long before idle expiry the browser starts its countdown warning. 120s is
    #: enough to notice a modal, return to the machine and dismiss it, without being so
    #: long that the warning becomes background furniture.
    iam_idle_warning_seconds: int = 120
    #: Failed logins per email before a temporary lock. Argon2 makes each attempt
    #: cost ~50 ms; this stops that cost being turned into a DoS and blunts
    #: credential stuffing.
    iam_max_login_attempts: int = 8
    iam_login_lockout_minutes: int = 15
    # ---- Place search (Nominatim / OpenStreetMap) ----
    #
    # Proxied through the backend rather than called from the browser: every query names a
    # district and belongs to someone setting up monitoring for their own farm, and the
    # 1 req/sec policy is per-IP, so type-ahead from a browser would be throttled instantly.
    #
    # Data is ODbL — attribution ("© OpenStreetMap contributors") is a LICENCE CONDITION
    # wherever results are shown, not a courtesy. `eo/places.ATTRIBUTION` is the one string.
    nominatim_url: str = "https://nominatim.openstreetmap.org"
    #: Minimum seconds between upstream calls, enforced process-wide. The public instance's
    #: policy is one request per second; going faster earns a block for the whole deployment.
    nominatim_min_interval_seconds: float = 1.0
    #: Short — this sits in a search-as-you-type path and must degrade rather than hang.
    nominatim_timeout_seconds: float = 4.0
    #: Long — place names do not move, and this is what keeps a busy deployment inside the
    #: usage policy.
    nominatim_cache_ttl_seconds: int = 604_800
    #: Path to an MMDB city database, used to turn an audit entry's IP into
    #: "Warrington, United Kingdom" in the portal.
    #:
    #: Vendor-neutral on purpose. Two databases ship in this format and either works:
    #:   * **DB-IP City Lite** — CC-BY 4.0, no account, no key. `make geoip` uses this.
    #:   * **MaxMind GeoLite2** — free but requires signup and a licence key.
    #: The filename is `city.mmdb` rather than a vendor name so swapping one for the other
    #: needs no config change, and nothing claims to be GeoLite2 when it is not.
    #:
    #: Optional and absent by default — the file is gitignored and not in the image, and
    #: `iam/geo.py` degrades to showing the raw IP rather than failing. Fetch it with
    #: `make geoip` (needs a free MaxMind licence key).
    #:
    #: Self-hosted rather than an API on purpose: every lookup is a subscriber's IP, and
    #: this subscriber list is farmers in named districts. Those addresses must not leave
    #: the deployment.
    #:
    #: The MaxMind licence key is deliberately NOT a setting: it is read by `make geoip`
    #: from the environment and never at runtime, so declaring it here would be an orphan —
    #: which `tests/test_config.py` correctly rejects.
    geoip_database_path: str = "/app/data/city.mmdb"
    # ---- Breached-password screening (Have I Been Pwned) ----
    #
    # Checks a candidate password against HIBP's corpus of ~900M exposed passwords using
    # their k-anonymity range API: only the first FIVE characters of the SHA-1 hash are
    # sent, and matching happens locally. The password, its full hash, the account and the
    # result all stay inside this process.
    #
    # Advisory, and it FAILS OPEN — see `iam/breached.py`. Refusing a signup because HIBP
    # was briefly unreachable would block a farmer from registering for flood warnings and
    # hand them an error they cannot act on.
    hibp_enabled: bool = True
    hibp_range_url: str = "https://api.pwnedpasswords.com/range"
    #: Short: this sits in the request path of a signup form. A slow check must degrade to
    #: "not breached" rather than making the user wait.
    hibp_timeout_seconds: float = 3.0
    #: Prefix buckets are public ranges shared by ~1-in-a-million hashes, so caching them
    #: leaks nothing about any account. A day keeps a burst of similar weak passwords to
    #: one upstream request.
    hibp_cache_ttl_seconds: int = 86_400
    #: How long an email-verification link stays valid.
    iam_verification_ttl_hours: int = 48
    #: Cap on live API keys per aggregator. Bounded so a compromised portal session
    #: cannot mint thousands of keys before anyone notices.
    iam_max_api_keys_per_account: int = 10
    #: Days of inactivity before a key is flagged `stale` in the portal. An unused
    #: live key is pure liability — nothing breaks when it is revoked.
    iam_key_stale_days: int = 90
    #: How far ahead an expiring key is flagged, so a partner is warned before the
    #: outage rather than by it.
    iam_key_expiry_warning_days: int = 14
    #: Default grace window, in hours, during which a rotated key still works. Lets a
    #: partner deploy the replacement before the old one dies — without it, rotation
    #: means choosing between an outage and leaving a compromised key live.
    iam_key_rotation_grace_hours: int = 24
    #: Audit entries retained per key. Bounded so the collection cannot grow without
    #: limit on a high-traffic integration; the recent history is what an incident
    #: review needs.
    iam_key_audit_retention_days: int = 180
    #: Audit-log retention, in days. 2 years: long enough for an insurance or
    #: regulatory review, and the TTL is stamped at insert so it cannot be shortened
    #: retroactively for entries already written.
    iam_audit_retention_days: int = 730

    # --- Passwordless and second-factor auth (app/iam/passwordless.py) ------
    #
    #: Magic-link sign-in. The primary path for individuals: no password to forget,
    #: mistype, or have written on a slip. Requires a working email transport, so it is
    #: gated — with no mailer the button would silently do nothing.
    iam_magic_link_enabled: bool = True
    #: Offer TOTP. Never forced: mandatory 2FA on a farmer with one handset and no
    #: authenticator app locks them out of their own flood warnings. Encouraged for
    #: commercial accounts, whose keys can read hundreds of farmers' data.
    iam_totp_enabled: bool = True
    #: Require TOTP for commercial accounts once they have enrolled. Off by default so
    #: enrolling is not a one-way door before an operator has stored recovery codes.
    iam_totp_required_for_commercial: bool = False
    #: Whether the deprecated shared `X-SHELTER-Key` may still authenticate platform
    #: routes.
    #:
    #: True keeps a pre-IAM deployment working during migration — a hard cutover would
    #: strand any frontend still holding the old key, and the failure would be a silent
    #: 401 on subscriber registration, i.e. signups failing in front of real users.
    #:
    #: Refused in production regardless once MONGO_URL is set (see
    #: `iam/platform._legacy_allowed`): at that point a scoped service-account key is
    #: available and the shared key is strictly worse.
    iam_legacy_shared_key_enabled: bool = True

    # Slack — incoming webhook or bot token
    slack_bot_token: str | None = None
    slack_default_channel: str = "#shelter-alerts"

    # NIGCOMSAT-1R broadcast — the offline last mile.
    # One-way push to the satellite footprint; no consumer internet required.
    # Payloads are hard-capped because broadcast bandwidth is the scarce resource.
    nigcomsat_gateway_url: str | None = None
    nigcomsat_api_key: str | None = None
    nigcomsat_beam_id: str = "NIG-1R-KU-WA"  # Ku-band, West Africa footprint
    nigcomsat_max_payload_bytes: int = 280
    # Escalate to broadcast when every terrestrial channel failed, or when the
    # alert is at/above this severity regardless of terrestrial success.
    nigcomsat_always_broadcast_at: str = "warning"

    # Generic outbound webhook (subscriber-supplied HTTPS endpoint)
    webhook_timeout_seconds: float = 15.0
    webhook_signing_secret: str | None = None

    # --- Webhook subscription engine (app/webhooks/) ------------------------
    # Distinct from the dispatcher above: that delivers one alert to one farmer's
    # chosen channel with no retry, this is the business-integration surface with
    # at-least-once delivery. See app/webhooks/engine.py.
    #
    #: Master switch. Off disables the endpoints and the retry sweep; nothing else
    #: in the pipeline changes.
    webhook_engine_enabled: bool = True
    #: Consecutive failures before an endpoint is auto-disabled. A dead
    #: integration would otherwise consume retry budget indefinitely, and the
    #: business is not watching. 20 spans several days of the retry schedule.
    webhook_max_consecutive_failures: int = 20
    #: Deliveries processed per retry sweep. Bounded so one badly-behaved endpoint
    #: with thousands of queued rows cannot monopolise a scheduler cycle.
    webhook_sweep_batch_size: int = 50
    #: How long delivery history is retained, in days. Long enough to answer "did
    #: you send it last week?", short enough that the table stays bounded.
    webhook_history_retention_days: int = 30

    # ---------------- Web search (SearXNG) ----------------
    # Self-hosted, for the same reason Signal and MinIO are: every query here
    # names a Nigerian district and a hazard, and routing that through a hosted
    # US API cuts against the sovereignty property the broadcast layer provides.
    #
    # Used ONLY by Fahis (verification) and chat. Never by Scout/Analyst/Oracle —
    # web prose in a risk score would break both reproducibility and the
    # never-invent-data rule.
    #: Which search backend Fahis uses: "searxng" | "tavily" | "none".
    #:
    #: Fahis is the only consumer. Without a backend it records NOT_ATTEMPTED — an outage, never a
    #: non-finding — so a deployment with no search still runs and simply cannot measure precision.
    #:
    #: **The two are NOT interchangeable behind one set of variables**, which is worth stating
    #: because the names invite the assumption. SearXNG is `GET /search?q=…&format=json` with no
    #: auth; Tavily is `POST /search` with the key in the JSON body, `include_domains` as an array
    #: and `days` as an integer. Pointing `TAVILY_API_BASE` at a SearXNG instance 404s. Hence a
    #: provider switch with two adapters rather than a shared URL.
    #:
    #: Neither ships in `docker-compose.yml`. Self-hosting SearXNG is the sovereignty-preserving
    #: option — every query names a Nigerian district and a hazard — but running it is an operator's
    #: choice, not a default that consumes resources on every deployment.
    search_provider: str = "none"

    searxng_url: str | None = None
    #: Optional bearer token, when SearXNG sits behind an authenticating proxy.
    searxng_api_key: str | None = None

    #: Tavily API key. Managed search for anyone who does not want to self-host.
    #:
    #: A credential, so no default — `test_config.py` fails the build on a credential with a
    #: non-None default.
    tavily_api_key: str | None = None
    #: Tavily endpoint. Overridable for a self-hosted or proxied deployment.
    tavily_api_base: str = "https://api.tavily.com"
    #: `basic` or `advanced`. Advanced costs more credits and returns longer extracts; basic is
    #: sufficient here because Fahis needs to know whether an event was reported, not to read the
    #: article.
    tavily_search_depth: str = "basic"
    #: Comma-separated SearXNG engine names. Empty means the instance default.
    #: SearXNG categories to query, comma-separated.
    #:
    #: **This decides whether results carry publication dates.** Measured on a live instance with
    #: one query: `general` returned 29 results and ZERO dates (duckduckgo, google cse); `news`
    #: returned 25 with dates on 23 (bing news, reuters, wikinews).
    #:
    #: Fahis asks whether a hazard occurred in a specific WINDOW, which is unanswerable without
    #: dates — and a model matching on place and hazard words alone will confirm a 2026 warning with
    #: a 2019 article. So the default is the category that supplies them.
    #:
    #: `news,general` widens recall at the cost of undated results, which `_recency` then reports as
    #: "unknown date" and which cannot support CONFIRMED on their own. Worth it for an area news
    #: engines cover thinly.
    search_categories: str = "news"

    #: Whether to pass SearXNG's coarse `time_range` filter. OFF by default.
    #:
    #: Measured: with the filter on, the news category returned 9 results and zero publication
    #: dates; with it off, 3 results of which 2 were dated. It widens the engine set to ones that do
    #: not report dates — trading the signal verification needs for volume it cannot date.
    #:
    #: Redundant as well as harmful here: `fahis._recency` classifies every source against the
    #: window and `_guard_verdict` downgrades a CONFIRMED whose dated sources all predate it, so the
    #: filtering already happens where it can be reasoned about.
    searxng_time_range: bool = False

    searxng_engines: str = ""
    search_language: str = "en"
    #: Domains treated as authoritative. A `confirmed` verdict requires one of
    #: these or a media domain — an aggregator alone is not corroboration.
    search_official_domains: str = (
        "nema.gov.ng,nimet.gov.ng,nihsa.gov.ng,ncdc.gov.ng,fmino.gov.ng,"
        "reliefweb.int,who.int,fao.org,unocha.org,fews.net"
    )
    #: Established outlets. Weaker than official, still citable.
    search_media_domains: str = (
        "premiumtimesng.com,punchng.com,vanguardngr.com,dailytrust.com,"
        "thisdaylive.com,channelstv.com,bbc.com,reuters.com,aljazeera.com"
    )
    #: Never cited. Our own site is excluded automatically via `public_site_url`;
    #: this is for additional mirrors that would republish our alerts and let
    #: verification confirm itself.
    search_exclude_domains: str = ""

    # ---------------- LLM transport (OpenAI-compatible) ----------------
    # Serves Fahis's adjudication and Herald's chat. Advisory generation stays on
    # the Anthropic SDK — its grounding rule and template fallback are tested and
    # must not change.
    #
    # Any `/v1/chat/completions` server works. Point this at a local vLLM to keep
    # inference on-premise.
    #: Reasoning depth for models that think before answering. Blank omits the parameter.
    #:
    #: **This is the single highest-leverage LLM setting on this deployment.** A reasoning model
    #: spends completion tokens on internal thinking before emitting any visible text, and that
    #: thinking counts against `max_tokens`. Measured against Gemini 2.5 Flash on the live
    #: endpoint, for one explanation prompt:
    #:
    #:     (unset)  ~1200-2200 completion tokens, frequently truncated mid-sentence
    #:     "none"           37 completion tokens, complete every time
    #:
    #: So `none` is roughly 40x cheaper, materially faster, and — because the visible answer is
    #: no longer competing with the thinking for budget — it is what removes truncation rather
    #: than merely making it rarer. Raising `max_tokens` chases a moving target, since the
    #: thinking budget varies per call.
    #:
    #: `none` is right for SHELTER's LLM work specifically: every surface either narrates
    #: measurements the Oracle already computed or drafts an advisory from a fixed evidence list.
    #: None of it is a reasoning problem. Keep thinking on only if a future surface has to weigh
    #: something genuinely open-ended.
    #:
    #: Omitted when blank, because a provider that does not know the parameter 400s on it — the
    #: same negotiated-not-assumed rule as `llm_max_tokens_param`.
    llm_reasoning_effort: str = ""

    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 1_500

    # --- Provider compatibility knobs ---
    # Defaults target the widest support. Change only if your provider rejects a
    # request; each of these exists because at least one major provider differs.
    #
    # OpenAI's reasoning models (o1/o3/gpt-5 family) reject `max_tokens` and
    # require `max_completion_tokens`. Everything else accepts `max_tokens`.
    llm_max_tokens_param: str = "max_tokens"
    # Those same models accept only the default temperature and 400 on any
    # explicit value, so it has to be omitted rather than set.
    llm_supports_temperature: bool = True
    # Structured-output negotiation: "auto" tries json_schema -> json_object ->
    # prompt-only and uses the first accepted. Pin it to skip the probing when you
    # know what your provider supports.
    llm_structured_output_mode: str = "auto"
    #: Hard ceiling on tool round trips per request. An unbounded loop is a
    #: runaway cost and latency risk on a service that must answer during a flood.
    llm_max_tool_rounds: int = 4
    #: Tool results are truncated to this before going back to the model, so one
    #: verbose search cannot blow the context window.
    llm_tool_result_max_chars: int = 6_000

    # ---------------- Fahis (agent 5 — verification) ----------------
    fahis_enabled: bool = True
    #: Days to wait after the forecast window closes before searching. Local news
    #: and agency bulletins trail events, so checking immediately finds nothing.
    fahis_reporting_lag_days: int = 3
    #: How wide a recency filter to apply when searching.
    fahis_search_window_days: int = 14
    fahis_max_queries: int = 3
    fahis_results_per_query: int = 6
    fahis_max_sources: int = 10
    fahis_snippet_max_chars: int = 800
    #: Assessments verified per scheduler sweep. Bounded so a backlog drains
    #: gradually instead of flooding a self-hosted SearXNG in one burst.
    fahis_batch_size: int = 20

    # ---------------- Embeddings (same OpenAI-compatible transport) ----------
    # Optional. Used for chat memory retrieval and the RAG surface. Absent, chat
    # falls back to replaying recent turns — more tokens, same behaviour.
    #
    # Separate base URL because the economical setup is a tiny local embedding
    # model beside a frontier chat model. Blank inherits `llm_base_url`.
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_model: str = "text-embedding-3-small"
    #: Inputs are truncated to this before sending, so an over-long turn cannot
    #: be rejected by the provider's input limit.
    embedding_max_input_chars: int = 8_000

    # ---------------- Token budget ----------------
    # An always-on service across thousands of subscribers can spend without
    # bound if nothing counts. Both ceilings FAIL OPEN by default: refusing to
    # explain a flood warning because a counter is unreachable is the worse
    # failure. Neither gates advisory generation — that is the product.
    llm_budget_enabled: bool = True
    #: 0 disables this ceiling.
    llm_daily_token_budget_per_subscriber: int = 50_000
    llm_daily_token_budget_global: int = 5_000_000
    #: True refuses when the counter cannot be read. For cost-capped deployments.
    llm_budget_fail_closed: bool = False

    # ---------------- Chat (Herald conversational surface) ----------------
    chat_enabled: bool = True
    #: Answer common questions from assessment data with zero tokens — "what
    #: should I do", "how sure are you", "why did you send this". The answers are
    #: already computed; paraphrasing them through a model would spend tokens and
    #: is the step at which a figure could get invented.
    chat_deterministic_answers: bool = True
    #: Prior turns *retrieved by relevance* and sent as context. Smaller than a
    #: recency window because retrieval returns the ones that matter, so 6 useful
    #: turns beat 12 mostly-irrelevant ones at half the tokens.
    chat_context_turns: int = 6
    #: Turns returned by GET /chat/{id}/history for display. Unrelated to what is
    #: sent to the model.
    chat_history_turns: int = 40
    chat_max_tool_rounds: int = 3
    chat_search_max_results: int = 5
    #: Cache identical questions within a session. Subscribers re-ask, especially
    #: on poor connections where they cannot tell whether a message sent. 0
    #: disables.
    chat_cache_ttl_seconds: int = 1_800
    #: Per-session hourly turn cap, independent of the token budget — this bounds
    #: request rate, the budget bounds cost.
    chat_rate_limit_per_hour: int = 60

    # ---------------- Autonomous scheduler ----------------
    scheduler_enabled: bool = True
    # How often the watch loop re-evaluates every active subscription.
    scheduler_interval_seconds: int = 21_600  # 6 hours
    scheduler_jitter_seconds: int = 300

    #: How often to repair unattributed monitored areas, in hours. 0 disables the sweep.
    #:
    #: **Not every cycle.** Reconciliation walks every area in Postgres and does two Mongo lookups
    #: per unattributed one, so running it on the 6-hour watch loop would spend that work almost
    #: entirely on areas that are already correct. Daily is frequent enough: the gap it closes is
    #: an area that is monitored but unbillable, which costs revenue over weeks and not hours.
    #:
    #: Latency is not the constraint either way — attribution is written at creation, and this
    #: only catches the paths that could not: a transient Mongo failure, or an area predating the
    #: feature. `attribution_reconcile_hours=0` turns it off for a deployment that would rather
    #: run it from cron.
    attribution_reconcile_hours: int = 24

    # ---------------- Partner-triggered scans ----------------
    #: Per-AREA hourly cap on `POST /customers/{id}/areas/{aoi}/scan`.
    #:
    #: This is the one Partner API call that spends real satellite catalogue quota on demand, and
    #: it costs the same whether or not the answer can have changed. Sentinel-1 revisits West
    #: Africa about every 6 days, so a partner looping over a customer list every minute would
    #: re-read the same scene hundreds of times for an identical answer — a self-inflicted denial
    #: of service against the free upstreams every deployment shares.
    #:
    #: Per area rather than per key: the cost is incurred per footprint, and a per-key cap would
    #: let one aggregator with many customers starve the queue while a small one is throttled for a
    #: single plot. 4/hour sits just above the ~6-day revisit it can usefully observe while still
    #: allowing a retry after a transient failure.
    scan_trigger_rate_limit_per_hour: int = 4

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached so every import shares one parsed copy."""
    return Settings()


settings = get_settings()
