"""OpenAI-compatible chat client and tool loop.

Raw `httpx` against `/v1/chat/completions` rather than the `openai` SDK: the
whole surface used here is one endpoint, and staying on httpx means the same code
runs unmodified against OpenAI, vLLM, Ollama, LiteLLM or any other server
speaking that shape. Every other HTTP client in this codebase is already httpx.

**The tool loop is the important part of this file.** Both Fahis and chat need
"call the model, run the tools it asks for, feed results back, repeat until it
stops" — and both need that loop to be bounded, because an unbounded one is a
runaway cost and latency risk on a service that must stay responsive during a
flood.

Three bounds, all enforced here rather than trusted to the model:

* `max_rounds` — hard ceiling on tool round trips.
* Unknown tool names return an error *to the model* rather than raising, so it can
  correct itself instead of the request dying.
* A tool that raises is reported to the model as a failed tool, not propagated.
  A search outage should produce "I could not check that" rather than a 500.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(90.0, connect=10.0)


class LLMUnavailable(RuntimeError):
    """No inference endpoint is configured, or the request failed."""


class LLMRefusal(RuntimeError):
    """The provider declined to answer.

    Distinct from `LLMUnavailable` because the correct response differs: a
    refusal will recur on retry and should fall straight through to a
    deterministic path, while an unavailable endpoint may not.
    """


class LLMTruncated(RuntimeError):
    """The provider stopped at its token ceiling with the answer unfinished.

    ## Why this is a distinct, raised failure rather than "return what we got"

    `finish_reason: "length"` was never checked here, so a response cut off mid-sentence looked
    identical to a complete one — it just returned a shorter string. Every explanation surface
    (`explain/base.py`) and the advisory generator both fall back to a deterministic template on
    ANY exception, precisely because "a truncated explanation is worse than a slightly long one,
    and far worse than the deterministic template" (see `explain/base.MAX_TOKENS`). That fallback
    already existed; the gap was that nothing here ever triggered it for this failure mode; a
    truncated string satisfied `.strip()` and shipped to a subscriber as if it were whole.

    Raised rather than silently retried with a larger budget: a caller-specific retry policy
    belongs with the caller, which knows whether a slower, more expensive second attempt is worth
    it for that surface (an advisory, yes; a chat answer under a per-turn budget, maybe not).
    """


@dataclass(frozen=True)
class ToolSpec:
    """One callable the model may invoke.

    `handler` receives the parsed arguments dict and returns anything
    JSON-serialisable; the loop serialises it before feeding it back.
    """

    name: str
    description: str
    #: JSON Schema for the arguments object.
    parameters: dict
    handler: Callable[[dict], Awaitable[Any]]

    def as_wire(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def available() -> bool:
    """True when an endpoint is configured.

    A self-hosted vLLM needs no key, so the base URL alone is enough — requiring
    a key here would make on-premise deployment impossible.
    """
    return bool(settings.llm_base_url)


async def _post(payload: dict) -> dict:
    if not available():
        raise LLMUnavailable("LLM_BASE_URL is not configured")

    headers = {"Content-Type": "application/json"}
    if settings.llm_api_key:
        headers["Authorization"] = f"Bearer {settings.llm_api_key}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                f"{settings.llm_base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            body = response.json()
            _note_usage(body)
            return body
    except httpx.HTTPStatusError as exc:
        # Include the body: an OpenAI-compatible server puts the actual reason
        # (bad model name, context overflow) there, and without it the log says
        # only "400".
        body = exc.response.text[:500] if exc.response is not None else ""
        raise LLMUnavailable(f"inference request failed: {exc} {body}") from exc
    except Exception as exc:
        raise LLMUnavailable(f"inference request failed: {exc}") from exc


def _base_payload(
    messages: list[dict],
    model: str | None,
    temperature: float | None,
    max_tokens: int | None,
) -> dict[str, Any]:
    """Payload with only parameters every OpenAI-compatible server accepts.

    Two portability quirks are handled here rather than at each call site:

    * **`max_tokens` vs `max_completion_tokens`.** OpenAI's reasoning models (o1,
      o3, gpt-5 family) reject `max_tokens` outright. `LLM_MAX_TOKENS_PARAM`
      selects the field name so one env var covers both generations.
    * **Temperature.** Those same models accept only the default and 400 on
      anything else. `LLM_SUPPORTS_TEMPERATURE=false` omits the field entirely.

    Both default to the widely-supported choice, so a stock deployment against
    gpt-4o-mini, Llama on vLLM, Mistral or Gemini's compatibility layer works with
    no configuration beyond the key and URL.
    """
    payload: dict[str, Any] = {
        "model": model or settings.llm_model,
        "messages": messages,
    }

    limit = max_tokens or settings.llm_max_tokens
    payload[settings.llm_max_tokens_param] = limit

    if settings.llm_supports_temperature:
        payload["temperature"] = (
            temperature if temperature is not None else settings.llm_temperature
        )

    # Reasoning depth, when the provider supports it.
    #
    # Sent only when configured, because a server that does not know the parameter rejects the
    # request outright — the same negotiated-not-assumed discipline as `llm_max_tokens_param`.
    # `none` disables thinking on Gemini and the OpenAI reasoning family, which is what keeps a
    # short explanation from being truncated by a thinking budget it has to share.
    if settings.llm_reasoning_effort:
        payload["reasoning_effort"] = settings.llm_reasoning_effort

    return payload


def _first_message(body: dict) -> dict:
    choices = body.get("choices") or []
    if not choices:
        raise LLMUnavailable("inference returned no choices")
    return choices[0].get("message", {}) or {}


#: Tokens reported by the most recent `_post` on this task, or 0 when the provider
#: omitted a usage block. Read immediately after a call — see `last_usage`.
_LAST_USAGE: dict[int, int] = {}


def _note_usage(body: dict) -> None:
    """Stash the provider's reported token usage for the current task.

    Keyed by asyncio task id rather than a module global because several requests
    run concurrently — a plain global would attribute one subscriber's tokens to
    another. The entry is overwritten per call and read straight afterwards, so it
    does not accumulate beyond the number of in-flight tasks.
    """
    import asyncio

    from app.llm import budget

    tokens = budget.usage_from_response(body)
    if not tokens:
        return
    try:
        _LAST_USAGE[id(asyncio.current_task())] = tokens
    except RuntimeError:
        pass


def last_usage() -> int:
    """Tokens the provider reported for this task's most recent call, else 0.

    Pop rather than peek: a stale value read after a call that reported nothing
    would double-count.
    """
    import asyncio

    try:
        return _LAST_USAGE.pop(id(asyncio.current_task()), 0)
    except RuntimeError:
        return 0


def _refusal(message: dict, body: dict) -> str | None:
    """Provider-neutral refusal detection.

    Providers signal a safety decline differently and none of them raise:

    * OpenAI (structured outputs) sets `message.refusal` to a string.
    * Anthropic's OpenAI-compatible layer and several others set
      `finish_reason` to `content_filter`.
    * Azure OpenAI can return an empty content with a filter annotation.

    Returning a reason string rather than a bool so the caller can log *why*,
    which is what makes a refusal debuggable instead of just a missing advisory.
    """
    if message.get("refusal"):
        return str(message["refusal"])

    choices = body.get("choices") or [{}]
    finish = choices[0].get("finish_reason")
    if finish in ("content_filter", "safety"):
        return f"finish_reason={finish}"

    return None


def _is_truncated(body: dict) -> bool:
    """Whether the provider stopped at its token ceiling, not at a natural end.

    `finish_reason` is the provider's own signal for this and every OpenAI-compatible server sets
    it to `"length"` — checking it is authoritative where sniffing the text for a trailing period
    is not (a reasoning model can truncate exactly at a sentence boundary and still have cut the
    NEXT sentence entirely).
    """
    choices = body.get("choices") or [{}]
    return choices[0].get("finish_reason") == "length"


async def complete(
    messages: list[dict],
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict | None = None,
) -> str:
    """Single completion, no tools. Returns the assistant's text.

    Raises `LLMRefusal` when the provider declined, so a caller can fall back to
    something deterministic rather than shipping an empty string.
    """
    payload = _base_payload(messages, model, temperature, max_tokens)
    if response_format:
        payload["response_format"] = response_format

    body = await _post(payload)
    message = _first_message(body)

    reason = _refusal(message, body)
    if reason:
        raise LLMRefusal(reason)

    text = (message.get("content") or "").strip()

    # Checked AFTER refusal (a decline can also report finish_reason="length" on some providers'
    # compatibility layers) and BEFORE returning — a truncated string must never reach a caller
    # that only checks "is this empty", which every explanation surface does.
    if _is_truncated(body):
        raise LLMTruncated(
            f"response cut off at the token ceiling ({len(text)} chars returned)"
        )

    return text


async def complete_json(
    messages: list[dict],
    schema: dict,
    *,
    schema_name: str = "response",
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict:
    """Completion constrained to a JSON object, with a portability ladder.

    Structured-output support varies more than any other part of the
    `/v1/chat/completions` surface, so this tries three modes in descending order
    of strictness and takes the first the server accepts:

      1. `response_format: {type: "json_schema", ...}` — OpenAI 4o+, vLLM with a
         guided-decoding backend, LiteLLM proxying either. Actually enforced.
      2. `response_format: {type: "json_object"}` — most others, including
         Anthropic's and Gemini's compatibility layers. Valid JSON, unenforced
         shape.
      3. Neither — the schema is appended to the prompt and we parse. Works
         anywhere, guarantees nothing.

    A 400 on modes 1 or 2 is treated as "this server doesn't support it" and we
    step down. That specific inference is why `_post` includes the response body
    in its error: without it, an unsupported-parameter 400 is indistinguishable
    from a bad model name.

    The result is validated as a dict either way, so a caller that also checks its
    own required keys is safe on every rung of the ladder.
    """
    ladder: list[tuple[str, dict | None]] = []

    if settings.llm_structured_output_mode in ("auto", "json_schema"):
        ladder.append(
            (
                "json_schema",
                {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "schema": schema,
                        "strict": True,
                    },
                },
            )
        )
    if settings.llm_structured_output_mode in ("auto", "json_object"):
        ladder.append(("json_object", {"type": "json_object"}))
    # Always keep the prompt-only rung: it is the one that cannot 400.
    ladder.append(("prompt", None))

    last_error: Exception | None = None

    for mode, response_format in ladder:
        attempt_messages = messages
        if mode == "prompt":
            # No server-side constraint, so the instruction has to carry it.
            attempt_messages = messages + [
                {
                    "role": "system",
                    "content": (
                        "Reply with a single JSON object and nothing else — no "
                        "prose, no markdown fence. It must match this schema:\n"
                        f"{json.dumps(schema)}"
                    ),
                }
            ]

        try:
            text = await complete(
                attempt_messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
            )
        except LLMRefusal:
            # A decline is a decision, not an incompatibility. Stepping down the
            # ladder would just get declined again with a weaker constraint.
            raise
        except LLMUnavailable as exc:
            last_error = exc
            if _looks_unsupported(exc) and mode != "prompt":
                log.info(
                    "structured output mode rejected; stepping down",
                    extra={"mode": mode, "error": str(exc)[:200]},
                )
                continue
            raise

        try:
            data = json.loads(_strip_fence(text))
        except (TypeError, ValueError) as exc:
            last_error = exc
            if mode != "prompt":
                log.info(
                    "structured output was not valid JSON; stepping down",
                    extra={"mode": mode},
                )
                continue
            raise LLMUnavailable(
                f"model did not return JSON: {text[:200]!r}"
            ) from exc

        if not isinstance(data, dict):
            raise LLMUnavailable(f"expected a JSON object, got {type(data).__name__}")

        if mode != "json_schema":
            # Worth knowing in the logs: the shape was not enforced by the
            # server, so the caller's own key checks are what stood between this
            # and a KeyError.
            log.debug("structured output unenforced", extra={"mode": mode})
        return data

    raise LLMUnavailable(f"structured output failed on every mode: {last_error}")


def _looks_unsupported(error: Exception) -> bool:
    """Whether a 400 means "I don't support that parameter" rather than a real bug.

    Providers word this inconsistently, so match on the vocabulary they share.
    Deliberately narrow: a genuine bad-request must still surface as an error
    rather than being silently retried with a weaker constraint.
    """
    text = str(error).lower()
    if "400" not in text and "unsupported" not in text and "invalid" not in text:
        return False
    return any(
        marker in text
        for marker in (
            "response_format",
            "json_schema",
            "json_object",
            "not supported",
            "unsupported",
            "unrecognized",
            "unknown parameter",
            "extra inputs",
        )
    )


def _strip_fence(text: str) -> str:
    """Remove a ```json fence if the model added one.

    Only needed on the prompt-only rung, but harmless everywhere, and models on
    that rung add fences often enough that not handling it would make mode 3
    fail most of the time.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if len(lines) < 2:
        return stripped
    body = lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:]
    return "\n".join(body).strip()


@dataclass
class ToolLoopResult:
    """Outcome of a bounded tool-calling conversation."""

    text: str
    #: Full message list including tool calls and results — persisted for chat
    #: history and for auditing what Fahis actually looked at.
    messages: list[dict]
    #: Names of tools invoked, in order, with duplicates. Cheap provenance.
    tools_used: list[str]
    #: True when the loop hit `max_rounds` with the model still asking for tools.
    truncated: bool = False
    #: Tokens summed across every round, from the providers' own usage blocks.
    #: 0 when the provider omits usage (vLLM and some proxies do), in which case
    #: the caller falls back to its own estimate.
    tokens_used: int = 0


async def run_tool_loop(
    messages: list[dict],
    tools: list[ToolSpec],
    *,
    model: str | None = None,
    max_rounds: int | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> ToolLoopResult:
    """Call the model, execute requested tools, repeat until it answers.

    Bounded by `max_rounds`. On hitting the bound the loop makes one final call
    with tools withheld, forcing a text answer from what it already has — better
    than returning nothing, and better than looping.
    """
    rounds = max_rounds or settings.llm_max_tool_rounds
    by_name = {t.name: t for t in tools}
    wire_tools = [t.as_wire() for t in tools]
    history = list(messages)
    used: list[str] = []
    tokens = 0

    for round_index in range(rounds):
        payload = _base_payload(history, model, temperature, max_tokens)
        if wire_tools:
            payload["tools"] = wire_tools
            payload["tool_choice"] = "auto"

        body = await _post(payload)
        # Accumulate per round: a 3-round loop costs roughly 3× a single call,
        # and attributing only the last round would understate it by that factor.
        tokens += last_usage()
        message = _first_message(body)
        calls = message.get("tool_calls") or []

        # No tool calls — the model has answered.
        if not calls:
            return ToolLoopResult(
                text=(message.get("content") or "").strip(),
                messages=history + [message],
                tools_used=used,
                tokens_used=tokens,
            )

        history.append(message)

        for call in calls:
            function = call.get("function", {}) or {}
            name = function.get("name", "")
            used.append(name)

            raw_args = function.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except (TypeError, ValueError):
                args = {}

            spec = by_name.get(name)
            if spec is None:
                # Tell the model rather than raising — it can pick a real tool on
                # the next round.
                result: Any = {"error": f"unknown tool: {name}"}
            else:
                try:
                    result = await spec.handler(args)
                except Exception as exc:
                    log.warning(
                        "tool call failed",
                        extra={"tool": name, "error": str(exc)},
                    )
                    # A tool failure is information, not an outage. The model
                    # should say it could not check something.
                    result = {"error": f"tool failed: {exc}"}

            history.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "name": name,
                    "content": json.dumps(result, default=str)[
                        : settings.llm_tool_result_max_chars
                    ],
                }
            )

        log.debug(
            "tool round complete",
            extra={"round": round_index + 1, "calls": [c.get("function", {}).get("name") for c in calls]},
        )

    # Bound reached. One more call with tools withheld so the model must answer
    # from what it has.
    log.info("tool loop hit round limit; forcing final answer", extra={"rounds": rounds})
    final = await _post(
        _base_payload(
            history
            + [
                {
                    "role": "system",
                    "content": (
                        "You have reached the tool-use limit. Answer now using only "
                        "what you have already gathered. Say plainly what you could "
                        "not determine."
                    ),
                }
            ],
            model,
            temperature,
            max_tokens,
        )
    )
    tokens += last_usage()
    text = (_first_message(final).get("content") or "").strip()
    return ToolLoopResult(
        text=text,
        messages=history,
        tools_used=used,
        truncated=True,
        tokens_used=tokens,
    )
