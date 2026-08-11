"""The intelligence vocabulary, and the contract it forms with partners.

`severity` alone is not actionable — it tells an integrator which colour to use and nothing about
what to do. So the meaning ships in the webhook payload, from the same table the Web UI reads.

What these tests defend is not the wording but the contract: the machine token is stable, the
confidence band tracks the Oracle's real behaviour, and the two audiences cannot drift apart.
"""

from __future__ import annotations

import json
import pathlib
import re

from app.agents.oracle import CONFIDENCE_ESCALATION_FLOOR
from app.models import intelligence
from app.models.enums import HazardType, Severity


def test_every_severity_has_a_meaning_and_a_response():
    """A category with no stated response is a colour, not intelligence.

    Enumerated over the enum rather than a hardcoded list, so a sixth severity added later fails
    this test until someone decides what it warrants.
    """
    for severity in Severity:
        entry = intelligence.CATEGORY[severity]
        for field in ("label", "meaning", "response", "urgency"):
            assert entry.get(field), f"{severity.value} is missing `{field}`"


def test_the_machine_token_is_the_severity_value():
    """`category` is what a partner switches on, so it must equal the wire value of the enum.

    A prettier token — "Watch" or "WATCH" — would force every integrator to normalise, and one of
    them would get it wrong.
    """
    for severity in Severity:
        block = intelligence.describe(severity, 0.8, HazardType.CROP_WATERLOGGING)
        assert block["category"] == severity.value


def test_severity_capped_tracks_the_oracles_real_floor():
    """**The field an integrator builds escalation logic on.**

    `severity_capped` must be true exactly when the Oracle would have capped severity at Watch.
    Restating 0.65 here instead of importing it would let the two drift, and the drift would be
    invisible: the payload would claim a reading could escalate when the Oracle had already
    prevented it.
    """
    just_below = CONFIDENCE_ESCALATION_FLOOR - 0.01
    just_above = CONFIDENCE_ESCALATION_FLOOR + 0.01

    assert intelligence.describe(Severity.WATCH, just_below, HazardType.FLOOD_INUNDATION)[
        "severity_capped"
    ] is True
    assert intelligence.describe(Severity.WATCH, just_above, HazardType.FLOOD_INUNDATION)[
        "severity_capped"
    ] is False


def test_confidence_bands_are_ordered_and_total():
    """Every value in 0-1 maps to exactly one band, and the order never inverts."""
    seen = [intelligence.confidence_band(c / 100) for c in range(0, 101)]
    assert set(seen) == {"low", "limited", "good", "high"}

    # Monotonic: a higher confidence must never yield a weaker band.
    rank = {"low": 0, "limited": 1, "good": 2, "high": 3}
    ranks = [rank[b] for b in seen]
    assert ranks == sorted(ranks), "confidence bands must not invert as confidence rises"


def test_the_good_band_starts_at_the_escalation_floor():
    """The band boundary IS the floor, so "good confidence" means "can escalate".

    Any other boundary would make the band cosmetic — an integrator could not infer anything about
    escalation from it, which is most of its value.
    """
    assert intelligence.confidence_band(CONFIDENCE_ESCALATION_FLOOR) == "good"
    assert intelligence.confidence_band(CONFIDENCE_ESCALATION_FLOOR - 0.001) == "limited"


def test_every_hazard_maps_to_a_track():
    """A hazard with no track would serialise `track: "agricultural"` by fallback and mislabel a
    flood as a crop finding."""
    for hazard in HazardType:
        assert hazard in intelligence.HAZARD_TRACK, f"{hazard.value} has no track"


# --------------------------------------------------------------------------- #
# The two audiences must not drift
# --------------------------------------------------------------------------- #


