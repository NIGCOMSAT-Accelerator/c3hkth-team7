"""Chat: explaining an alert a subscriber already received.

**The one rule that governs this whole module.**

An assessment's numbers come from measured satellite and meteorological data.
Chat may explain them, give agronomic or public-health context, and cite outside
reporting. It may **never** amend, contradict, or supplement those figures with a
web-sourced number.

Concretely — allowed: *"waterlogging starves rice roots of oxygen; three days is
usually survivable, a week often is not."* Not allowed: *"other sources say 45% of
your area is flooded."* The first adds understanding. The second replaces a
measured figure with an unattributed one, and it is exactly the failure mode the
grounding rule in `advisory/generator.py` exists to prevent. Two violations of
that rule have already been found and removed from this codebase.

The separation is enforced three ways:

1. **Tool design.** `get_my_alerts` returns the subscriber's own assessments —
   the only source of hazard figures. `search_web` is explicitly described to the
   model as background context only.
2. **The agent instructions** state the rule in the imperative.
3. **Typed scoping.** `ChatDeps.subscriber_id` reaches the tools through
   `RunContext[ChatDeps]`, so there is no tool parameter in which the model could
   ask for a different subscriber's data. This matters: `GET /alerts` is currently
   unauthenticated, and a chat endpoint able to read arbitrary subscribers would
   turn that into a convenient exfiltration interface.

**Division of labour with `app/agentic/chat_agent.py`.** The instructions, the
three tools and the `ChatDeps` scope live there as a Pydantic-AI agent — together,
because the grounding rule is expressed in all three at once and splitting them
across modules would let one drift from the others. This module keeps what the
framework does not do: the four-rung cost cascade, session persistence,
embedding-based context retrieval, and the cross-request token budget.
"""

from __future__ import annotations

import uuid

from app.agentic import provider
from app.agentic.chat_agent import ChatDeps, build_agent
from app.chat import answers
from app.config import settings
from app.db import session as db
from app.llm import budget, embeddings
from app.logging_config import get_logger
from app.models.schemas import Alert, ChatMessage, ChatTurn, SourceCitation
from app.store import cache, repository

log = get_logger(__name__)


class BudgetExceeded(RuntimeError):
    """A token ceiling was reached. Surfaced as HTTP 429, not 503 — the service
    is healthy, this caller has simply spent their allowance."""


class ChatUnavailable(RuntimeError):
    """No inference endpoint is configured, or the run failed.

    Deliberately no template fallback: a canned reply to a free-text question
    looks like an answer. The route surfaces this as a 503.
    """


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #


async def get_or_create_session(
    session_id: str | None, subscriber_id: str | None, language: str = "en"
) -> str:
    """Resolve a session id, creating one if needed.

    Verifies an existing session belongs to the claimed subscriber. Without that
    check, passing someone else's session id would inherit their data scope.
    """
    if session_id:
        row = await db.fetchrow(
            "SELECT id, subscriber_id FROM chat_sessions WHERE id = $1", session_id
        )
        if row is not None:
            if row["subscriber_id"] != subscriber_id:
                raise PermissionError("session does not belong to this subscriber")
            await db.execute(
                "UPDATE chat_sessions SET last_seen_at = now() WHERE id = $1",
                session_id,
            )
            return session_id

    new_id = f"chat_{uuid.uuid4().hex[:16]}"
    await db.execute(
        "INSERT INTO chat_sessions (id, subscriber_id, language) VALUES ($1, $2, $3)",
        new_id,
        subscriber_id,
        language,
    )
    return new_id


async def history(session_id: str, *, limit: int = 40) -> list[ChatMessage]:
    """Chronological history, for display. Not what is sent to the model."""
    rows = await db.fetch(
        "SELECT role, content, created_at FROM chat_messages "
        "WHERE session_id = $1 ORDER BY created_at DESC LIMIT $2",
        session_id,
        limit,
    )
    return [
        ChatMessage(role=r["role"], content=r["content"], created_at=r["created_at"])
        for r in reversed(rows)
    ]


