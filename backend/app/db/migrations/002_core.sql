-- Core schema: subscribers, areas, channels, assessments, alerts.
--
-- Design notes that matter later:
--
-- * Enums are Postgres enums, not free-text CHECKs, and their labels are the
--   exact wire values from app/models/enums.py. That means a rename needs a
--   migration on both sides — which is the intent, since those labels are the
--   wire contract.
--
-- * AOIs carry BOTH a bbox and a geometry column. The bbox is what the STAC and
--   COG layers consume (they take west/south/east/north and nothing else); the
--   geometry is what PostGIS indexes. Keeping both means the imagery pipeline is
--   untouched by this migration while spatial queries become index lookups.
--
-- * `assessments` is append-only. The previous Redis implementation kept ONE
--   assessment per AOI under a 48-hour TTL, overwriting it every cycle, which
--   made a historical timeline impossible to build. This table is the fix.

-- --------------------------------------------------------------------------- --
-- Enums — labels must match app/models/enums.py exactly
-- --------------------------------------------------------------------------- --

DO $$ BEGIN
    CREATE TYPE severity AS ENUM ('info', 'advisory', 'watch', 'warning', 'emergency');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE hazard_type AS ENUM (
        'crop_waterlogging', 'crop_drought_stress', 'crop_vegetation_anomaly',
        'flood_inundation', 'flood_forecast', 'malaria_risk'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE channel AS ENUM (
        'whatsapp', 'telegram', 'signal', 'email', 'slack', 'webhook',
        'nigcomsat_broadcast'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE subscriber_kind AS ENUM (
        'farmer', 'cooperative', 'government', 'emergency_responder',
        'public_health', 'insurer'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE delivery_status AS ENUM ('pending', 'sent', 'failed', 'skipped');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- --------------------------------------------------------------------------- --
-- Subscribers
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS subscribers (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    kind         subscriber_kind NOT NULL DEFAULT 'farmer',
    language     TEXT NOT NULL DEFAULT 'en',
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The scheduler's hot path is "every active subscriber", every cycle. Partial
-- index so it never reads the paused ones.
CREATE INDEX IF NOT EXISTS subscribers_active_idx
    ON subscribers (created_at) WHERE active;

-- --------------------------------------------------------------------------- --
-- Areas of interest
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS areas_of_interest (
    id             TEXT PRIMARY KEY,
    subscriber_id  TEXT NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,

    -- What the EO layer consumes. app/eo/* takes a BBox and nothing else, so
    -- these stay first-class rather than being derived from `geom` per read.
    west           DOUBLE PRECISION NOT NULL CHECK (west  BETWEEN -180 AND 180),
    south          DOUBLE PRECISION NOT NULL CHECK (south BETWEEN  -90 AND  90),
    east           DOUBLE PRECISION NOT NULL CHECK (east  BETWEEN -180 AND 180),
    north          DOUBLE PRECISION NOT NULL CHECK (north BETWEEN  -90 AND  90),
    CONSTRAINT aoi_east_of_west   CHECK (east  > west),
    CONSTRAINT aoi_north_of_south CHECK (north > south),

    -- What PostGIS indexes. Generated, so it can never drift from the bbox
    -- above — the two representations cannot disagree.
    geom           GEOGRAPHY(POLYGON, 4326) GENERATED ALWAYS AS (
                       ST_MakeEnvelope(west, south, east, north, 4326)::geography
                   ) STORED,

    country        TEXT NOT NULL DEFAULT 'NG',
    admin1         TEXT,
    admin2         TEXT,
    crop           TEXT,
    hectares       DOUBLE PRECISION,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The index that turns "who is inside this footprint?" from O(subscribers) into
-- a lookup. This is the single biggest reason to be on PostGIS.
CREATE INDEX IF NOT EXISTS aoi_geom_idx ON areas_of_interest USING GIST (geom);
CREATE INDEX IF NOT EXISTS aoi_subscriber_idx ON areas_of_interest (subscriber_id);

-- --------------------------------------------------------------------------- --
-- Channel bindings
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS channel_bindings (
    id             BIGSERIAL PRIMARY KEY,
    subscriber_id  TEXT NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    channel        channel NOT NULL,
    address        TEXT NOT NULL,
    enabled        BOOLEAN NOT NULL DEFAULT TRUE,
    min_severity   severity NOT NULL DEFAULT 'advisory',

    -- Per-subscriber HMAC secret for the webhook channel. NULL for every other
    -- channel. This replaces the single process-wide WEBHOOK_SIGNING_SECRET,
    -- under which any subscriber knowing the secret could forge a signed
    -- payload for any other subscriber's endpoint.
    secret         TEXT,

    -- Set once the subscriber proves they control the address. Nothing verifies
    -- ownership today, so this stays nullable and unenforced until the
    -- verification round trip exists — but the column is here so the dispatcher
    -- can start reading it without another migration.
    verified_at    TIMESTAMPTZ,

    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (subscriber_id, channel, address)
);

CREATE INDEX IF NOT EXISTS channel_bindings_subscriber_idx
    ON channel_bindings (subscriber_id) WHERE enabled;

-- --------------------------------------------------------------------------- --
-- Assessments — append-only time series
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS assessments (
    id             TEXT NOT NULL,
    aoi_id         TEXT NOT NULL,
    aoi_name       TEXT NOT NULL,
    hazard         hazard_type NOT NULL,
    severity       severity NOT NULL,
    score          DOUBLE PRECISION NOT NULL CHECK (score BETWEEN 0 AND 1),
    confidence     DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    lead_time_days INTEGER NOT NULL DEFAULT 7,

    -- Nested structures stay JSONB. They are read as whole objects and never
    -- filtered on individually, so normalising them would buy nothing and cost
    -- a join per read. `forecast` gets its own table below because the timeline
    -- chart DOES query it per-day.
    exposure       JSONB NOT NULL DEFAULT '{}'::jsonb,
    soil           JSONB NOT NULL DEFAULT '{}'::jsonb,
    health         JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence       TEXT[] NOT NULL DEFAULT '{}',
    cascade        hazard_type[] NOT NULL DEFAULT '{}',
    data_sources   TEXT[] NOT NULL DEFAULT '{}',

    assessed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Timescale requires the partitioning column in every unique constraint,
    -- hence the composite key rather than `id` alone.
    PRIMARY KEY (id, assessed_at)
);

-- Hypertable if Timescale is present; plain table otherwise. The rest of the
-- schema does not care which, so an environment without the extension still
-- works — it just partitions less gracefully.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        PERFORM create_hypertable(
            'assessments', 'assessed_at',
            chunk_time_interval => INTERVAL '${TIMESCALE_CHUNK_INTERVAL_DAYS} days',
            if_not_exists       => TRUE,
            migrate_data        => TRUE
        );
    END IF;
END $$;

-- "This AOI's history, newest first" — the intelligence timeline query.
CREATE INDEX IF NOT EXISTS assessments_aoi_time_idx
    ON assessments (aoi_id, assessed_at DESC);
CREATE INDEX IF NOT EXISTS assessments_severity_time_idx
    ON assessments (severity, assessed_at DESC);

-- Forecast points, one row per day per assessment. Separate table because the
-- timeline chart reads them as a series and may aggregate across assessments.
CREATE TABLE IF NOT EXISTS forecast_points (
    assessment_id  TEXT NOT NULL,
    assessed_at    TIMESTAMPTZ NOT NULL,
    day            INTEGER NOT NULL CHECK (day >= 0),
    date           TIMESTAMPTZ NOT NULL,
    risk           DOUBLE PRECISION NOT NULL CHECK (risk BETWEEN 0 AND 1),
    rainfall_mm    DOUBLE PRECISION NOT NULL DEFAULT 0,
    note           TEXT,
    PRIMARY KEY (assessment_id, assessed_at, day)
);

CREATE INDEX IF NOT EXISTS forecast_points_assessment_idx
    ON forecast_points (assessment_id, assessed_at);

-- --------------------------------------------------------------------------- --
-- Alerts and receipts
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS alerts (
    id             TEXT PRIMARY KEY,
    subscriber_id  TEXT NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    assessment_id  TEXT NOT NULL,
    assessed_at    TIMESTAMPTZ NOT NULL,

    -- The assessment is embedded as JSONB as well as referenced. Deliberate
    -- denormalisation: an alert is a RECORD of what was said at the time. If the
    -- assessment row is ever corrected, the alert must still show what actually
    -- went out.
    assessment     JSONB NOT NULL,

    headline       TEXT NOT NULL,
    body           TEXT NOT NULL,
    actions        TEXT[] NOT NULL DEFAULT '{}',
    broadcast_text TEXT NOT NULL DEFAULT '',
    language       TEXT NOT NULL DEFAULT 'en',
    generated_by   TEXT NOT NULL DEFAULT 'template',

    -- Object-store key for the voice note, NULL when there isn't one. A key,
    -- not a URL: presigned URLs expire, so the durable reference is the key and
    -- the URL is minted per request.
    audio_key      TEXT,
    audio_seconds  DOUBLE PRECISION,

    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS alerts_subscriber_time_idx
    ON alerts (subscriber_id, created_at DESC);
CREATE INDEX IF NOT EXISTS alerts_feed_idx ON alerts (created_at DESC);

CREATE TABLE IF NOT EXISTS delivery_receipts (
    id                  BIGSERIAL PRIMARY KEY,
    alert_id            TEXT NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    channel             channel NOT NULL,
    address             TEXT NOT NULL,
    status              delivery_status NOT NULL,
    provider_message_id TEXT,
    error               TEXT,
    attempted_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS delivery_receipts_alert_idx
    ON delivery_receipts (alert_id);
