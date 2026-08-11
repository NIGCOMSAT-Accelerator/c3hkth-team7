-- Per-area notification channels.
--
-- ## What this enables
--
-- A subscriber can now say "flood alerts for the rice plot by SMS, crop alerts for the palm
-- plantation by email". Before this, `channel_bindings` was keyed on `subscriber_id` alone, so
-- every plot shared one delivery configuration and there was **no way to change it after signup
-- at all** — a mistyped phone number was permanent.
--
-- ## Why `aoi_id` is NULLABLE, and why that is the whole design
--
-- NULL means **"applies to every area"**, which is exactly what every existing row means. So this
-- migration needs no data movement: the ~2 bindings already in the table keep working unchanged,
-- and keep working for plots added later.
--
-- The alternative — backfilling one row per (subscriber, area) — was rejected. It multiplies rows
-- by the plot count, and it silently freezes the configuration: a subscriber with three plots
-- would get three copies, and a fourth plot added tomorrow would inherit nothing.
--
-- So resolution is: **area-specific rows if any exist for that area, otherwise the NULL-area
-- rows.** Specific overrides general, and the general case stays a single row. `repository`
-- implements that; `tests/test_per_area_channels.py` asserts it, including that adding an override
-- for one plot cannot affect another.
--
-- ## Why ON DELETE CASCADE
--
-- An override belongs to its area. Removing a plot must not leave a binding pointing at nothing —
-- that would be a row `channels_for` could still return, dispatching to an address for land the
-- subscriber no longer monitors.

ALTER TABLE channel_bindings
    ADD COLUMN IF NOT EXISTS aoi_id TEXT
        REFERENCES areas_of_interest (id) ON DELETE CASCADE;

-- The dispatch read is "every binding for this subscriber, area-specific or general", and it runs
-- once per alert per subscriber. Partial on NOT NULL because the general rows are already covered
-- by the existing subscriber_id index, and an override table is expected to stay small.
CREATE INDEX IF NOT EXISTS channel_bindings_aoi_idx
    ON channel_bindings (aoi_id)
    WHERE aoi_id IS NOT NULL;

-- One binding per (subscriber, area, channel).
--
-- Without this, saving an override twice creates two rows for the same channel and the subscriber
-- receives duplicate alerts — the failure mode is invisible until someone gets two identical SMS
-- about the same flood.
--
-- Two indexes rather than one, because NULL is not equal to itself in a UNIQUE constraint: the
-- general rows need their own uniqueness or `aoi_id IS NULL` duplicates slip through.
CREATE UNIQUE INDEX IF NOT EXISTS channel_bindings_area_channel_key
    ON channel_bindings (subscriber_id, aoi_id, channel)
    WHERE aoi_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS channel_bindings_general_channel_key
    ON channel_bindings (subscriber_id, channel)
    WHERE aoi_id IS NULL;
