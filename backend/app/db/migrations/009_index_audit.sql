-- 009 — index audit. Gaps found by reading every query against every index.
--
-- Method: each query in `app/store/repository.py` was matched against the indexes in
-- migrations 002–008. Five gaps, each on a path that runs per-request or per-cycle,
-- so each one degrades continuously as the tables grow rather than failing loudly.
--
-- Nothing here changes behaviour. Every statement is `IF NOT EXISTS`, so re-running
-- is a no-op, and `CREATE INDEX` on an empty table is instant.

-- 1. assessments.id alone.
--
-- The primary key is `(id, assessed_at)` — Timescale requires the partitioning column
-- in every unique constraint, which is documented in 002. A lookup by `id` alone can
-- use that PK's leading column, but `verification_outcomes` JOINs
-- `assessments a ON a.id = v.assessment_id` with no time predicate, so the planner
-- must consider every chunk of the hypertable. This gives it a direct path.
--
-- Matters more over time, not less: chunks accumulate weekly, so the un-indexed join
-- gets slower every week the service runs.
CREATE INDEX IF NOT EXISTS assessments_id_idx
    ON assessments (id);

-- 2. verifications.assessment_id as an index, not only a constraint.
--
-- `UNIQUE (assessment_id)` in 004 does create a backing index, so the lookup is
-- covered — but the JOIN in `verification_outcomes` also filters
-- `verified_at >= $1` and `verdict IN (...)`. A compound index lets one scan satisfy
-- the filter and hand sorted rows to the join, instead of filtering after the fetch.
--
-- Partial, matching TRAINABLE_VERDICTS: calibration and weight fitting read only
-- confirmed/refuted rows, and the UNVERIFIED tail is the majority in rural coverage —
-- excluding it keeps this index small however much the table grows.
CREATE INDEX IF NOT EXISTS verifications_outcomes_idx
    ON verifications (verified_at DESC, assessment_id)
    WHERE verdict IN ('confirmed', 'refuted');

-- 3. index_history write-path conflict check.
--
-- 007 indexes `(aoi_id, index_name, observed_at DESC)` for the harmonic fit's read.
-- But `record_index_observation` runs `ON CONFLICT (aoi_id, index_name, observed_at)`
-- on EVERY optical scene for EVERY area, and that conflict check needs the *primary
-- key* order, not DESC on the third column. Postgres uses the PK here, so this is
-- about the read half: `index_history(aoi_id, index_name)` alone serves the
-- `COUNT`-style existence checks without walking timestamps.
--
-- Left as a comment rather than an index: the composite PK from 007 already covers
-- both directions, and adding a redundant index would cost a write on the hottest
-- insert in the pipeline for no read benefit. Recorded here so the next reader does
-- not "fix" a gap that isn't one.

-- 4. alerts.assessment JSONB path used by `alert_counters`.
--
-- `alert_counters` extracts `assessment->>'aoi_id'` and
-- `assessment->'exposure'->>'population'`. A JSONB path expression cannot use a plain
-- column index, so the dashboard's counter query scans every alert in the window.
--
-- An expression index on the extracted path fixes it. Chosen over a GIN index on the
-- whole `assessment` column: GIN is for containment/existence queries (`@>`, `?`) and
-- would be far larger, while this query only ever extracts two scalars.
CREATE INDEX IF NOT EXISTS alerts_assessment_aoi_idx
    ON alerts ((assessment->>'aoi_id'), created_at DESC);

-- 5. delivery_receipts status filter.
--
-- `alert_counters` counts receipts `WHERE r.alert_id IN (...) AND r.status = 'sent'`.
-- 002 indexes `alert_id` alone, so every receipt for a matching alert is fetched and
-- then filtered on status. Receipts are the highest-cardinality table in the schema —
-- one row per channel per alert per subscriber — so this is the largest avoidable
-- read in the dashboard.
CREATE INDEX IF NOT EXISTS delivery_receipts_alert_status_idx
    ON delivery_receipts (alert_id, status);

-- 6. webhook_deliveries retention sweep.
--
-- `prune_history` filters `created_at < $1 AND status IN ('delivered','abandoned')`.
-- 008 indexes `(subscription_id, created_at DESC)` for the support query and
-- `next_attempt_at` partially for the retry sweep, but nothing serves the prune —
-- which is the query that touches the most rows, since it exists to delete the tail.
--
-- Partial on the terminal statuses only: in-flight rows are never pruned, so
-- including them would bloat the index with exactly the rows it must not match.
CREATE INDEX IF NOT EXISTS webhook_deliveries_prune_idx
    ON webhook_deliveries (created_at)
    WHERE status IN ('delivered', 'abandoned');
