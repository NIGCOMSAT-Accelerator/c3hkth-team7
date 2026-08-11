"""Identity identifiers — 10-character alphanumeric, minted and owned by IAM.

## The format

    aggregator_id   A7K2M9P4QX      10 chars, [A-Z0-9], no prefix
    subscriber_id   B3N8R5T2VW      10 chars, [A-Z0-9], no prefix

Both are drawn from the same space and are indistinguishable by shape. That is
deliberate: they are the *public* identifiers a partner puts in their own database,
prints on a receipt and quotes in a support ticket, so they should be short, easy to
read aloud, and free of internal structure.

## Why the alphabet excludes I, O, 0 and 1

`ILLEGIBLE_CHARS` is removed, so the space is 32 characters rather than 36. A farmer
reading an id off a printed slip over a phone line, or an agent typing one from a
handwritten note, confuses `I`/`1` and `O`/`0` constantly. Every such confusion is a
support call about a "missing" record that exists under a neighbouring id.

The cost is entropy: 32^10 is ~2^50 rather than 36^10 (~2^51.7). Both are far too
large to enumerate, and neither is a security boundary — **an id is not a
credential**. Authorisation is the API key plus the `memberships` edge, so guessing an
id gains nothing: a request for another tenant's subscriber returns 404 regardless.

## Why uppercase only

Case-insensitive comparison is what people actually expect from a short reference
code, and a mixed-case id invites `a7k2m9p4qx` being typed and silently not matching.
Normalising on read (`normalise`) makes the lookup forgiving without making the
canonical form ambiguous.

## Uniqueness

`2^50` makes an accidental collision vanishingly unlikely, but "unlikely" is not
"impossible" and a collision on a primary identifier is a data-corruption event, not
a retry-later inconvenience. So:

1. The unique index in Atlas is the actual guarantee (`accounts.id`,
   `subscribers.id`).
2. `mint_unique` retries against a caller-supplied existence check, so the collision
   is resolved before the insert rather than surfacing as a duplicate-key error the
   caller has to interpret.

Belt and braces, in that order: the database is authoritative and the retry is what
makes the authoritative answer never reach a user.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Awaitable, Callable

from app.logging_config import get_logger

log = get_logger(__name__)

#: Length of an identity identifier. Exactly 10 — fixed, so a malformed reference is
#: rejected by shape before any lookup.
ID_LENGTH = 10

#: Characters excluded because they are confused when read aloud or handwritten.
#:
#: `I`/`1` and `O`/`0` are the classic pairs. Removing all four costs ~1.7 bits and
#: removes an entire class of support call about a record that exists under a
#: neighbouring id.
ILLEGIBLE_CHARS = "IO01"

#: The 32-character alphabet. Uppercase letters and digits, minus the illegible four.
ID_ALPHABET = "".join(
    c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" if c not in ILLEGIBLE_CHARS
)

#: Compiled once. Validation runs on every scoped lookup, so it is on a hot path.
_ID_PATTERN = re.compile(f"^[{ID_ALPHABET}]{{{ID_LENGTH}}}$")

#: Attempts before giving up on finding a free id.
#:
#: At 2^50 possibilities, five failures in a row is not "the space is full" — it is a
#: broken existence check or an unreachable database. Retrying forever would hang the
#: request; failing loudly names the real problem.
MAX_MINT_ATTEMPTS = 5


def mint() -> str:
    """One candidate identifier. Not checked for uniqueness — see `mint_unique`.

    `secrets.choice` rather than `random.choice`. An id is not a credential, so this is
    not strictly a security requirement — but a predictable sequence would make ids
    enumerable, and enumerable ids turn any future authorisation slip into a bulk
    extraction rather than a single-record one. The cost of the stronger primitive is
    nothing.
    """
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LENGTH))


async def mint_unique(
    exists: Callable[[str], Awaitable[bool]],
    *,
    label: str = "identifier",
) -> str:
    """A candidate that the caller's `exists` check says is free.

    The unique index is the real guarantee; this exists so a collision is resolved
    *before* the insert instead of arriving as a duplicate-key error that the caller
    would have to distinguish from a genuinely duplicate email.

    Raises after `MAX_MINT_ATTEMPTS`. Five consecutive collisions at 2^50 means the
    existence check is broken or the database is unreachable — a loud failure names
    that, whereas retrying forever would hang the request and look like a timeout.
    """
    for attempt in range(1, MAX_MINT_ATTEMPTS + 1):
        candidate = mint()
        try:
            taken = await exists(candidate)
        except Exception as exc:
            # A failing existence check must not silently yield a possibly-colliding
            # id: the unique index would then reject the insert with a confusing
            # error. Fail here, where the cause is still visible.
            raise RuntimeError(
                f"could not verify {label} uniqueness: {type(exc).__name__}: {exc}"
            ) from exc

        if not taken:
            if attempt > 1:
                # Worth a line: at this probability, even one collision suggests the
                # entropy source or the check is not behaving as assumed.
                log.warning(
                    "identifier collision resolved by retry",
                    extra={"label": label, "attempts": attempt},
                )
            return candidate

    raise RuntimeError(
        f"could not mint a unique {label} in {MAX_MINT_ATTEMPTS} attempts. At "
        f"{len(ID_ALPHABET)}^{ID_LENGTH} possibilities this is not exhaustion — check "
        f"that the uniqueness query is correct and the database is reachable."
    )


def is_valid(value: str | None) -> bool:
    """Whether a string is a well-formed identifier.

    Shape validation before a database round trip. These ids appear in URL paths
    (`/iam/customers/{account_id}`), so rejecting junk here keeps a malformed path from
    becoming a query — and makes a typo a 422 with a clear message rather than a 404
    that reads as "your record is gone".
    """
    return bool(value) and bool(_ID_PATTERN.match(value))


def normalise(value: str | None) -> str | None:
    """Canonical form: uppercase, whitespace and separators stripped.

    Forgiving on input, exact on storage. Someone quoting an id from a printed slip
    may lower-case it or add a hyphen for legibility (`A7K2-M9P4-QX`), and none of
    those should be a lookup failure. Returns None when the result is not a valid id,
    so a caller can distinguish "not an id" from "an id that does not exist".
    """
    if not value:
        return None
    cleaned = re.sub(r"[\s\-_]", "", value).upper()
    return cleaned if is_valid(cleaned) else None


def looks_like_legacy(value: str | None) -> bool:
    """Whether this is a pre-migration prefixed id (`acc_…`, `sub_…`).

    Kept so the transition is *legible*: existing rows keep working, and a log line or
    an error message can say "this is an old-format id" rather than "invalid". Without
    it, every pre-existing subscriber would look malformed to `is_valid` and the
    distinction between "old" and "wrong" would be lost.
    """
    if not value:
        return False
    return bool(re.match(r"^(acc|sub|aoi|key|mem|whs|whd)_[0-9a-f]{8,}$", value))
