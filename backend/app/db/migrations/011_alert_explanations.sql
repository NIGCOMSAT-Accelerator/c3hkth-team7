-- The three explanation surfaces, stored with the alert they belong to.
--
-- One JSONB column rather than three TEXT columns: they are read together, always written
-- together, and a fourth surface later should not need another migration. The shape is fixed by
-- `models.schemas.Explanations`, so there is no advantage to giving the database a view into it.
--
-- Defaulted to '{}' rather than NULL so every existing row deserialises into an `Explanations`
-- with three empty strings. NULL would make the model's default_factory unreachable and force
-- every reader to handle a None it should never see.
--
-- Not backfilled. These alerts were sent without explanations, and generating them now would
-- invent an account of what a subscriber was told — the same reason `assessments` is append-only.
ALTER TABLE alerts
    ADD COLUMN IF NOT EXISTS explanations JSONB NOT NULL DEFAULT '{}'::jsonb;
