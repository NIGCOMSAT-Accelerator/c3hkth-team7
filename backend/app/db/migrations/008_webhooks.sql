-- 008 — webhook subscriptions and the delivery ledger.
--
-- WHY THIS IS NOT THE EXISTING WEBHOOK DISPATCHER
--
-- `app/dispatch/webhook.py` posts an alert to a URL a *subscriber* configured, as
-- one channel among seven, inside the Herald's fan-out. One attempt, no retry, and
-- the only record is a `delivery_receipts` row. That is correct for its job:
-- delivering a warning to a farmer's chosen channel, where a failed webhook falls
-- back to SMS or NIGCOMSAT broadcast.
--
-- This is a different product. A business integrating SHELTER — an insurer's payout
-- engine, a state dashboard, a cooperative's own AI assistant — needs:
--
--   * to subscribe independently of being an alert recipient,
--   * event filtering, so a payout engine is not woken by every INFO advisory,
--   * per-endpoint secrets, so one leaked secret does not forge another's payloads,
--   * at-least-once delivery with backoff, because their endpoint will have outages,
--   * a queryable delivery history, because "did you send it?" is the first
--     question in every integration support thread.
--
-- None of that belongs in a fan-out step that must never block a flood warning.
--
-- THE DESIGN DECISION WORTH KNOWING
--
-- Attempts are recorded in Postgres and retried by the scheduler sweep, not by a
-- delayed queue message. Redis Streams cannot delay delivery, and a sleeping worker
-- would lose the timer on restart — the same reasoning as `assessments.verify_after`
-- for Fahis. `next_attempt_at` is a column the sweep queries.

CREATE TABLE IF NOT EXISTS webhook_subscriptions (
    id                TEXT PRIMARY KEY,

    -- Owner label, not an auth principal. Auth is the bootstrap API key at the
    -- edge; this is who to contact when an endpoint has been failing for a week.
    name              TEXT NOT NULL,
    url               TEXT NOT NULL,

    -- Per-endpoint HMAC secret. Distinct from the global
    -- WEBHOOK_SIGNING_SECRET so one business leaking their secret cannot forge
    -- payloads to another's endpoint.
    secret            TEXT NOT NULL,

    -- Event names this endpoint wants, e.g. {'shelter.alert','shelter.verification'}.
    -- Empty means all: a new integration should receive everything until it
    -- narrows, rather than silently receiving nothing.
    events            TEXT[] NOT NULL DEFAULT '{}',

    -- Minimum severity to deliver. A payout engine subscribes at 'warning' and is
    -- never woken by an INFO advisory. NULL means no floor.
    min_severity      severity,

    -- Optional AOI filter. NULL means every area; a state dashboard scopes to its
    -- own LGAs rather than filtering server responses client-side.
    aoi_ids           TEXT[] NOT NULL DEFAULT '{}',

    active            BOOLEAN NOT NULL DEFAULT true,

    -- Consecutive failures. The sweep disables an endpoint after
    -- WEBHOOK_MAX_CONSECUTIVE_FAILURES so a dead integration stops consuming
    -- retry budget forever. Reset to 0 on any success.
    failure_streak    INTEGER NOT NULL DEFAULT 0,
    last_success_at   TIMESTAMPTZ,
    last_failure_at   TIMESTAMPTZ,
    last_error        TEXT,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The sweep's query: active endpoints only.
CREATE INDEX IF NOT EXISTS webhook_subscriptions_active_idx
    ON webhook_subscriptions (active) WHERE active;


-- One row per (subscription, event) delivery, with its retry state.
--
-- Deliberately NOT one row per attempt: the operator question is "was this event
-- delivered to this endpoint?", and an append-per-attempt table answers it only
-- after a GROUP BY. Attempt detail lives in `attempts` and `last_error`.
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id                TEXT PRIMARY KEY,
    subscription_id   TEXT NOT NULL REFERENCES webhook_subscriptions(id) ON DELETE CASCADE,

    event             TEXT NOT NULL,
    -- The exact bytes signed and sent. Stored so a redelivery is byte-identical:
    -- re-serialising could reorder JSON keys and invalidate the signature the
    -- receiver is verifying.
    payload           JSONB NOT NULL,

    -- 'pending' | 'delivered' | 'failed' | 'abandoned'
    -- 'failed' is retryable; 'abandoned' has exhausted its attempts.
    status            TEXT NOT NULL DEFAULT 'pending',
    attempts          INTEGER NOT NULL DEFAULT 0,
    response_status   INTEGER,
    last_error        TEXT,

    -- When the scheduler should next try. NULL once terminal.
    next_attempt_at   TIMESTAMPTZ,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at      TIMESTAMPTZ
);

-- The retry sweep's exact access pattern: due, non-terminal deliveries.
-- Partial index so the growing tail of delivered rows costs nothing to skip.
CREATE INDEX IF NOT EXISTS webhook_deliveries_due_idx
    ON webhook_deliveries (next_attempt_at)
    WHERE status IN ('pending', 'failed');

-- The support-thread query: "show me everything for this endpoint, newest first".
CREATE INDEX IF NOT EXISTS webhook_deliveries_subscription_idx
    ON webhook_deliveries (subscription_id, created_at DESC);
