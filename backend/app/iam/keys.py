"""API-key lifecycle — the governance layer.

Modelled on how Anthropic, OpenAI, GitHub and Stripe actually run key material. The
individual pieces are unremarkable; what matters is that they compose, so no single
mistake is sufficient for a breach.

## The seven properties, and the failure each one prevents

| Property | Prevents |
|---|---|
| **Show once, at creation** | A key sitting readable in a dashboard, an Atlas backup, or a support screen-share forever |
| **Hashed at rest (SHA-256)** | A database dump yielding usable credentials |
| **Checksummed, prefixed body** | A truncated paste failing *at creation* rather than as a mystery 401 next week; and secret scanners recognising a leak |
| **Last-4 + hint stored** | Identifying *which* key a log line or a leak report refers to without the key |
| **Optional expiry, explicit choice** | Forgotten eternal keys — while never breaking an integration by surprise, because expiry is opt-in |
| **Rotation with a grace window** | The rotate-or-break dilemma: a partner can deploy the new key before the old one dies |
| **Immediate revocation + full audit** | A leaked key staying valid until expiry, and nobody being able to say what it did |

## Two decisions that differ from the obvious choice

**A checksum inside the key, not just a prefix.** `shltky<body><crc>` lets the API
reject a malformed key *without a database read*, which matters because unauthenticated
requests are the cheapest thing to flood. It also means a copy-paste that dropped the
last character is a clear "malformed key" error rather than an indistinguishable 401.
GitHub adopted this for PATs for the same reason.

**Rotation is not delete-then-create.** `rotate` mints the replacement and schedules
the old key's death, so both work during the window. Delete-then-create forces a
partner to choose between an outage and leaving a compromised key live — and faced with
that, they leave it live.

## What is deliberately *not* here

**No key recovery, ever.** Not by support, not by the account owner, not with a
password re-prompt. The plaintext is not stored, so recovery is not a policy decision
we could reverse — it is arithmetically impossible. That is the property that makes
"show once" true rather than merely stated.
"""

from __future__ import annotations

import binascii
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

#: Environment-tagged prefixes. A production key is visually distinct from a test
#: key, so the "we pasted the wrong environment's key" incident is caught by reading
#: rather than by debugging. Same reason Stripe uses sk_live_/sk_test_.
#:
#: Both are 6 characters so `TOTAL_KEY_LENGTH` is exact either way — a live and a test
#: key must be the same length, or a length check becomes an environment oracle.
PREFIX_LIVE = "shltky"
PREFIX_TEST = "shlttk"

#: Total key length, prefix included. Fixed and exact.
#:
#: A constant length is not cosmetic: `is_well_formed` can then reject a wrong-length
#: string before any database read, which matters because the API-key header is an
#: unauthenticated surface and therefore the cheapest thing for an attacker to flood.
TOTAL_KEY_LENGTH = 64

#: Length of the CRC32 checksum suffix, hex.
CHECKSUM_LENGTH = 6

#: Characters the random body is drawn from.
#:
#: Alphanumerics plus `-._~`, which are the RFC 3986 *unreserved* characters. That
#: choice is deliberate rather than "some punctuation for entropy":
#:
#: * A key must survive being pasted into a URL, a shell command, a YAML file and an
#:   HTTP header without escaping. `+`, `/` and `=` (base64) break URLs; `$`, `!` and
#:   backtick are shell-active; `#` truncates a URL fragment; `:` splits a header.
#: * Header values are further restricted — a raw `,` or `;` invites proxy mangling.
#:
#: 66 characters at 52 body positions is ~314 bits of entropy, far beyond
#: brute-forceable, so nothing is lost by excluding the hostile punctuation.
KEY_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    "-._~"
)

#: Random characters between the prefix and the checksum, derived so the total is
#: exact. Computed rather than hardcoded: changing the prefix or the checksum length
#: must not silently change the key length.
BODY_LENGTH = TOTAL_KEY_LENGTH - len(PREFIX_LIVE) - CHECKSUM_LENGTH

#: Characters of the body retained for display. Enough to disambiguate keys in a
#: list, far too few to reconstruct one.
HINT_LENGTH = 8


class KeyStatus(str, Enum):
    ACTIVE = "active"
    #: Superseded by a rotation but still valid until `grace_expires_at`. Visible in
    #: the portal as "rotating", so the state is legible rather than mysterious.
    ROTATING = "rotating"
    #: Explicitly revoked. Terminal, and never reused.
    REVOKED = "revoked"
    #: Reached `expires_at`. Terminal.
    EXPIRED = "expired"


