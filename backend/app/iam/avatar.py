"""Deterministic avatars — an emoji and a colour, derived from the account id.

## Why derived rather than stored

An account gets a recognisable avatar the moment it is created, with no upload, no image
host, no storage and no request. On a metered connection that last point is the whole
argument: a generated identicon or a hosted image is bytes the subscriber pays for, on
every page, for decoration.

## Why deterministic matters beyond convenience

The avatar is stable for the life of the account, so it becomes a recognition cue. On a
shared handset in a cooperative office — the case this product is actually deployed into —
someone glancing at the topbar sees the same 🌾 they always see. **A different emoji is a
visible signal that this is not their session**, which two sets of matching initials would
never give them. That is a small security property obtained for free.

`SHA-256` over the account id rather than Python's `hash()`: `hash()` is salted per process
(PYTHONHASHSEED), so the same account would get a different avatar on every worker and
after every restart — destroying exactly the stability the feature depends on.

## The emoji set is curated, not arbitrary

Every entry is chosen against three constraints, and a test asserts the first two:

  * **Single code point where possible.** Multi-codepoint sequences (ZWJ families, skin-tone
    modifiers) render as two boxes on older Android, which is a large share of the target
    devices.
  * **No human figures, no flags, no religious symbols.** An avatar assigned *to* someone
    must not imply their gender, nationality or faith. Flags are also politically loaded in
    a multi-country deployment.
  * **Recognisable at 26px.** Detailed emoji become mud at avatar size.

The set leans agricultural and meteorological because that is what the product is about,
and a farmer seeing a seedling reads it as belonging here.
"""

from __future__ import annotations

import hashlib

#: Curated avatar emoji. See the module docstring for the selection rules.
#:
#: 32 entries: enough that a collision between two people who know each other is unlikely,
#: few enough that every one could be checked for rendering on a low-end Android.
AVATAR_EMOJI: tuple[str, ...] = (
    "🌾", "🌱", "🌍", "🛰", "🌦", "🌤", "⛅", "🌊",
    "🍃", "🌳", "🌵", "🌻", "🌽", "🥜", "🍅", "🫘",
    "🐐", "🐄", "🐓", "🐝", "🦜", "🐘", "🦋", "🐟",
    "💧", "☀", "🌙", "⭐", "🏔", "🏞", "🧭", "🔆",
)

#: Background colours, paired with the emoji by the same hash.
#:
#: Muted rather than saturated: the avatar sits beside brand purple in the topbar, and eight
#: bright circles competing with the accent colour would make the header noisy. Each has
#: sufficient contrast against both the light and dark surface tokens.
AVATAR_COLORS: tuple[str, ...] = (
    "#6a0dad",  # brand
    "#0f8f63",  # viz rainfall aqua
    "#a16207",  # amber
    "#1d4ed8",  # indigo
    "#b91c5c",  # rose
    "#0e7490",  # teal
    "#4d7c0f",  # olive
    "#7c3aed",  # violet
)


def _digest(account_id: str) -> bytes:
    """Stable digest. See the docstring on why this is not `hash()`."""
    return hashlib.sha256(account_id.encode("utf-8")).digest()


def default_avatar(account_id: str) -> dict[str, str]:
    """The emoji and colour for an account that has not chosen one.

    Two independent bytes of the digest drive the two choices, so emoji and colour vary
    independently — deriving both from one byte would produce only 32 distinct combinations
    instead of 256.
    """
    if not account_id:
        # Never raise for a display concern. A missing id is a caller bug, but rendering a
        # neutral avatar is better than a 500 on a page that was otherwise fine.
        return {"emoji": AVATAR_EMOJI[0], "color": AVATAR_COLORS[0]}

    d = _digest(account_id)
    return {
        "emoji": AVATAR_EMOJI[d[0] % len(AVATAR_EMOJI)],
        "color": AVATAR_COLORS[d[1] % len(AVATAR_COLORS)],
    }


def resolve(account_id: str, chosen_emoji: str | None, chosen_color: str | None) -> dict[str, str]:
    """The avatar to display: the account's choice where made, otherwise the derived one.

    A chosen emoji is validated against `AVATAR_EMOJI` rather than trusted. The value
    reaches the browser and is rendered as text, so an unvalidated string would be a
    stored-content vector — and more mundanely, an arbitrary emoji could be one that does
    not render on the devices this serves. An invalid choice silently falls back to the
    derived avatar rather than erroring: a bad stored value should degrade, not break a
    page the subscriber needs.
    """
    fallback = default_avatar(account_id)

    emoji = chosen_emoji if chosen_emoji in AVATAR_EMOJI else fallback["emoji"]
    color = chosen_color if chosen_color in AVATAR_COLORS else fallback["color"]
    return {"emoji": emoji, "color": color}
