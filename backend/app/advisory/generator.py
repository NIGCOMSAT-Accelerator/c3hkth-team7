"""Advisory text generation.

A risk score is not an alert. A farmer needs to know what to do before Friday,
in their language, in under 300 characters if it is going out over satellite
broadcast. That translation is what this module does.

**Two hard rules, both enforced structurally rather than by hoping the model
complies. These are unchanged by the provider refactor and must stay that way:**

1. **Grounding.** The model receives only the Oracle's `evidence` list and may
   not introduce numbers of its own. Anything it can't support from that list
   it must omit.
2. **Fallback.** If no provider is configured, the model refuses, or the call
   fails, we emit a deterministic template advisory. An alert that reaches
   someone in plain language always beats a blank one.

**Provider portability.** This module used to be hard-locked to the Anthropic SDK
and six of its vendor-specific features (`beta.messages.create`,
`output_config.effort`, `betas=[...]`, `fallbacks`, `stop_reason == "refusal"`,
`stop_details.category`), which meant the safety-critical path could not switch
providers even though `app/llm/` could. It now routes through `app/llm/` — plain
`/v1/chat/completions` — so changing frontier provider is `LLM_BASE_URL` plus
`LLM_API_KEY`.

`ADVISORY_PROVIDER` selects:

    "auto"      (default) portable path if LLM_BASE_URL is set, else the
                Anthropic SDK if ANTHROPIC_API_KEY is set, else template.
    "openai"    force the portable path. Fail to template if unconfigured.
    "anthropic" force the native SDK, which keeps server-side fallback and
                effort control — features the portable path cannot express.
    "template"  no generation at all. Deterministic English output.

The native Anthropic path is retained rather than deleted because it offers two
things genuinely worth having on a safety-critical call: `fallbacks="default"`
re-serves a refusal from another model inside the same request, and `effort`
tunes reasoning depth. Neither has an OpenAI-compatible equivalent. Portability
is the default; that path is the opt-in upgrade.
"""

from __future__ import annotations

import json

from app.config import settings
from app.llm import client as llm
from app.logging_config import get_logger
from app.models.enums import HazardType, Severity
from app.models.schemas import Advisory, RiskAssessment, Subscriber

log = get_logger(__name__)

#: Lazily constructed, and only when the native Anthropic path is selected. The
#: import is inside the function so `anthropic` stays an optional dependency —
#: a deployment using OpenAI or a local vLLM should not need it installed.
_client = None

