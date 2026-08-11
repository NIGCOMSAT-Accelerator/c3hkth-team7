"""Low-risk readings are delivered too, so silence never means "probably broken".

## Why the floor is INFO

A subscriber who hears nothing for three weeks cannot distinguish working monitoring from a dead
pipeline — and the silence is indistinguishable from failure at exactly the moment trust matters.
SHELTER promises 24/7 watching, so a quiet reading is itself a useful message: "we looked, here is
what we saw, nothing needs doing."

## Why this is not four messages a day

The watch loop runs every 6 hours, so a naive floor of INFO would page four times daily per plot.
`_is_duplicate` suppresses an equal-or-LOWER severity inside `RESEND_WINDOW_HOURS`, so a quiet plot
produces roughly one heartbeat a day. Verified live: four INFO alerts delivered, then all four
suppressed on an immediate re-run.

**Escalation is never suppressed** — the dedupe compares severities rather than merely checking for
a recent send, so a WATCH after an INFO goes out immediately.

## The two settings that must agree

`herald.DISPATCH_FLOOR` and `ChannelBinding.min_severity`'s default. A floor of INFO with a binding
default of ADVISORY means the platform generates the heartbeat and every channel silently discards
it — the feature would look built and deliver nothing. Found exactly that way: the first live test
logged "no eligible channels for alert".
"""

from __future__ import annotations

from app.agents.herald import DISPATCH_FLOOR, RESEND_WINDOW_HOURS
from app.models.enums import SEVERITY_ORDER, Channel, Severity
from app.models.schemas import ChannelBinding, Subscriber


def test_the_dispatch_floor_admits_info():
    """The change itself. INFO is the lowest severity, so nothing is recorded-but-silent."""
    assert DISPATCH_FLOOR is Severity.INFO
    assert SEVERITY_ORDER[DISPATCH_FLOOR] == 0, (
        "the floor is above the lowest severity, so some readings are still never delivered"
    )


def test_the_binding_default_matches_the_floor():
    """**The gap that made the first live attempt deliver nothing.**

    A floor of INFO with a binding default of ADVISORY generates a heartbeat and then discards it
    at the channel — "no eligible channels for alert" in the log, and a feature that looks built.
    """
    binding = ChannelBinding(channel=Channel.EMAIL, address="a@example.com")

    assert binding.min_severity is Severity.INFO
    assert SEVERITY_ORDER[binding.min_severity] <= SEVERITY_ORDER[DISPATCH_FLOOR], (
        "the default channel floor is above the dispatch floor, so heartbeats are generated and "
        "then silently dropped"
    )


def test_a_heartbeat_reaches_a_default_channel():
    """End to end at the model level: an INFO reading resolves to a channel."""
    subscriber = Subscriber(
        id="sub_test",
        name="Test",
        channels=[ChannelBinding(channel=Channel.EMAIL, address="a@example.com")],
    )

    eligible = subscriber.channels_for(Severity.INFO)
    assert [b.channel for b in eligible] == [Channel.EMAIL]


def test_raising_min_severity_opts_out_per_channel_and_per_plot():
    """**The opt-out, which already existed.**

    Someone who wants only real warnings raises `min_severity`. Per binding, so it can be set for
    one plot or one channel while the rest keep the heartbeat — that granularity is why no new
    setting was added for this.
    """
    subscriber = Subscriber(
        id="sub_test",
        name="Test",
        channels=[
            ChannelBinding(
                channel=Channel.EMAIL,
                address="quiet@example.com",
                min_severity=Severity.ADVISORY,
            ),
            ChannelBinding(
                channel=Channel.WHATSAPP,
                address="+234",
                min_severity=Severity.INFO,
                aoi_id="aoi_rice",
            ),
        ],
    )

    # The general email binding opted out of the heartbeat.
    assert subscriber.channels_for(Severity.INFO) == []
    # And still receives a real advisory.
    assert [b.channel for b in subscriber.channels_for(Severity.ADVISORY)] == [Channel.EMAIL]
    # While the rice plot's own binding keeps it.
    assert [b.channel for b in subscriber.channels_for(Severity.INFO, "aoi_rice")] == [
        Channel.WHATSAPP
    ]


def test_the_resend_window_is_what_makes_this_affordable():
    """The scan cadence is 6 hours; without a dedupe longer than that, INFO pages four times a day.

    Asserted as a relationship rather than a value, so changing the scan interval cannot silently
    turn a daily heartbeat into a four-hourly one.
    """
    from app.config import settings

    scan_interval_hours = settings.scheduler_interval_seconds / 3600
    assert RESEND_WINDOW_HOURS > scan_interval_hours, (
        f"the resend window ({RESEND_WINDOW_HOURS}h) is shorter than the scan interval "
        f"({scan_interval_hours}h), so every cycle would send another heartbeat"
    )
    # And long enough to be about one a day rather than several.
    assert RESEND_WINDOW_HOURS >= 12


def test_escalation_is_never_suppressed_by_a_heartbeat():
    """A WATCH after an INFO must go out immediately.

    The dedupe compares severities — `<=` — rather than checking for any recent send. Losing that
    comparison would mean a heartbeat at 06:00 muted a real warning at 09:00, which is the one
    failure this whole feature must not introduce.
    """
    import inspect

    from app.agents.herald import HeraldAgent

    source = inspect.getsource(HeraldAgent._is_duplicate)
    assert "SEVERITY_ORDER[assessment.severity] <= SEVERITY_ORDER[last_severity]" in source, (
        "the dedupe no longer compares severities, so a heartbeat can suppress an escalation"
    )


# --------------------------------------------------------------------------- #
# Preflight: the two rules that contradicted each other
# --------------------------------------------------------------------------- #


def test_production_with_iam_does_not_require_the_legacy_shared_key():
    """**The contradiction that made production unstartable.**

    Two rules disagreed. One demanded `API_KEY` in production; the other errored when `API_KEY` was
    set *alongside* IAM, because the shared `X-SHELTER-Key` grants every write endpoint including
    NIGCOMSAT broadcast. With `MONGO_URL` configured, no value satisfied both — set it and rule two
    fails, unset it and rule one fails. The stack restart-looped with "2 configuration error(s)".

    They were never both meant to apply: `API_KEY` is the pre-IAM authentication story. Once IAM is
    configured, scoped service-account keys ARE the authentication.
    """
    from unittest import mock

    from app.preflight import main

    with (
        mock.patch("app.config.settings.environment", "production"),
        mock.patch("app.config.settings.api_key", None),
        mock.patch("app.config.settings.mongo_url", "mongodb://example/db"),
        mock.patch("app.config.settings.iam_jwt_secret", "x" * 64),
        mock.patch("app.config.settings.webhook_signing_secret", "y" * 64),
        mock.patch("app.config.settings.scheduler_enabled", True),
    ):
        assert main(["--role", "api"]) == 0, (
            "a production deployment authenticated by IAM alone is refused, so the only way to "
            "start is to re-introduce the shared key the other rule forbids"
        )


def test_production_with_neither_credential_is_still_refused():
    """The relaxation must not become a hole.

    A deployment with no `API_KEY` and no IAM genuinely is unauthenticated — anyone could register
    subscribers or trigger a broadcast — and that must still fail loudly.
    """
    from unittest import mock

    from app.preflight import main

    with (
        mock.patch("app.config.settings.environment", "production"),
        mock.patch("app.config.settings.api_key", None),
        mock.patch("app.config.settings.mongo_url", None),
    ):
        assert main(["--role", "api"]) != 0, (
            "production with neither API_KEY nor IAM starts, leaving every write endpoint open"
        )
