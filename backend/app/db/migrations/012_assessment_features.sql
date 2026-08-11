-- Persist the FEATURES behind an assessment, so a verified outcome becomes a training row.
--
-- ## Why this is needed for the Fahis retraining loop
--
-- `verification_outcomes` already joins each verdict back to the assessment it judged, and yields
-- `(confidence, outcome)`. That is enough to CALIBRATE a confidence — a scalar map from claimed to
-- observed reliability — and it is not enough to RETRAIN anything, because a model needs the inputs
-- that produced the prediction, not just the prediction.
--
-- Everything required was already computed and then dropped at this boundary:
--
--   * `score_drivers`      the exact contribution of observed / forecast / exposure to the score
--   * `stress_attribution` which of the four CropStressNet channels drove the crop verdict
--   * measured fractions   inundated_fraction and stressed_crop_fraction, with their `*_measured`
--                          flags so "not measured" stays distinguishable from "measured as zero"
--   * provenance           when each leg observed, from which platform, by which method
--
-- Without them, a CONFIRMED flood tells us only "the pipeline was right at confidence 0.88" — useful
-- for calibration, useless for learning which inputs to weight differently next time.
--
-- ## Why JSONB rather than columns
--
-- The driver and attribution shapes belong to the model, not to the schema. `CropStressNet` gained a
-- fourth channel this week; a column per channel would have needed a migration for that, and would
-- need another when a fifth arrives. JSONB keeps the schema stable across model revisions, which is
-- the right trade for a training-set sidecar that nothing queries relationally.
--
-- The measured fractions ARE columns, because they are stable physical quantities that a fitting
-- query filters and aggregates on, and because a NULL there carries meaning a JSONB key cannot: it
-- is the "not measured" case the whole `*_measured` flag design exists to preserve.
--
-- All nullable with no default. An assessment written before this migration has genuinely unknown
-- features, and a zero would assert something false about it.

ALTER TABLE assessments
    ADD COLUMN IF NOT EXISTS score_drivers JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS stress_attribution JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- NULL means NOT MEASURED. 0.0 means measured and absent. The distinction is load-bearing:
    -- conflating them is what made a radar failure classify as drought.
    ADD COLUMN IF NOT EXISTS inundated_fraction DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS stressed_crop_fraction DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS observed_at_flood TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS observed_at_stress TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS platform_flood TEXT,
    ADD COLUMN IF NOT EXISTS platform_stress TEXT,
    ADD COLUMN IF NOT EXISTS method_flood TEXT,
    ADD COLUMN IF NOT EXISTS method_stress TEXT;

-- The retraining query is "every trainable verdict, newest first, with its features".
--
-- Partial on the two trainable verdicts only. PARTIAL is genuinely ambiguous — right area, wrong
-- hazard or severity — and UNVERIFIED means nobody reported it, which in rural Nigeria is the common
-- case and not evidence of anything. Indexing all five would make the index mostly rows the fitting
-- query must then discard.
CREATE INDEX IF NOT EXISTS verifications_trainable_idx
    ON verifications (verified_at DESC)
    WHERE verdict IN ('confirmed', 'refuted');
