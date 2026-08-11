"""OpenAI-compatible inference transport — the portable path.

**Provider-agnostic by construction.** Everything here speaks only
`POST /v1/chat/completions` with parameters common to every server implementing
that shape, so switching frontier providers is two environment variables:

    LLM_BASE_URL=https://api.openai.com/v1          LLM_MODEL=gpt-4o
    LLM_BASE_URL=https://api.anthropic.com/v1       LLM_MODEL=claude-opus-5
    LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
                                                    LLM_MODEL=gemini-2.5-pro
    LLM_BASE_URL=https://api.deepseek.com/v1        LLM_MODEL=deepseek-chat
    LLM_BASE_URL=http://localhost:8000/v1           LLM_MODEL=<local>   (vLLM)

Nothing in this package uses a vendor extension. Where support genuinely differs
across providers, the difference is either negotiated at runtime
(`complete_json`'s three-mode ladder) or exposed as a setting
(`LLM_MAX_TOKENS_PARAM`, `LLM_SUPPORTS_TEMPERATURE`) rather than assumed.

`tests/test_llm_portability.py` asserts this: any vendor-specific parameter name
appearing in this package fails the build.

Used by Fahis adjudication, Herald chat, and — since the provider-agnostic
refactor — advisory generation via `app/advisory/generator.py`.
"""

from app.llm.client import (
    LLMRefusal,
    LLMUnavailable,
    ToolSpec,
    available,
    complete,
    complete_json,
    run_tool_loop,
)

__all__ = [
    "LLMRefusal",
    "LLMUnavailable",
    "ToolSpec",
    "available",
    "complete",
    "complete_json",
    "run_tool_loop",
]
