"""Token accounting and ceilings.

An early-warning service that runs 24/7 across thousands of subscribers can spend
an unbounded amount on inference if nothing counts. This module counts, and
enforces two ceilings:

* **Per-subscriber daily** (`LLM_DAILY_TOKEN_BUDGET_PER_SUBSCRIBER`) — stops one
  chatty subscriber consuming the deployment's budget.
* **Global daily** (`LLM_DAILY_TOKEN_BUDGET_GLOBAL`) — a backstop against a bug
  or an abusive client.

**Both fail OPEN by default, and that is a deliberate safety choice.** If the
cache is unreachable we cannot know the spend, and refusing to explain a flood
warning because a counter is down is the worse failure. `LLM_BUDGET_FAIL_CLOSED`
flips it for cost-sensitive deployments.

Counters live in Redis db1 with a TTL to the end of the UTC day, so they expire
themselves — no reset job, and no unbounded key growth on a database that shares
an instance with the job queue.

**What this does NOT do:** gate the advisory path. Advisory generation is the
product; it degrades to a deterministic template on its own if a provider is
unavailable, and a budget ceiling must never be the reason a farmer gets no
warning. Only chat and Fahis adjudication are gated — both are enhancements.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import settings
from app.logging_config import get_logger
from app.store import cache

log = get_logger(__name__)


def _day_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _seconds_to_midnight() -> int:
    """TTL so a counter expires exactly when its window does.

    Self-expiring rather than reset by a job: one less moving part, and it means
    a counter can never outlive its meaning.
    """
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(60, int((tomorrow - now).total_seconds()))


def _subscriber_key(subscriber_id: str) -> str:
    return cache.key("tokens", _day_key(), subscriber_id)


def _global_key() -> str:
    return cache.key("tokens", _day_key(), "_global")


def estimate_tokens(text: str) -> int:
    """Rough token count without a tokeniser.

    ~4 characters per token is the standard English approximation. Deliberately
    not exact: pulling in `tiktoken` would add a dependency and would still be
    wrong for non-OpenAI models and for Hausa, Yoruba or Igbo text, where
    per-character density differs. The ceiling only needs to be the right order of
    magnitude, and `record()` corrects it afterwards with the provider's own
    reported usage.
    """
    return max(1, len(text) // 4)


async def spent_today(subscriber_id: str | None = None) -> int:
    """Tokens recorded so far today. 0 when unknown."""
    key = _subscriber_key(subscriber_id) if subscriber_id else _global_key()
    raw = await cache.get_text(key)
    try:
        return int(raw) if raw else 0
    except (TypeError, ValueError):
        return 0


async def check(subscriber_id: str | None, estimated: int) -> tuple[bool, str]:
    """`(allowed, reason)` for a call of roughly this size.

    Checks the global ceiling first — a global overrun affects everyone, so it is
    the more important one to report accurately.
    """
    if not settings.llm_budget_enabled:
        return True, ""

    try:
        if settings.llm_daily_token_budget_global > 0:
            used = await spent_today(None)
            if used + estimated > settings.llm_daily_token_budget_global:
                return False, (
                    f"global daily token budget reached "
                    f"({used}/{settings.llm_daily_token_budget_global})"
                )

        if subscriber_id and settings.llm_daily_token_budget_per_subscriber > 0:
            used = await spent_today(subscriber_id)
            if used + estimated > settings.llm_daily_token_budget_per_subscriber:
                return False, (
                    f"your daily question limit is reached "
                    f"({used}/{settings.llm_daily_token_budget_per_subscriber} tokens)"
                )
    except Exception as exc:
        # Cache unreachable. Fail open unless told otherwise — see the module
        # docstring. Logged at warning because a silently unenforced budget is
        # worth knowing about.
        if settings.llm_budget_fail_closed:
            log.warning("budget check failed; failing closed", extra={"error": str(exc)})
            return False, "token budget cannot be verified right now"
        log.warning(
            "budget check failed; allowing the call (fail-open)",
            extra={"error": str(exc)},
        )

    return True, ""


async def record(
    subscriber_id: str | None, tokens: int, *, purpose: str = "chat"
) -> None:
    """Add to today's counters. Never raises.

    Increments both the per-subscriber and global counters. A failure here loses
    accounting for one call rather than failing the request the user is waiting
    on — the counter is advisory, the answer is the product.
    """
    if not settings.llm_budget_enabled or tokens <= 0:
        return

    ttl = _seconds_to_midnight()
    try:
        cache_client = cache.get_cache()
        pipe = cache_client.pipeline()
        if subscriber_id:
            pipe.incrby(_subscriber_key(subscriber_id), tokens)
            pipe.expire(_subscriber_key(subscriber_id), ttl, nx=True)
        pipe.incrby(_global_key(), tokens)
        pipe.expire(_global_key(), ttl, nx=True)
        await pipe.execute()
    except Exception as exc:
        log.debug("token accounting failed", extra={"error": str(exc)})
        return

    log.info(
        "tokens recorded",
        extra={"subscriber_id": subscriber_id, "tokens": tokens, "purpose": purpose},
    )


def usage_from_response(body: dict) -> int:
    """Total tokens from a provider's `usage` block.

    Every OpenAI-compatible server returns this, though not all populate it —
    vLLM and some proxies omit it. Returns 0 when absent, and the caller then
    keeps its own estimate rather than recording nothing.
    """
    usage = body.get("usage") or {}
    total = usage.get("total_tokens")
    if isinstance(total, int) and total > 0:
        return total

    # Some servers report the parts but not the total.
    prompt = usage.get("prompt_tokens") or 0
    completion = usage.get("completion_tokens") or 0
    if isinstance(prompt, int) and isinstance(completion, int):
        return prompt + completion

    return 0


async def summary() -> dict:
    """Today's spend, for /health and the operator console."""
    if not settings.llm_budget_enabled:
        return {"enabled": False}

    used = await spent_today(None)
    limit = settings.llm_daily_token_budget_global
    return {
        "enabled": True,
        "global_used_today": used,
        "global_limit": limit or None,
        "per_subscriber_limit": (
            settings.llm_daily_token_budget_per_subscriber or None
        ),
        "fail_closed": settings.llm_budget_fail_closed,
        "remaining": max(0, limit - used) if limit else None,
    }
