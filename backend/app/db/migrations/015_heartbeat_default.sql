-- Deliver low-risk readings too, so a subscriber can tell working monitoring from a dead pipeline.
--
-- ## Why the column default changes
--
-- `herald.DISPATCH_FLOOR` moved from ADVISORY to INFO: silence for three weeks is
-- indistinguishable from a broken pipeline, and the service promises 24/7 watching. A quiet
-- reading now goes out saying "we looked, nothing needs doing".
--
-- The two settings have to agree. A floor of INFO with a binding default of ADVISORY means the
-- platform generates the heartbeat and then every channel silently discards it — the feature would
-- look built and deliver nothing, which is worse than not shipping it.
--
-- ## Why existing rows are backfilled, and why that is the arguable part
--
-- Every binding created before this says `advisory`, because that was the default. Left alone, no
-- existing subscriber would ever receive a heartbeat — only accounts created after today would,
-- which is a silent two-tier product.
--
-- The backfill is therefore deliberate and it DOES change what current subscribers receive. That is
-- an opt-out being reset, which is normally the wrong thing to do to a preference. It is justified
-- here on two grounds:
--
--   * `advisory` on these rows was never a choice anyone expressed — it was the only value the
--     signup form and the API default ever produced for this field.
--   * The change adds roughly one message a day per plot and every one of them carries an
--     unsubscribe-equivalent: `PUT /subscribers/{id}/channels` with `min_severity: advisory`
--     restores the old behaviour per plot and per channel.
--
-- Anyone who has genuinely chosen a higher floor is untouched: only rows still sitting at exactly
-- `advisory` are moved, so `watch`, `warning` and `emergency` selections survive.
--
-- ## Why this is not four messages a day
--
-- The watch loop runs every 6 hours, so a naive floor of INFO would page four times daily per plot.
-- `herald._is_duplicate` suppresses an equal-or-lower severity inside `RESEND_WINDOW_HOURS` (18),
-- so a quiet plot produces about ONE heartbeat a day. An escalation still gets through immediately,
-- because the dedupe compares severities rather than merely checking for a recent send.

ALTER TABLE channel_bindings
    ALTER COLUMN min_severity SET DEFAULT 'info'::severity;

-- Only rows still at the old default. A subscriber who chose `watch` or higher keeps it.
UPDATE channel_bindings
   SET min_severity = 'info'::severity
 WHERE min_severity = 'advisory'::severity;
