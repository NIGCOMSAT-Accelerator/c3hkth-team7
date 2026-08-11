"""Magic links, TOTP and password reset.

Three credentials that all answer "prove you control this account", each suited to a
different situation — and each with a distinct failure mode worth naming.

## Magic link — the primary path for farmers

A single-use token emailed as a URL. The subscriber clicks and is signed in; there is
no password to remember, mistype, or have written on a slip someone else can read.

**Why this matters more here than for a typical SaaS.** The target user may share a
handset, have limited literacy in the interface language, and be on a metered
connection. A forgotten password means a support call; a link in an inbox they already
check does not.

**The security trade** is that email becomes the account boundary. Mitigated by: single
use, a short TTL (`MAGIC_LINK_TTL_MINUTES`), hashed at rest, and invalidation of any
outstanding link when a new one is requested — so a link intercepted from an old email
is already dead.

## TOTP — opt-in, and aimed at aggregators

An aggregator holds API keys that can read hundreds of farmers' data. A password alone
protects that, and a password is phishable. TOTP is offered to every account and
*encouraged* for commercial ones, but never forced: mandatory 2FA on a farmer with one
handset and no authenticator app locks them out of their own flood warnings.

**Why TOTP rather than SMS.** SMS one-time codes are interceptable by SIM swap, which
is a live fraud pattern in Nigeria, and they cost money per message. TOTP is offline,
free, and works on the same phone.

## Password reset — the fallback, not the default

Kept because someone with a password will eventually forget it. Deliberately the same
machinery as a magic link (single-use, hashed, short-lived), because the security
properties should not differ based on which button the user pressed.

## The property all three share

**A request never reveals whether the account exists.** `POST /auth/magic-link` for an
unknown address returns exactly what it returns for a known one. Otherwise the endpoint
is a free account-enumeration oracle over a list of farmers in named districts — which
is a privacy leak with physical consequences in a region where that list has value to
people other than us.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from app.config import settings
from app.iam import security
from app.logging_config import get_logger

log = get_logger(__name__)


class TokenPurpose(str, Enum):
    """What a single-use token authorises.

    Namespaced so a token minted for one purpose cannot be redeemed for another. A
    single `tokens` collection with no purpose field would let a password-reset link —
    which someone may forward to support while asking for help — be replayed as a
    sign-in.
    """

    MAGIC_LINK = "magic_link"
    PASSWORD_RESET = "password_reset"
    EMAIL_VERIFY = "email_verify"
    #: A team invitation. Redeeming it creates the account with **no password hash** and a
    #: session that can do nothing but set one.
    #:
    #: A separate purpose rather than reusing `MAGIC_LINK` because the two differ in every
    #: consequence: this one may create an account, lasts 14 days rather than 15 minutes, and
    #: yields a session scoped to a single action. Sharing a purpose would make a 14-day
    #: invitation redeemable as a full sign-in.
    TEAM_INVITE = "team_invite"
    #: A short code confirming a password change requested from INSIDE a session.
    #:
    #: Namespaced separately from `PASSWORD_RESET` because the two have different threat models and
    #: must not be interchangeable. A reset token is redeemable while signed OUT — it is the only
    #: credential in play, so it is 256 bits and arrives as a link. This code is a *second* factor
    #: behind a live session that already proved the password, which is what makes six characters
    #: defensible. Sharing a purpose would let a short code be replayed on the signed-out reset
    #: endpoint, turning a second factor into a sole credential.
    PASSWORD_CHANGE_CODE = "password_change_code"


#: Magic-link lifetime. 15 minutes.
#:
#: Short because the link *is* the credential: anything longer sits in an inbox as a
#: standing key to the account. Long enough to survive mail-server queuing and a
#: subscriber finishing what they were doing before opening it.
MAGIC_LINK_TTL_MINUTES = 15

#: Password-reset lifetime. 60 minutes.
#:
#: Longer than a magic link on purpose — resetting a password involves choosing and
#: confirming a new one, sometimes on a second device, and a 15-minute window there
#: produces a second reset request rather than a completed one.
PASSWORD_RESET_TTL_MINUTES = 60

#: Team-invitation lifetime. 14 days.
#:
#: Far longer than any other token here, and defensible only because of what it is: a
#: 256-bit single-use value that exists in one mailbox and is destroyed on first use. It
#: needs the length because accepting requires a colleague to act, which may wait for a
#: working day or a holiday.
#:
#: **A one-time PASSWORD at this lifetime would not be defensible**, which is why the
#: invitation carries no password. A password is valid at `POST /iam/login` — public,
#: reachable by anyone, and therefore an online guessing target for the whole window — and
#: has to be short enough to type, so perhaps 50-60 bits against a token's 256. This token
#: is only usable by someone holding the URL.
TEAM_INVITE_TTL_MINUTES = 14 * 24 * 60

#: In-session password-change code: length, lifetime and attempt ceiling.
#:
#: ## Why a short code is acceptable here when it would not be for a reset
#:
#: The note on `TEAM_INVITE_TTL_MINUTES` explains why a one-time *password* at a long lifetime is
#: indefensible: it is valid at a public endpoint, so it is an online guessing target for its whole
#: window. This code differs in all three respects, and every one of them is load-bearing:
#:
#:   * **It is not a credential.** It confirms a change requested by a session that already
#:     authenticated. Holding the code alone gets an attacker nothing — they also need the session
#:     cookie, which JavaScript cannot read.
#:   * **It is not reachable while signed out.** `require_password_change_code` runs behind
#:     `current_account`, so there is no public surface to spray.
#:   * **It is attempt-bounded.** `PASSWORD_CODE_MAX_ATTEMPTS` burns the code after five wrong
#:     guesses, which caps the search at 5 tries out of 32^6 rather than at "as many as fit in ten
#:     minutes". The ceiling, not the entropy, is what actually stops a guess.
#:
#: Six characters from the unambiguous alphabet is ~30 bits — chosen for a person retyping it from
#: their phone, which is the real constraint. `ID_ALPHABET` excludes O/0 and I/1, so a code cannot
#: fail because someone read a zero as a letter.
PASSWORD_CODE_LENGTH = 6

#: Ten minutes. Long enough to switch to a mail app and back, short enough that an abandoned
#: request does not leave a usable code sitting in an inbox for an hour.
PASSWORD_CODE_TTL_MINUTES = 10

#: Wrong guesses before the code is destroyed and a new one must be requested.
#:
#: This is the real control on a 30-bit secret. Five is generous for a typo and negligible for an
#: attacker. Burning the code rather than locking the account is deliberate: a lockout would let
#: someone who knows an email address deny the owner their own password change.
PASSWORD_CODE_MAX_ATTEMPTS = 5

#: Requests per address per window before throttling.
#:
#: Keyed on the address, not the IP: this endpoint sends mail on demand, so without a
#: limit it is a free email cannon pointed at anyone. Three is enough for a genuine
#: "did that send?" retry.
MAX_LINK_REQUESTS = 3
LINK_REQUEST_WINDOW_MINUTES = 15

#: TOTP settings. 30-second step and 6 digits are the RFC 6238 defaults that every
#: authenticator app assumes; changing either silently breaks Google Authenticator,
#: Authy and 1Password at once.
TOTP_DIGITS = 6
TOTP_INTERVAL_SECONDS = 30

#: Windows of drift accepted either side of the current step.
#:
#: 1 means ±30s. Phone clocks drift, and a user typing a 6-digit code may straddle a
#: step boundary. Zero tolerance produces "the code is wrong" for a correct code, which
#: is the single most common 2FA support complaint; larger windows widen the replay
#: surface for no real usability gain.
TOTP_DRIFT_WINDOWS = 1

#: Single-use recovery codes issued when TOTP is enabled.
#:
#: Without these, a lost or wiped phone is an unrecoverable account — and for an
#: aggregator that means losing access to every customer they manage. Ten is enough to
#: survive several incidents before regenerating.
RECOVERY_CODE_COUNT = 10


@dataclass(frozen=True)
class SingleUseToken:
    """A minted token. The plaintext exists only here and in the email."""

    plaintext: str
    token_hash: str
    purpose: TokenPurpose
    expires_at: datetime


def mint_token(purpose: TokenPurpose) -> SingleUseToken:
    """A URL-safe single-use token, hashed for storage.

    32 bytes of CSPRNG output. Not stretched — there is no dictionary to slow down for
    an unguessable random string, and the token is checked on a user-facing request
    where Argon2's ~50 ms would be felt.

    Only the hash is stored, so a database leak does not yield working sign-in links.
    """
    # A dict rather than a conditional chain: a new purpose that forgets to add a TTL gets
    # the shortest one, not the longest. Failing to the 15-minute default is the safe
    # direction — a too-short token produces a support request, a too-long one is a standing
    # credential nobody chose to issue.
    ttl = {
        TokenPurpose.PASSWORD_RESET: PASSWORD_RESET_TTL_MINUTES,
        TokenPurpose.TEAM_INVITE: TEAM_INVITE_TTL_MINUTES,
    }.get(purpose, MAGIC_LINK_TTL_MINUTES)
    plaintext = secrets.token_urlsafe(32)
    return SingleUseToken(
        plaintext=plaintext,
        token_hash=security.hash_token(plaintext),
        purpose=purpose,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=ttl),
    )


def token_hash(plaintext: str) -> str:
    """Hash a presented token for lookup. The only way a token is ever matched."""
    return security.hash_token(plaintext)


def is_expired(expires_at: datetime | None) -> bool:
    """Whether a token has passed its expiry.

    Mongo returns naive UTC datetimes, so tzinfo is normalised first — without that
    this raises `TypeError` on every token and the guard fails for the wrong reason.
    """
    if expires_at is None:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


def magic_link_url(token: str, *, next_path: str | None = None) -> str:
    """The URL that goes in the email.

    Points at the *portal*, not the API: the user needs a page that signs them in and
    then shows them something. A raw JSON response from a POST endpoint is a dead end in
    a mail client.

    `next_path` is validated as a relative path by the caller — an open redirect here
    would let an attacker send a legitimate-looking SHELTER link that lands on their own
    site with the token in the referrer.
    """
    base = settings.public_site_url.rstrip("/")
    url = f"{base}/auth/verify?token={token}"
    if next_path:
        from urllib.parse import quote

        url += f"&next={quote(next_path, safe='/')}"
    return url


def team_invite_url(token: str) -> str:
    """The link in a team invitation.

    Points at `/auth/invite`, a page rather than an endpoint, because redeeming is a POST: a
    GET that consumed the token would let a mail scanner or link preview burn a single-use
    invitation before the person ever clicked it.
    """
    base = settings.public_site_url.rstrip("/")
    return f"{base}/auth/invite?token={token}"


def password_reset_url(token: str) -> str:
    base = settings.public_site_url.rstrip("/")
    return f"{base}/auth/reset?token={token}"


def safe_next_path(candidate: str | None) -> str:
    """Sanitise a post-login redirect. Returns a safe relative path.

    **This is an open-redirect guard, not tidying.** Without it, a crafted
    `?next=https://evil.example` produces a genuine SHELTER magic link that signs the
    user in and then hands them to an attacker's page — with the session freshly
    established and the token in the referrer. Protocol-relative `//evil.example` is the
    variant people forget, so it is rejected explicitly.
    """
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return "/dashboard"
    if "\\" in candidate or "\n" in candidate or "\r" in candidate:
        return "/dashboard"
    return candidate


# --------------------------------------------------------------------------- #
# TOTP
# --------------------------------------------------------------------------- #


def new_totp_secret() -> str:
    """A base32 TOTP secret, as authenticator apps expect."""
    import pyotp

    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, email: str) -> str:
    """The `otpauth://` URI a QR code encodes.

    The issuer is set so the entry reads "SHELTER" in the user's app rather than an
    anonymous 6-digit code they cannot attribute — someone with several accounts needs
    to know which code belongs where.
    """
    import pyotp

    return pyotp.TOTP(
        secret, digits=TOTP_DIGITS, interval=TOTP_INTERVAL_SECONDS
    ).provisioning_uri(name=email, issuer_name=settings.app_name)


def verify_totp(secret: str, code: str) -> bool:
    """Check a 6-digit code, tolerating one step of clock drift.

    Never raises: a malformed code is a wrong code, not a server error. `pyotp` compares
    in constant time internally, so a timing attack on the code is not a concern.
    """
    if not secret or not code:
        return False
    try:
        import pyotp

        cleaned = code.strip().replace(" ", "")
        if not cleaned.isdigit() or len(cleaned) != TOTP_DIGITS:
            return False
        return pyotp.TOTP(
            secret, digits=TOTP_DIGITS, interval=TOTP_INTERVAL_SECONDS
        ).verify(cleaned, valid_window=TOTP_DRIFT_WINDOWS)
    except Exception:
        log.exception("TOTP verification error")
        return False


def new_recovery_codes() -> tuple[list[str], list[str]]:
    """`(plaintext_codes, hashed_codes)` — shown once, stored hashed.

    Formatted `XXXX-XXXX` because these get written down. The hyphen makes an
    8-character string legible on paper, and the alphabet excludes the characters that
    are misread when handwritten — the same reasoning as the account-id alphabet.
    """
    from app.iam.identifiers import ID_ALPHABET

    plain: list[str] = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = "".join(secrets.choice(ID_ALPHABET) for _ in range(8))
        plain.append(f"{raw[:4]}-{raw[4:]}")

    return plain, [security.hash_token(code) for code in plain]


def normalise_recovery_code(code: str) -> str:
    """Canonical form, so a user retyping a written code is not defeated by formatting.

    Someone reading `A7K2-M9P4` off paper may type it lower-case, without the hyphen, or
    with a space. All three should work; the stored form stays exact.
    """
    return code.strip().upper().replace(" ", "").replace("_", "-")


def mint_password_change_code() -> SingleUseToken:
    """A 6-character code confirming a password change from inside a session.

    ## Why this is not `mint_token`

    `mint_token` returns 32 URL-safe bytes, which is right for a credential that travels as a link
    and wrong for one a person retypes from their phone. This is the opposite trade: short enough to
    read across two apps, and safe only because of where it is checked — behind a live session, with
    a hard attempt ceiling. See `PASSWORD_CODE_LENGTH` for the full argument.

    `ID_ALPHABET` excludes the confusable characters (no O/0, no I/1), so the code cannot fail
    because a zero was read as a letter — which for a 6-character code is a real failure rate, not
    a nicety.

    Only the hash is stored, like every other token here. A database leak yields no usable codes.
    """
    from app.iam.identifiers import ID_ALPHABET

    plaintext = "".join(secrets.choice(ID_ALPHABET) for _ in range(PASSWORD_CODE_LENGTH))
    return SingleUseToken(
        plaintext=plaintext,
        token_hash=security.hash_token(plaintext),
        purpose=TokenPurpose.PASSWORD_CHANGE_CODE,
        expires_at=datetime.now(timezone.utc)
        + timedelta(minutes=PASSWORD_CODE_TTL_MINUTES),
    )


def normalise_password_change_code(code: str) -> str:
    """Canonical form of a typed code.

    Upper-cased and stripped of spaces, because a phone keyboard offers lower case by default and a
    reader may group the characters. The stored hash is of the canonical form, so normalising on
    both sides is what makes those inputs equivalent rather than wrong.
    """
    return code.strip().upper().replace(" ", "").replace("-", "")
