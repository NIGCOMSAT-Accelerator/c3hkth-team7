"""Embeddings, over the same OpenAI-compatible transport.

`POST /v1/embeddings` — the companion endpoint to `/v1/chat/completions`, and
equally standard, so the provider switch is the same two environment variables.
A separate `EMBEDDING_BASE_URL` is supported for the common case of running a
tiny local embedding model (which is cheap and fast) alongside a frontier chat
model (which is not).

**Every function here degrades to None rather than raising.** An embedding is an
optimisation: without one, chat falls back to replaying recent turns, which costs
more tokens but works. Nothing in the product stops because embeddings are
unavailable.

Cost note: embedding a chat turn is roughly two orders of magnitude cheaper than
the completion that produced it, so caching embeddings is not worth the
complexity — but embedding *queries* is worth batching where several are needed
at once, which `embed_many` does.
"""

from __future__ import annotations

import httpx

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=8.0)


def available() -> bool:
    """True when an embedding endpoint is reachable in configuration.

    Falls back to the chat endpoint's base URL, since most providers serve both
    from the same host — so setting `LLM_BASE_URL` alone enables embeddings too.
    """
    return bool(settings.embedding_base_url or settings.llm_base_url)


def _base_url() -> str | None:
    return settings.embedding_base_url or settings.llm_base_url


def _api_key() -> str | None:
    return settings.embedding_api_key or settings.llm_api_key


async def embed_many(texts: list[str]) -> list[list[float]] | None:
    """Embed several strings in one request. None on any failure.

    Batched because the per-request overhead dominates for short strings, and
    chat retrieval needs the query embedded while backfill needs many at once.
    """
    if not available() or not texts:
        return None

    # Guard the request size: a provider will reject an oversized batch, and
    # truncating each input is cheaper than discovering the limit at runtime.
    trimmed = [t[: settings.embedding_max_input_chars] for t in texts]

    headers = {"Content-Type": "application/json"}
    key = _api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                f"{_base_url().rstrip('/')}/embeddings",
                json={"model": settings.embedding_model, "input": trimmed},
                headers=headers,
            )
            response.raise_for_status()
            body = response.json()
    except Exception as exc:
        log.debug("embedding request failed", extra={"error": str(exc)})
        return None

    data = body.get("data") or []
    if len(data) != len(trimmed):
        log.warning(
            "embedding count mismatch",
            extra={"requested": len(trimmed), "returned": len(data)},
        )
        return None

    # Providers are supposed to return these in input order, but `index` is in the
    # response for exactly this reason — sorting on it means a provider that
    # reorders cannot silently mis-pair vectors with their text.
    try:
        ordered = sorted(data, key=lambda d: d.get("index", 0))
        vectors = [list(d["embedding"]) for d in ordered]
    except (KeyError, TypeError):
        return None

    if any(len(v) != settings.embedding_dimensions for v in vectors):
        # A dimension mismatch means EMBEDDING_MODEL and EMBEDDING_DIMENSIONS
        # disagree. Inserting these would fail at the column type anyway, so fail
        # here where the message is useful.
        log.error(
            "embedding dimension mismatch; check EMBEDDING_DIMENSIONS",
            extra={
                "expected": settings.embedding_dimensions,
                "got": len(vectors[0]) if vectors else 0,
                "model": settings.embedding_model,
            },
        )
        return None

    return vectors


async def embed(text: str) -> list[float] | None:
    """Embed one string. None on any failure."""
    result = await embed_many([text])
    return result[0] if result else None
