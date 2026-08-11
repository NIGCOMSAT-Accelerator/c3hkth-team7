"""Alert delivery is changeable, and can differ per plot.

## Two gaps this closes

1. **Channels were settable only at signup.** No endpoint existed to change them — the portal's
   Settings page could display the preferred channel but not edit it. A farmer who mistyped a phone
   number had alerts going nowhere with no way to fix it.
2. **One configuration served every plot.** `channel_bindings` was keyed on `subscriber_id` alone,
   so "flood alerts for the riverside plot by SMS, crop alerts for the rest by email" was
   inexpressible.

## The vulnerability found while testing this

`resolve_audience` checked the **platform key before the portal session**, and
`frontend/lib/api.ts` attaches that key to every request. So a browser request carrying both
resolved as *unrestricted*, and a signed-in subscriber could change another subscriber's alert
delivery by editing the id in the URL. Reproduced: the write returned 200 and overwrote a real
address.

A session is the more specific credential — it names a person, where the platform key names only
"the portal" — so it is now checked first. The key still reaches the unrestricted branch when it
arrives alone, which is the operations-dashboard case it exists for.
"""

from __future__ import annotations

import inspect
import textwrap

import pytest

from app.models.enums import Channel, Severity
from app.models.schemas import ChannelBinding, Subscriber


def _subscriber(*bindings: ChannelBinding) -> Subscriber:
    return Subscriber(id="sub_test", name="Test", channels=list(bindings))


def test_a_general_binding_applies_to_every_area():
    """`aoi_id: None` means all plots — which is what every pre-existing binding means.

    This is why migration 013 needed no data movement: the rows already in the table keep working,
    and keep applying to plots added later.
    """
    subscriber = _subscriber(
        ChannelBinding(channel=Channel.EMAIL, address="a@x", min_severity=Severity.ADVISORY)
    )

    for area in ("aoi_one", "aoi_two", None):
        got = subscriber.channels_for(Severity.WATCH, area)
        assert [b.channel for b in got] == [Channel.EMAIL], f"failed for {area}"


def test_a_specific_binding_overrides_rather_than_adds():
    """**The design decision worth pinning.**

    A union would mean adding an SMS override for one plot silently leaves the general email
    binding firing too — so the subscriber gets two alerts and cannot turn the first off without
    losing it everywhere. "Override" has to mean override.
    """
    subscriber = _subscriber(
        ChannelBinding(channel=Channel.EMAIL, address="a@x", min_severity=Severity.ADVISORY),
        ChannelBinding(
            channel=Channel.WHATSAPP,
            address="+234",
            min_severity=Severity.ADVISORY,
            aoi_id="aoi_rice",
        ),
    )

    rice = subscriber.channels_for(Severity.WATCH, "aoi_rice")
    assert [b.channel for b in rice] == [Channel.WHATSAPP], (
        "the general email binding still fires for an overridden plot, so the subscriber gets two "
        "alerts for one hazard"
    )

    # And a plot with no override is untouched by the existence of one elsewhere.
    palm = subscriber.channels_for(Severity.WATCH, "aoi_palm")
    assert [b.channel for b in palm] == [Channel.EMAIL]


def test_an_override_below_its_severity_floor_falls_back_to_general():
    """An override that is not eligible must not silence the plot.

    A WhatsApp override from `watch` upwards, on an `advisory` alert, has to leave the general
    email binding delivering — otherwise raising a threshold on one channel accidentally mutes the
    plot entirely for lower severities.
    """
    subscriber = _subscriber(
        ChannelBinding(channel=Channel.EMAIL, address="a@x", min_severity=Severity.ADVISORY),
        ChannelBinding(
            channel=Channel.WHATSAPP,
            address="+234",
            min_severity=Severity.WATCH,
            aoi_id="aoi_rice",
        ),
    )

    got = subscriber.channels_for(Severity.ADVISORY, "aoi_rice")
    assert [b.channel for b in got] == [Channel.EMAIL], (
        "an ineligible override silenced the plot instead of falling through"
    )


def test_no_area_returns_only_the_general_bindings():
    """`aoi_id=None` from a caller means "no particular area".

    It must not fan out to an override the subscriber set for one specific plot — the manual
    dispatch path passes nothing, and sending a rice-plot SMS for an unrelated assessment would be
    worse than sending nothing.
    """
    subscriber = _subscriber(
        ChannelBinding(channel=Channel.EMAIL, address="a@x", min_severity=Severity.ADVISORY),
        ChannelBinding(
            channel=Channel.WHATSAPP,
            address="+234",
            min_severity=Severity.ADVISORY,
            aoi_id="aoi_rice",
        ),
    )

    got = subscriber.channels_for(Severity.WATCH)
    assert [b.channel for b in got] == [Channel.EMAIL]


