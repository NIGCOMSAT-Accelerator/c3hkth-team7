"""Provider portability contract.

The promise: **switching frontier model providers is `LLM_BASE_URL` plus
`LLM_API_KEY`.** Nothing else.

That promise is easy to break by accident — one convenient vendor parameter in a
payload and the stack silently only works against one provider until someone tries
to move. These tests make that a build failure.

They also pin the advisory generator's two hard rules across the refactor, because
those are the ones with a safety consequence: grounding (evidence only, no invented
numbers) and the deterministic template fallback.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from app.advisory import generator
from app.config import Settings, settings
from app.llm import client as llm

LLM_PACKAGE = pathlib.Path("app/llm")


# --------------------------------------------------------------------------- #
# No vendor extensions in the portable path
# --------------------------------------------------------------------------- #

#: Parameters that exist on exactly one vendor's API. Any of these in app/llm/
#: breaks the switch-provider-with-two-env-vars promise.
_VENDOR_ONLY_PARAMS = (
    # Anthropic Messages API
    "output_config",
    "stop_reason",
    "stop_details",
    "betas",
    "anthropic_version",
    "system=",  # Anthropic takes `system` as a top-level param, not a message
    # Google Gemini native
    "generationConfig",
    "safetySettings",
    "contents",
    # Cohere / AI21 / misc
    "preamble",
    "chat_history",
    "prompt_truncation",
)


def test_llm_package_uses_no_vendor_only_parameters():
    """The core portability guard."""
    for path in LLM_PACKAGE.glob("*.py"):
        code = _code_only(path)
        for param in _VENDOR_ONLY_PARAMS:
            assert param not in code, (
                f"{path} references {param!r}, which is vendor-specific. "
                "app/llm must stay portable across OpenAI-compatible providers."
            )


def test_llm_package_imports_no_vendor_sdk():
    """httpx only. A vendor SDK would smuggle in vendor assumptions."""
    for path in LLM_PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        for sdk in ("anthropic", "openai", "google", "cohere", "mistralai", "boto3"):
            assert sdk not in imported, f"{path} imports the {sdk} SDK"


def test_llm_targets_only_chat_completions():
    """One endpoint, the de-facto standard one. `/v1/responses` and
    `/v1/messages` are vendor-specific and would not port."""
    source = (LLM_PACKAGE / "client.py").read_text()

    assert "/chat/completions" in source
    for vendor_endpoint in ("/v1/messages", "/v1/responses", ":generateContent"):
        assert vendor_endpoint not in source


def test_auth_uses_bearer_header():
    """`Authorization: Bearer` is what every OpenAI-compatible server accepts.
    `x-api-key` and `anthropic-version` are vendor headers."""
    source = (LLM_PACKAGE / "client.py").read_text()

    assert "Bearer" in source
    assert "x-api-key" not in source.lower()
    assert "anthropic-version" not in source.lower()


# --------------------------------------------------------------------------- #
# Compatibility knobs
# --------------------------------------------------------------------------- #


def test_max_tokens_param_is_configurable():
    """OpenAI's reasoning models require `max_completion_tokens` and reject
    `max_tokens`. Hardcoding either would break one generation of models."""
    assert Settings.model_fields["llm_max_tokens_param"].default == "max_tokens"

    payload = llm._base_payload([{"role": "user", "content": "x"}], None, None, 100)
    assert payload["max_tokens"] == 100


def test_max_tokens_param_switches(monkeypatch):
    monkeypatch.setattr(settings, "llm_max_tokens_param", "max_completion_tokens")

    payload = llm._base_payload([{"role": "user", "content": "x"}], None, None, 100)

    assert payload["max_completion_tokens"] == 100
    assert "max_tokens" not in payload


def test_temperature_can_be_omitted(monkeypatch):
    """Reasoning models 400 on any explicit temperature, so it must be omittable
    rather than merely settable."""
    monkeypatch.setattr(settings, "llm_supports_temperature", False)

    payload = llm._base_payload([{"role": "user", "content": "x"}], None, 0.5, 100)

    assert "temperature" not in payload


def test_temperature_included_by_default():
    payload = llm._base_payload([{"role": "user", "content": "x"}], None, 0.5, 100)
    assert payload["temperature"] == 0.5


def test_base_payload_carries_nothing_else():
    """Only universally-accepted fields. An extra key would 400 on strict
    providers that reject unknown parameters."""
    from app.config import settings

    # `reasoning_effort` is opt-in and this developer's `.env` sets it, so it is neutralised here
    # — this test is about the universally-accepted BASE, which is what a provider with no
    # extensions must receive.
    original = settings.llm_reasoning_effort
    try:
        settings.llm_reasoning_effort = ""
        payload = llm._base_payload([{"role": "user", "content": "x"}], "m", 0.1, 10)
    finally:
        settings.llm_reasoning_effort = original

    assert set(payload) == {"model", "messages", "temperature", "max_tokens"}


def test_reasoning_effort_is_omitted_unless_configured():
    """`reasoning_effort` is NOT universally accepted, so it must be opt-in.

    A provider that does not know the parameter rejects the whole request rather than ignoring
    it — the same reason `max_tokens` vs `max_completion_tokens` is a setting instead of a guess.
    So the default must be blank and the key absent, and it appears only when a deployment names
    a value.

    It matters enough to test because the payoff is large and the failure is total: measured
    against Gemini 2.5 Flash, `none` cut an explanation from ~1200-2200 completion tokens with
    frequent truncation to 37 tokens with none — but sending it to a provider that rejects it
    would break every LLM call on that deployment.
    """
    from app.config import Settings, settings

    # The CODE default, read from the field rather than from the live `settings` — this test must
    # assert what a fresh deployment gets, and the developer running it has a real `.env` where
    # the value is deliberately set.
    assert Settings.model_fields["llm_reasoning_effort"].default == "", (
        "the default must be blank so a provider that rejects the parameter never receives it"
    )

    original = settings.llm_reasoning_effort
    try:
        settings.llm_reasoning_effort = ""
        blank = llm._base_payload([{"role": "user", "content": "x"}], "m", 0.1, 10)
        assert "reasoning_effort" not in blank, "blank must omit the key entirely"

        settings.llm_reasoning_effort = "none"
        configured = llm._base_payload([{"role": "user", "content": "x"}], "m", 0.1, 10)
        assert configured["reasoning_effort"] == "none"
    finally:
        settings.llm_reasoning_effort = original


# --------------------------------------------------------------------------- #
# Structured output negotiation
# --------------------------------------------------------------------------- #


def test_structured_output_degrades_through_three_modes():
    """Schema support varies more than anything else on this endpoint, so the
    ladder must end on a rung that cannot fail."""
    source = inspect.getsource(llm.complete_json)

    assert "json_schema" in source
    assert "json_object" in source
    assert '"prompt"' in source
    # The prompt rung is appended unconditionally — it is the fallback that works
    # against a provider with no structured-output support at all.
    assert 'ladder.append(("prompt", None))' in source


def test_unsupported_parameter_detection_is_narrow():
    """Must catch "I don't support response_format" without swallowing genuine
    bad requests — otherwise a real bug gets silently retried."""
    assert llm._looks_unsupported(
        Exception("400 Bad Request: response_format is not supported")
    )
    assert llm._looks_unsupported(Exception("400 unknown parameter: json_schema"))

    # Real errors must not be mistaken for incompatibility.
    assert not llm._looks_unsupported(Exception("401 Unauthorized"))
    assert not llm._looks_unsupported(Exception("429 rate limit exceeded"))
    assert not llm._looks_unsupported(Exception("500 internal server error"))
    assert not llm._looks_unsupported(Exception("connection refused"))


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        '  {"a": 1}  ',
    ],
)
def test_fence_stripping(text):
    """Models on the prompt-only rung add markdown fences often enough that not
    handling it would make that mode fail most of the time."""
    import json

    assert json.loads(llm._strip_fence(text)) == {"a": 1}


def test_structured_output_mode_setting_is_valid():
    default = Settings.model_fields["llm_structured_output_mode"].default
    assert default in ("auto", "json_schema", "json_object", "prompt")


# --------------------------------------------------------------------------- #
# Provider-neutral refusal handling
# --------------------------------------------------------------------------- #


def test_refusal_detected_from_message_field():
    """OpenAI structured outputs set `message.refusal`."""
    assert llm._refusal({"refusal": "I cannot help"}, {}) == "I cannot help"


def test_refusal_detected_from_finish_reason():
    """Anthropic's and others' compatibility layers use `content_filter`."""
    body = {"choices": [{"finish_reason": "content_filter"}]}
    assert llm._refusal({}, body) is not None


def test_normal_completion_is_not_a_refusal():
    body = {"choices": [{"finish_reason": "stop"}]}
    assert llm._refusal({"content": "hello"}, body) is None


def test_refusal_is_distinct_from_unavailable():
    """A refusal recurs on retry and should go straight to the deterministic path;
    an unavailable endpoint may not."""
    assert llm.LLMRefusal is not llm.LLMUnavailable
    assert not issubclass(llm.LLMRefusal, llm.LLMUnavailable)


# --------------------------------------------------------------------------- #
# Advisory generator — provider selection
# --------------------------------------------------------------------------- #


def test_advisory_provider_default_is_auto():
    assert Settings.model_fields["advisory_provider"].default == "auto"


def test_auto_prefers_the_portable_path(monkeypatch):
    """So a deployment that sets only LLM_* gets generation without needing to
    know about ANTHROPIC_API_KEY."""
    monkeypatch.setattr(settings, "advisory_provider", "auto")
    monkeypatch.setattr(settings, "llm_base_url", "https://api.openai.com/v1")
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-xxx")

    assert generator._resolve_provider() == "openai"


def test_auto_falls_back_to_anthropic_then_template(monkeypatch):
    monkeypatch.setattr(settings, "advisory_provider", "auto")
    monkeypatch.setattr(settings, "llm_base_url", None)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-xxx")
    assert generator._resolve_provider() == "anthropic"

    monkeypatch.setattr(settings, "anthropic_api_key", None)
    assert generator._resolve_provider() == "template"


def test_forced_provider_degrades_to_template_not_the_other_provider(monkeypatch):
    """`openai` with nothing configured must not silently use Anthropic — an
    explicit choice that quietly routes elsewhere is worse than a template."""
    monkeypatch.setattr(settings, "advisory_provider", "openai")
    monkeypatch.setattr(settings, "llm_base_url", None)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-xxx")

    assert generator._resolve_provider() == "template"


def test_template_provider_disables_generation(monkeypatch):
    monkeypatch.setattr(settings, "advisory_provider", "template")
    monkeypatch.setattr(settings, "llm_base_url", "https://api.openai.com/v1")

    assert generator._resolve_provider() == "template"


def test_advisory_provider_setting_accepts_only_known_values():
    default = Settings.model_fields["advisory_provider"].default
    assert default in ("auto", "openai", "anthropic", "template")


def test_missing_anthropic_sdk_degrades_to_template(monkeypatch):
    """`anthropic` is optional in requirements.txt, so a deployment can have the
    key set (copied from a template .env) with the package absent.

    That must resolve to template up front, not raise on every advisory and fall
    back only after logging a traceback — which would be noisy and would bury real
    failures.
    """
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-xxx")
    monkeypatch.setattr(settings, "llm_base_url", None)
    # Simulate the SDK being absent.
    monkeypatch.setattr(generator, "_anthropic_ready", lambda: False)

    monkeypatch.setattr(settings, "advisory_provider", "anthropic")
    assert generator._resolve_provider() == "template"

    monkeypatch.setattr(settings, "advisory_provider", "auto")
    assert generator._resolve_provider() == "template"


def test_anthropic_readiness_checks_both_key_and_sdk():
    """A key alone is not enough, and neither is the SDK alone."""
    source = inspect.getsource(generator._anthropic_ready)

    assert "anthropic_api_key" in source
    assert "find_spec" in source


# --------------------------------------------------------------------------- #
# Advisory generator — the two hard rules survive the refactor
# --------------------------------------------------------------------------- #


def test_anthropic_sdk_import_is_lazy():
    """`anthropic` must be optional: a deployment on OpenAI or a local vLLM
    should not need it installed."""
    tree = ast.parse(pathlib.Path("app/advisory/generator.py").read_text())

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "anthropic"
        ):
            assert node.col_offset > 0, (
                "the anthropic import must be inside a function so the SDK stays "
                "an optional dependency"
            )


def test_grounding_rule_is_intact():
    """Rule 1. The prompt must still restrict the model to the evidence list."""
    prompt = generator._SYSTEM_PROMPT

    assert "ONLY the facts in the EVIDENCE list" in prompt
    assert "Do not add statistics" in prompt


def test_prompt_passes_only_evidence(monkeypatch):
    """Structural check that no other assessment field reaches the model.

    Exposure, soil and health numbers exist on the assessment but are NOT in the
    evidence list; leaking them would let the model cite figures the Oracle chose
    not to publish.
    """
    from app.models.enums import HazardType, Severity
    from app.models.schemas import ExposureSummary, RiskAssessment, Subscriber

    assessment = RiskAssessment(
        aoi_id="aoi_1",
        aoi_name="Test Area",
        hazard=HazardType.FLOOD_INUNDATION,
        severity=Severity.WARNING,
        score=0.7,
        confidence=0.8,
        evidence=["31% of the area is under standing water"],
        exposure=ExposureSummary(population=99_999),
    )
    prompt = generator._build_prompt(assessment, Subscriber(name="T"))

    assert "31% of the area is under standing water" in prompt
    # The population figure is not in `evidence`, so it must not be in the prompt.
    assert "99,999" not in prompt
    assert "99999" not in prompt


def test_template_fallback_exists_for_every_hazard():
    """Rule 2. No configuration may leave a subscriber with no advisory."""
    from app.models.enums import HazardType

    for hazard in HazardType:
        assert hazard in generator._HAZARD_LABEL, f"no label for {hazard.value}"
        assert hazard in generator._HAZARD_ACTIONS, f"no actions for {hazard.value}"


async def test_generate_returns_template_when_unconfigured(monkeypatch):
    from app.models.enums import HazardType, Severity
    from app.models.schemas import RiskAssessment, Subscriber

    monkeypatch.setattr(settings, "advisory_provider", "auto")
    monkeypatch.setattr(settings, "llm_base_url", None)
    monkeypatch.setattr(settings, "anthropic_api_key", None)

    advisory = await generator.generate(
        RiskAssessment(
            aoi_id="aoi_1",
            aoi_name="Test Area",
            hazard=HazardType.FLOOD_INUNDATION,
            severity=Severity.WARNING,
            score=0.7,
            confidence=0.8,
            evidence=["31% of the area is under standing water"],
        ),
        Subscriber(name="Test"),
    )

    assert advisory.generated_by == "template"
    assert advisory.headline
    assert advisory.actions


# --------------------------------------------------------------------------- #
# Result coercion — needed because the schema may not have been enforced
# --------------------------------------------------------------------------- #


def _assessment():
    from app.models.enums import HazardType, Severity
    from app.models.schemas import RiskAssessment

    return RiskAssessment(
        aoi_id="aoi_1",
        aoi_name="Test Area",
        hazard=HazardType.FLOOD_INUNDATION,
        severity=Severity.WARNING,
        score=0.7,
        confidence=0.8,
        evidence=["31% flooded"],
    )


def test_coerce_tolerates_missing_optional_fields():
    """On a provider without schema enforcement, a key can genuinely be absent.
    Discarding a usable advisory over one missing field would be wrong."""
    from app.models.schemas import Subscriber

    advisory = generator._coerce(
        {"headline": "Flooding in Test Area"},
        _assessment(),
        Subscriber(name="T"),
        generated_by="gpt-4o",
    )

    assert advisory.headline == "Flooding in Test Area"
    assert advisory.body  # falls back to the headline
    assert advisory.actions  # falls back to the curated hazard table
    assert advisory.broadcast_text
    assert advisory.generated_by == "gpt-4o"


def test_coerce_falls_back_to_template_without_a_headline():
    from app.models.schemas import Subscriber

    advisory = generator._coerce(
        {"body": "some text"}, _assessment(), Subscriber(name="T"), generated_by="x"
    )

    assert advisory.generated_by == "template"


def test_coerce_truncates_broadcast_on_bytes_not_characters():
    """Advisories may be in multi-byte languages, and the gateway rejects an
    overrun burst rather than trimming it."""
    from app.models.schemas import Subscriber

    advisory = generator._coerce(
        {
            "headline": "Test",
            "body": "b",
            "actions": ["a"],
            # Yoruba diacritics are multi-byte in UTF-8.
            "broadcast_text": "Ọ̀gbàrá omi " * 60,
        },
        _assessment(),
        Subscriber(name="T"),
        generated_by="x",
    )

    encoded = len(advisory.broadcast_text.encode("utf-8"))
    assert encoded <= settings.nigcomsat_max_payload_bytes
    # Truncation must not leave a broken character.
    advisory.broadcast_text.encode("utf-8").decode("utf-8")


def test_coerce_respects_advisory_field_limits():
    """Limits must match the Pydantic constraints on `Advisory`, or validation at
    the API boundary fails on a model that ran long."""
    from app.models.schemas import Subscriber

    advisory = generator._coerce(
        {
            "headline": "x" * 500,
            "body": "b",
            "actions": [f"action {i}" for i in range(20)],
            "broadcast_text": "short",
        },
        _assessment(),
        Subscriber(name="T"),
        generated_by="x",
    )

    assert len(advisory.headline) <= 140
    assert len(advisory.actions) <= 4


# --------------------------------------------------------------------------- #
# Helper
# --------------------------------------------------------------------------- #


def _code_only(path: pathlib.Path) -> str:
    """Source with docstrings stripped.

    These checks are about what the code does; the docstrings here deliberately
    name vendor parameters while explaining why they are avoided.
    """
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)
