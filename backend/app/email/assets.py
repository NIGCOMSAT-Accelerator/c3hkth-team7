"""The FreePass wordmark as base64 PNG, for email.

## Why a PNG data URI and not the inline SVG

The footer used inline `<svg>`, chosen so the logo would render before a recipient allows
remote images. It worked in Apple Mail and rendered **nothing at all** in Outlook — reported
from New Outlook for Windows.

The cause is not the renderer. Microsoft's HTML sanitiser **strips `<svg>` elements** from
incoming mail before display, in Outlook.com, New Outlook for Windows and Outlook for Mac. The
markup never reaches the layout engine, so there is no partial or broken rendering to notice —
the logo is simply absent.

A `data:` URI keeps the original property that motivated inline SVG: the bytes travel inside
the message, so nothing is fetched and image-blocking cannot hide it. And unlike SVG, a PNG
`<img>` survives the sanitiser.

## Why two colours rather than one

Measured: no single grey is legible on both a white card and a dark client background. The
wordmark needs `#2b3444` on light (11.41:1) and `#e8e6ef` on dark (14.09:1); a mid-slate
compromise measured 4.41:1 and 3.60:1 — under the readable threshold in both directions, which
is the "logo isn't clear" report that started this.

So both are embedded and a media query swaps them. The **dark-ink asset is the default**, so a
client that strips `<style>` still shows a correctly-coloured logo on the white card.

## Why the bytes live in Python rather than a file

Baked into the module, the asset cannot be lost to a `.dockerignore` rule, a missing `COPY`, or
a packaging change — the three ways a file-based asset silently becomes a broken image in
production. It is ~4.5 KB of base64 each, against a ~12 KB email; both together stay far inside
Gmail's 102 KB clipping threshold.

Regenerated from `frontend/public/freepass-logo.png` (230x46, white on transparent) by
recolouring the opaque pixels and preserving alpha. See `tests/test_email_branding.py` for the
contrast assertions that constrain the two inks.
"""

from __future__ import annotations

#: 230x46 wordmark, ink #2b3444 — for a light background. The DEFAULT.
FREEPASS_PNG_DARK_INK = (
    "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"
)

#: 230x46 wordmark, ink #e8e6ef — swapped in under `prefers-color-scheme: dark`.
FREEPASS_PNG_LIGHT_INK = (
    "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"
)

#: Rendered size in the footer. Half the asset's pixel width, so it is 2x for retina.
FREEPASS_WIDTH = 115
FREEPASS_HEIGHT = 23

#: The NIGCOMSAT emblem, 108x100, embedded verbatim.
#:
#: **Not recoloured, unlike the FreePass wordmark.** That one is a single-colour wordmark, so
#: swapping its ink per colour scheme is lossless. This is a multicolour brand mark — measured,
#: 116 distinct colours over a blue `#0060b4` and grey palette — and flattening it to one ink
#: would destroy the mark rather than adapt it. Blue on transparent reads acceptably on both a
#: white card and a dark client background, so one asset serves both.
#:
#: It was previously set as a TEXT wordmark, because a remote `<img>` would be blocked on first
#: open. Embedding it as base64 removes that objection: the bytes travel inside the message, so
#: there is nothing to block and the real emblem shows beside the real FreePass logo.
NIGCOMSAT_PNG = (
    "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"
)

#: Rendered at half its pixel height, so it is 2x for retina and optically matches the
#: 23px-tall FreePass wordmark beside it. The emblem is near-square (108x100) while FreePass is
#: 5:1, so equal HEIGHTS are what make the pair look balanced — not equal widths.
NIGCOMSAT_WIDTH = 25
NIGCOMSAT_HEIGHT = 23