def test_dispatch_passes_the_area_through():
    """The router had `assessment.aoi_id` on hand and was not using it.

    Without this the per-area schema exists and does nothing — every plot would still share one
    configuration, and the feature would look broken rather than absent.

    Asserted on the parsed call rather than on an exact source string. The original matched
    `"channels_for(assessment.severity, assessment.aoi_id)"` literally, which broke the moment the
    call gained a third argument and had to wrap — a failure that named a real regression ("overrides
    can never fire") while the override behaviour was in fact intact. What matters is that the area
    reaches `channels_for`, not how the line is formatted.
    """
    import ast

    from app.dispatch import router

    tree = ast.parse(textwrap.dedent(inspect.getsource(router.deliver)))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "channels_for"
    ]
    assert calls, "dispatch never resolves channels at all"

    passed = {ast.unparse(a) for c in calls for a in c.args} | {
        ast.unparse(k.value) for c in calls for k in c.keywords
    }
    assert "assessment.aoi_id" in passed, (
        "dispatch resolves channels without the area, so overrides can never fire"
    )
    assert "assessment.severity" in passed, (
        "dispatch resolves channels without the severity, so min_severity can never filter"
    )
    # Added with the subscriber's score dial. Without it `min_score` is dead configuration: it
    # saves, displays, and never affects a single delivery.
    assert "assessment.score" in passed, (
        "dispatch resolves channels without the score, so min_score can never filter"
    )


def test_the_read_path_carries_the_area():
    """`_subscriber_from_rows` builds bindings from an explicit field list.

    A missing `aoi_id` there would persist correctly and then behave as though it applied
    everywhere — a binding that silently sends a plot's alerts to the wrong channel.
    """
    from app.store import repository

    source = inspect.getsource(repository._subscriber_from_rows)
    assert '"aoi_id": c["aoi_id"]' in source, (
        "the row mapper drops aoi_id, so every override is lost on read"
    )


# --------------------------------------------------------------------------- #
# The vulnerability
# --------------------------------------------------------------------------- #


def test_a_portal_session_outranks_the_platform_key():
    """**The write vulnerability.**

    The frontend attaches the platform key to every request. Checked first, it made any browser
    request resolve as unrestricted — so a subscriber could change another's alert delivery by
    editing the id in the URL. Reproduced live: 200, and a real address overwritten.
    """
    from app.api import audience

    source = inspect.getsource(audience.resolve_audience)
    session_at = source.find("session_token = getattr(session")
    platform_at = source.find("if platform_key:")

    assert session_at >= 0, "the session branch is gone"
    assert platform_at >= 0
    assert session_at < platform_at, (
        "the platform key is resolved before the portal session; because the frontend sends that "
        "key on every request, a signed-in subscriber would be treated as unrestricted"
    )


def test_the_channels_endpoint_is_tenant_scoped():
    """A write is at least as sensitive as a read, and this one redirects hazard alerts."""
    from app.api.routes import subscribers as sub

    source = inspect.getsource(sub.replace_channels)

    assert "resolve_audience" in str(inspect.signature(sub.replace_channels))
    assert "caller.may_see(subscriber_id)" in source, "the endpoint does not check tenancy"
    assert "status_code=404" in source, (
        "a cross-tenant write should answer 404, not 403 — a 403 confirms the id exists"
    )


def test_a_binding_cannot_name_an_area_the_subscriber_does_not_own():
    """It could never deliver anything, and accepting it looks like a save that worked."""
    from app.api.routes import subscribers as sub

    source = inspect.getsource(sub.replace_channels)
    assert "binding.aoi_id not in owned" in source
    assert "422" in source or "UNPROCESSABLE" in source


def test_an_unchanged_save_sends_no_notification():
    """A "your settings changed" email when nothing changed teaches people to ignore the real one.

    And this is exactly the notice that matters when somebody else made the change.
    """
    from app.api.routes import subscribers as sub

    source = inspect.getsource(sub.replace_channels)
    assert "if current != previous:" in source, (
        "the confirmation fires on every save, including no-ops"
    )


@pytest.mark.parametrize("actor", ["account:ABC", "portal-commercial:ABC"])
def test_only_a_third_party_change_names_an_actor(actor):
    """"You changed your own settings" is noise; "someone else changed yours" is the point."""
    from app.api.routes import subscribers as sub

    source = inspect.getsource(sub._confirm_channels_changed)
    assert 'audience.startswith("aggregator:")' in source, (
        "the confirmation names an actor for self-service changes too, which is noise"
    )