# JSON Schema the model must fill. Structured output means we never parse prose.
_ADVISORY_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "string",
            "description": "Under 140 characters. States the hazard and the timeframe.",
        },
        "body": {
            "type": "string",
            "description": "2-4 short sentences of plain-language explanation.",
        },
        "actions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "2-4 concrete steps, each starting with a verb.",
        },
        "broadcast_text": {
            "type": "string",
            "description": (
                "Under 240 characters, for one-way satellite broadcast to a "
                "basic handset. Must stand alone with no link and no reply path."
            ),
        },
    },
    "required": ["headline", "body", "actions", "broadcast_text"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = """\
You write early-warning advisories for SHELTER, a satellite hazard warning \
service for Sub-Saharan Africa. Your readers are smallholder farmers, \
cooperative officers, district emergency staff and public-health officers in \
Nigeria and neighbouring countries. Many will read the message on a basic \
handset, some over a one-way satellite broadcast with no way to ask a \
follow-up question.

Grounding rules — these are absolute:
- Use ONLY the facts in the EVIDENCE list. Do not add statistics, place names, \
dates, or rainfall figures that are not there.
- If the evidence is thin, write a shorter advisory. Never pad with invented \
specifics.
- If the evidence says imagery or rainfall data was unavailable, say so plainly \
rather than implying a confident reading.

Writing rules:
- Lead with what is happening and by when. The reader decides in one line \
whether this concerns them.
- Every action must be something the reader can actually do with the resources \
a smallholder farm or district office has. "Harvest the low-lying plots first" \
is useful; "deploy flood defences" is not.
- Plain words. No jargon, no NDVI, no decibels, no model names.
- Do not promise certainty. This is a forecast.
- Never invent a phone number, URL, agency name, or deadline.
"""


def _anthropic_ready() -> bool:
    """Whether the native path can actually run.

    Needs a key **and** the optional SDK. Checking the import here rather than
    discovering it at call time matters: `anthropic` is optional in
    `requirements.txt`, so a deployment can legitimately have the key set (copied
    from a template `.env`) with the package absent. Without this check every
    advisory would raise, log a traceback, and only then fall back — noisy, and it
    hides real failures.
    """
    if not settings.anthropic_api_key:
        return False

    import importlib.util

    return importlib.util.find_spec("anthropic") is not None


def _resolve_provider() -> str:
    """Which generation path to use: "openai", "anthropic" or "template".

    `auto` prefers the portable path, so a deployment that sets only
    `LLM_BASE_URL` + `LLM_API_KEY` gets generation without also having to know
    about `ANTHROPIC_API_KEY`.
    """
    configured = settings.advisory_provider.lower()

    if configured == "template":
        return "template"

    if configured == "openai":
        return "openai" if llm.available() else "template"

    if configured == "anthropic":
        if _anthropic_ready():
            return "anthropic"
        if settings.anthropic_api_key:
            log.warning(
                "ADVISORY_PROVIDER=anthropic but the anthropic SDK is not "
                "installed; falling back to template. Install it, or set "
                "ADVISORY_PROVIDER=openai with LLM_BASE_URL."
            )
        return "template"

    # auto — portable first, native second, template last.
    if llm.available():
        return "openai"
    if _anthropic_ready():
        return "anthropic"
    if settings.anthropic_api_key:
        log.warning(
            "ANTHROPIC_API_KEY is set but the anthropic SDK is not installed. "
            "Set LLM_BASE_URL to use the portable path, or install the SDK."
        )
    return "template"


async def generate(
    assessment: RiskAssessment, subscriber: Subscriber
) -> Advisory:
    """Produce an advisory, falling back to a template on any failure.

    Rule 2 lives here and applies to every provider: nothing this function can do
    results in a caller receiving no advisory.
    """
    provider = _resolve_provider()

    if provider == "template":
        log.info("no advisory provider configured; using template")
        return _template(assessment, subscriber)

    try:
        if provider == "openai":
            return await _generate_openai(assessment, subscriber)
        return await _generate_anthropic(assessment, subscriber)
    except llm.LLMRefusal as exc:
        # A decline recurs on retry, so go straight to the deterministic path.
        log.warning("advisory generation refused", extra={"reason": str(exc)[:200]})
        return _template(assessment, subscriber)
    except llm.LLMTruncated as exc:
        # Greppable separately from the generic failure below: a pattern of these means
        # `advisory_max_tokens` is too low for the model actually configured, not that the
        # provider or the prompt is broken. See `app/explain/base.py::LLMTruncated`.
        log.warning(
            "advisory generation truncated at the token ceiling; using template",
            extra={"provider": provider, "reason": str(exc), "max_tokens": settings.advisory_max_tokens},
        )
        return _template(assessment, subscriber)
    except Exception:
        log.exception(
            "advisory generation failed; using template",
            extra={"provider": provider},
        )
        return _template(assessment, subscriber)


# --------------------------------------------------------------------------- #
# Portable path — any OpenAI-compatible provider
# --------------------------------------------------------------------------- #


async def _generate_openai(
    assessment: RiskAssessment, subscriber: Subscriber
) -> Advisory:
    """Generate via `app/llm/`. No vendor extensions.

    `complete_json` negotiates structured output down from strict `json_schema` to
    prompt-only, so this works against a provider with no schema support at all —
    the payoff being that `_coerce` never assumes the shape was enforced.
    """
    data = await llm.complete_json(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_prompt(assessment, subscriber)},
        ],
        _ADVISORY_SCHEMA,
        schema_name="advisory",
        model=settings.advisory_model_openai or None,
        max_tokens=settings.advisory_max_tokens,
    )
    return _coerce(data, assessment, subscriber, generated_by=_openai_label())


def _openai_label() -> str:
    """Provenance string for `Advisory.generated_by`.

    Records the model actually used, which on the portable path may be a
    deployment-specific name — worth having on the dashboard when several
    providers are in play across environments.
    """
    return settings.advisory_model_openai or settings.llm_model


# --------------------------------------------------------------------------- #
# Native Anthropic path — opt-in, for server-side fallback and effort control
# --------------------------------------------------------------------------- #


def _get_anthropic_client():
    global _client
    if not settings.anthropic_api_key:
        return None
    if _client is None:
        # Imported here so `anthropic` is an optional dependency: a deployment on
        # OpenAI or a local vLLM must not need it installed.
        from anthropic import AsyncAnthropic

        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


