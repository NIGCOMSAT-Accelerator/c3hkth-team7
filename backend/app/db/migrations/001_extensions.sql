-- Extensions.
--
-- Split into its own migration because CREATE EXTENSION needs privileges the
-- later migrations don't, and because a deployment on managed Postgres may have
-- to have these enabled by the provider instead. If that is the case, this file
-- becomes a no-op (IF NOT EXISTS) rather than a failure.
--
-- timescaledb is the only one that is optional at runtime: 002 degrades to
-- native range partitioning if it is absent. postgis and vector are required.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS timescaledb;