def test_the_frontend_vocabulary_matches_the_backend():
    """**Why this test exists.**

    `frontend/lib/intelligence.ts` cannot import Python, so the vocabulary is written twice. A
    drift would mean a subscriber's portal and their aggregator's dashboard describing the same
    alert differently — which is worse than either wording alone, because it makes the platform
    look like two products.

    Compares the CATEGORY KEYS, not the prose: the wording is allowed to be tuned per surface (a
    dashboard has more room than an SMS), but the set of categories and the machine tokens must be
    identical.
    """
    ts = pathlib.Path("../frontend/lib/intelligence.ts")
    if not ts.exists():  # backend-only checkout
        return

    source = ts.read_text()
    for severity in Severity:
        assert f"{severity.value}:" in source, (
            f"the frontend vocabulary is missing `{severity.value}` — the portal would render no "
            f"meaning for a {severity.value} alert"
        )

    # And the frontend must not invent a SEVERITY the backend does not have.
    #
    # Scoped to the INTELLIGENCE table specifically. The same file also declares `TRACK_META` and
    # `VERDICT_META`, whose keys are tracks and Fahis verdicts — different vocabularies with their
    # own backend counterparts, and sweeping them in here made this test fail on a correct change.
    block_start = source.index("export const INTELLIGENCE")
    block = source[block_start : source.index("\n}", block_start)]

    declared = set(re.findall(r"^\s{2}(\w+): \{", block, re.M))
    known = {s.value for s in Severity}
    assert declared, "could not read the frontend severity table"
    assert not declared - known, (
        f"the frontend declares severities the backend does not: {declared - known}"
    )


# --------------------------------------------------------------------------- #
# The published contract
# --------------------------------------------------------------------------- #


def test_the_webhook_payload_carries_the_intelligence_block():
    """Published from the Herald, after the alert is logged.

    A partner's endpoint must never delay or fail a farmer's warning, so the fan-out is last — and
    the block must be built from the shared table rather than hand-assembled at the call site,
    where it would drift from the Web UI.
    """
    source = pathlib.Path("app/agents/herald.py").read_text()
    assert "intelligence.describe(" in source
    assert '"explanations"' in source, (
        "the payload must carry the same plain-language surfaces the email and portal show"
    )


def test_redoc_documents_every_category_with_a_sample():
    """Five samples, one per category.

    An integrator building a handler needs to see the shape for each, and `warning`/`emergency` are
    currently unreachable on this deployment — so a sample is the ONLY way they can build for them
    before weights are trained.
    """
    from app.api.routes.devdocs import DEVELOPER_INTRO

    for severity in Severity:
        assert f'"category": "{severity.value}"' in DEVELOPER_INTRO, (
            f"the partner reference has no payload sample for `{severity.value}`"
        )

    # And the caveat that two of them cannot currently occur.
    assert "not reachable" in DEVELOPER_INTRO, (
        "the docs must say warning/emergency are unreachable until weights are deployed, or a "
        "partner will wait forever for one to test against"
    )


def test_redoc_explains_severity_routing_rather_than_event_names():
    """Severity is a FIELD, not an event name.

    `min_severity` automatically covers everything at or above a level, including categories added
    later. An event-name filter would silently miss a new one — a partner subscribed to `watch` and
    `warning` would not receive `emergency` at all. The docs have to steer them to the filter that
    does not rot.
    """
    from app.api.routes.devdocs import DEVELOPER_INTRO

    assert "min_severity" in DEVELOPER_INTRO
    assert "shelter.alert" in DEVELOPER_INTRO


def test_the_documented_block_matches_what_is_sent():
    """Docs that disagree with the wire are worse than no docs.

    Builds a real block and asserts every field appears in the reference — so adding a field
    without documenting it, or documenting one that is not sent, fails the build.
    """
    from app.api.routes.devdocs import DEVELOPER_INTRO

    block = intelligence.describe(Severity.WATCH, 0.55, HazardType.FLOOD_INUNDATION)
    for field in block:
        assert field in DEVELOPER_INTRO, (
            f"`{field}` is sent in the payload but not documented in the partner reference"
        )

    # The sample JSON must be valid, or an integrator copying it gets a parse error.
    for match in re.finditer(r"```json\n(.*?)```", DEVELOPER_INTRO, re.S):
        body = match.group(1)
        if '"event": "shelter.alert"' not in body:
            continue
        # The samples elide repeated shapes with "…", which is not valid JSON — strip those lines
        # before parsing, since the point is that the STRUCTURE is well-formed.
        cleaned = "\n".join(
            line for line in body.splitlines() if "…" not in line
        )
        cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
        json.loads(cleaned)


