"""Breached-password check, via Have I Been Pwned's k-anonymity range API.

## The password never leaves this process, and neither does its full hash

This is the part worth understanding before touching the code, because a naive
implementation of "check the password against HIBP" would be a catastrophic privacy
regression on a service holding a list of farmers.

The protocol:

  1. SHA-1 the candidate password locally.                    `market rain bicycle → 6F3B2…`
  2. Send only the **first five hex characters** of that hash. `GET /range/6F3B2`
  3. HIBP returns every suffix it holds under that prefix — typically 300–900 of them.
  4. Match the remaining 35 characters **locally**, against that list.

So the service learns a 5-character prefix shared by roughly one in a million hashes, and
never sees the password, the full hash, the account, or whether a match occurred. There is
no request that means "is *this* password breached" — only "what do you have under this
prefix", which every caller asks identically.

**SHA-1 is correct here and is not a security decision.** It is the index HIBP publishes;
we are looking up a key in someone else's table, not hashing for storage. Passwords are
stored with Argon2id (`security.py`) and that is unaffected.

## Why this is advisory and fails OPEN

`check()` returns `False` — "not known to be breached" — on any network failure, timeout or
malformed response. That is deliberate and it is the same reasoning as `llm/budget.py`:

  * A **false negative** lets one reused password through. Real, but bounded: the 12-char
    minimum, the banned-word list and Argon2id all still apply.
  * A **false positive** — refusing a signup because HIBP was briefly unreachable — blocks a
    farmer from registering for flood warnings, and gives them an error they cannot act on
    because the password they chose is in fact fine.

The second failure is worse, so the check is a filter that improves the common case rather
than a gate that can deny service. `HIBP_ENABLED=false` turns it off entirely.

## Caching

Prefix responses are cached for `HIBP_CACHE_TTL_SECONDS`. A prefix bucket is not
account-specific — it is a public range shared by ~1-in-a-million hashes — so caching it
leaks nothing, and it means a burst of signups with similar weak passwords costs one
request rather than one per attempt. Stored via `store/cache.py`, so the TTL is mandatory.
"""

from __future__ import annotations

import hashlib

import httpx

from app.config import settings
from app.logging_config import get_logger
from app.store import cache

log = get_logger(__name__)

#: Cache namespace. Only ever holds public prefix buckets, never anything account-scoped.
_PREFIX = "iam:hibp"

#: HIBP asks for a descriptive User-Agent and may throttle requests without one.
_HEADERS = {
    "User-Agent": "SHELTER-EarlyWarning/1.0 (+https://shelter.zerorate.io)",
    # Pads each response with random hashes so a network observer cannot infer the real
    # bucket size — free extra privacy, one header.
    "Add-Padding": "true",
}


def _sha1(password: str) -> str:
    # usedforsecurity=False documents intent to any auditor and to linters: this is a
    # lookup key in HIBP's published index, not a password hash. Storage is Argon2id.
    return hashlib.sha1(password.encode("utf-8"), usedforsecurity=False).hexdigest().upper()


async def _fetch_range(prefix: str) -> str | None:
    """The raw suffix list for one 5-char prefix, or None on any failure."""
    cached = await _cached(prefix)
    if cached is not None:
        return cached

    url = f"{settings.hibp_range_url.rstrip('/')}/{prefix}"
    try:
        async with httpx.AsyncClient(timeout=settings.hibp_timeout_seconds) as client:
            response = await client.get(url, headers=_HEADERS)
        if response.status_code != 200:
            log.warning("HIBP range returned %s; treating as not-breached", response.status_code)
            return None
        body = response.text
    except Exception as exc:  # noqa: BLE001 — fail open, see the module docstring
        log.warning("HIBP range unreachable (%s); treating as not-breached", exc)
        return None

    try:
        await cache.set_text(
            f"{_PREFIX}:{prefix}", body, ttl_seconds=settings.hibp_cache_ttl_seconds
        )
    except Exception:  # noqa: BLE001 — a cache miss is not a failure
        pass
    return body


async def _cached(prefix: str) -> str | None:
    try:
        return await cache.get_text(f"{_PREFIX}:{prefix}")
    except Exception:  # noqa: BLE001 — reads must never raise; fall through to the network
        return None


async def check(password: str) -> tuple[bool, int]:
    """`(is_breached, times_seen)`.

    `times_seen` is HIBP's occurrence count, surfaced because it changes the message a user
    should see: 3 occurrences is a plausible coincidence, 3 million means the password is in
    every cracking dictionary in existence. `(False, 0)` when unknown, unreachable or
    disabled — never an exception, so no caller needs a try block around it.
    """
    if not settings.hibp_enabled or not password:
        return False, 0

    digest = _sha1(password)
    prefix, suffix = digest[:5], digest[5:]

    body = await _fetch_range(prefix)
    if body is None:
        return False, 0

    # Lines are `SUFFIX:COUNT`. Parsed defensively: one malformed line must not discard the
    # whole bucket, because the rest of it is still a valid answer.
    for line in body.splitlines():
        candidate, _, count = line.partition(":")
        if candidate.strip().upper() != suffix:
            continue
        try:
            return True, int(count.strip().replace(",", ""))
        except ValueError:
            return True, 0

    return False, 0
