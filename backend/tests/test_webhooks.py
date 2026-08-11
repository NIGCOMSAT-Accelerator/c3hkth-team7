"""The webhook subscription engine and the container entrypoint.

`app/webhooks/engine.py` is written as pure functions precisely so this file can
assert real security properties rather than mocking an HTTP client: a signature
either verifies or it does not, a replayed payload either is rejected or it is not.

The properties that matter most here are the ones a business integration depends on
and cannot check for itself:

1. **The signature covers a timestamp**, so a captured payload is not replayable
   forever. A body-only signature would stay valid indefinitely and one captured
   flood alert could trigger repeat insurance payouts.
2. **Comparison is constant-time**, so a signature cannot be recovered byte by byte
   through response timing.
3. **4xx is not retried**, so a receiver rejecting a payload is not hammered.
4. **Filters default to open**, so a new integration that configures nothing receives
   everything rather than silently nothing.
"""

from __future__ import annotations

import json
import pathlib
import time

from app.webhooks import engine

# --------------------------------------------------------------------------- #
# Signing
# --------------------------------------------------------------------------- #


def test_signature_round_trips():
    """The reference verifier must accept what the signer produces.

    `verify` exists so the integration docs can point at real code, and this test is
    what stops a change to `sign` silently breaking every receiver in the field.
    """
    body = engine.canonical_body({"event": "shelter.test", "data": {"a": 1}})
    secret = engine.new_secret()

    signature, timestamp = engine.sign(body, secret)

    assert engine.verify(body, secret, signature, timestamp) is True


def test_signature_rejects_a_tampered_body():
    body = engine.canonical_body({"severity": "watch"})
    secret = engine.new_secret()
    signature, timestamp = engine.sign(body, secret)

    tampered = engine.canonical_body({"severity": "emergency"})
    assert engine.verify(tampered, secret, signature, timestamp) is False


def test_signature_rejects_the_wrong_secret():
    """Per-endpoint secrets are the point: one business leaking theirs must not let
    them forge payloads to another's endpoint."""
    body = engine.canonical_body({"x": 1})
    signature, timestamp = engine.sign(body, engine.new_secret())

    assert engine.verify(body, engine.new_secret(), signature, timestamp) is False


def test_signature_covers_the_timestamp_so_replays_expire():
    """**The replay defence.**

    A body-only signature stays valid forever: anyone who captures one valid payload
    can resend it indefinitely and trigger a repeat payout. Binding the timestamp
    into the signed string is what bounds that window.
    """
    body = engine.canonical_body({"event": "shelter.alert"})
    secret = engine.new_secret()

    stale_timestamp = int(time.time()) - 3600
    signature, _ = engine.sign(body, secret, timestamp=stale_timestamp)

    # Correctly signed for that timestamp...
    assert engine.verify(body, secret, signature, stale_timestamp,
                         tolerance_seconds=7200) is True
    # ...but outside a sane tolerance it must be refused.
    assert engine.verify(body, secret, signature, stale_timestamp,
                         tolerance_seconds=300) is False


def test_signature_is_timestamp_bound_not_body_only():
    """Structural proof of the property above: the same body at two timestamps must
    produce two different signatures. If it did not, the timestamp would be
    decorative."""
    body = engine.canonical_body({"x": 1})
    secret = engine.new_secret()

    first, _ = engine.sign(body, secret, timestamp=1_700_000_000)
    second, _ = engine.sign(body, secret, timestamp=1_700_000_001)

    assert first != second


def test_signature_is_versioned():
    """A prefix means the scheme can be rotated without breaking every receiver on
    the same day — they can accept both during a transition."""
    signature, _ = engine.sign("{}", engine.new_secret())
    assert signature.startswith(f"{engine.SIGNATURE_VERSION}=")


def test_verification_uses_constant_time_comparison():
    """String `==` short-circuits on the first differing byte, leaking the correct
    prefix through timing and letting an attacker recover a signature byte by byte.

    Asserted structurally because a timing test is inherently flaky on shared CI.
    """
    source = pathlib.Path("app/webhooks/engine.py").read_text()
    verify_body = source.split("def verify(")[1].split("\ndef ")[0]

    assert "compare_digest" in verify_body
    # And the naive comparison must not be what decides it.
    assert "== signature" not in verify_body


def test_secrets_are_unique_and_prefixed():
    """Prefixed so a leaked string is recognisable in a log or a paste, and long
    enough that guessing is not a threat."""
    secrets = {engine.new_secret() for _ in range(50)}

    assert len(secrets) == 50, "secrets must not collide"
    for secret in secrets:
        assert secret.startswith("whsec_")
        assert len(secret) > 40


