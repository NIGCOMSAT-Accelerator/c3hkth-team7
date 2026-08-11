"""Catalogue authentication.

Two mechanisms, both cached because they are per-request costs on a hot path:

**Planetary Computer SAS signing.** PC asset hrefs point at Azure Blob Storage
and are unreadable without a short-lived SAS token appended as a query string.
Tokens are per-collection and last ~1 hour. Without this, every windowed COG
read against PC returns 403 — the bug this module fixes.

**Copernicus Data Space OAuth.** Client-credentials grant against the CDSE
Keycloak realm. Anonymous search works for open collections, so a missing
credential downgrades rather than fails.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(20.0, connect=8.0)

#: Refresh this many seconds before actual expiry, so a token never dies
#: mid-read on a slow COG fetch.
#: Safety margin subtracted from a token's stated expiry, in seconds.
#:
#: ## The SAS API, verified against the live service 2026-08-11
#:
#: `GET /api/sas/v1/openapi.json` declares exactly three endpoints and **no `securitySchemes`** —
#: confirming the API is genuinely keyless, not merely tolerant of missing keys:
#:
#:     /api/sas/v1/token/{collection_id}
#:     /api/sas/v1/token/{storage_account}/{container}    <- what `sas_token` uses
#:     /api/sas/v1/sign?href=...                          <- signs one asset URL
#:
#: Each accepts optional `duration`, `write`, `delete`. **`duration` is authentication-gated:**
#: measured, an anonymous request for `duration=60` (or 240, or 1440) returns
#: `HTTP 403 {"detail":"This operation requires authentication."}`, while omitting it returns 200
#: with a **45-minute** lifetime. So a `PLANETARY_COMPUTER_API_KEY` buys a longer token and a higher
#: rate limit; it does not gate access. That is why `credential_key=None` in `eo/sources.py`, and why
#: `scout._REPLAY_CEILING_MINUTES = 30` sits inside 45 rather than being tuned upward — we cannot
#: extend the token on the keyless path, so the cache ceiling must respect the fixed lifetime.
#:
#: 300s is a fifth of that 45-minute window: long enough that a token cannot expire mid-read on a
#: slow COG fetch, short enough not to discard most of a token's usable life.
_EXPIRY_MARGIN = 300

_sas_cache: dict[str, tuple[str, float]] = {}
_sas_lock = asyncio.Lock()

_oauth_cache: tuple[str, float] | None = None
_oauth_lock = asyncio.Lock()


# --------------------------------------------------------------------------- #
# Planetary Computer
# --------------------------------------------------------------------------- #


async def sas_token(collection: str) -> str | None:
    """Fetch (and cache) a SAS token for one PC collection.

    Returns None when the token endpoint is unreachable — the caller then uses
    the unsigned href, which will fail on private collections but still works
    for the handful PC serves anonymously.
    """
    cached = _sas_cache.get(collection)
    now = time.time()
    if cached and cached[1] - _EXPIRY_MARGIN > now:
        return cached[0]

    async with _sas_lock:
        # Re-check: another coroutine may have refreshed while we waited.
        cached = _sas_cache.get(collection)
        if cached and cached[1] - _EXPIRY_MARGIN > time.time():
            return cached[0]

        url = f"{settings.planetary_sas_url.rstrip('/')}/token/{collection}"
        headers = {}
        if settings.planetary_computer_api_key:
            # Raises the rate limit and unlocks some restricted collections.
            headers["Ocp-Apim-Subscription-Key"] = settings.planetary_computer_api_key

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                body = response.json()
        except Exception as exc:
            log.warning(
                "planetary computer SAS token request failed",
                extra={"collection": collection, "error": str(exc)},
            )
            return None

        token = body.get("token")
        if not token:
            return None

        # `msft:expiry` is ISO-8601; fall back to a conservative 45 minutes.
        expiry = time.time() + 2_700
        raw_expiry = body.get("msft:expiry")
        if raw_expiry:
            try:
                from datetime import datetime

                expiry = datetime.fromisoformat(
                    raw_expiry.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                pass

        _sas_cache[collection] = (token, expiry)
        log.info("SAS token acquired", extra={"collection": collection})
        return token


def container_key(href: str) -> str | None:
    """`"{storage_account}/{container}"` for a Planetary blob href, or None if unparseable.

    ## Why the collection id is the wrong key

    `sas_token()` was called with the STAC collection id, which hits
    `/api/sas/v1/token/{collection_id}`. That endpoint issues a token for every collection we use —
    but the token it returns does not necessarily cover the container the assets actually live in.
    Measured on `cop-dem-glo-30`:

        collection token          -> HTTP 403
        {account}/{container}     -> HTTP 206
        /sign?href=...            -> HTTP 206

    The same 403 hit `jrc-gsw`. Both are Planetary-only sources, so the failure had no fallback:
    `cog.read_band` raised `CogReadError`, `terrain.permanent_water_mask` returned None, and the
    Analyst reported "dem read failed" / "permanent-water read failed" on every single cycle — which
    is exactly what the pipeline logs had been showing all along.

    Deriving the account and container from the href is what makes the token match the data. It also
    means one token serves every asset in a container regardless of which collection indexed it.
    """
    try:
        # https://{account}.blob.core.windows.net/{container}/path...
        parts = href.split("?", 1)[0].split("/")
        account = parts[2].split(".")[0]
        container = parts[3]
    except (IndexError, AttributeError):
        return None
    if not account or not container:
        return None
    return f"{account}/{container}"


def sign_href(href: str, token: str | None) -> str:
    """Append a SAS token to a blob href, preserving any existing query."""
    if not token:
        return href
    if "?" in href:
        # Already signed (or carries other params) — don't double-append.
        existing = href.split("?", 1)[1]
        if "sig=" in existing:
            return href
        return f"{href}&{token}"
    return f"{href}?{token}"


# --------------------------------------------------------------------------- #
# Copernicus Data Space
# --------------------------------------------------------------------------- #


async def copernicus_token() -> str | None:
    """Client-credentials access token for CDSE, cached until near expiry."""
    if not (settings.copernicus_client_id and settings.copernicus_client_secret):
        return None

    global _oauth_cache
    if _oauth_cache and _oauth_cache[1] - _EXPIRY_MARGIN > time.time():
        return _oauth_cache[0]

    async with _oauth_lock:
        if _oauth_cache and _oauth_cache[1] - _EXPIRY_MARGIN > time.time():
            return _oauth_cache[0]

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(
                    settings.copernicus_token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": settings.copernicus_client_id,
                        "client_secret": settings.copernicus_client_secret,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                response.raise_for_status()
                body = response.json()
        except Exception as exc:
            log.warning("copernicus token request failed", extra={"error": str(exc)})
            return None

        token = body.get("access_token")
        if not token:
            return None

        _oauth_cache = (token, time.time() + float(body.get("expires_in", 600)))
        log.info("copernicus access token acquired")
        return token


# --------------------------------------------------------------------------- #
# NASA Earthdata
# --------------------------------------------------------------------------- #


def earthdata_headers() -> dict[str, str]:
    """Bearer header for GES DISC / IMERG, empty when no token is configured."""
    if not settings.nasa_earthdata_token:
        return {}
    return {"Authorization": f"Bearer {settings.nasa_earthdata_token}"}
