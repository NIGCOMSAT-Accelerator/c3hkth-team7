"""SHELTER API.

Satellite Hazard & Early-warning Local Tactical Emergency Response.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse

from app import scheduler
from app.api.routes import (
    alerts,
    chat,
    devdocs,
    health,
    iam,
    places,
    risk,
    subscribers,
    verification,
    webhooks,
)
from app.config import settings
from app.db import migrations
from app.db import session as db
from app.dispatch import router as dispatch_router
from app.iam import store as iam_store
from app.logging_config import configure_logging, get_logger
from app.models.enums import JobStage
from app.queue import broker, redis_client
from app.store import objects

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log.info(
        "starting SHELTER",
        extra={"environment": settings.environment, "site": settings.public_site_url},
    )

    # --- Postgres: the system of record. ---
    # Unlike Redis, an unreachable database is close to fatal — every read path
    # goes through it. We still don't exit: /health reports it, and a
    # container that stays up with a clear health signal is easier to diagnose
    # than a crash loop.
    if await db.ping():
        installed = await db.extensions()
        missing = [name for name, present in installed.items() if not present]
        if missing:
            log.warning(
                "postgres extensions missing",
                extra={"missing": missing, "installed": installed},
            )
        if settings.postgres_auto_migrate:
            try:
                await migrations.apply_pending()
            except Exception:
                log.exception("migrations failed; schema may be incomplete")
    else:
        log.error("postgres unreachable at startup")

    # --- Redis db0: job streams. ---
    if not await redis_client.ping():
        # Not fatal: the API can still serve reads from Postgres. The
        # queue-backed paths will fail loudly, and /health reports degraded.
        log.error("redis unreachable at startup", extra={"url": settings.redis_url})
    else:
        for stage in JobStage:
            await broker.ensure_group(stage)

        # Eviction would let the server drop stream entries — a queued satellite
        # scan silently never running. Worth a loud line, since it is a server
        # setting nothing in this codebase can enforce.
        info = await redis_client.server_info()
        if info.get("maxmemory_policy") not in (None, "noeviction", "unknown"):
            log.warning(
                "queue database has eviction enabled; job loss is possible",
                extra={"policy": info.get("maxmemory_policy"), "server": info.get("server")},
            )

    # --- Redis db1: cache. Degrades latency only. ---
    if not await redis_client.ping_cache():
        log.warning(
            "cache unreachable; every read will fall through to postgres",
            extra={"url": settings.cache_url},
        )

    # --- Object store: voice notes and imagery crops. Optional. ---
    if objects.available():
        await objects.ensure_buckets()
        if not await objects.ping():
            log.warning("object store configured but unreachable")
    else:
        log.info("object store not configured; voice notes disabled")

    # --- IAM identity store (MongoDB Atlas). Optional. ---
    # Indexes are ensured here rather than in a migration because Mongo has no
    # migration ledger; `create_index` is idempotent, so this is safe on every boot.
    # A failure is logged and tolerated: the collections still work without the
    # constraint, and refusing to boot over an index would take the warning pipeline
    # down for an identity concern.
    if iam_store.available():
        if await iam_store.ping():
            await iam_store.ensure_indexes()
        else:
            log.warning("IAM store configured but unreachable; onboarding will 503")
    else:
        log.info("IAM store not configured; accounts and API keys unavailable")

    channels = dispatch_router.available_channels()
    log.info(
        "dispatch channels configured",
        extra={"channels": [c.value for c in channels]},
    )
    if not channels:
        log.warning("no delivery channels configured — alerts will not be sent")

    # Same condition as `preflight`: only when IAM is ALSO absent.
    #
    # With IAM configured, scoped service-account keys are the authentication and the absence of
    # the legacy shared key is the desired state — warning about it there would tell an operator
    # their writes are unauthenticated when they are not, which is worse than silence. A warning
    # that cries wolf is one people learn to ignore, and this is the log line that must be believed.
    if settings.is_production and not settings.api_key and not settings.mongo_url:
        log.warning(
            "Neither API_KEY nor IAM is configured in production: write endpoints are "
            "unauthenticated"
        )

    scheduler.start()
    try:
        yield
    finally:
        # Reverse order of acquisition: stop producing work, then close the
        # connections that work depends on.
        await scheduler.stop()
        await iam_store.close()
        await redis_client.close_redis()
        await db.close_pool()
        log.info("SHELTER stopped")


# API metadata, as module constants.
#
# Lifted out of the `FastAPI()` call so `app/openapi_export.py` can reuse the SAME
# values. It builds its own bare app — deliberately, so the spec can be generated
# without importing the geospatial stack — and used to carry a third, separately
# maintained description. Three copies of the integration contract is two too many:
# the exported `openapi.json` is what a partner generates a client from, and it was
# already the poorest of the three.
API_DESCRIPTION = (
    "Africa's sovereign early-warning network for cascading climate disasters. "
    "A 7-day warning gives farmers what satellites never did: time to harvest.\n\n"
    "**This is the internal console — the complete surface, including IAM, chat and "
    "operator actions.** Partners should use "
    "[the filtered developer reference](/shelter/v1/api/dev-docs), which excludes "
    "endpoints a partner key can never call.\n\n"
    "## Setting up monitoring\n\n"
    "**Nothing here requires you to construct a bounding box.** "
    "`POST /places/resolve` takes a place and a size in ordinary words and returns a "
    "validated, submittable area:\n\n"
    "```json\n"
    '{ "place": "Argungu", "size": "5 hectares" }\n'
    "```\n\n"
    "Sizes are accepted as people write them — `5ha`, `12 acres`, `2 plots`, `20 km2`, "
    "`medium`, or blank. An unrecognised value resolves to a documented default flagged "
    "`size_is_estimate` rather than failing, because the response carries a map-ready "
    "area the user can check.\n\n"
    "### Two flows, both ending in an autonomous pipeline\n\n"
    "**An individual** (what the web portal does):\n\n"
    "1. `POST /iam/signup/individual` — creates the account, issues a session\n"
    "2. `POST /places/resolve` — words to an area\n"
    "3. `POST /iam/activate` — binds the area. Requires a **confirmed** email\n\n"
    "**An aggregator's customer** (one call, for bulk import):\n\n"
    "1. `POST /places/resolve` — words to an area\n"
    "2. `POST /iam/customers` — creates the person *and* binds the area, with an "
    "`X-SHELTER-API-Key`\n\n"
    "`POST /places/preview` validates an area without saving it, and reports "
    "`envelope_ratio` — above ~1.5, sending a true field outline instead of a rectangle "
    "measurably changes the reading. A riverside strip is typically 3x.\n\n"
    "All four `/places/*` endpoints require `X-SHELTER-API-Key` — an aggregator key or a "
    "platform service key. No specific scope: the gate is for attribution and for "
    "protecting a rate-limited upstream geocoder, not for authorisation.\n\n"
    # Deliberately NOT a path under `docs/` — that directory is internal and gitignored, and
    # this string is inherited by the partner reference at `/dev-docs`. Pointing an integrator
    # at a file they cannot open is worse than not pointing them anywhere.
    "Every field is documented on the operation itself, below."
)

# Tag descriptions, so the operation groups explain themselves. Without these, a reader
# facing 39 `iam` operations has no way to tell setup from session management.
API_TAGS = [
    {
        "name": "places",
        "description": (
            "**Start here.** Turn a place name and a size in words into a monitorable "
            "area. Public — no credential, because the signup form uses these before an "
            "account exists."
        ),
    },
    {
        "name": "iam",
        "description": (
            "Accounts, sessions, API keys, audit and tenancy. The monitoring-setup "
            "routes in here are `POST /iam/signup/individual`, `POST /iam/activate` "
            "(individual) and `POST /iam/customers` (aggregator, one call). Everything "
            "else is authentication, key lifecycle or the audit trail."
        ),
    },
    {
        "name": "subscribers",
        "description": (
            "The operations view of subscribers and their areas. Platform-scoped: this "
            "is not how a partner reads their own customers — that is "
            "`GET /iam/customers`."
        ),
    },
    {
        "name": "risk",
        "description": (
            "Assess an area on demand, or read its latest assessment. `POST /risk/assess` "
            "runs the pipeline inline rather than queueing it."
        ),
    },
    {
        "name": "alerts",
        "description": "Advisories that were dispatched, with the evidence each cited.",
    },
    {
        "name": "webhook",
        "description": (
            "Event delivery into a partner's own systems, with HMAC signing, retries "
            "and a delivery log."
        ),
    },
    {
        "name": "chat",
        "description": (
            "Herald's subscriber-facing Q&A. Hazard figures come only from measured "
            "satellite data; web search supplies background and can never contribute a "
            "number."
        ),
    },
    {
        "name": "verification",
        "description": (
            "Fahis — the accountability agent. Precision is reported over confirmed and "
            "refused verdicts only, with coverage beside it."
        ),
    },
    {"name": "health", "description": "Liveness, readiness and resolved configuration."},
    {"name": "meta", "description": "Service metadata."},
]


app = FastAPI(
    title="SHELTER",
    description=API_DESCRIPTION,
    openapi_tags=API_TAGS,
    version="1.0.0",
    lifespan=lifespan,
    # **Deliberately ungated: no API key, no session.**
    #
    # The OpenAPI document and the Swagger UI are the integration contract. An
    # aggregator evaluating SHELTER, or a developer wiring up `openapi-generator`,
    # must be able to read the contract *before* they have a credential — gating it
    # would mean every integration starts with a support request, and the spec
    # describes the shape of the API rather than any subscriber's data.
    #
    # What is protected is the *data*: every endpoint here still enforces its scope.
    # Publishing the map is not publishing the territory.
    #
    # Docs live *under the API prefix*, not at the root.
    #
    # This matters for the target deployment shape: the backend sits behind an SSL
    # reverse proxy that routes by path, so everything the service owns has to share
    # one prefix. With docs at `/docs` the proxy would need a second location block,
    # and mounting SHELTER alongside anything else on the same host would collide.
    # One prefix means one `location /shelter/v1/api/` and nothing else to configure.
    # Swagger UI is served by a CUSTOM route (see `_swagger_ui` below) rather than by
    # FastAPI's built-in page, so the favicon can be ours.
    #
    # `swagger_favicon_url` was previously passed here and did NOTHING: it is not a
    # `FastAPI()` parameter — it belongs to `get_swagger_ui_html` — and FastAPI accepts
    # unknown keyword arguments silently. So the tab kept showing fastapi.tiangolo.com's
    # icon while the code claimed otherwise, which is worse than an obvious gap.
    docs_url=None,
    # ReDoc is DISABLED here on purpose. FastAPI's built-in page loads
    # `cdn.jsdelivr.net/npm/redoc@next/...`, which now returns 404 — so `/redoc`
    # rendered a blank white screen with no error. Rather than patch the CDN URL for an
    # internal page nobody asked for, the partner-facing reference at `/dev-docs` serves
    # a pinned bundle over a FILTERED spec. `/docs` remains the full internal console.
    redoc_url=None,
    openapi_url=f"{settings.api_prefix}/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    # The frontend reads these on cross-origin responses. Without an explicit
    # expose list a browser hides every non-simple header, so the Netlify frontend
    # could not see its own rate-limit budget or correlate a failure to a log line.
    expose_headers=["X-SHELTER-Request-Id", "X-RateLimit-Remaining", "Retry-After"],
)

@app.get(f"{settings.api_prefix}/docs", include_in_schema=False)
async def _swagger_ui() -> HTMLResponse:
    """Swagger UI with SHELTER's own favicon.

    Replaces FastAPI's built-in `/docs`, which cannot be told to use a different icon from
    the `FastAPI()` constructor — `swagger_favicon_url` is a `get_swagger_ui_html`
    parameter, and passing it to `FastAPI()` is silently ignored.

    `include_in_schema=False` so the docs page does not appear as an operation inside its
    own spec, which would also change the committed `openapi.json` and fail the freshness
    check for no reason.

    The favicon is served from `/dev-docs/favicon.svg` — one asset for both consoles, so
    the internal and partner-facing pages cannot end up branded differently.
    """
    return get_swagger_ui_html(
        openapi_url=f"{settings.api_prefix}/openapi.json",
        title=f"{settings.app_name} — internal API console",
        swagger_favicon_url=f"{settings.api_prefix}/dev-docs/favicon.svg",
    )


app.include_router(health.router, prefix=settings.api_prefix)
# Place search and AOI preview. Public: the signup form needs them before an
# account exists, and they are pure functions over open data.
app.include_router(places.router, prefix=settings.api_prefix)
app.include_router(subscribers.router, prefix=settings.api_prefix)
app.include_router(risk.router, prefix=settings.api_prefix)
app.include_router(alerts.router, prefix=settings.api_prefix)
app.include_router(chat.router, prefix=settings.api_prefix)
app.include_router(verification.router, prefix=settings.api_prefix)
app.include_router(webhooks.router, prefix=settings.api_prefix)
app.include_router(iam.router, prefix=settings.api_prefix)
app.include_router(devdocs.router, prefix=settings.api_prefix)


@app.get("/", tags=["meta"])
async def root() -> dict:
    """Service discovery.

    Kept at the bare root deliberately, even though everything else is prefixed:
    it is the one URL an operator will try by hand after a deploy, and answering
    with the real prefix turns "where is the API?" into a single curl.
    """
    return {
        "service": "SHELTER",
        "tagline": (
            "A 7-day warning gives farmers what satellites never did: "
            "time to harvest."
        ),
        "version": app.version,
        "api_base": settings.api_prefix,
        "docs": f"{settings.api_prefix}/docs",
        "openapi": f"{settings.api_prefix}/openapi.json",
        "health": f"{settings.api_prefix}/health",
        "site": settings.public_site_url,
    }
