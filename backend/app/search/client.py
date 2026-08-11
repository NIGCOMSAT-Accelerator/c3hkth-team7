"""Search for Fahis — SearXNG or Tavily, selected by `SEARCH_PROVIDER`.

## Two providers, and why they need separate adapters

`SEARCH_PROVIDER` picks one. They are **not** interchangeable behind a shared URL, which is worth
saying because the naming invites that assumption:

| | SearXNG | Tavily |
|---|---|---|
| Request | `GET /search?q=…&format=json` | `POST /search`, JSON body |
| Auth | none, or a proxy bearer token | `api_key` in the body |
| Domain filter | `site:` inside the query | `include_domains` array |
| Time window | `time_range` day/week/month | `days` integer |

So `TAVILY_API_BASE` pointed at a SearXNG instance returns 404 — SearXNG has no `POST /search`.
Each provider gets its own request builder, and both normalise into the same `SearchResult`, so
`agents/fahis.py` and the tier logic never learn which one answered.

## Neither ships by default

`SEARCH_PROVIDER=none` is the default and Fahis records NOT_ATTEMPTED — an outage, never a
non-finding. Nothing self-hosted is in `docker-compose.yml`: running a search instance is an
operator's decision, not a cost imposed on every deployment.

Self-hosting SearXNG remains the sovereignty-preserving option, for the same reason
`signal_channel.py` and MinIO are self-hosted — every query names a Nigerian district and a hazard,
and sending that to a US SaaS endpoint cuts against the property the NIGCOMSAT layer exists to
provide. Tavily is offered because that argument is ours to make, not one to impose on a tester who
just wants verification working in ten minutes.

**Four things this module does that a bare SearXNG call would not:**

1. **Source tiering.** Results are labelled `official` / `media` / `other` from a
   configured domain list. Fahis weights an NEMA bulletin above a blog, and the
   tier is what lets it.
2. **Own-domain exclusion.** `shelter.zerorate.io` is filtered out of every
   result set. Without this, verification could "confirm" a SHELTER alert by
   finding SHELTER's own published alert — circular self-confirmation, and the
   most likely way this feature produces a confident wrong answer.
3. **Recency filtering.** Verification only cares about the days around an event.
4. **Never raises for the caller's convenience.** An unavailable search engine
   yields an empty list, which Fahis must read as UNVERIFIED — never as REFUTED.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(20.0, connect=8.0)


class SearchUnavailable(RuntimeError):
    """The search backend could not be reached.

    Raised only by `search_or_raise`. The normal `search()` entry point returns
    an empty list instead, because every caller here treats "no results" and
    "could not search" as the same non-finding — and must not treat either as
    evidence of absence.
    """


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str
    snippet: str
    #: "official" | "media" | "other" — see `_tier`.
    tier: str = "other"
    published: str | None = None
    engine: str | None = None

    @property
    def domain(self) -> str:
        return urlparse(self.url).netloc.lower().removeprefix("www.")


@dataclass
class SearchResponse:
    query: str
    results: list[SearchResult] = field(default_factory=list)
    #: False when the backend was unreachable, as distinct from "searched and
    #: found nothing". Fahis needs to tell those apart.
    searched: bool = True

    @property
    def official(self) -> list[SearchResult]:
        return [r for r in self.results if r.tier == "official"]

    @property
    def media(self) -> list[SearchResult]:
        return [r for r in self.results if r.tier == "media"]


def provider() -> str:
    """The resolved backend: "searxng" | "tavily" | "none".

    Resolves to `none` when the selected provider is not actually configured, rather than failing
    later on every query. A deployment that set `SEARCH_PROVIDER=tavily` and forgot the key gets
    NOT_ATTEMPTED verdicts and a warning, which is diagnosable — not a stack trace per assessment.
    """
    configured = (settings.search_provider or "none").strip().lower()

    if configured == "searxng":
        return "searxng" if settings.searxng_url else "none"
    if configured == "tavily":
        return "tavily" if settings.tavily_api_key else "none"
    return "none"


def available() -> bool:
    """True when a search backend is configured and usable."""
    return provider() != "none"


def _domain_list(raw: str) -> set[str]:
    return {d.strip().lower().removeprefix("www.") for d in raw.split(",") if d.strip()}


def _tier(domain: str) -> str:
    """Classify a result's source.

    Substring-suffix match, so `nema.gov.ng` matches `www.nema.gov.ng` and any
    subdomain. Government and agency domains outrank news; everything else is
    `other` and Fahis will not verify on it alone.
    """
    for official in _domain_list(settings.search_official_domains):
        if domain == official or domain.endswith(f".{official}"):
            return "official"
    for media in _domain_list(settings.search_media_domains):
        if domain == media or domain.endswith(f".{media}"):
            return "media"
    return "other"


def _is_self(domain: str) -> bool:
    """Whether a result is our own publication.

    Guards against circular verification: finding SHELTER's own alert republished
    and reading it as independent corroboration.
    """
    own = urlparse(settings.public_site_url).netloc.lower().removeprefix("www.")
    if own and (domain == own or domain.endswith(f".{own}")):
        return True
    return any(
        domain == d or domain.endswith(f".{d}")
        for d in _domain_list(settings.search_exclude_domains)
    )


async def search(
    query: str,
    *,
    max_results: int = 8,
    include_domains: list[str] | None = None,
    days: int | None = None,
) -> SearchResponse:
    """Query the configured backend. Never raises.

    Returns `searched=False` when no backend is configured or the request failed, so the caller can
    distinguish "no corroboration found" from "could not look". Fahis depends on that distinction:
    the first is UNVERIFIED, the second is NOT_ATTEMPTED, and conflating them would count our own
    outage as evidence a warning was wrong.

    Dispatches to one of two adapters. Both return raw provider dicts, normalised by
    `_normalise` — so tiering, own-domain exclusion and ordering happen once regardless of which
    provider answered.
    """
    backend = provider()
    if backend == "none":
        return SearchResponse(query=query, results=[], searched=False)

    if backend == "tavily":
        raw_results = await _query_tavily(
            query, max_results=max_results, include_domains=include_domains, days=days
        )
    else:
        raw_results = await _query_searxng(
            query, include_domains=include_domains, days=days
        )

    if raw_results is None:
        # The request failed. `searched=False` so this is never read as a non-finding.
        return SearchResponse(query=query, results=[], searched=False)

    results = _normalise(raw_results, max_results=max_results)
    log.info(
        "search complete",
        extra={
            "provider": backend,
            "query": query,
            "results": len(results),
            "official": sum(1 for r in results if r.tier == "official"),
        },
    )
    return SearchResponse(query=query, results=results, searched=True)


async def _query_searxng(
    query: str,
    *,
    include_domains: list[str] | None,
    days: int | None,
) -> list[dict] | None:
    """SearXNG: `GET /search?format=json`. None on failure.

    Requires `format: json` in the instance's `settings.yml` — a stock image returns **403** for
    JSON output because it is disabled by default. That is configuration, not an API difference, and
    it is the first thing to check when a self-hosted instance returns nothing.
    """
    params: dict[str, str] = {
        "q": query,
        "format": "json",
        "language": settings.search_language,
        "safesearch": "0",
    }
    if settings.search_categories:
        # Category matters more than it looks, because it decides whether results carry DATES.
        #
        # Measured against a live instance for the same query: the `general` category returned 29
        # results and **zero** publication dates (duckduckgo, google cse); the `news` category
        # returned 25 results with dates on 23 of them (bing news, reuters, wikinews).
        #
        # Fahis is asking "did this hazard occur in this WINDOW?" — a question it cannot answer
        # without dates. A source with no date can only ever support a weaker verdict, so querying
        # the category that supplies them is the difference between a dated corroboration and an
        # undated maybe.
        params["categories"] = settings.search_categories
    if settings.searxng_engines:
        params["engines"] = settings.searxng_engines
    if days and settings.searxng_time_range:
        # OFF by default, and that is a measured decision rather than caution.
        #
        # Against a live instance for one query: with `time_range=month` the news category returned
        # 9 results and **zero** publication dates; without it, 3 results of which 2 were dated. The
        # filter widens the engine set to ones that do not report dates, so it trades exactly the
        # signal verification needs for volume it cannot date.
        #
        # It is also redundant. `_recency` classifies every source against the window and
        # `_guard_verdict` downgrades a CONFIRMED whose dated sources all predate it — so filtering
        # at the engine buys nothing that is not already enforced downstream, and costs the dates
        # that enforcement depends on.
        #
        # Kept as a setting because a heavily-covered area might prefer recall, and because
        # narrowing at the engine is cheaper than fetching and discarding.
        params["time_range"] = "day" if days <= 1 else "week" if days <= 7 else "month"
    if include_domains:
        # SearXNG has no include-domains parameter; `site:` in the query is the portable way, and
        # it works across the engines it proxies.
        sites = " OR ".join(f"site:{d}" for d in include_domains)
        params["q"] = f"{query} ({sites})"

    headers = {"Accept": "application/json"}
    if settings.searxng_api_key:
        # Optional: SearXNG can be fronted by a proxy or gateway requiring a token.
        headers["Authorization"] = f"Bearer {settings.searxng_api_key}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(
                f"{settings.searxng_url.rstrip('/')}/search",
                params=params,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        log.warning(
            "searxng query failed",
            extra={"query": query, "error": f"{type(exc).__name__}: {exc}"},
        )
        return None

    return payload.get("results", []) or []


async def _query_tavily(
    query: str,
    *,
    max_results: int,
    include_domains: list[str] | None,
    days: int | None,
) -> list[dict] | None:
    """Tavily: `POST /search` with the key in the JSON body. None on failure.

    A genuinely different contract from SearXNG, not a variant of it — verified against the stock
    SearXNG image, where a Tavily-shaped POST returns an HTML search page rather than JSON, ignoring
    every field in the body. Hence two adapters instead of one shared URL.

    `include_domains` and `days` are first-class parameters here, so neither needs the `site:` and
    bucket-rounding workarounds the SearXNG path uses.
    """
    body: dict = {
        "api_key": settings.tavily_api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": settings.tavily_search_depth,
    }
    if include_domains:
        body["include_domains"] = include_domains
    if days:
        body["days"] = days
    if settings.search_exclude_domains:
        # Applied at the provider as well as in `_normalise`, so our own domain is never returned in
        # the first place — belt and braces on the circular-verification guard, and it saves a
        # result slot for something that might actually corroborate.
        body["exclude_domains"] = sorted(_domain_list(settings.search_exclude_domains))

    # Authenticate BOTH ways, deliberately.
    #
    # Tavily's own API accepts the key in the body (legacy) and as a Bearer header (current), and
    # Tavily-COMPATIBLE endpoints implement one or the other — a self-hosted SearXNG stack with a
    # "Tavily switch-over" layer, a gateway, a local shim. Sending both means the same configuration
    # works against all of them, and neither party objects to the redundant one.
    #
    # This matters because the failure is silent: an endpoint that ignores body auth returns 401,
    # and Fahis reads a failed search as NOT_ATTEMPTED — an outage indistinguishable from a
    # misconfiguration unless someone reads the logs.
    headers = {"Content-Type": "application/json"}
    if settings.tavily_api_key:
        headers["Authorization"] = f"Bearer {settings.tavily_api_key}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = await client.post(
                f"{settings.tavily_api_base.rstrip('/')}/search",
                json=body,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        log.warning(
            "tavily query failed",
            extra={"query": query, "error": f"{type(exc).__name__}: {exc}"},
        )
        return None

    # Tavily names the extract `content` and has no `engine`; mapped here so `_normalise` sees one
    # shape. `published_date` rather than SearXNG's `publishedDate`.
    return [
        {
            "url": item.get("url"),
            "title": item.get("title"),
            "content": item.get("content"),
            "publishedDate": item.get("published_date"),
            "engine": "tavily",
        }
        for item in (payload.get("results") or [])
    ]


def _normalise(raw_results: list[dict], *, max_results: int) -> list[SearchResult]:
    """Provider dicts to `SearchResult`, tiered, self-filtered and ordered.

    Shared by both adapters so the three things that matter for verification happen exactly once:
    source tiering, own-domain exclusion, and official-first ordering. Duplicating them per provider
    is how one path ends up able to confirm a SHELTER alert with SHELTER's own published alert.
    """
    results: list[SearchResult] = []

    for raw in raw_results:
        url = raw.get("url")
        if not url:
            continue

        domain = urlparse(url).netloc.lower().removeprefix("www.")
        if _is_self(domain):
            log.debug("excluding own-domain result", extra={"domain": domain})
            continue

        results.append(
            SearchResult(
                url=url,
                title=(raw.get("title") or "").strip(),
                snippet=(raw.get("content") or "").strip(),
                tier=_tier(domain),
                published=raw.get("publishedDate"),
                engine=raw.get("engine"),
            )
        )
        if len(results) >= max_results:
            break

    # Official sources first — Fahis reads the head of this list, and an agency bulletin should
    # reach it before a blog aggregating one.
    order = {"official": 0, "media": 1, "other": 2}
    results.sort(key=lambda r: order.get(r.tier, 3))
    return results


async def search_or_raise(query: str, **kwargs) -> SearchResponse:
    """`search()` that raises `SearchUnavailable` instead of returning empty.

    For the chat tool loop, where the model should be told the tool failed rather
    than being handed an empty result it may interpret as "nothing exists".
    """
    response = await search(query, **kwargs)
    if not response.searched:
        raise SearchUnavailable(f"search backend unavailable for query: {query!r}")
    return response
