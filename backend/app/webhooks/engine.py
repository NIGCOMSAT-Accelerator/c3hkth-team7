"""Webhook delivery engine — at-least-once, signed, with backoff.

**How this differs from `app/dispatch/webhook.py`, and why both exist.** That module
delivers an alert to a URL a *farmer* configured, as one of seven channels in the
Herald's fan-out: one attempt, no retry, and a failure falls through to SMS or
NIGCOMSAT broadcast. Correct for its job — a warning must not wait on a retry
schedule, and the router must never block.

This is the business-integration surface. An insurer's payout engine or a
cooperative's own AI assistant subscribes here, and its requirements are different
in kind: event filtering, per-endpoint secrets, at-least-once delivery with backoff,
and a queryable history. Putting retries into the Herald's fan-out would mean a slow
endpoint delaying a flood warning, which is why these are separate.

**Three properties this guarantees, and what each costs:**

1. **At-least-once, never at-most-once.** A delivery row is written *before* the
   HTTP attempt, so a crash mid-request leaves a retryable row rather than a
   silently dropped event. The cost is that receivers must be idempotent — hence
   `X-SHELTER-Delivery`, a stable id across retries, documented as the
   deduplication key.

2. **Signed with a per-endpoint secret**, and the signature covers a timestamp as
   well as the body. Body-only signatures are replayable: an attacker who captures
   one valid payload can resend it indefinitely and trigger repeat payouts. The
   timestamp bounds that window.

3. **The exact sent bytes are stored.** A redelivery re-sends them verbatim rather
   than re-serialising, because Python dict ordering could differ between processes
   and the receiver's signature check would fail on a payload we consider identical.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings
from app.logging_config import get_logger
from app.models.enums import Severity
from app.webhooks import schemas

log = get_logger(__name__)

#: Severity ordering for the `min_severity` filter. Declared explicitly rather
#: than relying on enum definition order, which is not a documented contract and
#: would silently reorder if a member were inserted.
SEVERITY_RANK: dict[str, int] = {
    Severity.INFO.value: 0,
    Severity.ADVISORY.value: 1,
    Severity.WATCH.value: 2,
    Severity.WARNING.value: 3,
    Severity.EMERGENCY.value: 4,
}

#: Retry delays in seconds: ~1 min, 5 min, 30 min, 2 h, 6 h.
#:
#: Front-loaded then widening, because the failure distribution is bimodal — most
#: are a redeploy or a brief network blip (recovered within minutes), and the rest
#: are a genuine outage (hours). A uniform schedule serves neither. Five attempts
#: spanning ~9 hours, after which the delivery is abandoned and visible in the
#: history rather than retried forever.
RETRY_SCHEDULE_SECONDS: tuple[int, ...] = (60, 300, 1800, 7200, 21600)

#: Signature version prefix. Present so the scheme can change without every
#: receiver's verification breaking on the same day — they can accept both.
SIGNATURE_VERSION = "v1"


def new_secret() -> str:
    """A fresh per-endpoint signing secret.

    `token_urlsafe` rather than `uuid4`: 32 bytes of CSPRNG entropy against
    uuid4's 122 bits, and it is URL- and header-safe so a business can paste it
    into their own config without escaping.
    """
    return f"whsec_{secrets.token_urlsafe(32)}"


def new_delivery_id() -> str:
    return f"whd_{uuid.uuid4().hex[:20]}"


def new_subscription_id() -> str:
    return f"whs_{uuid.uuid4().hex[:20]}"


def canonical_body(payload: dict) -> str:
    """Serialise deterministically, so the same payload always signs identically.

    `sort_keys` plus tight separators. Without sorting, two processes could emit
    different byte strings for the same dict and a receiver verifying a stored
    signature would reject a payload we consider unchanged.
    """
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)


def sign(body: str, secret: str, *, timestamp: int | None = None) -> tuple[str, int]:
    """`(signature_header, timestamp)` for a payload.

    The signed string is `f"{timestamp}.{body}"`, not the body alone. **This is the
    replay defence**: a body-only signature stays valid forever, so anyone who
    captures one payload can resend it and trigger a repeat insurance payout.
    Binding the timestamp lets the receiver reject anything older than its own
    tolerance.

    Returns the header value as `v1=<hex>` so the scheme is versioned and can be
    rotated without breaking every integration on the same day.
    """
    stamp = timestamp if timestamp is not None else int(time.time())
    signed = f"{stamp}.{body}".encode()
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}", stamp


def verify(body: str, secret: str, signature: str, timestamp: int,
           *, tolerance_seconds: int = 300) -> bool:
    """Reference verification — what a receiver should implement.

    Exists in the codebase for two reasons: the integration docs can point at real
    code rather than pseudocode, and `test_webhooks.py` asserts sign/verify round
    trip so a change to `sign` cannot silently break every receiver.

    Note `compare_digest`, not `==`. String equality short-circuits on the first
    differing byte, which leaks the correct prefix through timing and lets an
    attacker recover a signature byte by byte.
    """
    if abs(int(time.time()) - timestamp) > tolerance_seconds:
        return False
    expected, _ = sign(body, secret, timestamp=timestamp)
    return hmac.compare_digest(expected, signature)


def matches(subscription: dict, event: str, severity: str | None,
            aoi_id: str | None) -> bool:
    """Whether an endpoint wants this event.

    Every filter is **opt-out**: an empty `events` list means all events, a NULL
    `min_severity` means no floor, an empty `aoi_ids` means every area. A new
    integration that has not configured filters receives everything and narrows
    from there — the opposite default would mean a business subscribes, configures
    nothing, receives nothing, and concludes the product is broken.
    """
    events = subscription.get("events") or []
    if events and event not in events:
        return False

    floor = subscription.get("min_severity")
    if floor and severity:
        if SEVERITY_RANK.get(severity, 0) < SEVERITY_RANK.get(floor, 0):
            return False

    scoped = subscription.get("aoi_ids") or []
    if scoped and aoi_id and aoi_id not in scoped:
        return False

    return True


def next_attempt_delay(attempts: int) -> int | None:
    """Seconds until the next retry, or None when attempts are exhausted.

    `attempts` is the count already made. Returning None is what moves a delivery
    to `abandoned` — a terminal state that stays visible in the history rather than
    being retried forever or quietly deleted.
    """
    if attempts < 1 or attempts > len(RETRY_SCHEDULE_SECONDS):
        return None if attempts >= len(RETRY_SCHEDULE_SECONDS) else RETRY_SCHEDULE_SECONDS[0]
    return RETRY_SCHEDULE_SECONDS[attempts - 1] if attempts <= len(RETRY_SCHEDULE_SECONDS) else None


def is_retryable(status_code: int | None) -> bool:
    """Whether an HTTP response warrants a retry.

    The distinction that matters: **4xx means "your payload is wrong", so retrying
    is pointless and just hammers someone's endpoint.** 5xx and network errors mean
    "try later". Two exceptions to the 4xx rule, both real:

      * **408 Request Timeout** — the receiver is slow, not wrong.
      * **429 Too Many Requests** — explicitly asking us to retry later.
    """
    if status_code is None:
        return True                     # network error, DNS failure, timeout
    if status_code in (408, 429):
        return True
    return status_code >= 500


async def deliver(url: str, body: str, secret: str, *, event: str,
                  delivery_id: str) -> tuple[bool, int | None, str | None]:
    """One HTTP attempt. Returns `(delivered, status_code, error)`.

    **Never raises.** A dead endpoint is an expected condition, not an exception —
    the caller records the outcome and schedules a retry, and one broken integration
    must not affect any other.
    """
    signature, timestamp = sign(body, secret)

    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"SHELTER/{settings.app_version}",
        "X-SHELTER-Event": event,
        # Stable across retries — the receiver's idempotency key. Documented as
        # such, because at-least-once delivery makes duplicates the receiver's
        # problem to handle and this is the only tool for it.
        "X-SHELTER-Delivery": delivery_id,
        "X-SHELTER-Signature": signature,
        "X-SHELTER-Timestamp": str(timestamp),
    }

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.webhook_timeout_seconds, connect=5.0),
            # No redirect following: a 301 to http:// would silently downgrade a
            # signed payload onto plaintext, and a redirect to an internal address
            # would turn the engine into an SSRF vector.
            follow_redirects=False,
        ) as client:
            response = await client.post(url, content=body, headers=headers)
    except Exception as exc:
        return False, None, f"{type(exc).__name__}: {exc}"

    if 200 <= response.status_code < 300:
        return True, response.status_code, None

    # Truncate: a receiver returning an HTML error page would otherwise put
    # kilobytes of markup into every log line and database row.
    detail = response.text[:200].replace("\n", " ") if response.text else ""
    return False, response.status_code, f"HTTP {response.status_code}: {detail}".strip()


def event_payload(event: str, data: dict, *, delivery_id: str) -> dict:
    """The envelope every webhook shares.

    A stable outer shape means a receiver writes one parser. `data` carries the
    event-specific body, so adding an event type does not change the envelope.
    """
    return {
        "event": event,
        "delivery_id": delivery_id,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        # The PAYLOAD contract version, distinct from the service version below.
        #
        # `api_version` changes on every release, so a partner watching it for contract changes
        # sees one on a release that fixed a typo — and learns to ignore it, which is worse than no
        # version at all. `contract_version` bumps only on a removal or rename, the two changes
        # that break a handler. See `app/webhooks/schemas.py`.
        "contract_version": schemas.CONTRACT_VERSION,
        "api_version": settings.app_version,
        "data": data,
    }


def due_at(delay_seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
