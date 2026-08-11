"""Startup validation — run by `docker-entrypoint.sh` before the port opens.

**The failure this prevents.** A typo'd `POSTGRES_DSN` used to produce a container
that started cleanly, passed its health check for the first 40 seconds, accepted
traffic, and returned 500 on every read. The deployment *looked* fine. Validating
here means the process exits non-zero instead, so the orchestrator restarts rather
than serving a broken deployment, and the operator gets one clear message instead of
a log full of connection errors.

**Why Python and not bash.** `app/config.py` owns the defaults, the types and the
`.env` parsing. A shell script would use `os.getenv`-equivalent lookups, which do
**not** read `.env` — that bug has already shipped once in this codebase (the
WhatsApp template lookup). Reusing pydantic-settings is the only way to validate
what the app will actually see.

**Three severities, and the distinction is the whole design:**

| | Meaning | Effect |
|---|---|---|
| **error** | The service cannot do its job correctly | exit 1 — refuse to start |
| **warning** | A capability is unavailable but the core pipeline works | log, continue |
| **info** | Resolved configuration worth echoing | log |

Getting that split wrong in either direction is harmful. Erroring on a missing
WhatsApp token would stop a working flood-warning service over an optional channel.
Warning on an unauthenticated production deployment would leave a broadcast API open
to anyone who found the URL.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from urllib.parse import urlparse

from app.config import settings


@dataclass(frozen=True)
class Finding:
    level: str          # "error" | "warning" | "info"
    message: str
    #: What the operator should actually do. Required for errors — a validation
    #: failure without a remedy just moves the confusion.
    remedy: str = ""


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


def _check_infrastructure_urls() -> list[Finding]:
    """The three connection strings the service cannot work without.

    Parsed rather than pinged: this runs before the port opens and must not hang
    for 30 seconds on a firewalled host. A malformed DSN is the common failure and
    is detectable offline; an unreachable-but-valid host is reported by `/health`
    and retried by the pool.
    """
    findings: list[Finding] = []

    dsn = urlparse(settings.postgres_dsn)
    if dsn.scheme not in ("postgresql", "postgres"):
        findings.append(Finding(
            "error",
            f"POSTGRES_DSN has scheme {dsn.scheme!r}, expected postgresql://",
            "Example: postgresql://shelter:shelter@postgres:5432/shelter",
        ))
    elif not dsn.hostname:
        findings.append(Finding(
            "error",
            "POSTGRES_DSN has no host",
            "In Docker this must be the compose service name, e.g. @postgres:5432",
        ))

    for name, url in (("REDIS_URL", settings.redis_url), ("CACHE_URL", settings.cache_url)):
        parsed = urlparse(url)
        if parsed.scheme not in ("redis", "rediss"):
            findings.append(Finding(
                "error",
                f"{name} has scheme {parsed.scheme!r}, expected redis://",
                f"Example: redis://dragonfly:6379/{'0' if name == 'REDIS_URL' else '1'}",
            ))

    # db0 and db1 must be different databases. Sharing one would put cache keys
    # in the same keyspace as the job streams, where a `delete_prefix` sweep
    # could take out queued scans.
    if settings.redis_url == settings.cache_url:
        findings.append(Finding(
            "error",
            "REDIS_URL and CACHE_URL point at the same database",
            "The queue must be isolated from the cache: use /0 for REDIS_URL and "
            "/1 for CACHE_URL. Sharing one keyspace lets a cache sweep delete "
            "queued satellite scans.",
        ))

    return findings


def _check_auth_posture(role: str) -> list[Finding]:
    """Production must not be open.

    `require_api_key` is a deliberate no-op when `API_KEY` is unset, which keeps
    local development frictionless. In production that means anyone who finds the
    URL can register subscribers and trigger NIGCOMSAT broadcasts in someone
    else's name — so production without a key is an error, not a warning.
    """
    findings: list[Finding] = []

    # ## Only when IAM is NOT the authentication layer
    #
    # These two rules used to contradict each other, and the deployment was unstartable as a
    # result. This one demanded `API_KEY` in production; the IAM rule below errors when `API_KEY`
    # is set *alongside* IAM, because the shared `X-SHELTER-Key` grants every write endpoint
    # including NIGCOMSAT broadcast. With `MONGO_URL` configured, no value satisfied both — set it
    # and rule two fails, unset it and rule one fails.
    #
    # The resolution is that they were never both meant to apply. `API_KEY` is the *pre-IAM*
    # authentication story, kept for deployments running without an identity layer. Once IAM is
    # configured, scoped service-account keys ARE the authentication, and the shared key is a
    # liability rather than a requirement — which is exactly what the rule below says.
    #
    # So this fires only when IAM is absent. A production deployment with neither is genuinely
    # unauthenticated and must still be refused.
    if settings.is_production and not settings.api_key and not settings.mongo_url:
        findings.append(Finding(
            "error",
            "ENVIRONMENT=production but neither API_KEY nor IAM is configured",
            "Write endpoints would be completely unauthenticated: anyone could "
            "register subscribers or trigger a satellite broadcast. Either configure IAM "
            "(set MONGO_URL and provision scoped keys with `make iam-service-account`), or set "
            "API_KEY with `openssl rand -hex 32`. To run without auth deliberately, set "
            "ENVIRONMENT=development.",
        ))

    if settings.api_key and len(settings.api_key) < 16:
        findings.append(Finding(
            "error",
            f"API_KEY is only {len(settings.api_key)} characters",
            "Use at least 16, ideally 32+: `openssl rand -hex 32`. A short key is "
            "brute-forceable over an SSL reverse proxy that keeps connections open.",
        ))

    # --- IAM session signing -------------------------------------------------
    # `security._signing_key()` falls back to API_KEY so local development needs no
    # extra configuration. In production that fallback is dangerous in a specific way:
    # API_KEY is shared with the frontend server and appears in Netlify's environment,
    # so anyone who can read it could mint a session token for ANY account — including
    # a commercial one, and then mint API keys from it.
    #
    # Only checked when IAM is actually configured: without MONGO_URL there are no
    # accounts to impersonate and the endpoints return 503.
    if settings.is_production and settings.mongo_url and not settings.iam_jwt_secret:
        findings.append(Finding(
            "error",
            "ENVIRONMENT=production with IAM enabled but IAM_JWT_SECRET is unset",
            "Session signing would fall back to API_KEY, which the frontend also "
            "holds — anyone able to read it could forge a session for any account "
            "and then mint API keys from it. Set a distinct secret: "
            "`openssl rand -hex 32`.",
        ))

    if settings.iam_jwt_secret and settings.iam_jwt_secret == settings.api_key:
        findings.append(Finding(
            "error",
            "IAM_JWT_SECRET is the same value as API_KEY",
            "They must differ. API_KEY is distributed to the frontend; the session "
            "signing key must not be, or the frontend's environment becomes enough to "
            "impersonate any subscriber.",
        ))

    if settings.iam_jwt_secret and len(settings.iam_jwt_secret) < 32:
        findings.append(Finding(
            "error",
            f"IAM_JWT_SECRET is only {len(settings.iam_jwt_secret)} characters",
            "HS256 signing needs a high-entropy secret; use at least 32 characters "
            "from `openssl rand -hex 32`. A short secret is brute-forceable offline "
            "from a single captured token.",
        ))

    # --- Migrating off the shared key ---------------------------------------
    # API_KEY gates 29 write endpoints with no attribution, no scoping, and no way to
    # revoke one consumer. Once IAM is available a scoped service-account key is the
    # better credential, so holding both means the weaker one is still live.
    if settings.api_key and settings.mongo_url and settings.iam_legacy_shared_key_enabled:
        findings.append(Finding(
            "warning" if not settings.is_production else "error",
            "API_KEY is set alongside IAM: the shared X-SHELTER-Key still grants "
            "every write endpoint, including NIGCOMSAT broadcast",
            "Provision scoped keys and remove it:\n"
            "  make iam-service-account NAME=netlify-frontend EMAIL=ops@example.com\n"
            "  -> set the printed key as SHELTER_API_KEY in the frontend\n"
            "  -> set IAM_LEGACY_SHARED_KEY_ENABLED=false\n"
            "  -> delete API_KEY",
        ))

    # The frontend signs nothing; it presents the key. So an HMAC secret matters
    # only for outbound webhooks, and only in production.
    if settings.is_production and not settings.webhook_signing_secret:
        findings.append(Finding(
            "warning",
            "WEBHOOK_SIGNING_SECRET is unset; outbound webhooks will be unsigned",
            "Subscribers cannot verify an alert came from SHELTER. Set it with "
            "`openssl rand -hex 32` before onboarding a business integration.",
        ))

    return findings


def _check_scheduler_singleton(role: str) -> list[Finding]:
    """Exactly one process may run the watch loop.

    Two schedulers means every AOI is scanned twice per cycle — double the
    catalogue load, double the advisories, and duplicate alerts to subscribers.
    """
    if role == "worker" and settings.scheduler_enabled:
        return [Finding(
            "error",
            "SCHEDULER_ENABLED is true on a worker container",
            "The watch loop must run in exactly one process (the api container). "
            "Two schedulers queue every scan twice and subscribers get duplicate "
            "alerts. Set SCHEDULER_ENABLED=false here.",
        )]
    if role == "api" and not settings.scheduler_enabled:
        return [Finding(
            "warning",
            "SCHEDULER_ENABLED is false on the api container",
            "Nothing will queue scans autonomously; the pipeline runs only when "
            "POST /risk/scan is called. Intentional for a read-only replica.",
        )]
    return []


def _check_migration_ownership(role: str) -> list[Finding]:
    """Only the API container migrates.

    `migrations.py` takes a `pg_advisory_lock`, so concurrent migrators are safe
    rather than corrupting — but a worker applying schema changes inverts the
    intended ownership and makes a rollback harder to reason about.
    """
    if role == "worker" and settings.postgres_auto_migrate:
        return [Finding(
            "warning",
            "POSTGRES_AUTO_MIGRATE is true on a worker container",
            "Migrations are the api container's job. The advisory lock makes this "
            "safe, but set POSTGRES_AUTO_MIGRATE=false to keep ownership clear.",
        )]
    return []


def _check_degradation_notices() -> list[Finding]:
    """Things that are absent-but-fine, reported so the operator is not surprised.

    Every one of these has a documented fallback. Naming them at startup is the
    difference between "advisories are English templates because no LLM key is
    set" and someone later wondering why the prose looks mechanical.
    """
    findings: list[Finding] = []

    if not settings.llm_base_url and not settings.anthropic_api_key:
        findings.append(Finding(
            "info",
            "No inference provider configured: advisories will use English "
            "templates, chat answers only the deterministic questions, and Fahis "
            "records NOT_ATTEMPTED",
            "",
        ))

    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for label, relative in (
        ("SAR flood", settings.sar_flood_weights),
        ("crop stress", settings.crop_stress_weights),
    ):
        path = Path(relative)
        if not path.is_absolute():
            path = root / relative
        if not path.exists():
            findings.append(Finding(
                "info",
                f"No trained {label} weights: falling back to documented physical "
                f"thresholds at confidence 0.55 (severity capped at WATCH)",
                "",
            ))

    if not settings.searxng_url:
        findings.append(Finding(
            "info",
            "No SEARXNG_URL: Fahis cannot verify and chat has no web context",
            "",
        ))

    if not settings.mongo_url:
        findings.append(Finding(
            "info",
            "No MONGO_URL: subscriber accounts and aggregator API keys are "
            "unavailable (/iam/* returns 503). The satellite pipeline is unaffected",
            "",
        ))

    return findings


def _check_cors_for_split_deployment() -> list[Finding]:
    """The frontend-on-Netlify, backend-on-a-VPS case.

    This is the deployment shape the project targets. The checks below are worth having, but
    the docstring used to overstate their importance and that was misleading enough to send
    someone debugging in the wrong direction:

    **The portal does not depend on CORS.** `frontend/lib/api.ts` is `server-only`, so every
    call to this API happens inside Netlify's renderer or a Server Action — server to server,
    where CORS does not apply. A wrong `CORS_ORIGINS` therefore does NOT produce an empty
    dashboard, and a right one will not fix one.

    What `CORS_ORIGINS` does protect is anything that genuinely calls from a browser: a future
    embedded widget, a partner's own web client, a debugging fetch from a console. Those are
    real, which is why the checks stay — but when the portal shows no data, `SHELTER_API_URL`
    and `SHELTER_API_KEY` are the places to look, and they surface in the renderer's function
    log rather than the browser console.

    Also checks for MISNAMED variables. `CORS_URL` is a natural guess and is silently
    discarded — pydantic-settings runs with `extra="ignore"`, so the default quietly stays in
    force and nothing anywhere reports it.
    """
    findings: list[Finding] = []
    origins = settings.cors_origin_list

    # A misnamed key is invisible to pydantic, so it has to be looked for in the raw
    # environment. Verified behaviour: exporting CORS_URL leaves `cors_origins` at its
    # default, and no warning is emitted anywhere.
    for wrong, right in (
        ("CORS_URL", "CORS_ORIGINS"),
        ("CORS_ORIGIN", "CORS_ORIGINS"),
        ("ALLOWED_ORIGINS", "CORS_ORIGINS"),
        ("API_BASE_URL", "API_BASE_URL is a FRONTEND concern; the backend reads none"),
    ):
        if os.environ.get(wrong):
            findings.append(Finding(
                "warning",
                f"{wrong} is set but is not a setting — it is being ignored",
                f"pydantic-settings runs with extra='ignore', so this value is silently "
                f"discarded. Did you mean {right}?",
            ))

    if not origins:
        return [Finding(
            "error",
            "CORS_ORIGINS is empty",
            "No browser origin can read a response. Set it to your frontend URL, "
            "e.g. CORS_ORIGINS=https://shelter.zerorate.io",
        )]

    if "*" in origins and settings.is_production:
        findings.append(Finding(
            "error",
            "CORS_ORIGINS is '*' in production while allow_credentials is on",
            "Browsers reject that combination outright, so every cross-origin "
            "request will fail. Name your frontend origins explicitly.",
        ))

    if settings.is_production:
        insecure = [o for o in origins if o.startswith("http://") and "localhost" not in o]
        if insecure:
            findings.append(Finding(
                "warning",
                f"CORS_ORIGINS contains non-TLS origins in production: {insecure}",
                "An API key travelling over plain HTTP is readable in transit. "
                "Terminate TLS at the reverse proxy and use https:// origins.",
            ))

    return findings


def _check_api_prefix() -> list[Finding]:
    """The prefix must be a rooted path, because it is string-concatenated.

    `frontend/lib/api.ts` builds `${API_URL}${API_PREFIX}${path}`. A prefix
    without a leading slash produces `http://apishelter/v1/api/health` — a
    nonsense host — and a trailing slash produces a double slash that some
    reverse proxies 404 and others silently normalise.
    """
    prefix = settings.api_prefix
    findings: list[Finding] = []

    if not prefix.startswith("/"):
        findings.append(Finding(
            "error",
            f"API_PREFIX={prefix!r} does not start with '/'",
            "It is concatenated onto the base URL, so it must be rooted: "
            "API_PREFIX=/shelter/v1/api",
        ))
    if prefix.endswith("/"):
        findings.append(Finding(
            "error",
            f"API_PREFIX={prefix!r} ends with '/'",
            "Routes already begin with '/', so a trailing slash produces '//' "
            "which some proxies 404. Use API_PREFIX=/shelter/v1/api",
        ))
    return findings


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def run_checks(role: str) -> list[Finding]:
    """Every offline check, in the order an operator would want to read them."""
    return [
        *_check_api_prefix(),
        *_check_infrastructure_urls(),
        *_check_auth_posture(role),
        *_check_scheduler_singleton(role),
        *_check_migration_ownership(role),
        *_check_cors_for_split_deployment(),
        *_check_degradation_notices(),
    ]


async def _schema_present() -> bool:
    """True when the core tables exist.

    Used by the worker's `wait_for_schema` loop. Checks `assessments` because it
    is created by `002_core.sql` — if it is there, the core schema has been
    applied. A connection failure is reported as "not ready" so the caller keeps
    waiting rather than crashing while Postgres is still booting.
    """
    from app.db import session as db

    try:
        async with db.acquire() as conn:
            return bool(await conn.fetchval("SELECT to_regclass('public.assessments')"))
    except Exception:
        return False
    finally:
        await db.close_pool()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.preflight", description=__doc__)
    parser.add_argument("--role", default="api", choices=("api", "worker"))
    parser.add_argument(
        "--check-schema",
        action="store_true",
        help="Exit 0 only if the core tables exist. Used by the worker's start gate.",
    )
    args = parser.parse_args(argv)

    if args.check_schema:
        return 0 if asyncio.run(_schema_present()) else 1

    findings = run_checks(args.role)
    errors = [f for f in findings if f.level == "error"]

    for finding in findings:
        prefix = {"error": "ERROR  ", "warning": "WARNING", "info": "note   "}[finding.level]
        print(f"  {prefix} {finding.message}")
        if finding.remedy:
            for line in finding.remedy.splitlines():
                print(f"          -> {line.strip()}")

    if errors:
        print(f"\n  {len(errors)} configuration error(s). Refusing to start.\n")
        return 1

    print(f"  configuration OK ({settings.environment}, prefix {settings.api_prefix})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
