"""IP → approximate location, from a self-hosted MaxMind GeoLite2 database.

## Why self-hosted rather than an API

Every lookup is a subscriber's IP address. This platform's subscriber list is farmers in
named Nigerian districts, and posting their IPs to a third party — one request per audit
page render — would build a second, unmanaged record of where those people are, held by
someone with no obligation to them. A local database keeps the question and the answer
inside the deployment.

Three practical benefits fall out of that: no rate limit, no network latency on a page
render, and the same input always produces the same output, which is what makes the value
auditable rather than a snapshot of whatever a vendor said that day.

## It is optional, and absent by default

`GeoLite2-City.mmdb` is gitignored and not in the image. Without it, `lookup()` returns
`None` and every caller degrades to showing the raw IP — the same discipline as
`app/ml/weights/*.pt`, which fall back to documented physical thresholds rather than
failing. A portal that cannot name a city is mildly less useful; a portal that will not
render because a data file is missing is broken.

Fetch it with `make geoip` (needs a free MaxMind licence key in `MAXMIND_LICENCE_KEY`).

## Accuracy is stated, not implied

GeoLite2 city accuracy in Sub-Saharan Africa is materially worse than in Europe or North
America — often the ISP's regional hub rather than the subscriber's town, and mobile
carriers frequently route through a single national gateway. So:

  * `confidence` is returned alongside the label, and the UI must render the caveat.
  * The label degrades to country-only when the city is unknown, instead of guessing.
  * Nothing security-critical keys on this. It is a recognition aid — "does this look like
    me?" — not an authorisation input. Treating a geo mismatch as grounds to block a
    sign-in would lock out a farmer whose carrier re-routed traffic.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Location:
    """An approximate location, with an honest confidence.

    `label` is display-ready: `"Warrington, United Kingdom"`, or `"Nigeria"` when the city
    is unknown. Callers should not compose their own from the parts — the degradation rules
    live here so every surface degrades identically.
    """

    label: str
    city: str | None
    region: str | None
    country: str | None
    country_code: str | None
    #: "city" when a city was resolved, "country" when only the country was, "none" when
    #: the address is private or unresolvable. Drives how firmly the UI states it.
    confidence: str


#: Reserved for addresses we can say something true about without any database.
_PRIVATE = Location(
    label="Local network",
    city=None,
    region=None,
    country=None,
    country_code=None,
    confidence="none",
)


@lru_cache(maxsize=1)
def _reader():
    """The open database handle, or None.

    Cached because opening the file per lookup would mean an mmap and parse on every audit
    row. `lru_cache` also means the "not installed" answer is computed once rather than
    logging a warning on every request.

    The import is **inside the function** for the same reason `app/eo/exposure.py` imports
    rasterio lazily: `geoip2` must not be required to import this module, so the IAM layer
    stays importable and unit-testable without it.
    """
    path = Path(settings.geoip_database_path)
    if not path.exists():
        log.info(
            "GeoLite2 database not found at %s — locations will show the raw IP. "
            "Run `make geoip` to enable city lookup.",
            path,
        )
        return None

    try:
        import geoip2.database
    except ImportError:
        log.warning("geoip2 is not installed; IP locations are disabled")
        return None

    try:
        return geoip2.database.Reader(str(path))
    except Exception as exc:  # noqa: BLE001 — a corrupt db must not break the portal
        log.warning("could not open GeoLite2 database: %s", exc)
        return None


def available() -> bool:
    """Whether city lookup is possible. Surfaced on `/health`."""
    return _reader() is not None


def lookup(ip: str | None) -> Location | None:
    """Approximate location for an IP, or None when it cannot be determined.

    Returns `_PRIVATE` rather than None for RFC1918 and loopback addresses. That
    distinction matters in development, where every request arrives from Docker's gateway:
    "Local network" is the truth, whereas None would make the UI show a raw `192.168.65.1`
    and imply the lookup failed.
    """
    if not ip:
        return None

    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return None

    if parsed.is_private or parsed.is_loopback or parsed.is_link_local:
        return _PRIVATE

    reader = _reader()
    if reader is None:
        return None

    try:
        response = reader.city(ip)
    except Exception:  # noqa: BLE001 — AddressNotFoundError and friends
        # An unresolvable public IP is normal, not an error: satellite and some mobile
        # carrier ranges are simply absent from the database.
        return None

    city = response.city.name
    country = response.country.name
    code = response.country.iso_code
    region = response.subdivisions[0].name if response.subdivisions else None

    if city and country:
        return Location(
            label=f"{city}, {country}",
            city=city,
            region=region,
            country=country,
            country_code=code,
            confidence="city",
        )
    if country:
        # Country-only rather than inventing a city. Naming the wrong town is worse than
        # naming none: a subscriber who does not recognise the city may conclude their
        # account is compromised when nothing happened.
        return Location(
            label=country,
            city=None,
            region=region,
            country=country,
            country_code=code,
            confidence="country",
        )
    return None
