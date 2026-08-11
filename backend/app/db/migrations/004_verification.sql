-- Fahis: ground-truth verification.
--
-- The one table in this schema that records whether the system was RIGHT. Model
-- weights are absent by default and inference falls back to threshold
-- heuristics, so without these rows there is no way to know whether any of it
-- works.
--
-- Two design points worth stating because they are easy to get wrong later:
--
-- 1. `verdict` has FIVE values, not three. 'unverified' (nobody reported it) and
--    'not_attempted' (search was down) are distinct from 'refuted' (someone
--    affirmatively said it did not happen). A flood in a remote LGA may simply
--    never be indexed — absence of evidence is not evidence of absence, and
--    collapsing these would record correct warnings as false alarms.
--
-- 2. Scheduling is a Postgres column, not a Redis delayed message. Redis Streams
--    have no delayed delivery, and a 7-day sleep in a worker would not survive a
--    restart. `verify_after` is a durable timestamp the scheduler sweeps.

DO $$ BEGIN
    CREATE TYPE verdict AS ENUM (
        'confirmed', 'partial', 'refuted', 'unverified', 'not_attempted'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- --------------------------------------------------------------------------- --
-- Verifications
-- --------------------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS verifications (
    id                TEXT PRIMARY KEY,
    assessment_id     TEXT NOT NULL,
    aoi_id            TEXT NOT NULL,
    alert_id          TEXT REFERENCES alerts(id) ON DELETE SET NULL,

    -- Copied from the assessment so a verdict reads without a join, and so it
    -- survives even if the assessment row is later corrected. Same reasoning as
    -- the denormalised assessment JSONB on `alerts`.
    claimed_hazard    hazard_type NOT NULL,
    claimed_severity  severity NOT NULL,
    assessed_at       TIMESTAMPTZ NOT NULL,

    verdict           verdict NOT NULL DEFAULT 'unverified',
    -- Confidence in the VERDICT, not in the original alert. One blog confirming
    -- is weak; two agencies is strong.
    confidence        DOUBLE PRECISION NOT NULL DEFAULT 0
                          CHECK (confidence BETWEEN 0 AND 1),
    rationale         TEXT NOT NULL DEFAULT '',

    -- Sources verbatim as the model saw them. Re-fetching later may return
    -- changed content, which would make the audit trail worthless.
    sources           JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Queries actually issued: provenance for why a search found nothing.
    queries           TEXT[] NOT NULL DEFAULT '{}',

    verified_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- One verdict per assessment. A re-run updates rather than accumulating.
    UNIQUE (assessment_id)
);

CREATE INDEX IF NOT EXISTS verifications_verdict_idx
    ON verifications (verdict, verified_at DESC);
CREATE INDEX IF NOT EXISTS verifications_aoi_idx
    ON verifications (aoi_id, verified_at DESC);
-- Partial index for the metrics query, which only ever reads trainable rows.
CREATE INDEX IF NOT EXISTS verifications_trainable_idx
    ON verifications (claimed_hazard, claimed_severity)
    WHERE verdict IN ('confirmed', 'refuted');

-- --------------------------------------------------------------------------- --
-- Deferred scheduling
-- --------------------------------------------------------------------------- --

-- When this assessment becomes eligible for verification. NULL means never —
-- below-threshold findings are not worth a search budget.
--
-- On `assessments` rather than a queue because the wait is days: Redis Streams
-- cannot delay delivery, and a sleeping worker would lose the timer on restart.
ALTER TABLE assessments
    ADD COLUMN IF NOT EXISTS verify_after TIMESTAMPTZ;

-- The scheduler's sweep: "assessments due for verification, not yet verified".
-- Partial index so it never scans the ones already done or never eligible.
CREATE INDEX IF NOT EXISTS assessments_verify_due_idx
    ON assessments (verify_after)
    WHERE verify_after IS NOT NULL;

-- --------------------------------------------------------------------------- --
-- Chat sessions
-- --------------------------------------------------------------------------- --

-- Herald's conversational surface. Scoped to a subscriber: chat can read that
-- subscriber's own alerts, and a session with no owner could be used to read
-- anyone's. Nullable only for an operator console session, which is API-key
-- gated instead.
CREATE TABLE IF NOT EXISTS chat_sessions (
    id             TEXT PRIMARY KEY,
    subscriber_id  TEXT REFERENCES subscribers(id) ON DELETE CASCADE,
    language       TEXT NOT NULL DEFAULT 'en',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chat_sessions_subscriber_idx
    ON chat_sessions (subscriber_id, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS chat_messages (
    id           BIGSERIAL PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role         TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content      TEXT NOT NULL,
    -- Sources cited in an assistant turn. Empty when the answer came only from
    -- the subscriber's own alert data.
    sources      JSONB NOT NULL DEFAULT '[]'::jsonb,
    tools_used   TEXT[] NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chat_messages_session_idx
    ON chat_messages (session_id, created_at);
