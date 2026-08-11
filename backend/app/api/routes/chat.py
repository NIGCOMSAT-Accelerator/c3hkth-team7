"""Chat — ask about an alert you received.

**Auth note.** These endpoints take `subscriber_id` and scope every data tool to
it. Until the session layer in `docs/frontend-journey-review.md` §5.2 lands, that
identifier is asserted rather than proven, so this router is API-key gated: the
Next.js server holds the key and passes it through a Server Action, and the
browser never sees it.

That is deliberately stricter than `GET /alerts`, which is currently
unauthenticated. Chat can *summarise* a subscriber's alerts, so leaving it open
would make it a more convenient exfiltration interface than the raw endpoint.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.agentic import provider as agentic_provider
from app.chat import service as chat_service
from app.config import settings
from app.iam.models import ApiKeyScope
from app.iam.platform import require_platform_scope
from app.logging_config import get_logger
from app.models.schemas import ChatMessage, ChatTurn
from app.store import cache

log = get_logger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2_000)
    session_id: str | None = None
    #: Whose data this session may read. Every tool is bound to it.
    subscriber_id: str | None = None
    language: str = "en"


@router.post(
    "", response_model=ChatTurn, dependencies=[Depends(require_platform_scope(ApiKeyScope.PLATFORM_OPERATE))]
)
async def ask(request: ChatRequest) -> ChatTurn:
    """One conversational turn."""
    if not settings.chat_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chat is disabled on this deployment.",
        )

    # Note the order: the LLM check is NOT first. Deterministic answers need no
    # inference endpoint, so a deployment with no provider configured can still
    # answer "what should I do" and "how sure are you" from assessment data.
    # `answer()` raises LLMUnavailable only if it actually reaches that rung.
    if not agentic_provider.available() and not settings.chat_deterministic_answers:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "No inference endpoint configured. Set LLM_BASE_URL to enable chat."
            ),
        )

    # Rate limit per session, or per subscriber before a session exists. Chat
    # calls an inference endpoint and a search backend, both of which cost per
    # call. Fails open — `cache.incr` returns 0 when the cache is down, and
    # locking people out of an explanation during a flood is the worse failure.
    limit_key = cache.key(
        "chat-rate", request.session_id or request.subscriber_id or "anon"
    )
    used = await cache.incr(limit_key, 3_600)
    if used > settings.chat_rate_limit_per_hour:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many questions this hour. Please try again later.",
        )

    try:
        return await chat_service.answer(
            request.question,
            session_id=request.session_id,
            subscriber_id=request.subscriber_id,
            language=request.language,
        )
    except PermissionError as exc:
        # A session id that belongs to someone else.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc
    except chat_service.BudgetExceeded as exc:
        # 429, not 503: the service is healthy, this caller has spent their
        # allowance. A client can distinguish "come back later" from "broken".
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Question limit reached — {exc}. Your alerts still arrive "
                "normally; only the chat assistant is paused."
            ),
        ) from exc
    except chat_service.ChatUnavailable as exc:
        log.warning("chat run failed", extra={"error": str(exc)})
        # Deliberately generic to the caller: the underlying error can carry the
        # endpoint URL and model name, which a subscriber should not see.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The assistant is temporarily unavailable. Please try again.",
        ) from exc


@router.get(
    "/{session_id}/history",
    response_model=list[ChatMessage],
    dependencies=[Depends(require_platform_scope(ApiKeyScope.PLATFORM_OPERATE))],
)
async def get_history(
    session_id: str, limit: int | None = None
) -> list[ChatMessage]:
    """Full conversation for display.

    Unrelated to the context sent to the model — that is retrieved by relevance
    and bounded by `CHAT_CONTEXT_TURNS`. Raising this limit costs no tokens.
    """
    requested = limit or settings.chat_history_turns
    return await chat_service.history(session_id, limit=min(requested, 200))


@router.get("/economics", dependencies=[Depends(require_platform_scope(ApiKeyScope.PLATFORM_OPERATE))])
async def economics(days: int = 7) -> dict:
    """Where answers came from and what they cost.

    The cascade's whole purpose is that most answers cost nothing. Check
    `zero_token_share` here rather than assuming it — a low value means the
    deterministic intents are not matching real questions and the phrase list in
    `chat/answers.py` needs widening.
    """
    return await chat_service.economics(days=min(days, 90))


@router.get("/suggestions", dependencies=[Depends(require_platform_scope(ApiKeyScope.PLATFORM_OPERATE))])
async def suggestions(subscriber_id: str | None = None) -> dict:
    """Starter prompts, tailored to the latest alert when there is one.

    Non-LLM and cheap. A blank chat box gets no engagement; this gives a farmer an
    obvious first question.
    """
    return {"questions": await chat_service.suggested_questions(subscriber_id)}
