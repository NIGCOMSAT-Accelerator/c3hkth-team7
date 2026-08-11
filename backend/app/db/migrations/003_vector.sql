-- Vector store: embeddings for retrieval, and agent memory.
--
-- `${EMBEDDING_DIMENSIONS}` is substituted by the migration runner from
-- settings.embedding_dimensions. pgvector's column type carries its dimension,
-- so this cannot be a runtime parameter — changing the embedding model means a
-- new migration, which is the honest constraint.
--
-- IMPORTANT — read before wiring this into the advisory path:
--
-- The advisory generator is given ONLY `RiskAssessment.evidence` and is
-- instructed to introduce no numbers of its own (app/advisory/generator.py).
-- Two violations of that rule have already been found and removed from this
-- codebase. A retrieval layer is a machine for injecting text into prompts,
-- i.e. precisely that failure mode at scale.
--
-- So: retrieved context may inform TONE, PHRASING and ACTION SELECTION. It must
-- never become a cited figure. Keep the grounded generation call (evidence-only)
-- separate from any retrieval-augmented call, and when this table is eventually
-- read, serve the operator console and interactive Q&A from it — not the
-- autonomous advisory path.

-- --------------------------------------------------------------------------- --
-- Advisory embeddings — "what did we say last time this happened?"
--
-- STATUS: PROVISIONED, NOT YET POPULATED. Nothing writes or queries this table.
-- The indexes below exist so the retrieval surface can be built without a further
-- migration, but there is no read path today — `tests/test_schema_contract.py`
-- lists it in KNOWN_UNREACHED and will fail if that entry outlives the gap.
--
-- Not an oversight: what the operator console should retrieve is undecided, and
-- wiring retrieval into the *advisory* path is the thing the note below forbids.
-- `chat_messages.embedding` (migration 005) is the retrieval that does exist.
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS advisory_embeddings (
    id           BIGSERIAL PRIMARY KEY,
    alert_id     TEXT REFERENCES alerts(id) ON DELETE CASCADE,
    aoi_id       TEXT,
    hazard       hazard_type,
    severity     severity,
    language     TEXT NOT NULL DEFAULT 'en',

    -- The text the vector was computed from. Stored so a retrieval hit can be
    -- shown to an operator verbatim rather than re-fetched and possibly changed.
    content      TEXT NOT NULL,
    embedding    VECTOR(${EMBEDDING_DIMENSIONS}) NOT NULL,

    -- Copied from the AOI so a similarity search can be spatially filtered
    -- without joining. This is the whole reason for pgvector-in-Postgres over a
    -- standalone vector database: one query does both.
    geom         GEOGRAPHY(POLYGON, 4326),

    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- HNSW over cosine distance. Chosen over IVFFlat because it needs no training
-- step — IVFFlat's recall depends on being built after enough rows exist, which
-- is a footgun on a system that starts empty.
CREATE INDEX IF NOT EXISTS advisory_embeddings_hnsw_idx
    ON advisory_embeddings USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS advisory_embeddings_geom_idx
    ON advisory_embeddings USING GIST (geom);
CREATE INDEX IF NOT EXISTS advisory_embeddings_hazard_idx
    ON advisory_embeddings (hazard, severity);

-- --------------------------------------------------------------------------- --
-- Agent memory — shared, cross-run, spatially scoped
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS agent_memory (
    id           BIGSERIAL PRIMARY KEY,

    -- Which agent wrote it: 'scout' | 'analyst' | 'oracle' | 'herald', or a
    -- non-pipeline producer such as 'operator'. Free text rather than the
    -- job_stage enum because not every writer is a pipeline stage.
    agent        TEXT NOT NULL,

    -- Coarse bucket for filtering: 'observation' | 'outcome' | 'correction' |
    -- 'reference'. Corrections are the valuable ones — an operator recording
    -- that an alert was wrong is the only ground truth this system ever gets.
    kind         TEXT NOT NULL,

    aoi_id       TEXT,
    content      TEXT NOT NULL,
    embedding    VECTOR(${EMBEDDING_DIMENSIONS}),
    geom         GEOGRAPHY(POLYGON, 4326),

    -- Arbitrary structured detail. Kept as JSONB rather than columns because
    -- each agent's useful metadata differs and pinning it down now would be a
    -- guess.
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Memory that stops being true. NULL means it does not expire. Nothing
    -- deletes on this automatically yet; it exists so a retention job has a
    -- column to honour rather than needing a schema change first.
    expires_at   TIMESTAMPTZ,

    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agent_memory_hnsw_idx
    ON agent_memory USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS agent_memory_geom_idx
    ON agent_memory USING GIST (geom);
CREATE INDEX IF NOT EXISTS agent_memory_agent_kind_idx
    ON agent_memory (agent, kind, created_at DESC);