class KeyEvent(str, Enum):
    """Audit-trail event types.

    Every state change is recorded. The question after any incident is "what did this
    key do, and who changed it when?" — and that is unanswerable from application logs
    alone, which rotate and are not scoped per key.
    """

    CREATED = "created"
    USED = "used"
    ROTATED = "rotated"
    REVOKED = "revoked"
    EXPIRED = "expired"
    #: A presented key that did not resolve. Recorded because a burst of these
    #: against one account is the signal that a key has leaked and is being probed.
    REJECTED = "rejected"
    SCOPE_DENIED = "scope_denied"


#: Rotation grace window. The old key keeps working this long after a rotation.
#:
#: 24 hours: long enough for a partner to notice, deploy and verify across time
#: zones; short enough that a compromised key is not live for a week. Configurable,
#: because an aggregator mid-incident wants zero and one mid-migration wants longer.
DEFAULT_ROTATION_GRACE_HOURS = 24


@dataclass(frozen=True)
class MintedKey:
    """A freshly-created key. The plaintext exists only in this object."""

    plaintext: str
    key_hash: str
    hint: str
    last_four: str
    prefix: str


def _checksum(body: str) -> str:
    """CRC32 of the key body, hex, truncated.

    CRC32 rather than a cryptographic hash on purpose: this detects *transcription
    errors*, it is not a security control. Using SHA-256 here would imply the checksum
    authenticates the key, which it must not — the authentication is the stored hash
    comparison, and confusing the two is how a checksum becomes a bypass.
    """
    return format(binascii.crc32(body.encode()) & 0xFFFFFFFF, "08x")[:CHECKSUM_LENGTH]


def mint(*, test_mode: bool = False) -> MintedKey:
    """Generate a key. The plaintext is returned once and never persisted.

    Shape: `shltky` + 52 random chars + 6-char CRC = **64 characters exactly**.

    `secrets.choice` over an explicit alphabet rather than `token_urlsafe`, for two
    reasons: `token_urlsafe` emits base64 whose length is a multiple of 4/3 and cannot
    be pinned to an arbitrary total, and its `-`/`_` output would silently differ from
    the documented character set. Drawing per character makes both the length and the
    alphabet exact.

    `secrets.choice` is CSPRNG-backed — `random.choice` here would be a critical
    vulnerability, since the sequence would be reproducible from a few observed keys.
    """
    prefix = PREFIX_TEST if test_mode else PREFIX_LIVE
    body = "".join(secrets.choice(KEY_ALPHABET) for _ in range(BODY_LENGTH))
    plaintext = f"{prefix}{body}{_checksum(body)}"

    return MintedKey(
        plaintext=plaintext,
        key_hash=hash_key(plaintext),
        hint=body[:HINT_LENGTH],
        last_four=plaintext[-4:],
        prefix=prefix,
    )


def is_well_formed(presented: str) -> bool:
    """Shape and checksum validation, with no database access.

    Rejects junk, truncated pastes and session tokens before spending an Atlas round
    trip — which matters because the API-key header is an unauthenticated surface and
    therefore the cheapest thing for an attacker to flood.
    """
    if not presented or not presented.startswith((PREFIX_LIVE, PREFIX_TEST)):
        return False

    # Exact length, not a minimum. Keys are fixed-width, so anything else is a
    # truncated paste or a fabrication — rejected here, before any database read.
    if len(presented) != TOTAL_KEY_LENGTH:
        return False

    prefix = PREFIX_LIVE if presented.startswith(PREFIX_LIVE) else PREFIX_TEST
    remainder = presented[len(prefix) :]

    body, checksum = remainder[:-CHECKSUM_LENGTH], remainder[-CHECKSUM_LENGTH:]

    # Every body character must be from the documented alphabet. Without this, a key
    # carrying a shell metacharacter or a header separator would be accepted and then
    # mangled by a proxy — failing as an unexplained 401 rather than a clear rejection.
    if any(c not in KEY_ALPHABET for c in body):
        return False
    # compare_digest even here: the checksum is not a secret, but using it uniformly
    # means no future reader has to reason about which comparisons are timing-safe.
    return hmac.compare_digest(_checksum(body), checksum)