async def _relevant_history(
    session_id: str, question: str, *, limit: int
) -> list[ChatMessage]:
    """Context for the model: the most *relevant* prior turns, not the latest.

    This is the main per-turn token saving. Replaying the last N turns costs
    tokens linearly in N on every turn and mostly replays material irrelevant to
    the current question. Retrieving K by similarity costs a constant and returns
    the turns that actually bear on it.

    Always keeps the last exchange regardless of similarity — a follow-up like
    "and the second one?" is meaningless without its predecessor, and similarity
    search will not rank it highly because it shares no vocabulary.

    Degrades to recency when embeddings are unavailable, so chat works with no
    embedding provider configured.
    """
    vector = await embeddings.embed(question) if embeddings.available() else None

    if vector is None:
        return await history(session_id, limit=limit)

    rows = await db.fetch(
        """
        WITH recent AS (
            -- The immediately preceding exchange, always included: a follow-up
            -- question is unintelligible without it and similarity will not find
            -- it.
            SELECT id, role, content, created_at, 0::float AS distance
            FROM chat_messages
            WHERE session_id = $1
            ORDER BY created_at DESC
            LIMIT 2
        ),
        similar AS (
            SELECT id, role, content, created_at, (embedding <=> $2) AS distance
            FROM chat_messages
            WHERE session_id = $1
              AND embedding IS NOT NULL
            ORDER BY embedding <=> $2
            LIMIT $3
        )
        SELECT DISTINCT ON (id) id, role, content, created_at, distance
        FROM (SELECT * FROM recent UNION ALL SELECT * FROM similar) merged
        ORDER BY id, distance
        """,
        session_id,
        vector,
        limit,
    )

    # Chronological for the model — a conversation out of order reads as
    # incoherent even when every turn is relevant.
    ordered = sorted(rows, key=lambda r: r["created_at"])
    return [
        ChatMessage(role=r["role"], content=r["content"], created_at=r["created_at"])
        for r in ordered
    ]


async def _save_message(
    session_id: str,
    role: str,
    content: str,
    sources: list[SourceCitation] | None = None,
    tools_used: list[str] | None = None,
    *,
    tokens_used: int = 0,
    answered_by: str | None = None,
) -> None:
    """Persist a turn, embedding it best-effort.

    A failed embedding leaves the column NULL, which only means the turn will not
    be retrieved later — it must not fail the write, because the turn itself is
    the record of the conversation.
    """
    vector = None
    if embeddings.available():
        vector = await embeddings.embed(content)

    await db.execute(
        "INSERT INTO chat_messages "
        "(session_id, role, content, sources, tools_used, embedding, tokens_used, "
        " answered_by) "
        "VALUES ($1, $2, $3, $4::jsonb, $5::text[], $6, $7, $8)",
        session_id,
        role,
        content,
        [s.model_dump(mode="json") for s in (sources or [])],
        tools_used or [],
        vector,
        tokens_used,
        answered_by,
    )


# --------------------------------------------------------------------------- #
# Answering
# --------------------------------------------------------------------------- #


