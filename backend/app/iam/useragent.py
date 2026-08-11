"""User-agent parsing — device, operating system, browser.

## Why this is hand-rolled rather than a dependency

`user-agents` / `ua-parser` carry a regex database of thousands of patterns, refreshed
upstream, to identify every browser and handset ever shipped. That is the right tool for
analytics. It is the wrong tool here for three reasons:

  * **The question is narrow.** A subscriber reading their own audit log needs to recognise
    *"was this me?"* — "Chrome 141 on Android" answers that. Distinguishing a Tecno Spark 10
    from a Spark 10 Pro does not.
  * **A stale pattern database silently degrades.** An unrecognised UA in a full parser
    returns "Other", which reads as a failure. Here an unrecognised UA falls back to a
    truthful summary of what was actually sent.
  * **It runs on every audit read.** A regex sweep over a large pattern set, per entry, per
    page, on a deployment sized for one VPS.

So this recognises the browsers and platforms that matter for Sub-Saharan Africa — Chrome
and Chrome Mobile dominate, then Safari, Opera Mini, Samsung Internet, Firefox — and is
honest when it does not know.

## Order matters, and it is the whole correctness story

Almost every browser lies in its user-agent string for compatibility. Chrome claims to be
Safari; Edge claims to be Chrome; Opera claims to be both. So the checks run
**most-specific-first**, and the ordering in `_BROWSERS` is load-bearing rather than
stylistic:

```
Edg/         must precede Chrome/   — Edge sends "Chrome/141 ... Edg/141"
OPR/ Opera   must precede Chrome/   — Opera sends "Chrome/141 ... OPR/121"
SamsungBrowser must precede Chrome/ — same trick
Chrome/      must precede Safari/   — Chrome sends "Safari/537.36" always
```

Reversing any pair reports every Edge user as Chrome, and every Chrome user as Safari. A
test asserts the order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Browser families, **most specific first**. See the module docstring — this order is a
#: correctness requirement, not a preference.
#:
#: Each entry is (label, marker, version_pattern). `marker` is a cheap substring test that
#: gates the more expensive regex.
_BROWSERS: tuple[tuple[str, str, str], ...] = (
    # Chromium derivatives that also send "Chrome/" — must come first.
    ("Microsoft Edge", "Edg/", r"Edg/(\d+)"),
    ("Opera", "OPR/", r"OPR/(\d+)"),
    ("Opera Mini", "Opera Mini", r"Opera Mini/(\d+)"),
    ("Samsung Internet", "SamsungBrowser", r"SamsungBrowser/(\d+)"),
    ("UC Browser", "UCBrowser", r"UCBrowser/(\d+)"),
    ("Yandex", "YaBrowser", r"YaBrowser/(\d+)"),
    ("Brave", "Brave/", r"Brave/(\d+)"),
    # Firefox does not impersonate anything.
    ("Firefox", "Firefox/", r"Firefox/(\d+)"),
    # Chrome — after every derivative above.
    ("Chrome", "Chrome/", r"Chrome/(\d+)"),
    # Safari sends "Version/17.0 ... Safari/605" and must come after Chrome, which also
    # sends "Safari/".
    ("Safari", "Safari/", r"Version/(\d+)"),
)

#: Operating systems, most specific first.
#:
#: `Android` precedes `Linux` because every Android UA also contains "Linux"; iPadOS
#: precedes iOS because an iPad sends "iPad" alongside "like Mac OS X".
_PLATFORMS: tuple[tuple[str, str, str | None], ...] = (
    ("Android", "Android", r"Android (\d+)"),
    ("iPadOS", "iPad", r"OS (\d+)"),
    ("iOS", "iPhone", r"OS (\d+)"),
    ("Windows", "Windows NT", r"Windows NT ([\d.]+)"),
    ("macOS", "Mac OS X", r"Mac OS X (\d+[._]\d+)"),
    ("Linux", "Linux", None),
)

#: Windows sends a kernel version, not a marketing name. Nobody recognises "10.0".
_WINDOWS_NAMES = {"10.0": "10 or 11", "6.3": "8.1", "6.2": "8", "6.1": "7"}

#: Device class. Deliberately coarse: three buckets a person recognises, not a model name.
#:
#: `Mobile` is checked before `Tablet` markers in the tablet branch below because Android
#: tablets send "Android" without "Mobile", which is the only reliable signal — the
#: distinction is genuinely ambiguous in UA strings and this errs toward "Mobile", the
#: overwhelmingly likelier case for this product.
_BOT_MARKERS = ("bot", "crawler", "spider", "curl/", "wget", "python-requests", "httpx")


@dataclass(frozen=True)
class ParsedAgent:
    """What a person needs to recognise their own session.

    `summary` is the display string, e.g. `"Mobile · Android 14 · Chrome 141"`. The parts
    are kept separately so a UI can lay them out differently — the portal shows device and
    OS on one line and the browser beneath.
    """

    device: str
    os: str
    browser: str
    summary: str
    #: True for a script, not a person. Surfaced so an audit entry from a health checker or
    #: an integration is not mistaken for a human sign-in — which is exactly the kind of
    #: false alarm that trains someone to ignore their own security log.
    is_bot: bool


def _match(ua: str, pattern: str) -> str | None:
    m = re.search(pattern, ua)
    return m.group(1) if m else None


def parse(user_agent: str | None) -> ParsedAgent:
    """Best-effort parse. Never raises, never returns an empty field.

    An absent user-agent is reported as "Unknown device" rather than blank: an audit row
    with an empty column reads as a rendering fault, whereas "Unknown" is a fact about what
    the client sent.
    """
    if not user_agent or not user_agent.strip():
        return ParsedAgent(
            device="Unknown device",
            os="Unknown OS",
            browser="Unknown browser",
            summary="Unknown device",
            is_bot=False,
        )

    ua = user_agent.strip()
    lowered = ua.lower()

    # Bots first. A crawler's UA often contains "Chrome/" and would otherwise be reported
    # as a browser session, which is misleading in a security log.
    if any(marker in lowered for marker in _BOT_MARKERS):
        # The token before the first space is the tool name in every common case
        # (`curl/8.7.1`, `python-requests/2.32`), which is more useful than "Bot".
        tool = ua.split()[0][:40]
        return ParsedAgent(
            device="Automated client",
            os="—",
            browser=tool,
            summary=f"Automated client · {tool}",
            is_bot=True,
        )

    # ---- operating system ------------------------------------------------- #
    os_name, os_version = "Unknown OS", None
    for label, marker, version_pattern in _PLATFORMS:
        if marker not in ua:
            continue
        os_name = label
        if version_pattern:
            raw = _match(ua, version_pattern)
            if raw:
                if label == "Windows":
                    os_version = _WINDOWS_NAMES.get(raw, raw)
                else:
                    # macOS and iOS use underscores in the UA: "10_15_7".
                    os_version = raw.replace("_", ".")
        break

    os_display = f"{os_name} {os_version}".strip() if os_version else os_name

    # ---- browser ---------------------------------------------------------- #
    browser_name, browser_version = "Unknown browser", None
    for label, marker, version_pattern in _BROWSERS:
        if marker not in ua:
            continue
        browser_name = label
        browser_version = _match(ua, version_pattern)
        break

    browser_display = (
        f"{browser_name} {browser_version}".strip() if browser_version else browser_name
    )

    # ---- device class ----------------------------------------------------- #
    #
    # Opera Mini is checked first and explicitly. Its UA is
    # `Opera/9.80 (Android; Opera Mini/85...)` — "Android" without "Mobile" — which the
    # tablet heuristic below would classify as a Tablet. It is overwhelmingly a phone
    # browser, and it has real share in Nigeria precisely because it compresses pages for
    # cheap handsets on metered data, so getting this wrong mislabels a large slice of the
    # target audience.
    if browser_name == "Opera Mini":
        device = "Mobile"
    elif "iPad" in ua or ("Android" in ua and "Mobile" not in ua):
        # Android tablets omit "Mobile"; that is the only reliable signal in the string.
        device = "Tablet"
    elif "Mobile" in ua or "iPhone" in ua or "Android" in ua:
        device = "Mobile"
    else:
        device = "Desktop"

    # Drop unknown parts from the summary rather than printing "Unknown OS" beside real
    # values — a half-known agent should read as partial, not as broken.
    parts = [device]
    if os_name != "Unknown OS":
        parts.append(os_display)
    if browser_name != "Unknown browser":
        parts.append(browser_display)

    return ParsedAgent(
        device=device,
        os=os_display,
        browser=browser_display,
        summary=" · ".join(parts),
        is_bot=False,
    )
