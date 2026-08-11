-- 007 — vegetation-index history, for the seasonal anomaly baseline (step 6).
--
-- WHY THIS TABLE EXISTS
--
-- `stressed_crop_fraction` was computed as `NDVI < 0.35`, a fixed cut on a
-- quantity whose normal level is seasonal. That cannot tell a drowning rice field
-- in August from the same field bare in January, and it sets a floor on what
-- counts as stress: a plot that has crashed from 0.75 to 0.45 is in trouble and
-- the test says nothing.
--
-- `AnalystResult.stressed_crop_fraction` and the Oracle's evidence string both
-- already claimed "below its seasonal norm". There was no norm. This table is what
-- makes the existing claim true.
--
-- The fix needs each AOI's own index history, so `app/stats/anomaly.py` can fit a
-- harmonic (Fourier) seasonal curve and measure today's reading against it in
-- units of that AOI's own scatter.
--
-- WHY NOT REUSE `assessments`
--
-- `assessments` stores derived severity, not the raw index values -- `indices` is
-- not persisted anywhere, so the series required to fit a baseline is simply not
-- recoverable from it. This is the narrow time series that fit needs and nothing
-- more: five numbers plus a date.

CREATE TABLE IF NOT EXISTS index_history (
    aoi_id         TEXT NOT NULL,
    -- 'ndvi' | 'ndmi' | 'ndwi'. Not an enum: adding an index should not need a
    -- migration, and nothing branches on this value.
    index_name     TEXT NOT NULL,

    -- Day of year (1-366) is stored alongside the timestamp on purpose. The
    -- harmonic fit is a regression against day-of-year, and deriving it in SQL on
    -- every read would prevent the index below from being used for the ordering.
    day_of_year    SMALLINT NOT NULL CHECK (day_of_year BETWEEN 1 AND 366),

    mean           DOUBLE PRECISION NOT NULL,
    -- Carried so a future per-pixel refinement can weight observations by how much
    -- of the AOI was actually visible. A reading from 10% of the pixels should not
    -- influence the baseline as much as a clear one.
    valid_fraction DOUBLE PRECISION NOT NULL CHECK (valid_fraction BETWEEN 0 AND 1),

    observed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- One reading per AOI per index per timestamp. Re-running a scan for the same
    -- scene must not double-weight that date in the fit.
    PRIMARY KEY (aoi_id, index_name, observed_at)
);

-- The fit's exact access pattern: all readings for one AOI and one index, over a
-- multi-year window. Composite so the whole query is index-only.
CREATE INDEX IF NOT EXISTS index_history_series_idx
    ON index_history (aoi_id, index_name, observed_at DESC);
