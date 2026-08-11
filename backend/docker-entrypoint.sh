#!/usr/bin/env bash
#
# Container entrypoint — the safety gate between `docker run` and the app.
#
# WHY THIS EXISTS
#
# The previous `CMD ["uvicorn", ...]` started serving immediately. That is fine on
# a laptop and wrong behind a reverse proxy on a VPS, for three reasons this script
# fixes:
#
#   1. **A misconfiguration became a 500 at request time, not a startup failure.**
#      A typo'd POSTGRES_DSN produced a container that reported healthy, accepted
#      traffic, and failed every read. Config is validated here, before the port
#      opens, and the process exits non-zero so the orchestrator restarts rather
#      than serving a broken deployment.
#   2. **Workers raced the API's migrations.** `depends_on: service_healthy` waits
#      for Postgres to accept connections, not for the schema to exist, so a worker
#      could consume a job before `002_core.sql` had run. `wait-for-schema` blocks
#      until the tables are actually there.
#   3. **Nothing enforced the production auth posture.** An unset API_KEY in
#      production silently left write endpoints open. That is now a hard refusal.
#
# Every check is idempotent and every failure prints what to fix, because the
# person reading this output is likely 20 minutes into their first deploy.
#
# USAGE
#
#   docker-entrypoint.sh api            # uvicorn, migrations, scheduler
#   docker-entrypoint.sh worker [args]  # queue consumer; waits for the schema
#   docker-entrypoint.sh <anything>     # exec'd verbatim (shell, pytest, ...)

# -e: any failed command aborts. -u: an unset variable is a bug, not an empty
# string. -o pipefail: a failure mid-pipe is not masked by a successful tail.
set -euo pipefail

readonly ROLE="${1:-api}"
shift || true

log()  { printf '%s [entrypoint] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail() { printf '%s [entrypoint] FATAL: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# Configuration validation
#
# Delegated to Python rather than reimplemented in bash: `app/config.py` is the
# single source of truth for defaults, types and the .env file, and `os.getenv`
# in a shell script would not read .env at all — the exact bug the config
# contract in CLAUDE.md warns about.
# --------------------------------------------------------------------------- #
validate_config() {
    log "validating configuration"
    python -m app.preflight --role "$ROLE" || fail \
        "configuration is invalid; see the errors above. Nothing has started, so
    no traffic was served against a broken deployment. Fix .env (or the compose
    environment) and start again."
}

# --------------------------------------------------------------------------- #
# Schema readiness — workers only
#
# The API container owns migrations (POSTGRES_AUTO_MIGRATE). Workers must not
# migrate (they would race each other), but they also must not start consuming
# before the schema exists.
# --------------------------------------------------------------------------- #
wait_for_schema() {
    local attempts="${SCHEMA_WAIT_ATTEMPTS:-60}"
    local delay="${SCHEMA_WAIT_SECONDS:-2}"

    log "waiting for the database schema (up to $((attempts * delay))s)"
    for ((i = 1; i <= attempts; i++)); do
        if python -m app.preflight --check-schema >/dev/null 2>&1; then
            log "schema is ready"
            return 0
        fi
        # Only log occasionally: 60 identical lines buries the real error.
        if ((i % 10 == 0)); then
            log "still waiting for the schema (attempt ${i}/${attempts})"
        fi
        sleep "$delay"
    done

    fail "the database schema did not appear within $((attempts * delay))s.
    The API container applies migrations — check that it started and look at
    'docker compose logs api'. This worker is exiting rather than consuming jobs
    it cannot persist."
}

# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
case "$ROLE" in
    api)
        validate_config

        # --- Uvicorn, not Gunicorn. -------------------------------------------
        #
        # Gunicorn is a pre-fork WSGI server. Running ASGI under it means
        # `-k uvicorn.workers.UvicornWorker`, i.e. Gunicorn supervising uvicorn
        # processes — so uvicorn is serving either way and Gunicorn contributes only
        # process management. Since uvicorn gained `--workers`, that is a dependency
        # and a config surface for something already built in.
        #
        # Two SHELTER-specific reasons it would actively cost us:
        #
        #   1. **The scheduler must run in exactly one process.** SCHEDULER_ENABLED
        #      is true here, and `lifespan` starts the watch loop. Under N workers
        #      the loop starts N times and every AOI is scanned N times per cycle —
        #      duplicate advisories to subscribers. Uvicorn's own `--workers` has the
        #      identical problem, which is why WEB_CONCURRENCY defaults to 1 below
        #      and the script *refuses* to combine it with the scheduler.
        #   2. **Memory.** Each worker imports torch and GDAL: ~400-600 MB resident
        #      before serving a request. `worker-analyst` is already capped at 2 GB.
        #      Four API workers would exceed a typical 4 GB VPS on their own.
        #
        # And the workload does not want more processes anyway: every endpoint is
        # I/O-bound (Postgres, Redis, STAC, COG range reads), which is exactly what a
        # single-process event loop handles best. The CPU-heavy work — forward passes,
        # flow accumulation — is already in the worker containers, off this path.
        # Scale the API by adding replicas behind the reverse proxy, not workers
        # inside one container: replicas scale horizontally across hosts and keep
        # the scheduler-singleton property explicit rather than accidental.
        #
        # WEB_CONCURRENCY is honoured for a deliberately-configured read-only
        # replica (SCHEDULER_ENABLED=false), where multiple workers are safe.
        readonly workers="${WEB_CONCURRENCY:-1}"
        if [[ "$workers" -gt 1 && "${SCHEDULER_ENABLED:-true}" == "true" ]]; then
            fail "WEB_CONCURRENCY=$workers with SCHEDULER_ENABLED=true.
    The watch loop runs once per worker process, so every area would be scanned
    $workers times per cycle and subscribers would get duplicate alerts. Either set
    WEB_CONCURRENCY=1, or set SCHEDULER_ENABLED=false and run the scheduler in a
    separate single-process container."
        fi

        # --proxy-headers + --forwarded-allow-ips: the target deployment is behind
        # an SSL reverse proxy, so without these every client IP logs as the proxy's
        # and any generated absolute URL comes out http:// instead of https://.
        # Default '*' trusts the immediate peer, which is correct when only the proxy
        # can reach this port (compose publishes it to the host, and the VPS firewall
        # is expected to keep 8000 off the public internet). Narrow it with
        # FORWARDED_ALLOW_IPS if the port is reachable more widely.
        #
        # `exec` replaces this shell, so uvicorn becomes PID 1 and receives SIGTERM
        # directly. Without it, `docker stop` signals bash, uvicorn never shuts down
        # gracefully, and every stop takes the full timeout and drops in-flight
        # requests mid-advisory.
        log "starting API on 0.0.0.0:${PORT:-8000} (workers=$workers)"
        exec uvicorn app.main:app \
            --host 0.0.0.0 \
            --port "${PORT:-8000}" \
            --workers "$workers" \
            --proxy-headers \
            --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" \
            --timeout-graceful-shutdown "${GRACEFUL_TIMEOUT:-30}" \
            "$@"
        ;;

    worker)
        validate_config
        wait_for_schema
        log "starting worker: ${*:-all stages}"
        exec python -m app.queue.worker "$@"
        ;;

    *)
        # Escape hatch for `docker compose run api bash`, one-off pytest, psql.
        # Deliberately no validation: a debugging shell must work even when — in
        # fact especially when — the configuration is broken.
        exec "$ROLE" "$@"
        ;;
esac
