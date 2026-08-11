-- Per-binding minimum risk score — the subscriber's own sensitivity dial.
--
-- ## What this enables, and why `min_severity` was not enough
--
-- `min_severity` is a five-step ladder. Between WATCH (score 0.40) and WARNING (0.60) there is a
-- 0.20-wide band with nothing in it, and that is exactly where subscribers disagree: a commercial
-- farm with irrigation wants everything from 0.30 up, a smallholder who walks an hour to act on a
-- warning wants nothing below 0.55. Both are "watch and up" on the ladder, so today they get
-- identical delivery.
--
-- So this is the continuous control the severity ladder cannot express — and asking for a NUMBER
-- rather than a category is why it belongs here rather than as a sixth severity label.
--
-- ## Why this is a DELIVERY filter and not a risk-model parameter
--
-- The one thing this must not become is a way for a subscriber to change what the Oracle decides.
-- `score`, `confidence` and `severity` stay deterministic functions of measured inputs — that is
-- what makes `tests/test_oracle.py` possible with no provider configured, and what lets a WARNING
-- be defended to a state agriculture officer.
--
-- The distinction is precise: **the assessment is unchanged and still persisted**; this column only
-- decides whether a given channel is used to tell someone about it. A subscriber raising their dial
-- to 0.9 still has every assessment in Postgres, still sees each one in the portal, and can still
-- read the history. They have opted out of being *messaged*, not out of being *watched*.
--
-- In particular it cannot reach `CONFIDENCE_ESCALATION_FLOOR`. A subscriber can make themselves
-- harder to reach; they cannot make an under-confident reading escalate. Filtering only ever
-- REMOVES a delivery, so the failure mode is a message not sent — never a warning invented.
--
-- ## Why NULL rather than 0.0
--
-- NULL means "no score filter, use `min_severity` alone", which is what every existing row means.
-- 0.0 would be indistinguishable from a subscriber who deliberately chose the lowest setting, and
-- the two need to stay separable: `NULL` lets the severity ladder govern completely, and a future
-- default change would silently rewrite an explicit choice. No backfill, no data movement.
--
-- CHECK matches `RiskAssessment.score` (`Field(ge=0, le=1)`), so an out-of-range dial is refused by
-- the database as well as by Pydantic — a value above 1.0 would silence the channel permanently
-- while looking like a valid setting.

ALTER TABLE channel_bindings
    ADD COLUMN IF NOT EXISTS min_score DOUBLE PRECISION
        CHECK (min_score IS NULL OR (min_score >= 0 AND min_score <= 1));

-- No index. This column is only ever read as part of the per-subscriber binding fetch that
-- `channel_bindings_aoi_idx` and the subscriber_id index already serve, and it is filtered in
-- Python inside `channels_for` rather than in SQL — the row count per subscriber is single digits,
-- so an index here would cost writes and save nothing.
