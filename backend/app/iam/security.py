"""Password hashing and session tokens.

Every function here is pure and synchronous, so `tests/test_iam.py` can assert real
cryptographic properties — that a hash verifies, that a tampered token is rejected,
that two keys never collide — rather than mocking a security layer, which proves
nothing about whether it is secure.

**The four decisions worth defending:**

1. **Argon2id, not bcrypt.** bcrypt silently truncates input at 72 bytes, so a long
   passphrase is weaker than it looks, and it has no memory-hardness — a leaked hash
   is cheap to attack on commodity GPUs. Argon2id won the Password Hashing
   Competition and is the OWASP first choice.

2. **Tokens are hashed with SHA-256, not Argon2.** A verification token is 32 bytes
   of CSPRNG output, so it has no guessable structure and key-stretching defends
   against nothing. Passwords are low-entropy and verified rarely, which is the
   opposite trade. API-key material lives in `keys.py`, not here — it used to be
   duplicated in both, so two places defined the prefix.

3. **Session tokens are stateless JWTs; API keys are not.** A portal session is
   short-lived and high-frequency, so a database read per request is waste. An API
   key is long-lived and must be revocable *immediately* — statelessness would mean a
   leaked key stays valid until it expires, which for a key with no expiry is
   forever.

4. **The JWT audience claim is the individual/API boundary.** A portal token carries
   `aud=portal` and the API-key guard does not accept JWTs at all, so a stolen farmer
   session cannot be replayed against the machine API.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

#: Argon2id parameters. These are the argon2-cffi defaults, which track the OWASP
#: recommendation (~64 MB, 3 passes) — deliberately not tuned down. On the target
#: CPU-only VPS a verify costs ~50 ms, which is imperceptible on a login and
#: expensive at scale for an attacker with a stolen hash. `iam/routes.py` rate-limits
#: login so that cost cannot be turned against us.
_hasher = PasswordHasher()

#: Audience claim for portal sessions. The API-key path never issues or accepts a
#: JWT, so this is what structurally separates the two credential types.
PORTAL_AUDIENCE = "shelter:portal"

# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #


def hash_password(password: str) -> str:
    """Argon2id hash, including its own parameters and salt.

    The returned string is self-describing (`$argon2id$v=19$m=...`), so raising the
    cost parameters later does not invalidate existing hashes — `verify_password`
    reports when a stored hash needs rehashing.
    """
    return _hasher.hash(password)


def verify_password(password: str, stored_hash: str | None) -> bool:
    """Constant-time-ish verification that never raises.

    **Returns False for a None hash rather than short-circuiting earlier**, so an
    aggregator-created account with no password behaves identically to a wrong
    password. Distinguishing them would let an attacker enumerate which addresses are
    claimable, and `authenticate` also performs a dummy hash so the timing matches.
    """
    if not stored_hash:
        return False
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except Exception:
        # A malformed stored hash is a data problem, not an authentication success.
        log.exception("password verification error")
        return False


def needs_rehash(stored_hash: str) -> bool:
    """Whether a hash was made with weaker parameters than we now use.

    Called after a successful login so cost parameters can be raised over time and
    existing accounts upgrade transparently on next use.
    """
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except Exception:
        return False


def dummy_verify() -> None:
    """Burn the same CPU a real verification would.

    Called when an account does not exist. Without it, a missing address returns in
    microseconds and a real one in ~50 ms, which is a reliable account-enumeration
    oracle — a meaningful privacy leak when the addresses are farmers in a named
    district.
    """
    try:
        _hasher.verify(
            "$argon2id$v=19$m=65536,t=3,p=4$"
            "c29tZXNhbHRzb21lc2FsdA$" + "0" * 43,
            "timing-equalisation",
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Portal sessions
# --------------------------------------------------------------------------- #


def _signing_key() -> str:
    """The JWT signing secret.

    Falls back to `API_KEY` so local development needs no extra configuration, and
    `app/preflight.py` makes an unset `IAM_JWT_SECRET` a hard error in production —
    a predictable signing key would let anyone mint a session for any account.
    """
    return settings.iam_jwt_secret or settings.api_key or "shelter-development-only"


#: A session that may do exactly one thing: set the account's first password.
#:
#: Issued when a team invitation is redeemed, so the invited colleague lands signed in and
#: chooses their own password without a temporary one ever existing. `current_account` refuses
#: every other route while this scope is present, which is what makes it safe to hand out.
SCOPE_SET_PASSWORD = "set_password"


def issue_session(
    account_id: str,
    kind: str,
    *,
    minutes: int | None = None,
    scope: str | None = None,
) -> tuple[str, int]:
    """`(token, expires_in_seconds)` for a portal session.

    Claims are deliberately minimal — an id, a kind and the standard time/audience
    fields. **No email, no name.** A JWT is only signed, not encrypted, so anything
    in it is readable by anyone holding the token, including whatever logs the
    `Authorization` header along the way.

    `scope` narrows what the session may do. Absent means a full session; `SCOPE_SET_PASSWORD`
    means the holder can only set a password. Carried **in the token** rather than in a
    database row so the restriction travels with the credential and cannot be lost by a
    cache miss — a scope check that fails open would be no restriction at all.
    """
    ttl = minutes if minutes is not None else settings.iam_session_minutes
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=ttl)

    payload = {
        "sub": account_id,
        "kind": kind,
        "aud": PORTAL_AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        # Unique per token so a specific session can be denylisted later without
        # invalidating every session for that account.
        "jti": uuid.uuid4().hex,
    }
    if scope:
        payload["scope"] = scope
    token = jwt.encode(payload, _signing_key(), algorithm="HS256")
    return token, ttl * 60


def read_session(token: str) -> dict | None:
    """Verified claims, or None.

    `algorithms=["HS256"]` is an allow-list, not a hint: accepting the token's own
    `alg` header is the classic JWT vulnerability — `alg: none` would make every
    signature optional. The audience check is what stops a token minted for some
    other service being replayed here.
    """
    try:
        return jwt.decode(
            token,
            _signing_key(),
            algorithms=["HS256"],
            audience=PORTAL_AUDIENCE,
        )
    except jwt.PyJWTError:
        return None


# --------------------------------------------------------------------------- #
# Single-use tokens
#
# API-key material lives in `app/iam/keys.py` — the format, the checksum, the
# alphabet and the hashing. It used to be duplicated here, which meant two places
# defined the prefix and a change to one would silently diverge from the other.
# Only the verification-token helpers remain.
# --------------------------------------------------------------------------- #


def hash_token(token: str) -> str:
    """SHA-256 of a single-use token, hex.

    Used for email-verification links. Plain SHA-256 rather than Argon2 for the same
    reason as API keys: the input is 32 bytes of CSPRNG output with no guessable
    structure, so stretching defends against nothing.

    Stored hashed because the link grants a state change — a database leak must not
    let anyone verify arbitrary addresses.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def new_verification_token() -> str:
    """Single-use token for the email-confirmation link.

    URL-safe, 32 bytes. Stored hashed for the same reason API keys are: the
    confirmation link grants a state change, so a database leak must not let anyone
    verify arbitrary addresses.
    """
    return secrets.token_urlsafe(32)
