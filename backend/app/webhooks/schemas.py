"""The webhook contract, declared.

## Why these are models and not dict literals

The `shelter.alert` payload was assembled as a dict literal in `agents/herald.py`. That works and
is completely wrong for a published contract:

  * **Nothing declares the shape**, so it cannot appear in OpenAPI — a partner generating a client
    from our spec gets no type for the thing they receive most.
  * **Nothing stops a rename.** Changing `"severity"` to `"level"` in that literal would break every
    integration silently, and no test would fail.
  * **The docs and the wire could disagree** with nothing to catch it.

An aggregator writes their handler once and runs it for years. So the payload is a declared model,
the field names are the contract, and `tests/test_intelligence.py` asserts the published samples
match what is actually sent.

## Versioning: `contract_version`, not the app version

The envelope carried `api_version: settings.app_version`, which changes on every release — so a
partner watching it for contract changes would see one on a release that only fixed a typo, and
learn to ignore it. That is worse than no version at all.

`contract_version` changes **only** when this file's shape changes in a way a receiver must handle.
Additive fields do not bump it: a receiver parsing JSON ignores keys it does not know, and
requiring a bump for every addition would make partners defer upgrades they do not need.

It bumps for a **removal or a rename** — the two changes that break a handler. When that happens,
both shapes are sent in parallel for a deprecation window rather than switched.

## Extra fields are permitted, deliberately

`model_config` does not forbid extras. A partner who receives an unexpected key should ignore it,
and a strict model here would mean adding one field required a coordinated release with every
integrator — which in practice means the field never gets added.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

#: The contract version. Bumped ONLY on a removal or rename — see the module docstring.
#:
#: `1` is the first declared version. The undeclared dict-literal payload that preceded it carried
#: the same field names, so an existing integration continues to work: this formalises the shape
#: rather than changing it.
CONTRACT_VERSION = 1


class IntelligenceBlock(BaseModel):
    """What an alert category means and what it warrants.

    Sent so a partner does not encode our thresholds in their own system. Built by
    `models.intelligence.describe`, from the same table the Web UI reads.
    """

    category: str = Field(
        description=(
            "The stable machine token: `info`, `advisory`, `watch`, `warning`, `emergency`. "
            "Switch on this. It equals the wire value of the severity enum and will not change "
            "without a contract version bump."
        ),
        examples=["watch"],
    )
    label: str = Field(
        description="Human label, safe to display. May be reworded; do not parse.",
        examples=["Watch"],
    )
    meaning: str = Field(
        description="What this category is, in one sentence. Display, do not parse.",
    )
    response: str = Field(
        description="What the category warrants. Display, do not parse.",
    )
    urgency: str = Field(
        description="How soon, in words.", examples=["Next day or two"]
    )
    confidence: float = Field(
        ge=0,
        le=1,
        description=(
            "How sure SHELTER is **of its own measurement** — not the probability of the hazard. "
            "Provided for completeness; prefer `confidence_band` for routing."
        ),
        examples=[0.55],
    )
    confidence_band: str = Field(
        description=(
            "`high`, `good`, `limited` or `low`. Prefer this to a numeric threshold of your own: "
            "ours move as models are trained, and the band boundary tracks the escalation floor."
        ),
        examples=["limited"],
    )
    severity_capped: bool = Field(
        description=(
            "True when confidence sat below the escalation floor, so this could NOT have been "
            "raised above `watch` however severe the measurement. A capped watch means the data "
            "was the ceiling, not the hazard."
        ),
        examples=[True],
    )
    track: str = Field(
        description="`agricultural`, `environmental` or `public_health`.",
        examples=["agricultural"],
    )


class ExplanationsBlock(BaseModel):
    """The three plain-language surfaces, as shown to the subscriber.

    Identical to what appears in their email and portal — so an aggregator's dashboard and the
    farmer's own view never describe one finding differently.

    Each may be empty: no inference provider configured, a refusal, or an alert predating the
    feature. Empty string rather than null, so a receiver never has to guard before rendering.
    """

    crop: str = Field(
        default="",
        description="What the crop is doing, in plain language.",
    )
    drivers: str = Field(
        default="",
        description="Why the risk level was reached, narrated as causes.",
    )
    irrigation: str = Field(
        default="",
        description=(
            "Irrigate or hold, with the reason. Never recommends irrigating a waterlogged or "
            "flooded plot — that path does not consult a model at all."
        ),
    )


class AlertEventData(BaseModel):
    """The body of a `shelter.alert` event."""

    alert_id: str = Field(
        description="Stable id for this alert. Use with `delivery_id` to deduplicate.",
        examples=["alert_8ebc462217904d78"],
    )
    severity: str = Field(
        description=(
            "Same value as `intelligence.category`, kept at the top level because it is what most "
            "handlers route on first."
        ),
        examples=["watch"],
    )
    hazard: str = Field(
        description="The primary hazard classified for this area.",
        examples=["flood_inundation"],
    )
    intelligence: IntelligenceBlock
    explanations: ExplanationsBlock
    advisory: dict = Field(
        description=(
            "The generated advisory: `headline`, `body`, `actions`, `broadcast_text`, `language`, "
            "`generated_by`. Typed as an object because its own shape is documented on "
            "`Advisory`."
        )
    )
    assessment: dict = Field(
        description=(
            "The full assessment, including `evidence` and `data_sources`. Every figure in the "
            "advisory traces to a line in `evidence`."
        )
    )


class VerificationSourceRef(BaseModel):
    """One source Fahis found. Cited so a verdict can be checked, not just believed."""

    url: str
    title: str = ""
    tier: str = Field(
        description=(
            "How much weight the source carries: `official` (a government or agency), `media`, or "
            "`low`. A CONFIRMED resting only on low-tier sources is downgraded to PARTIAL before "
            "it is ever recorded."
        ),
        examples=["official"],
    )
    published: str | None = Field(
        default=None, description="Publication date where the source stated one."
    )


class VerificationEventData(BaseModel):
    """The body of a `shelter.verification` event.

    ## What this is for

    Days after an alert, Fahis looks for independent confirmation of the hazard we warned about and
    records a verdict. This event carries that verdict so an integrator can close the loop on an
    alert they already received — join on `alert_id`.

    ## What it deliberately does NOT carry

    No severity, no score, no advisory. A verdict must never travel alongside a revised assessment,
    because the moment it does, unattributed web prose is one hop from a number a farmer acts on —
    the exact failure the grounding rule prevents, and one this codebase has violated twice before.

    Fahis writes to `verifications` and nowhere else, `next_stage is None`, and a structural test
    asserts the risk layer never imports the search module. This event is a **read** of that record,
    not a channel back into the pipeline.
    """

    verification_id: str = Field(examples=["ver_3f81c2a9"])
    #: Join on this to correlate with the `shelter.alert` you already received.
    alert_id: str | None = Field(
        default=None,
        description=(
            "The alert this verdict judges. Null when the assessment was recorded but never "
            "dispatched — below the severity floor, or suppressed as a duplicate. Those are still "
            "verified, because accuracy is measured on what we concluded rather than on what we "
            "chose to send."
        ),
        examples=["alert_8ebc462217904d78"],
    )
    assessment_id: str = Field(
        description="Always present. The stable join key when `alert_id` is null.",
        examples=["risk_14950a405aab4c1f"],
    )
    aoi_id: str = Field(examples=["aoi_091d52d4eb874c6d"])

    claimed_hazard: str = Field(
        description="What we warned about, copied so the verdict reads without a join.",
        examples=["flood_inundation"],
    )
    claimed_severity: str = Field(examples=["watch"])
    assessed_at: datetime = Field(description="When the original assessment was made.")

    verdict: str = Field(
        description=(
            "One of five, and the distinction between the last three matters:\n"
            "- `confirmed` — independent sources describe this hazard, here, then.\n"
            "- `partial` — right area, wrong hazard or wrong severity.\n"
            "- `refuted` — a source affirmatively says it did NOT happen. Rare.\n"
            "- `unverified` — nothing found either way. **The common rural case.** A flood in a "
            "remote local government area may never be reported by anything indexable, so this is "
            "NOT evidence the warning was wrong.\n"
            "- `not_attempted` — verification was unavailable. An outage, not a finding."
        ),
        examples=["confirmed"],
    )
    confidence: float = Field(
        ge=0,
        le=1,
        description=(
            "How sure Fahis is **of the verdict itself** — not of the original alert. A confirmed "
            "from one blog is low; from two agencies, high."
        ),
        examples=[0.8],
    )
    rationale: str = Field(
        description="Plain-language justification, citing only the sources below."
    )
    sources: list[VerificationSourceRef] = Field(default_factory=list)
    trainable: bool = Field(
        description=(
            "True only for `confirmed` and `refuted`. Precision is computed over those two alone, "
            "because counting `unverified` would measure news coverage rather than model accuracy."
        ),
        examples=[True],
    )
    verified_at: datetime


class WebhookEnvelope(BaseModel):
    """The outer shape every event shares.

    Stable so a receiver writes one parser: `data` carries the event-specific body, and adding an
    event type never changes the envelope.
    """

    event: str = Field(
        description="Event type. `shelter.alert` today.", examples=["shelter.alert"]
    )
    delivery_id: str = Field(
        description=(
            "Unique per delivery ATTEMPT. Delivery is at-least-once, so deduplicate on this — the "
            "same `alert_id` may arrive more than once after a retry."
        ),
        examples=["whd_8f2c1a94"],
    )
    sent_at: datetime = Field(description="When this attempt was made, UTC, ISO-8601.")
    contract_version: int = Field(
        default=CONTRACT_VERSION,
        description=(
            "The PAYLOAD contract version, not the service version. Changes only when a field is "
            "removed or renamed; additive fields do not bump it."
        ),
        examples=[CONTRACT_VERSION],
    )
    api_version: str = Field(
        description=(
            "Deployed service version. Informational — it changes on every release, so do not "
            "branch on it. Use `contract_version` for that."
        ),
        examples=["0.1.0"],
    )
    #: Event-specific body. `AlertEventData` for `shelter.alert`, `VerificationEventData` for
    #: `shelter.verification`. Switch on `event` before reading it.
    data: AlertEventData | VerificationEventData


class VerificationWebhookEvent(WebhookEnvelope):
    """A `shelter.verification` delivery, for documentation purposes.

    Arrives days after the alert it judges — `VERIFY_AFTER_DAYS` past the forecast window, so the
    outcome has had time to be reported. Correlate on `alert_id`, or `assessment_id` when the
    assessment was never dispatched.
    """

    data: VerificationEventData


class AlertWebhookEvent(WebhookEnvelope):
    """A `shelter.alert` delivery, for documentation purposes.

    Declared as a distinct model so the partner reference can show one concrete example per
    severity against a named schema, rather than describing the shape in prose a client generator
    cannot read.
    """

    data: AlertEventData
