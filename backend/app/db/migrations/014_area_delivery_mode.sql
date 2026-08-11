-- Who contacts the farmer about one plot: SHELTER, or the aggregator who onboarded them.
--
-- ## The requirement
--
-- An aggregator that owns the customer relationship needs to be the only voice reaching their
-- farmer. Today SHELTER dispatches to the subscriber's own channels *and* publishes a webhook
-- event to the aggregator — so a partner running their own SMS pipeline produces two messages
-- about one flood, in two different voices, and cannot switch ours off.
--
-- ## The three modes
--
--   direct      SHELTER contacts the subscriber on their channels. The default, and what every
--               existing area does today.
--   webhook     SHELTER contacts NOBODY directly; the aggregator's webhook is the only delivery
--               and they relay it themselves. Their peace of mind: one voice, theirs.
--   both        Direct dispatch AND the webhook. For a partner who wants their own record of
--               every alert while SHELTER still reaches the farmer.
--
-- The webhook fires in every mode — it always has, and suppressing it would break integrations
-- that rely on it for reporting. What the mode governs is **direct** dispatch.
--
-- ## Why `direct` is the default and this column is NOT NULL
--
-- A nullable column would make "unset" mean something, and the safe reading of unset has to be
-- "keep contacting the farmer". Making that explicit is better than a NULL every reader has to
-- remember to interpret — and there is exactly one right answer for existing rows, so the
-- backfill is unambiguous.
--
-- **Only an aggregator-managed area may leave `direct`.** An individual who signed up themselves
-- has no aggregator to relay for them, so setting `webhook` on their plot would silence their
-- alerts entirely. `PUT /subscribers/{id}/channels` enforces that; `tests/test_delivery_mode.py`
-- asserts it, including that the enforcement is not merely documented.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'delivery_mode') THEN
        CREATE TYPE delivery_mode AS ENUM ('direct', 'webhook', 'both');
    END IF;
END
$$;

ALTER TABLE areas_of_interest
    ADD COLUMN IF NOT EXISTS delivery_mode delivery_mode NOT NULL DEFAULT 'direct';

-- The dispatch read is "what mode is this one area in", answered per alert. Indexed only on the
-- non-default values: `direct` is the overwhelming majority and already the fast path, so an
-- index covering it would be mostly dead weight.
CREATE INDEX IF NOT EXISTS areas_delivery_mode_idx
    ON areas_of_interest (delivery_mode)
    WHERE delivery_mode <> 'direct';