# --------------------------------------------------------------------------- #
# The declared contract
#
# An aggregator writes their handler once and runs it for years. These assert the things that
# would break such a handler silently: a renamed field, a sample that no longer matches the wire,
# a version that cannot be reasoned about, or a schema absent from the spec they generate from.
# --------------------------------------------------------------------------- #


def test_the_payload_is_built_from_a_declared_model():
    """**Why `webhooks/schemas.py` exists.**

    The payload was assembled as a dict literal in the Herald. That works and is wrong for a
    published contract: nothing declared the shape, so it could not appear in OpenAPI, and renaming
    a key would break every integration with no test failing.
    """
    source = pathlib.Path("app/agents/herald.py").read_text()
    assert "webhook_schemas.AlertEventData(" in source, (
        "the alert payload must be built through the declared model, not a dict literal"
    )


def test_the_wire_validates_against_the_declared_envelope():
    """Build a payload the way the Herald does, then parse it back.

    If the model and the envelope disagree, one of them is wrong and a partner discovers which in
    production.
    """
    from app.webhooks import engine, schemas

    data = schemas.AlertEventData(
        alert_id="alert_x",
        severity="watch",
        hazard="flood_inundation",
        intelligence=intelligence.describe(
            Severity.WATCH, 0.55, HazardType.FLOOD_INUNDATION
        ),
        explanations={"crop": "c", "drivers": "d", "irrigation": "i"},
        advisory={"headline": "h"},
        assessment={"aoi_id": "a"},
    )
    payload = engine.event_payload(
        "shelter.alert", data.model_dump(mode="json"), delivery_id="whd_x"
    )

    envelope = schemas.WebhookEnvelope.model_validate(payload)
    assert envelope.data.intelligence.category == "watch"
    assert envelope.data.intelligence.severity_capped is True
    assert envelope.data.explanations.irrigation == "i"


def test_contract_version_is_distinct_from_the_service_version():
    """`api_version` changes on every release.

    A partner watching it for contract changes sees one on a release that fixed a typo, learns to
    ignore it, and then misses a real change. `contract_version` bumps only on a removal or rename,
    and is an integer so a receiver can compare it.
    """
    from app.config import settings
    from app.webhooks import engine, schemas

    payload = engine.event_payload("shelter.alert", {}, delivery_id="d")

    assert payload["contract_version"] == schemas.CONTRACT_VERSION
    assert payload["api_version"] == settings.app_version
    assert isinstance(payload["contract_version"], int), (
        "the contract version must be an integer, not a semver string a receiver has to parse"
    )


def test_every_documented_sample_validates_against_the_schema():
    """A sample a partner cannot parse is worse than no sample.

    Each is built through the same `AlertEventData` and `intelligence.describe` the Herald uses, so
    this also catches a field rename that updated the model but not the published example.
    """
    from app.api.routes.webhooks import _SAMPLES, _sample_event
    from app.webhooks import schemas

    assert set(_SAMPLES) == {s.value for s in Severity}, (
        "every severity must have a published sample — an integrator cannot build a handler for a "
        "category they have never seen the shape of"
    )

    for severity in _SAMPLES:
        envelope = schemas.WebhookEnvelope.model_validate(_sample_event(severity))
        assert envelope.data.severity == severity
        assert envelope.data.intelligence.category == severity