async def _generate_anthropic(
    assessment: RiskAssessment, subscriber: Subscriber
) -> Advisory:
    """Generate via the Anthropic SDK, using features the portable path lacks.

    Kept because two of them are genuinely valuable on a safety-critical call:
    `fallbacks="default"` re-serves a refusal from another model inside the same
    request, and `effort` tunes reasoning depth. Neither has an OpenAI-compatible
    equivalent, which is why this path exists rather than being deleted.
    """
    client = _get_anthropic_client()
    if client is None:
        return _template(assessment, subscriber)

    response = await client.beta.messages.create(
        model=settings.advisory_model,
        max_tokens=settings.advisory_max_tokens,
        system=_SYSTEM_PROMPT,
        output_config={
            "effort": settings.advisory_effort,
            "format": {"type": "json_schema", "schema": _ADVISORY_SCHEMA},
        },
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": _build_prompt(assessment, subscriber)}],
    )

    # A refusal returns HTTP 200 with an empty or partial content array, so this
    # has to be checked before touching `content`.
    if response.stop_reason == "refusal":
        raise llm.LLMRefusal(
            str(getattr(response.stop_details, "category", "refused"))
        )

    # Anthropic's own name for hitting the token ceiling — `client.py`'s `_is_truncated` checks
    # the OpenAI-compatible `finish_reason == "length"`, which this SDK path never sets, so it
    # needs its own check rather than inheriting that one. A truncated response here is usually
    # invalid JSON and would fail at `json.loads` below anyway, but raising explicitly gives a
    # clean, greppable log line instead of a parse-error trace that looks like a different bug.
    if response.stop_reason == "max_tokens":
        raise llm.LLMTruncated(
            f"response cut off at max_tokens={settings.advisory_max_tokens}"
        )

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        return _template(assessment, subscriber)

    return _coerce(
        json.loads(text), assessment, subscriber, generated_by=settings.advisory_model
    )


# --------------------------------------------------------------------------- #
# Shared result handling
# --------------------------------------------------------------------------- #


def _coerce(
    data: dict,
    assessment: RiskAssessment,
    subscriber: Subscriber,
    *,
    generated_by: str,
) -> Advisory:
    """Build an `Advisory` from a model's JSON, tolerating a missing field.

    Written defensively on purpose. On the portable path, `complete_json` may have
    fallen back to a mode where the schema was *not* enforced server-side, so a
    required key can genuinely be absent. Rather than raise — which would discard a
    usable advisory over one missing field — fall back per field, and to the
    template only when the headline is unusable.

    Truncation limits match the Pydantic constraints on `Advisory`, so validation
    at the API boundary cannot fail on a model that ran long.
    """
    headline = str(data.get("headline") or "").strip()
    if not headline:
        log.warning("model returned no headline; using template")
        return _template(assessment, subscriber)

    body = str(data.get("body") or "").strip()
    if not body:
        # A headline with no body is still deliverable; an empty body is not worth
        # discarding the headline over.
        body = headline

    raw_actions = data.get("actions")
    actions = (
        [str(a).strip() for a in raw_actions if str(a).strip()][:4]
        if isinstance(raw_actions, list)
        else []
    )
    if not actions:
        # Fall back to the curated table for this hazard rather than shipping an
        # advisory with no instruction — the actions are the actionable part.
        actions = _HAZARD_ACTIONS.get(assessment.hazard, [])

    broadcast = str(data.get("broadcast_text") or "").strip()
    if not broadcast:
        broadcast = f"SHELTER {assessment.severity.value.upper()}: {headline}"
    # Truncate on ENCODED BYTES, not characters: advisories may be in multi-byte
    # languages and the gateway rejects an overrun burst rather than trimming it.
    broadcast = _truncate_bytes(broadcast, settings.nigcomsat_max_payload_bytes)

    return Advisory(
        headline=headline[:140],
        body=body,
        actions=actions,
        broadcast_text=broadcast,
        language=subscriber.language,
        generated_by=generated_by,
    )


def _truncate_bytes(text: str, limit: int) -> str:
    """Cut to a byte budget without leaving a partial character."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore").rstrip()


def _build_prompt(assessment: RiskAssessment, subscriber: Subscriber) -> str:
    evidence = "\n".join(f"- {fact}" for fact in assessment.evidence) or "- (none)"
    cascade = (
        ", ".join(h.value.replace("_", " ") for h in assessment.cascade) or "none"
    )

    return f"""\
Write an advisory for this alert.

