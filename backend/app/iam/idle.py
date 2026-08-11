"""Server-side idle-session tracking.

## What this adds that the JWT `exp` cannot

`security.issue_session` mints a token with a fixed 12-hour lifetime. That is an
*absolute* ceiling and nothing more: a token stolen five minutes after issue stays valid
for the rest of the day, and an unattended laptop in a co-operative office stays logged in
until the clock runs out. Neither is acceptable for a surface that shows a subscriber's
plots and lets an aggregator mint API keys.

A JWT is stateless by design, so it cannot express "idle for 15 minutes". This module adds
exactly one piece of server-side state — a last-seen timestamp per session — and enforces
the idle window against it.

## The design constraint that shapes everything here

**An active user must never be logged out.** That is the requirement, and it is the reason
the window is refreshed by real interaction rather than by request traffic. A browser makes
requests for reasons that have nothing to do with a human being present: prefetches,
service workers, a polling status widget. If any request refreshed the window, the timeout
would never fire on a dashboard that polls — which is precisely what this codebase's
`ServiceStatus` component does.

So there are two distinct operations:

  * `touch()` — called **only** from the explicit activity endpoint, which the browser
    calls in response to real input (pointer, key, scroll, focus). This is the only thing
    that extends a session.
  * `check()` — called on every authenticated request. Reads, never writes.

That split is what makes the timeout mean "no human here for 15 minutes" rather than "no
HTTP traffic for 15 minutes".

## Why Dragonfly db1 and not Postgres

One small write per activity ping and one read per request. In Postgres that is a row
update on the hot path of every authenticated call; in the cache it is an O(1) operation on
a key that expires on its own. The key carries a TTL of its own — mandatory in
`store/cache.py` — so an abandoned session leaves nothing behind and there is no sweep job.

**Fails open, deliberately.** If the cache is unreachable we cannot know when the user was
last active, and the two options are "log everyone out" or "fall back to the JWT's own
expiry". Logging every subscriber out of a hazard dashboard because a cache node restarted
is the worse failure, and the absolute `exp` still bounds the damage. This mirrors
`llm/budget.py`, which fails open for the same reason and says so.
"""

from __future__ import annotations

import time

from app.config import settings
from app.logging_config import get_logger
from app.store import cache

log = get_logger(__name__)

#: Namespace for the last-seen keys. Distinct prefix so `delete_prefix` can drop every
#: tracked session without touching other cached values.
_PREFIX = "iam:idle"


def _key(jti: str) -> str:
    return f"{_PREFIX}:{jti}"


def window_seconds() -> int:
    """The idle window, in seconds."""
    return max(60, settings.iam_idle_timeout_minutes * 60)


def warning_seconds() -> int:
    """How long before expiry the browser should start warning the user."""
    return max(15, settings.iam_idle_warning_seconds)


async def touch(jti: str) -> None:
    """Record real user activity for this session.

    Called from `POST /iam/session/activity` only. The key's TTL *is* the idle window, so
    writing it both records the timestamp and schedules the expiry — there is no separate
    cleanup path, and a session nobody touches disappears on its own.

    Never raises. A failed write means the session falls back to its absolute `exp`, which
    is worse than a refreshed window but far better than an exception on the hot path of a
    dashboard that is otherwise working.
    """
    try:
        await cache.set_text(_key(jti), str(int(time.time())), ttl_seconds=window_seconds())
    except Exception as exc:  # noqa: BLE001 — never break a request over telemetry
        log.warning("idle touch failed; session falls back to absolute expiry: %s", exc)


class IdleState:
    """The answer to "is this session still live, and how long has it got?".

    `seconds_remaining` is what the browser needs to run an honest countdown: computing it
    client-side from a locally stored timestamp would drift, and would be wrong entirely
    after the machine slept — which is exactly the case the idle timeout exists for.
    """

    __slots__ = ("expired", "seconds_remaining", "tracked")

    def __init__(self, *, expired: bool, seconds_remaining: int, tracked: bool) -> None:
        self.expired = expired
        self.seconds_remaining = seconds_remaining
        #: False when no last-seen record exists — either the cache is down or this is the
        #: first request of a session. Surfaced so the caller can tell "idle window not
        #: being enforced" from "plenty of time left", which look identical otherwise.
        self.tracked = tracked


async def check(jti: str) -> IdleState:
    """Read-only idle assessment. Never extends the window.

    A missing key is treated as **live, untracked** rather than expired. Two situations
    produce it and neither should sign anyone out:

      * the cache is unavailable — fail open, per the module docstring;
      * the session was just issued and the browser has not yet sent its first ping.

    Treating absence as expiry would log a user out during their first second, and would
    turn a cache blip into a site-wide forced logout.
    """
    window = window_seconds()

    try:
        raw = await cache.get_text(_key(jti))
    except Exception as exc:  # noqa: BLE001
        log.warning("idle check failed; treating session as live: %s", exc)
        return IdleState(expired=False, seconds_remaining=window, tracked=False)

    if raw is None:
        return IdleState(expired=False, seconds_remaining=window, tracked=False)

    try:
        last_seen = int(raw)
    except ValueError:
        # Corrupt value — treat as untracked rather than expired, same reasoning.
        return IdleState(expired=False, seconds_remaining=window, tracked=False)

    idle_for = max(0, int(time.time()) - last_seen)
    remaining = window - idle_for
    return IdleState(
        expired=remaining <= 0,
        seconds_remaining=max(0, remaining),
        tracked=True,
    )


async def end(jti: str) -> None:
    """Drop the tracking key on explicit sign-out.

    Not strictly required — the key expires by itself — but it makes a deliberate logout
    take effect immediately rather than leaving a live window behind, which matters if the
    same token is replayed from somewhere else before the TTL lapses.
    """
    try:
        await cache.delete(_key(jti))
    except Exception as exc:  # noqa: BLE001
        log.warning("idle key delete failed on sign-out: %s", exc)