def test_canonical_body_is_deterministic():
    """Two processes must serialise the same payload to the same bytes.

    Without sorted keys, a redelivery could produce different bytes for an identical
    payload and the receiver's signature check would fail on something we consider
    unchanged.
    """
    payload = {"z": 1, "a": {"n": 2, "m": 3}, "k": [1, 2]}

    assert engine.canonical_body(payload) == engine.canonical_body(dict(reversed(list(payload.items()))))
    # And it must be valid JSON, not a repr.
    assert json.loads(engine.canonical_body(payload)) == payload


# --------------------------------------------------------------------------- #
# Retry policy
# --------------------------------------------------------------------------- #


def test_client_errors_are_not_retried():
    """**The distinction that protects receivers.**

    4xx means "your payload is wrong". Retrying cannot fix it and would hammer
    someone's endpoint five times for nothing.
    """
    for status in (400, 401, 403, 404, 410, 422):
        assert engine.is_retryable(status) is False, f"{status} must not be retried"


def test_server_errors_and_network_failures_are_retried():
    for status in (500, 502, 503, 504):
        assert engine.is_retryable(status) is True
    # None means the request never got a response — DNS, TLS, timeout.
    assert engine.is_retryable(None) is True


def test_timeout_and_rate_limit_are_retried_despite_being_4xx():
    """Two deliberate exceptions to the 4xx rule.

    408 means the receiver was slow, not wrong. 429 is the receiver explicitly asking
    us to try later — not retrying would ignore a direct instruction.
    """
    assert engine.is_retryable(408) is True
    assert engine.is_retryable(429) is True


def test_retry_schedule_widens_then_terminates():
    """Front-loaded then widening, because failures are bimodal: most are a redeploy
    (recovered in minutes), the rest a real outage (hours). And it must end — an
    infinite retry loop against a dead endpoint never stops."""
    delays = [engine.next_attempt_delay(n) for n in range(1, len(engine.RETRY_SCHEDULE_SECONDS) + 1)]

    assert all(d is not None for d in delays)
    assert delays == sorted(delays), "delays must not shrink"
    # Exhausted -> None, which is what moves a delivery to `abandoned`.
    assert engine.next_attempt_delay(len(engine.RETRY_SCHEDULE_SECONDS) + 1) is None


def test_retry_schedule_spans_hours_not_minutes():
    """An endpoint down for a deploy window must still receive its backlog."""
    assert sum(engine.RETRY_SCHEDULE_SECONDS) > 6 * 3600


# --------------------------------------------------------------------------- #
# Event filtering
# --------------------------------------------------------------------------- #


def test_filters_default_to_receiving_everything():
    """**Opt-out, not opt-in.**

    A business that subscribes and configures no filters must receive events. The
    opposite default means they subscribe, get nothing, and conclude the product is
    broken.
    """
    empty = {"events": [], "min_severity": None, "aoi_ids": []}

    assert engine.matches(empty, "shelter.alert", "info", "aoi_1") is True
    assert engine.matches(empty, "anything.at.all", None, None) is True


def test_event_filter_excludes_unwanted_events():
    subscription = {"events": ["shelter.verification"], "min_severity": None, "aoi_ids": []}

    assert engine.matches(subscription, "shelter.verification", None, None) is True
    assert engine.matches(subscription, "shelter.alert", None, None) is False


def test_severity_floor_suppresses_quiet_events():
    """A payout engine subscribes at `warning` and must not be woken by an INFO
    advisory — that is the difference between a usable integration and a muted one."""
    subscription = {"events": [], "min_severity": "warning", "aoi_ids": []}

    assert engine.matches(subscription, "shelter.alert", "emergency", None) is True
    assert engine.matches(subscription, "shelter.alert", "warning", None) is True
    assert engine.matches(subscription, "shelter.alert", "watch", None) is False
    assert engine.matches(subscription, "shelter.alert", "info", None) is False


def test_severity_ranking_is_explicit_and_ordered():
    """Declared explicitly rather than relying on enum definition order, which is not
    a documented contract and would silently reorder if a member were inserted."""
    ranks = [engine.SEVERITY_RANK[s] for s in ("info", "advisory", "watch", "warning", "emergency")]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == 5


def test_aoi_filter_scopes_to_named_areas():
    subscription = {"events": [], "min_severity": None, "aoi_ids": ["aoi_kebbi"]}

    assert engine.matches(subscription, "shelter.alert", None, "aoi_kebbi") is True
    assert engine.matches(subscription, "shelter.alert", None, "aoi_lagos") is False


def test_severity_floor_ignored_when_event_has_no_severity():
    """A verification verdict has no severity. It must not be silently dropped by a
    severity floor that cannot apply to it."""
    subscription = {"events": [], "min_severity": "warning", "aoi_ids": []}

    assert engine.matches(subscription, "shelter.verification", None, None) is True


# --------------------------------------------------------------------------- #
# Envelope
# --------------------------------------------------------------------------- #