READER
Type: {subscriber.kind.value.replace("_", " ")}
Language: write the advisory in {subscriber.language} (ISO-639-1 code)

ALERT
Area: {assessment.aoi_name}
Hazard: {assessment.hazard.value.replace("_", " ")}
Severity: {assessment.severity.value}
Lead time: {assessment.lead_time_days} days
Expected to trigger next: {cascade}

EVIDENCE (the only facts you may use)
{evidence}

Return the four fields defined by the schema. The broadcast_text must stand \
alone on a basic handset with no link and no reply path."""


# --------------------------------------------------------------------------- #
# Deterministic fallback
# --------------------------------------------------------------------------- #

_HAZARD_LABEL: dict[HazardType, str] = {
    HazardType.FLOOD_INUNDATION: "Flooding detected",
    HazardType.FLOOD_FORECAST: "Flooding expected",
    HazardType.CROP_WATERLOGGING: "Waterlogged cropland",
    HazardType.CROP_DROUGHT_STRESS: "Crop drought stress",
    HazardType.CROP_VEGETATION_ANOMALY: "Crop condition changing",
    HazardType.MALARIA_RISK: "Raised malaria risk",
}

_HAZARD_ACTIONS: dict[HazardType, list[str]] = {
    HazardType.FLOOD_INUNDATION: [
        "Move livestock and stored grain to higher ground today.",
        "Harvest any mature crop on low-lying plots first.",
        "Clear field drains and channel outlets while access is still possible.",
    ],
    HazardType.FLOOD_FORECAST: [
        "Clear field drains and channel outlets before the rain arrives.",
        "Move stored grain and inputs off the ground.",
        "Plan to harvest low-lying plots early if the crop is near maturity.",
    ],
    HazardType.CROP_WATERLOGGING: [
        "Open drainage furrows on the worst-affected plots.",
        "Delay fertiliser application until the soil drains.",
        "Inspect roots for rot before deciding whether to harvest early.",
    ],
    HazardType.CROP_DROUGHT_STRESS: [
        "Prioritise irrigation for plots at flowering or grain-fill.",
        "Mulch exposed soil to slow moisture loss.",
        "Hold off on top-dressing fertiliser until moisture returns.",
    ],
    HazardType.CROP_VEGETATION_ANOMALY: [
        "Walk the affected plots and check for pests and disease.",
        "Compare growth against neighbouring fields on the same soil.",
    ],
    HazardType.MALARIA_RISK: [
        "Make sure treated nets are in use, especially for children.",
        "Drain standing water near dwellings where practical.",
        "Report fever cases to the nearest health post early.",
    ],
}

_SEVERITY_URGENCY: dict[Severity, str] = {
    Severity.EMERGENCY: "Act today.",
    Severity.WARNING: "Act within 48 hours.",
    Severity.WATCH: "Prepare this week.",
    Severity.ADVISORY: "Keep watch.",
    Severity.INFO: "For information.",
}


def _template(assessment: RiskAssessment, subscriber: Subscriber) -> Advisory:
    """Deterministic advisory built from the assessment alone.

    English-only by design: a machine-translated safety instruction is worse
    than an English one the reader can seek help with. When Claude is available
    it writes in the subscriber's language; this path does not pretend to.
    """
    label = _HAZARD_LABEL.get(assessment.hazard, "Hazard detected")
    urgency = _SEVERITY_URGENCY.get(assessment.severity, "")
    actions = _HAZARD_ACTIONS.get(assessment.hazard, [])

    headline = f"{label} — {assessment.aoi_name}. {urgency}"[:140]

    evidence_text = " ".join(f"{fact}." for fact in assessment.evidence[:3])
    body = (
        f"{label} in {assessment.aoi_name}, with a {assessment.lead_time_days}-day "
        f"outlook. {evidence_text} "
        f"Severity is rated {assessment.severity.value}."
    ).strip()

    if assessment.cascade:
        followers = ", ".join(
            h.value.replace("_", " ") for h in assessment.cascade
        )
        body += f" This may lead to {followers} in the weeks that follow."

    broadcast = f"SHELTER {assessment.severity.value.upper()}: {label} - {assessment.aoi_name}. {urgency}"
    if actions:
        broadcast += f" {actions[0]}"

    return Advisory(
        headline=headline,
        body=body,
        actions=actions,
        broadcast_text=broadcast[: settings.nigcomsat_max_payload_bytes],
        language="en",
        generated_by="template",
    )