async def answer(
    question: str,
    *,
    session_id: str | None = None,
    subscriber_id: str | None = None,
    language: str = "en",
) -> ChatTurn:
    """One conversational turn, cheapest path first.

    **The cascade, in order. Each rung is tried only if the one above misses:**

      1. **Deterministic** (0 tokens) — "what should I do", "how sure are you",
         "why did you send this". The answers are already in the assessment;
         paraphrasing them through a model would spend tokens *and* be the step at
         which a number could get invented.
      2. **Response cache** (0 tokens) — the same question in the same session
         within `CHAT_CACHE_TTL_SECONDS`. Subscribers re-ask, especially over SMS-
         grade connections where they are unsure the first message sent.
      3. **Budget check** — refuse before spending if a ceiling is reached, rather
         than discovering it from a bill.
      4. **LLM with retrieved context** — only genuinely novel questions, and with
         the relevant K turns rather than the last N.

    Raises `llm.LLMUnavailable` when rung 4 is needed but no endpoint is
    configured. There is deliberately no template fallback for a free-text
    question: a canned reply to something specific looks like an answer.
    """
    resolved = await get_or_create_session(session_id, subscriber_id, language)

    # --- 1. Deterministic. English only: the templates are not translated, and a
    # machine-translated safety instruction is worse than an English one the
    # reader can seek help with — the same rule advisory/_template follows.
    if settings.chat_deterministic_answers and language == "en":
        latest = await _latest_alert(subscriber_id)
        canned = answers.try_answer(question, latest)
        if canned is not None:
            reply, intent = canned
            await _save_message(resolved, "user", question)
            await _save_message(
                resolved, "assistant", reply, answered_by="deterministic"
            )
            log.info(
                "chat answered deterministically",
                extra={"session_id": resolved, "intent": intent, "tokens": 0},
            )
            return ChatTurn(
                session_id=resolved, reply=reply, sources=[], tools_used=[]
            )

    # --- 2. Response cache, keyed on the normalised question within a session.
    cache_key = cache.key("chat-answer", resolved, _question_digest(question))
    if settings.chat_cache_ttl_seconds > 0:
        cached = await cache.get_json(cache_key)
        if cached:
            log.info(
                "chat answered from cache",
                extra={"session_id": resolved, "tokens": 0},
            )
            return ChatTurn(
                session_id=resolved,
                reply=cached["reply"],
                sources=[SourceCitation.model_validate(s) for s in cached.get("sources", [])],
                tools_used=cached.get("tools_used", []),
            )

    # --- 3. Budget. Estimate before spending; `record` corrects it afterwards
    # with the provider's own figure.
    estimated = budget.estimate_tokens(question) + settings.llm_max_tokens
    allowed, reason = await budget.check(subscriber_id, estimated)
    if not allowed:
        raise BudgetExceeded(reason)

    # --- 4. The agent.
    if not provider.available():
        raise ChatUnavailable(
            "No inference endpoint configured. Set LLM_BASE_URL to enable chat."
        )

    await _save_message(resolved, "user", question)

    prior = await _relevant_history(
        resolved, question, limit=settings.chat_context_turns
    )

    deps = ChatDeps(subscriber_id=subscriber_id, language=language)
    agent = build_agent()

    # `prior` ends with the question just saved, so the last entry is the prompt
    # and everything before it is history. Splitting them this way means the
    # question is never sent twice.
    prompt = prior[-1].content if prior else question
    message_history = _as_message_history(prior[:-1])

    try:
        result = await agent.run(
            prompt,
            deps=deps,
            message_history=message_history,
            usage_limits=provider.usage_limits(
                max_requests=settings.chat_max_tool_rounds + 1
            ),
        )
    except Exception as exc:
        # Includes UsageLimitExceeded and UnexpectedModelBehavior. The user is
        # waiting on a free-text answer, so there is nothing honest to substitute.
        log.warning("chat run failed", extra={"error": str(exc)[:300]})
        raise ChatUnavailable(str(exc)) from exc

    # De-duplicate citations by URL, preserving order of first appearance.
    # `deps.sources` was appended to by the search tool during the run.
    seen: set[str] = set()
    sources: list[SourceCitation] = []
    for citation in deps.sources:
        if citation.url in seen:
            continue
        seen.add(citation.url)
        sources.append(citation)

    tools_used = _tools_called(result)

    # Prefer the framework's usage accounting; fall back to our estimate when the
    # provider omits a usage block (vLLM and some proxies do), so the daily budget
    # is never silently unenforced.
    tokens = _total_tokens(result) or estimated
    await budget.record(subscriber_id, tokens, purpose="chat")

    reply = result.output or ""

    await _save_message(
        resolved,
        "assistant",
        reply,
        sources=sources,
        tools_used=tools_used,
        tokens_used=tokens,
        answered_by="llm",
    )

    if settings.chat_cache_ttl_seconds > 0 and reply:
        await cache.set_json(
            cache_key,
            {
                "reply": reply,
                "sources": [s.model_dump(mode="json") for s in sources],
                "tools_used": tools_used,
            },
            settings.chat_cache_ttl_seconds,
        )

    log.info(
        "chat turn complete",
        extra={
            "session_id": resolved,
            "subscriber_id": subscriber_id,
            "tools": tools_used,
            "sources": len(sources),
            "tokens": tokens,
            "context_turns": len(prior),
            "model": provider.model_label(),
        },
    )

    return ChatTurn(
        session_id=resolved,
        reply=reply,
        sources=sources,
        tools_used=tools_used,
    )


def _as_message_history(turns: list[ChatMessage]) -> list:
    """Convert stored turns into Pydantic-AI message objects.

    Only `user` and `assistant` roles are persisted, so this mapping is total.
    Instructions are deliberately *not* included: the agent re-sends its own per
    run, which is why `instructions` was chosen over `system_prompt` — a stale
    copy of the grounding rule can never be replayed from history.
    """
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        UserPromptPart,
    )

    messages: list = []
    for turn in turns:
        if turn.role == "user":
            messages.append(ModelRequest(parts=[UserPromptPart(content=turn.content)]))
        else:
            messages.append(ModelResponse(parts=[TextPart(content=turn.content)]))
    return messages