def test_envelope_carries_the_deduplication_key():
    """Delivery is at-least-once, so duplicates are the receiver's problem to handle
    and `delivery_id` is the only tool they have for it."""
    payload = engine.event_payload("shelter.alert", {"x": 1}, delivery_id="whd_abc")

    assert payload["delivery_id"] == "whd_abc"
    assert payload["event"] == "shelter.alert"
    assert payload["data"] == {"x": 1}
    assert "sent_at" in payload and "api_version" in payload


def test_delivery_ids_are_unique():
    assert len({engine.new_delivery_id() for _ in range(100)}) == 100


# --------------------------------------------------------------------------- #
# Isolation from the alert path
# --------------------------------------------------------------------------- #


def test_engine_does_not_import_the_pipeline():
    """The dependency must point one way.

    The Herald calls `publisher.publish`; nothing in `app/webhooks/` may reach back
    into the agents. If it could, a partner's integration could participate in the
    assessment path — and a business endpoint must never be able to influence, delay
    or fail a farmer's warning.
    """
    import ast

    for path in sorted(pathlib.Path("app/webhooks").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import | ast.ImportFrom):
                name = node.module if isinstance(node, ast.ImportFrom) else node.names[0].name
                assert name is None or not name.startswith("app.agents"), (
                    f"{path.name} imports {name}: the webhook engine must not reach "
                    "into the pipeline"
                )


def test_publisher_never_raises_on_a_disabled_engine():
    """`publish` runs inside the Herald. It must be a no-op when disabled, not an
    exception that fails the stage delivering a flood warning."""
    import asyncio
    from unittest import mock

    from app.webhooks import publisher

    with mock.patch("app.config.settings.webhook_engine_enabled", False):
        assert asyncio.run(publisher.publish("shelter.alert", {})) == 0
        assert asyncio.run(publisher.sweep()) == {"attempted": 0, "delivered": 0}


def test_herald_publishes_after_dispatch_not_before():
    """Ordering is a safety property.

    Fan-out to business endpoints must happen after every subscriber channel has been
    attempted, so a partner's slow endpoint cannot delay the warning that is the
    actual product.
    """
    source = pathlib.Path("app/agents/herald.py").read_text()

    dispatch_index = source.index("receipts")
    publish_index = source.index("webhook_publisher.publish")

    assert publish_index > dispatch_index, (
        "webhook fan-out must come after channel dispatch"
    )


# --------------------------------------------------------------------------- #
# Container entrypoint
# --------------------------------------------------------------------------- #


ENTRYPOINT = pathlib.Path("docker-entrypoint.sh")


def test_entrypoint_exists_and_is_executable():
    """A non-executable entrypoint means the container exits immediately with
    'permission denied' on every start."""
    assert ENTRYPOINT.exists()
    # The Dockerfile chmods it too, because a source tarball or a checkout with
    # core.fileMode=false would not preserve the bit.
    assert "chmod +x" in pathlib.Path("Dockerfile").read_text()


def test_entrypoint_uses_strict_mode():
    """Without `set -e` a failed validation would be logged and then the server would
    start anyway — exactly the failure this script exists to prevent."""
    script = ENTRYPOINT.read_text()
    assert "set -euo pipefail" in script


def test_entrypoint_execs_the_server():
    """`exec` makes uvicorn PID 1 so it receives SIGTERM directly.

    Without it `docker stop` signals bash, uvicorn never shuts down gracefully, and
    every stop drops in-flight requests after the full timeout.
    """
    script = ENTRYPOINT.read_text()
    assert "exec uvicorn" in script
    assert "exec python -m app.queue.worker" in script


def test_entrypoint_validates_before_serving():
    """Config validation must precede the exec, or a broken deployment serves
    traffic while reporting healthy."""
    script = ENTRYPOINT.read_text()

    api_block = script.split("    api)")[1].split(";;")[0]
    assert api_block.index("validate_config") < api_block.index("exec uvicorn")


def test_entrypoint_refuses_multiple_workers_with_the_scheduler():
    """**The duplicate-alert guard.**

    `lifespan` starts the watch loop, so N uvicorn workers run N schedulers and every
    area is scanned N times per cycle. Subscribers would receive duplicate alerts.
    """
    script = ENTRYPOINT.read_text()

    assert "WEB_CONCURRENCY" in script
    assert "SCHEDULER_ENABLED" in script
    # It must be a hard failure, not a warning.
    guard = script.split("WEB_CONCURRENCY:-1")[1].split("fi")[0]
    assert "fail" in guard


def test_entrypoint_passes_proxy_headers():
    """The target deployment is behind an SSL reverse proxy. Without these, every
    client IP logs as the proxy's and generated absolute URLs come out http://."""
    script = ENTRYPOINT.read_text()
    assert "--proxy-headers" in script
    assert "--forwarded-allow-ips" in script