def hash_key(plaintext: str) -> str:
    """SHA-256 of the full key, hex.

    Plain SHA-256, not Argon2 and not HMAC-with-a-pepper:

    * The input is 256 bits of CSPRNG output with no guessable structure, so
      key-stretching defends against nothing — there is no dictionary to slow down.
    * This runs on **every** authenticated API request. Argon2's ~50 ms would be a
      self-inflicted rate limit on the partner API.
    * A pepper would add a second secret to manage whose compromise is as likely as
      the database's, for no gain against an unguessable input.

    Passwords are the opposite case — low entropy, verified rarely — which is exactly
    why `security.hash_password` uses Argon2id and this does not.
    """
    return hashlib.sha256(plaintext.encode()).hexdigest()


def matches(presented: str, stored_hash: str) -> bool:
    """Constant-time hash comparison.

    `compare_digest`, never `==`: string equality short-circuits at the first
    differing byte, and over a keep-alive connection behind a reverse proxy that
    timing is measurable enough to recover a hash byte by byte.
    """
    return hmac.compare_digest(hash_key(presented), stored_hash)


def expiry_from_days(days: int | None) -> datetime | None:
    """Absolute expiry, or None for a non-expiring key.

    **None is a legitimate, supported choice, not an oversight.** A key that silently
    stops working breaks a partner's integration at an unpredictable moment, and the
    resulting incident is worse than a long-lived key that is monitored and revocable.
    The portal states the trade-off and makes the user pick, which is what GitHub does
    with PAT expiry.
    """
    if days is None:
        return None
    return datetime.now(timezone.utc) + timedelta(days=days)


def is_expired(expires_at: datetime | None) -> bool:
    """Whether a key has passed its expiry.

    Mongo returns naive UTC datetimes, so the tzinfo is normalised before comparing —
    without that, this raises `TypeError` on every expiring key and the guard fails
    closed for the wrong reason.
    """
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


def rotation_deadline(grace_hours: int | None = None) -> datetime:
    """When a rotated key stops working."""
    hours = DEFAULT_ROTATION_GRACE_HOURS if grace_hours is None else max(0, grace_hours)
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def usable_status(doc: dict) -> KeyStatus | None:
    """The effective status of a stored key, or None if it may not authenticate.

    Centralised so the guard, the portal listing and the audit view cannot disagree
    about whether a key is live — three separate implementations of "is this valid?"
    is how a revoked key keeps working in one code path.

    Order matters: revocation beats everything, and expiry beats a rotation grace
    window (an expired key is dead even if it was rotated an hour ago).
    """
    status = doc.get("status", KeyStatus.ACTIVE.value)

    if status == KeyStatus.REVOKED.value:
        return None
    if is_expired(doc.get("expires_at")):
        return None

    if status == KeyStatus.ROTATING.value:
        if is_expired(doc.get("grace_expires_at")):
            return None
        return KeyStatus.ROTATING

    return KeyStatus.ACTIVE if status == KeyStatus.ACTIVE.value else None


def redact(plaintext: str) -> str:
    """Safe-to-log representation: `shltky…a1b2`.

    Used wherever a key might otherwise reach a log line. A log aggregator is a much
    wider audience than the database, and a key in a log is a key in every downstream
    system that ingests logs — which is precisely how these leak in practice.
    """
    if not plaintext or len(plaintext) < 8:
        return "<redacted>"
    prefix = PREFIX_LIVE if plaintext.startswith(PREFIX_LIVE) else (
        PREFIX_TEST if plaintext.startswith(PREFIX_TEST) else ""
    )
    return f"{prefix}…{plaintext[-4:]}"


def key_health(doc: dict) -> dict:
    """Governance summary for the portal, per key.

    Surfaces the two things an operator acts on and would otherwise never see:

    * **`stale`** — created but never used, or unused for a long time. An unused live
      key is pure liability: nothing breaks when it is revoked, and it is exactly the
      kind that sits forgotten in an old CI config.
    * **`expiring_soon`** — so a partner is warned before the outage, not by it.
    """
    now = datetime.now(timezone.utc)
    last_used = doc.get("last_used_at")
    if last_used is not None and last_used.tzinfo is None:
        last_used = last_used.replace(tzinfo=timezone.utc)

    expires_at = doc.get("expires_at")
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    days_until_expiry = (expires_at - now).days if expires_at else None

    return {
        "never_used": last_used is None,
        "days_since_last_use": (now - last_used).days if last_used else None,
        "stale": last_used is None or (now - last_used).days >= settings.iam_key_stale_days,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "days_until_expiry": days_until_expiry,
        "expiring_soon": (
            days_until_expiry is not None
            and 0 <= days_until_expiry <= settings.iam_key_expiry_warning_days
        ),
        "never_expires": expires_at is None,
    }