def _tools_called(result) -> list[str]:
    """Tool names invoked during the run, in order, for provenance.

    Read from the message trace rather than tracked by hand — the framework owns
    the loop now, so its record is authoritative.
    """
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    names: list[str] = []
    try:
        for message in result.all_messages():
            if isinstance(message, ModelResponse):
                names.extend(
                    part.tool_name
                    for part in message.parts
                    if isinstance(part, ToolCallPart)
                )
    except Exception:  # noqa: BLE001 — provenance must never fail a reply
        return []
    return names


def _total_tokens(result) -> int:
    """Tokens for the whole run, or 0 when the provider reported none."""
    try:
        usage = result.usage()
    except TypeError:
        # `.usage` is a property on some versions and a method on others.
        usage = result.usage
    except Exception:
        return 0

    try:
        return int(getattr(usage, "total_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0


async def _latest_alert(subscriber_id: str | None):
    """The subscriber's most recent alert, or None.

    Cached briefly: the deterministic path reads this on most turns, and an alert
    changes only when the pipeline runs.
    """
    if not subscriber_id:
        return None

    key = cache.key("latest-alert", subscriber_id)
    cached = await cache.get_json(key)
    if cached:
        try:
            return Alert.model_validate(cached)
        except Exception:
            await cache.delete(key)

    alerts = await repository.list_alerts(subscriber_id, limit=1)
    if not alerts:
        return None

    await cache.set_json(
        key, alerts[0].model_dump(mode="json"), settings.cache_default_ttl_seconds
    )
    return alerts[0]


def _question_digest(question: str) -> str:
    """Stable key for the response cache.

    Normalised so trivial variations — case, punctuation, spacing — hit the same
    entry. Not semantic: two differently-worded questions with the same meaning
    are different keys, which is the conservative choice, since a semantic cache
    can serve a subtly wrong answer.
    """
    import hashlib
    import re

    normalised = re.sub(r"[^\w\s]", "", question.lower())
    normalised = re.sub(r"\s+", " ", normalised).strip()
    return hashlib.sha256(normalised.encode()).hexdigest()[:16]


async def economics(*, days: int = 7) -> dict:
    """Where chat answers came from, and what they cost.

    The point of the cascade is that most answers cost nothing; this is how you
    verify that rather than assuming it.
    """
    from datetime import datetime, timedelta, timezone

    since = datetime.now(timezone.utc) - timedelta(days=days)

    row = await db.fetchrow(
        """
        SELECT
            count(*) FILTER (WHERE answered_by = 'deterministic') AS deterministic,
            count(*) FILTER (WHERE answered_by = 'llm')           AS llm,
            count(*) FILTER (WHERE role = 'assistant')            AS total,
            coalesce(sum(tokens_used), 0)                         AS tokens
        FROM chat_messages
        WHERE created_at >= $1 AND role = 'assistant'
        """,
        since,
    )

    if row is None:
        return {}

    total = int(row["total"] or 0)
    deterministic = int(row["deterministic"] or 0)

    return {
        "window_days": days,
        "answers_total": total,
        "answered_deterministically": deterministic,
        "answered_by_llm": int(row["llm"] or 0),
        "zero_token_share": round(deterministic / total, 3) if total else None,
        "tokens_used": int(row["tokens"] or 0),
        "mean_tokens_per_answer": (
            round(int(row["tokens"] or 0) / total, 1) if total else None
        ),
        "budget": await budget.summary(),
    }


async def suggested_questions(subscriber_id: str | None) -> list[str]:
    """Starter prompts, tailored to the subscriber's latest alert if there is one.

    Cheap and non-LLM. A blank chat box gets no engagement; these give a farmer an
    obvious first thing to ask.
    """
    generic = [
        "What does my latest alert mean?",
        "What should I do first?",
        "How sure are you about this?",
    ]
    if not subscriber_id:
        return generic

    alerts = await repository.list_alerts(subscriber_id, limit=1)
    if not alerts:
        return generic

    assessment = alerts[0].assessment
    hazard = assessment.hazard.value.replace("_", " ")
    return [
        f"Why do you think there is {hazard} in {assessment.aoi_name}?",
        "What should I do first?",
        f"How does this compare to the last few weeks in {assessment.aoi_name}?",
        "What happens if I do nothing?",
    ]