def test_worker_waits_for_the_schema():
    """`depends_on: service_healthy` waits for Postgres to accept connections, not
    for the schema to exist — so a worker could consume a job before `002_core.sql`
    had run."""
    script = ENTRYPOINT.read_text()

    worker_block = script.split("    worker)")[1].split(";;")[0]
    assert "wait_for_schema" in worker_block
    assert worker_block.index("wait_for_schema") < worker_block.index("exec python")


def test_preflight_passes_for_the_api_on_default_configuration():
    """A fresh clone must start. If the shipped defaults fail their own validation,
    the first command a reviewer runs fails.

    ## Why the two production settings are patched out

    This asserts the SHIPPED DEFAULTS are valid, which is a statement about a fresh clone. But
    pydantic-settings reads `../.env`, so on a machine with a real deployment configured the test
    was reading that instead — and once `ENVIRONMENT=production` and `API_KEY` were set for a real
    build, preflight correctly refused and the test failed for the right reason about the wrong
    subject.

    Patching them restores the thing under test. Note this is NOT weakening the check: preflight's
    production rules are exercised by their own tests, and refusing to start with a shared key in
    production is behaviour we want.
    """
    from unittest import mock

    from app.preflight import main

    with (
        mock.patch("app.config.settings.environment", "development"),
        mock.patch("app.config.settings.api_key", None),
    ):
        assert main(["--role", "api"]) == 0


def test_preflight_passes_for_a_worker_configured_as_compose_configures_it():
    """The *defaults* are the API's defaults — `SCHEDULER_ENABLED=true` is correct
    there and wrong on a worker, which is why `docker-compose.yml` overrides it.

    Parameterising both roles over the bare defaults would have asserted that a
    misconfigured worker is valid, which is the opposite of the guarantee.
    """
    from unittest import mock

    from app.preflight import main

    # `environment` and `api_key` for the same reason as the API test above: a real `.env` on the
    # developer's machine would otherwise be the thing under test.
    with mock.patch("app.config.settings.environment", "development"), \
        mock.patch("app.config.settings.api_key", None), \
        mock.patch("app.config.settings.scheduler_enabled", False), \
         mock.patch("app.config.settings.postgres_auto_migrate", False):
        assert main(["--role", "worker"]) == 0


def test_preflight_rejects_production_without_a_key():
    """An unauthenticated production deployment lets anyone register subscribers and
    trigger satellite broadcasts, so this is an error rather than a warning.

    ## Why `mongo_url` is now pinned to None

    "Unauthenticated" means no `API_KEY` **and no IAM**. Without pinning it, this test inherited
    `MONGO_URL` from the developer's `.env` — so once IAM was configured it was asserting that a
    perfectly well-authenticated deployment must be refused.

    That mattered: the rule it was protecting used to demand `API_KEY` in production
    unconditionally, while another rule errored when `API_KEY` was set alongside IAM. The two
    contradicted each other and production was unstartable. See
    `test_per_area_channels`-adjacent coverage in `test_heartbeat_dispatch.py` for both halves.
    """
    from unittest import mock

    from app.preflight import main

    with (
        mock.patch("app.config.settings.environment", "production"),
        mock.patch("app.config.settings.api_key", None),
        mock.patch("app.config.settings.mongo_url", None),
    ):
        assert main(["--role", "api"]) == 1


def test_preflight_rejects_a_malformed_prefix():
    """`frontend/lib/api.ts` concatenates the prefix, so a missing leading slash
    produces a nonsense host and a trailing one produces '//'."""
    from unittest import mock

    from app.preflight import main

    for bad in ("shelter/v1/api", "/shelter/v1/api/"):
        with mock.patch("app.config.settings.api_prefix", bad):
            assert main(["--role", "api"]) == 1, f"{bad!r} should be rejected"


def test_preflight_rejects_a_shared_queue_and_cache_database():
    """Sharing one keyspace lets a cache sweep delete queued satellite scans."""
    from unittest import mock

    from app.preflight import main

    with mock.patch("app.config.settings.redis_url", "redis://d:6379/0"), \
         mock.patch("app.config.settings.cache_url", "redis://d:6379/0"):
        assert main(["--role", "api"]) == 1


def test_preflight_rejects_the_double_scheduler():
    """Two schedulers means every scan is queued twice."""
    from unittest import mock

    from app.preflight import main

    with mock.patch("app.config.settings.scheduler_enabled", True):
        assert main(["--role", "worker"]) == 1


def test_preflight_errors_all_carry_a_remedy():
    """A validation failure without a fix just moves the confusion."""
    from app.preflight import run_checks

    for finding in run_checks("api"):
        if finding.level == "error":
            assert finding.remedy, f"error without a remedy: {finding.message}"
