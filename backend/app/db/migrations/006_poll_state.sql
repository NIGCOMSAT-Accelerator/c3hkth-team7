-- Scout's poll state — what has been fetched, when, and whether it is still fresh.
--
-- Without this, Scout is stateless: every cycle re-searches every catalogue and
-- re-reads terrain that has not moved since the last ice age. The registry
-- (`app/eo/sources.py`) declares each source's real publication cadence; this
-- table is the memory that lets Scout honour it.
--
-- The cost saved is not marginal. On a 6-hour cycle a stateless Scout would
-- re-poll SoilGrids and the DEM ~1,460 times a year per AOI for an answer that
-- changes never.
--
-- WHY POSTGRES, NOT db1 (cache). This is durable state that must survive a restart
-- and be auditable — "when did we last successfully see this AOI's imagery?" is a
-- question an operator asks during an incident. Redis db1 is disposable by
-- construction (every write carries a TTL); a lost poll cursor would silently
-- re-trigger a full re-poll storm across every subscriber at once.

CREATE TABLE IF NOT EXISTS source_poll_state (
    -- One row per (area, source). A subscriber's areas poll independently, which
    -- is what makes the pipeline parallel across datasets.
    aoi_id            TEXT NOT NULL REFERENCES areas_of_interest(id) ON DELETE CASCADE,
    -- Registry key from `app/eo/sources.py` (e.g. 'element84', 'soilgrids').
    -- Deliberately NOT a Postgres enum: adding a source should not need a
    -- migration, and `tests/test_datasources.py` already guards the key set.
    source_key        TEXT NOT NULL,

    -- Last attempt, successful or not. Drives the re-poll decision.
    last_polled_at    TIMESTAMPTZ,
    -- Last attempt that actually returned usable data. The gap between these two
    -- is the health signal: polling hourly and succeeding never is invisible if
    -- you only record one of them.
    last_success_at   TIMESTAMPTZ,

    -- Consecutive failures. Drives exponential backoff, so a dead upstream is
    -- retried less often rather than hammered every cycle.
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,

    -- Object-store key for the cached payload, when this source is cacheable.
    -- A KEY, not the payload: MinIO holds windowed crops and JSON blobs, and
    -- Postgres holds the pointer. Same rule as `alerts.audio_key`.
    cache_key         TEXT,
    -- Bytes cached, for the storage-growth question an operator will eventually
    -- ask.
    cache_bytes       BIGINT,

    -- Free-form provenance: scene ids, collection actually used, which catalogue
    -- in the chain answered. Kept as JSONB because it differs per source kind and
    -- pinning a schema now would be a guess.
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,

    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (aoi_id, source_key)
);

-- Scout's scheduling query: "which (aoi, source) pairs are due?" Leads with
-- source_key because the cadence floor is per-source, so the planner can seek to
-- one source's rows and range-scan by staleness.
CREATE INDEX IF NOT EXISTS source_poll_state_due_idx
    ON source_poll_state (source_key, last_success_at NULLS FIRST);

-- The health query: which pairs are failing repeatedly. Partial, so it stays tiny
-- on a healthy deployment.
CREATE INDEX IF NOT EXISTS source_poll_state_failing_idx
    ON source_poll_state (consecutive_failures DESC, updated_at DESC)
    WHERE consecutive_failures > 0;
