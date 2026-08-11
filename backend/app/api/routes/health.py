"""Health and readiness.

`/health` is deliberately verbose about which channels and models are actually
configured. A deployment missing its WhatsApp token should be visible on a
dashboard on a calm Tuesday, not discovered during a flood.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Response, status

from app.agentic import provider as agentic_provider
from app.api.deps import require_api_key
from app.config import settings
from app.db import migrations
from app.db import session as db
from app.dispatch import router as dispatch_router
from app.eo import sources as source_registry
from app.iam import geo
from app.iam import mailer as iam_mailer
from app.llm import budget, embeddings
from app.llm import client as llm
from app.models.enums import JobStage
from app.queue import broker, redis_client
from app.search import client as search
from app.store import cache, objects, poll_state

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    # Every probe here is independent and none of them raise, so one dead
    # dependency still yields a full picture of the others.
    redis_ok = await redis_client.ping()
    cache_ok = await redis_client.ping_cache()
    postgres_ok = await db.ping()

    # `status` gates on the two the pipeline genuinely cannot run without:
    # Postgres is every read path, db0 is every queued scan. A cold cache or a
    # missing object store degrades features, not correctness.
    return {
        "status": "ok" if (redis_ok and postgres_ok) else "degraded",
        "service": settings.app_name,
        "environment": settings.environment,
        "redis": "up" if redis_ok else "down",
        "postgres": {
            "status": "up" if postgres_ok else "down",
            "extensions": await db.extensions() if postgres_ok else {},
            "pending_migrations": await migrations.pending() if postgres_ok else [],
        },
        "cache": {
            "status": "up" if cache_ok else "down",
            **(await cache.stats() if cache_ok else {}),
        },
        "queue_server": await redis_server_summary(redis_ok),
        "object_store": {
            "configured": objects.available(),
            "status": "up" if await objects.ping() else "down",
        },
        # Both optional. Absent, Fahis records NOT_ATTEMPTED (never "refuted")
        # and chat returns 503 — neither affects the core pipeline.
        # Which backend Fahis will actually use, not merely whether something is set.
        #
        # `configured: true` alone was not diagnosable: a deployment with SEARXNG_URL set but
        # SEARCH_PROVIDER left at "none" reads as configured while Fahis records NOT_ATTEMPTED on
        # every assessment. Naming the resolved provider makes that visible on a calm day rather
        # than when someone asks why precision is null.
        "search": {
            "configured": search.available(),
            "provider": search.provider(),
        },
        # Optional. False means the portal shows raw IP addresses instead of city names —
        # a degraded display, not a fault, so it does not affect overall status. Reported
        # because "why does the audit log show numbers?" is otherwise unanswerable without
        # shelling into the container.
        "geoip": {"configured": geo.available()},
        # The resolved provider, so a swap can be confirmed without reading logs.
        # `base_url` is echoed but the key never is.
        "inference": {
            "configured": llm.available(),
            "base_url": settings.llm_base_url,
            "model": settings.llm_model if llm.available() else None,
            "max_tokens_param": settings.llm_max_tokens_param,
            "structured_output_mode": settings.llm_structured_output_mode,
            "embeddings": {
                "configured": embeddings.available(),
                "model": settings.embedding_model if embeddings.available() else None,
                "dimensions": settings.embedding_dimensions,
            },
            "budget": await budget.summary(),
        },
        # Fahis and chat run on Pydantic-AI agents (`app/agentic/`), which build
        # their model through the same LLM_BASE_URL — so this reports the agent
        # surfaces' readiness, distinct from `inference` above, which reports the
        # transport the advisory generator uses.
        "fahis": {
            "enabled": settings.fahis_enabled,
            # Both dependencies must be present for a verdict to be adjudicated.
            "operational": settings.fahis_enabled
            and search.available()
            and agentic_provider.available(),
        },
        "chat": {
            "enabled": settings.chat_enabled,
            # Deterministic answers need no model at all, so chat can serve the
            # common questions with no provider configured.
            "operational": settings.chat_enabled
            and (agentic_provider.available() or settings.chat_deterministic_answers),
        },
        "channels_configured": [c.value for c in dispatch_router.available_channels()],
        # Which transport onboarding email will actually use, resolved rather than
        # configured. A deployment that set NOTIFICATION_PROVIDER but is missing the
        # credential resolves to "noop" — visible here on a calm day rather than
        # discovered when a subscriber never receives a verification link.
        "notifications": {
            "configured": settings.notification_provider,
            "resolved": iam_mailer.resolve_provider(),
            "operational": iam_mailer.available(),
        },
        # Which path actually generates advisories, resolved rather than assumed.
        # A deployment that thinks it configured a provider but resolves to
        # "template" should see that here, not infer it from advisory prose.
        "advisory_generator": _advisory_generator_summary(),
        "models": {
            "sar_flood": _weights_present(settings.sar_flood_weights),
            "crop_stress": _weights_present(settings.crop_stress_weights),
        },
        # Which feeds are configured, and how fresh Scout's view of them is. This
        # is the "are the datasets reachable?" question an operator asks on a
        # bootstrap deployment, answered without reading logs.
        "data_sources": {
            "declared": len(source_registry.SOURCES),
            "configured": sum(
                1 for s in source_registry.SOURCES if source_registry.configured(s)
            ),
            "chains": [
                source_registry.chain_status(kind) for kind in source_registry.Kind
            ],
            # Scout's per-(area, source) freshness. Empty on a fresh deployment
            # with no subscribers — which is the bootstrap state, not a fault.
            "poll_state": await poll_state.summary(),
        },
        "scheduler": _scheduler_status(),
        "queue_depth": (
            {stage.value: await broker.depth(stage) for stage in JobStage}
            if redis_ok
            else {}
        ),
    }


def _scheduler_status() -> dict:
    """Watch-loop status, with the import deferred.

    `app.scheduler` imports the agent pipeline, which reaches `eo/cog` and therefore
    rasterio. Importing it at module scope would drag GDAL into every consumer of this
    router — including `app/openapi_export.py`, which must build the schema in a
    lightweight CI job with no geospatial stack. Same reasoning as the deferred imports
    in `eo/exposure.py`.
    """
    from app import scheduler

    return scheduler.status()


def _advisory_generator_summary() -> dict:
    """The resolved advisory path, and which model it will actually use.

    Reports the *resolved* provider rather than the configured one, because
    `auto` and the SDK-missing case both mean the two can differ — and a
    deployment silently emitting English templates when it meant to generate is
    exactly the kind of thing that should be visible on a calm Tuesday.
    """
    from app.advisory import generator

    provider = generator._resolve_provider()

    model: str | None = None
    if provider == "openai":
        model = settings.advisory_model_openai or settings.llm_model
    elif provider == "anthropic":
        model = settings.advisory_model

    return {
        "provider": provider,
        "configured": settings.advisory_provider,
        "model": model,
        # The frontend renders this string on the landing page.
        "label": model or "template",
    }


async def redis_server_summary(redis_ok: bool) -> dict:
    """Which server is behind db0, and whether eviction threatens the streams.

    Surfaced because eviction is a server-wide setting nothing in this codebase
    can enforce, and with it on, the queue can silently lose a scan.
    """
    if not redis_ok:
        return {}
    return await redis_client.server_info()


@router.get("/bootstrap")
async def bootstrap() -> dict:
    """Everything the frontend needs to talk to this backend, in one call.

    **The problem this solves.** The frontend deploys on Netlify and the backend on a
    VPS behind an SSL reverse proxy, so the two are configured independently and
    nothing verifies they agree. Three failure modes followed from that, all of which
    presented as a blank dashboard with no useful error:

      1. A prefix mismatch — the frontend built `/api/v1/...` against a backend
         serving `/shelter/v1/api/...`, producing 404s that look like empty data.
      2. A CORS origin the backend does not allow — the browser blocks the response
         while the backend logs a normal 200.
      3. `SHELTER_API_KEY` set on Netlify but not matching `API_KEY` here, so every
         write 401s while reads keep working.

    This endpoint lets the frontend assert all three at build or boot time and fail
    loudly instead. `safeApi` still degrades the page if the backend is down; this is
    about catching *misconfiguration*, which degrading silently only hides.

    **It is deliberately unauthenticated, and deliberately contains no secrets.** It
    echoes whether a key is *required* and whether the caller's key was *accepted* —
    never the key itself, and never any subscriber data. A caller who cannot reach
    this cannot configure a client at all, so gating it behind the key it exists to
    validate would be circular.
    """
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        # The authoritative prefix. A client that reads this cannot drift from it.
        "api_base": settings.api_prefix,
        "endpoints": {
            "docs": f"{settings.api_prefix}/docs",
            "openapi": f"{settings.api_prefix}/openapi.json",
            "health": f"{settings.api_prefix}/health",
            "ready": f"{settings.api_prefix}/ready",
            "subscribers": f"{settings.api_prefix}/subscribers",
            "alerts": f"{settings.api_prefix}/alerts",
            "assess": f"{settings.api_prefix}/risk/assess",
            "chat": f"{settings.api_prefix}/chat",
            "verification": f"{settings.api_prefix}/verification/metrics",
            "webhook": f"{settings.api_prefix}/webhook",
        },
        "auth": {
            "header": "X-SHELTER-Key",
            # Whether writes need a key here. False in development is expected and
            # is why the startup log warns about it in production.
            "required": bool(settings.api_key),
            "scheme": "bootstrap-api-key",
        },
        # So a frontend can verify its own origin is allowed *before* a user hits a
        # blocked request. This is public information: the browser learns it from
        # the Access-Control-Allow-Origin header on any request anyway.
        "cors_allowed_origins": settings.cors_origin_list,
        "capabilities": {
            "chat": settings.chat_enabled,
            "verification": settings.fahis_enabled,
            "webhooks": settings.webhook_engine_enabled,
            # So the portal can warn at signup if confirmation email cannot be sent,
            # instead of leaving an account that can never be activated.
            "email": iam_mailer.available(),
            "channels": [c.value for c in dispatch_router.available_channels()],
            "advisory_generator": _advisory_generator_summary()["label"],
        },
    }


@router.post("/bootstrap/verify")
async def verify_bootstrap(_: None = Depends(require_api_key)) -> dict:
    """Confirm the caller's API key is accepted by *this* backend.

    A separate POST rather than a field on `/bootstrap`, because the two questions
    have different auth requirements: "what is the contract?" must be answerable
    without a key, and "is my key valid?" cannot be. Splitting them keeps
    `/bootstrap` open without leaking whether any given key works.

    The frontend calls this once at startup. A 401 here means the Netlify
    `SHELTER_API_KEY` and the VPS `API_KEY` have diverged — which otherwise shows up
    only as a failed subscriber registration in front of a real user.

    **Deliberately still guarded by the legacy `require_api_key`.** Its whole purpose
    is to answer "is the credential I hold accepted here?", so it must validate the
    credential the caller actually presents rather than requiring a scoped key —
    which is the thing they may not have yet.
    """
    return {
        "authenticated": True,
        "key_required": bool(settings.api_key),
        "api_base": settings.api_prefix,
    }


@router.get("/ready")
async def ready(response: Response) -> dict:
    """Readiness for the container orchestrator — cheap, no fan-out.

    Gates on Postgres as well as db0: an instance that cannot reach its system of
    record should not be sent traffic.

    **Returns 503 when not ready, not 200 with `ready: false`.** The Docker
    healthcheck is `curl -fsS`, which only fails on a non-2xx status — so a 200
    carrying `false` reported every broken deployment as healthy, and
    `depends_on: service_healthy` would release dependents against a database that
    was never reachable. The body is kept for humans; the status code is what
    orchestrators actually read.
    """
    ready_now = await redis_client.ping() and await db.ping()
    if not ready_now:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"ready": ready_now}


def _weights_present(relative_path: str) -> str:
    path = Path(relative_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[3] / relative_path
    return "trained" if path.exists() else "heuristic-fallback"


# `/health/search` rather than `/search/probe`: this router carries no prefix, so a bare
# `/search/...` path sits beside `/places/search` in the route table and reads as a product
# endpoint. Grouping it under `/health` says what it is — an operator diagnostic.
@router.get("/health/search", include_in_schema=False)
async def search_probe() -> dict:
    """Send one real query to the configured search backend and report what came back.

    ## Why this exists

    A search backend fails silently by design: `search()` never raises, and Fahis reads an empty
    result as NOT_ATTEMPTED. That is correct — an outage must never be mistaken for evidence a
    warning was wrong — but it means a misconfigured endpoint looks identical to a working one that
    found nothing, and the only symptom is `precision: null` weeks later.

    This makes the difference visible in one request. Particularly useful against a **Tavily-
    compatible** endpoint rather than Tavily itself: a self-hosted SearXNG stack with a switch-over
    layer, a gateway, or a shim. Those vary in which auth form they accept and in whether they
    return Tavily's field names, and neither divergence produces an error — just an empty list.

    `include_in_schema=False`: an operator diagnostic, not part of the partner contract. It issues a
    real query and so costs a credit on a metered provider, which is another reason it is not
    something to poll.
    """
    from app.search import client as search_client

    backend = search_client.provider()
    if backend == "none":
        return {
            "provider": "none",
            "reachable": False,
            "detail": (
                "No search backend configured. Set SEARCH_PROVIDER to `searxng` or `tavily` and "
                "supply that provider's credentials. Fahis records NOT_ATTEMPTED until then, "
                "which is honest but leaves precision unmeasurable."
            ),
        }

    # A query with a real answer, so an empty result means the endpoint is wrong rather than the
    # subject being obscure.
    response = await search_client.search("Nigeria flooding", max_results=3, days=30)

    if not response.searched:
        return {
            "provider": backend,
            "reachable": False,
            "detail": (
                "The request failed. Check the API logs for the exact error — for SearXNG the "
                "usual cause is JSON output being disabled (a stock instance returns 403 for "
                "?format=json; set SEARXNG_SEARCH_FORMATS=['json','html']). For a Tavily-"
                "compatible endpoint, check the key and that the path is POST {base}/search."
            ),
        }

    return {
        "provider": backend,
        "reachable": True,
        "results": len(response.results),
        "by_tier": {
            "official": len(response.official),
            "media": len(response.media),
        },
        # First URL only — enough to confirm the endpoint returned real search results rather than
        # a placeholder or an HTML page parsed into nothing.
        "sample_url": response.results[0].url if response.results else None,
        "detail": (
            "Search is working."
            if response.results
            else (
                "The endpoint answered but returned no results. If this repeats for a query that "
                "should match, the response shape may not be what the adapter expects — check that "
                "results carry `url`, `title` and `content`."
            )
        ),
    }