def test_each_sample_carries_content_that_fits_its_category():
    """Not one shared body reused five times.

    A shared body would put "prepare drainage" under an `emergency` sample and "act immediately"
    under `info`, teaching an integrator the wrong shape for the category they most need to handle
    correctly.
    """
    from app.api.routes.webhooks import _sample_event

    info = _sample_event("info")["data"]
    emergency = _sample_event("emergency")["data"]

    # Distinct advisories, not a template.
    assert info["advisory"]["headline"] != emergency["advisory"]["headline"]
    assert info["explanations"]["crop"] != emergency["explanations"]["crop"]

    # An info reading needs no action; an emergency needs several.
    assert info["advisory"]["actions"] == []
    assert len(emergency["advisory"]["actions"]) >= 2

    # And the evidence must differ, since the categories describe different situations.
    assert info["assessment"]["evidence"] != emergency["assessment"]["evidence"]


def test_no_sample_recommends_irrigating_a_flooded_plot():
    """The same safeguard the live surface enforces, checked in the published examples.

    A sample that said "irrigate" on a flooded plot would be copied into a partner's test fixtures
    and become the behaviour they build against.
    """
    from app.api.routes.webhooks import _SAMPLES, _sample_event

    for severity, sample in _SAMPLES.items():
        if sample["hazard"].value not in (
            "flood_inundation",
            "flood_forecast",
            "crop_waterlogging",
        ):
            continue
        text = _sample_event(severity)["data"]["explanations"]["irrigation"].lower()
        assert text.startswith("hold"), (
            f"the {severity} sample must not recommend irrigation on a wet plot: {text[:60]!r}"
        )


def test_the_samples_are_deterministic():
    """The exported OpenAPI document must not change on every generation.

    `event_payload` stamps `sent_at` with the current time, so an unpinned sample made
    `make openapi` produce a different file every run and `openapi-check` fail on a spec nobody had
    touched.
    """
    from app.api.routes.webhooks import _sample_event

    first = _sample_event("watch")
    second = _sample_event("watch")
    assert first == second, "a sample must not vary between calls"
    assert not first["sent_at"].endswith("+00:00") or "T" in first["sent_at"]


def test_the_partner_spec_publishes_the_webhook_schemas():
    """A partner generates a client from the spec, so the payload must be a NAMED schema there.

    Describing it in prose means they hand-write a parser and discover a mis-read field in
    production.
    """
    from app.api.routes.devdocs import partner_schema

    schemas_block = partner_schema()["components"]["schemas"]
    for name in ("AlertWebhookEvent", "AlertEventData", "IntelligenceBlock", "ExplanationsBlock"):
        assert name in schemas_block, (
            f"`{name}` must be published so a partner can generate a typed client"
        )


def test_the_partner_spec_publishes_one_example_per_category():
    """Five examples on the documented route, so a handler can be built for all five.

    `warning` and `emergency` are unreachable on this deployment until trained weights land, so a
    published example is the only way to build for them at all.
    """
    from app.api.routes.devdocs import partner_schema

    path = "/shelter/v1/api/webhook/event-schema"
    spec = partner_schema()
    assert path in spec["paths"], "the event-schema route must reach the partner reference"

    examples = spec["paths"][path]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["examples"]
    assert set(examples) == {s.value for s in Severity}


def test_reading_the_spec_does_not_mutate_it():
    """**The bug an ETag exposed.**

    `partner_schema` edits operations in place — stripping security refs, prepending the auth note —
    and `app.openapi()` returns a CACHED dict. So it was mutating the live schema: the auth note
    accumulated, and a partner reloading ReDoc three times saw it three times on every gated
    endpoint. The internal Swagger console inherited the same corruption.
    """
    from app.api.routes.devdocs import partner_schema

    first = partner_schema()
    second = partner_schema()
    assert first == second, "partner_schema must be pure — it is mutating the cached app schema"

    # And specifically: the note must appear exactly once however many times the spec is read.
    partner_schema()
    final = partner_schema()
    for path, item in final["paths"].items():
        for method, operation in item.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            description = operation.get("description") or ""
            assert description.count("Authentication required") <= 1, (
                f"{method.upper()} {path} has a duplicated authentication note"
            )
