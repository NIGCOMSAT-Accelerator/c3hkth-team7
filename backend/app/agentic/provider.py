"""Model construction — the one place provider portability is preserved.

Pydantic-AI's `'openai:gpt-4o'` string shorthand reads the provider's own
environment variables and would bypass `LLM_BASE_URL` / `LLM_API_KEY` entirely.
So the model is always built explicitly from an `OpenAIProvider` carrying our
settings, which keeps the promise the rest of the stack makes: **switching
frontier providers is two environment variables and a container recreate.**

    LLM_BASE_URL=https://api.openai.com/v1                    LLM_MODEL=gpt-4o
    LLM_BASE_URL=https://api.anthropic.com/v1                 LLM_MODEL=claude-opus-5
    LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
    LLM_BASE_URL=http://localhost:8000/v1                     (vLLM, no key)

`OpenAIChatModel` targets `/v1/chat/completions` — the same endpoint
`app/llm/client.py` posts to by hand. A test asserts no vendor-specific model
adapter is imported anywhere in this package, mirroring the guard that already
protects `app/llm/`.
"""

from __future__ import annotations

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

_model = None


def available() -> bool:
    """True when an inference endpoint is configured.

    Base URL alone is sufficient — a self-hosted vLLM needs no key, and requiring
    one would make on-premise inference impossible.
    """
    return bool(settings.llm_base_url)


def model_label() -> str:
    """Model name for provenance on stored turns and verdicts."""
    return settings.llm_model


def build_model():
    """The shared model instance, built once.

    Raises `RuntimeError` when unconfigured — callers check `available()` first
    and surface a 503 (chat) or record NOT_ATTEMPTED (Fahis) rather than letting
    this propagate.
    """
    global _model
    if not available():
        raise RuntimeError("LLM_BASE_URL is not configured")

    if _model is None:
        # Imported here rather than at module scope so `app.agentic` stays
        # importable without pydantic-ai installed — the same reason
        # `advisory/generator.py` imports the Anthropic SDK inside a function.
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        _model = OpenAIChatModel(
            settings.llm_model,
            provider=OpenAIProvider(
                base_url=settings.llm_base_url,
                # A local vLLM accepts any non-empty key; passing a placeholder
                # keeps the client constructible when no real key is set.
                api_key=settings.llm_api_key or "not-required",
            ),
        )
        log.info(
            "agentic model ready",
            extra={"model": settings.llm_model, "base_url": settings.llm_base_url},
        )
    return _model


def model_settings() -> dict:
    """Per-run settings honouring the provider-compatibility flags.

    `LLM_SUPPORTS_TEMPERATURE=false` omits temperature entirely — OpenAI's
    reasoning models (o1/o3/gpt-5 family) reject any explicit value, so it has to
    be absent rather than set. Same reasoning as `llm/client._base_payload`.
    """
    payload: dict = {"max_tokens": settings.llm_max_tokens}
    if settings.llm_supports_temperature:
        payload["temperature"] = settings.llm_temperature
    return payload


def usage_limits(*, max_requests: int):
    """Per-run bounds. Complements — does not replace — `llm/budget.py`.

    `UsageLimits` is per-run; the daily per-subscriber and global ceilings are
    cross-request and stay in `llm/budget.py`, which counts into Redis db1.
    """
    from pydantic_ai import UsageLimits

    return UsageLimits(
        request_limit=max_requests,
        # Generous relative to `llm_max_tokens`: this is a runaway guard for the
        # whole tool loop, not a per-response cap.
        total_tokens_limit=settings.llm_max_tokens * (max_requests + 2),
    )
