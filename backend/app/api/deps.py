"""Shared route dependencies."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Security, status

from app.api.security_schemes import legacy_shared_key
from app.config import settings


async def require_api_key(
    # Declared as a security scheme so platform-only endpoints show a padlock rather than a
    # bare header parameter. This is the LEGACY shared key — `IAM_LEGACY_SHARED_KEY_ENABLED`
    # exists to retire it in favour of scoped service accounts.
    x_shelter_key: str | None = Security(legacy_shared_key),
) -> None:
    """Guard write endpoints.

    When `API_KEY` is unset the guard is a no-op, which keeps local development
    frictionless — but the startup log says so loudly, because an unauthenticated
    production deployment lets anyone register a subscriber and trigger
    satellite broadcasts in someone else's name.
    """
    if not settings.api_key:
        return

    if not x_shelter_key or not hmac.compare_digest(x_shelter_key, settings.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-SHELTER-Key",
        )
