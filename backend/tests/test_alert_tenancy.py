"""Alerts are private, and the scope comes from the credential.

## The incident this file exists for

`GET /alerts` took `subscriber_id` as an optional query parameter with **no authentication of any
kind**, and `None` meant "every alert on the platform". Reproduced with a bare curl and no
credentials during triage:

    subscriber=MDN4D9KSEV aoi='My Irri Palm Fruit Plantation'
    subscriber=MDN4D9KSEV aoi='My Rice plantation'

Reported from a freshly-created aggregator account (`WBMLMQ4J5Z`, no customers, no bound plot) that
signed in and landed on `/dashboard` — the post-login destination — to find an unrelated
individual's advisories, plot names and delivery receipts.

An alert says where a farmer's field is, what is wrong with it, and which phone number was messaged.
It is the most sensitive record the platform holds, and it was world-readable.

## What the fix is

Scope is resolved from whoever is calling (`alert_audience`), and `subscriber_id` may only ever
NARROW that audience. Three credentials, three answers; no credential is a 401, not a global feed.

These tests are structural — they assert the wiring that makes the leak impossible, without needing
Mongo, Postgres or a live session. The end-to-end behaviour was verified by hand against the running
stack during the incident (aggregator: 0 alerts scoped and 0 when forging the victim's id; owner: 9
of their own; anonymous: 401; cross-tenant by id: 404).
"""

from __future__ import annotations

import inspect

from app.api import audience as audience_mod
from app.api.routes import alerts as alerts_route


def test_the_list_endpoint_takes_an_audience_dependency():
    """The handler must not be reachable without resolving who is asking.

    A default of `None` on `subscriber_id` is fine — it is what a subscriber's own portal sends —
    but only because the audience is resolved independently. Remove the dependency and the default
    becomes "everything" again, which is exactly the shape of the original bug.
    """
    signature = inspect.signature(alerts_route.list_alerts)
    caller = signature.parameters.get("caller")

    assert caller is not None, "list_alerts has no audience parameter; it cannot be scoped"
    assert caller.default is not inspect.Parameter.empty, "audience must be a Depends(...)"
    assert "resolve_audience" in repr(caller.default), (
        f"list_alerts must depend on resolve_audience, got {caller.default!r}"
    )


def test_the_single_alert_endpoint_also_takes_the_audience():
    """`GET /alerts/{id}` is the same leak by a different route.

    Scoping only the list would leave direct-by-id reads open, and alert ids appear in logs, in
    webhook payloads and in dispatch receipts — they are not secrets.
    """
    signature = inspect.signature(alerts_route.get_alert)
    caller = signature.parameters.get("caller")

    assert caller is not None, "get_alert is unscoped; another tenant's alert is readable by id"
    assert "resolve_audience" in repr(caller.default)


def test_an_unresolvable_caller_is_refused_rather_than_given_everything():
    """The 401 must be raised by `alert_audience` itself, not left to a caller to remember.

    The failure mode being prevented: an early `return` that yields an unrestricted audience when no
    credential matched. That is a one-line edit away from the original bug, so the raise is asserted
    in the source rather than inferred.
    """
    source = inspect.getsource(audience_mod.resolve_audience)

    assert "HTTP_401_UNAUTHORIZED" in source, (
        "alert_audience must refuse an unidentified caller, not fall through to a feed"
    )
    # And the only unrestricted answer must be the platform branch.
    unrestricted = [
        line for line in source.splitlines() if "permitted_subscriber_ids=None" in line
    ]
    assert len(unrestricted) == 1, (
        f"exactly one branch may return an unrestricted audience, found {len(unrestricted)}"
    )
    assert 'label="platform"' in source, (
        "the unrestricted branch must be the platform-key branch"
    )


def test_the_unrestricted_branch_requires_the_platform_read_scope():
    """A platform key without `platform:read` must not reach the global feed.

    `PLATFORM_READ` is a distinct scope precisely so a key minted for, say, subscriber writes cannot
    also drain the alert history.
    """
    source = inspect.getsource(audience_mod.resolve_audience)
    platform_branch = source.split("--- 2. aggregator key")[0]

    assert "PLATFORM_READ" in platform_branch, (
        "the global feed is not gated on platform:read"
    )


def test_an_empty_audience_returns_nothing_rather_than_everything():
    """**The distinction the original bug got wrong.**

    An aggregator with no customers, or an account with no plot bound, has an EMPTY permitted set.
    Empty must mean "no alerts". Treating a falsy scope as "unfiltered" is precisely how
    `subscriber_id=None` came to mean "every subscriber".

    `WBMLMQ4J5Z` was exactly this case: commercial, `subscriber_id=None`, zero customers.
    """
    source = inspect.getsource(alerts_route.list_alerts)

    assert "if permitted is None:" in source, (
        "unrestricted must be tested as `is None`, never as falsy — an empty set is falsy too, "
        "and conflating them restores the leak for every customerless aggregator"
    )
    assert "if not permitted:" in source
    assert "return []" in source


def test_a_supplied_subscriber_id_can_only_narrow_the_audience():
    """`subscriber_id` is a filter, never an authorisation.

    Verified live during the incident: the reporting aggregator passing the victim's id explicitly
    received 0 alerts, not the victim's 9.
    """
    source = inspect.getsource(alerts_route.list_alerts)

    assert "subscriber_id in permitted" in source, (
        "a supplied id must be intersected with the permitted set, not trusted"
    )


def test_a_cross_tenant_read_is_a_404_not_a_403():
    """A 403 confirms the alert exists, turning the endpoint into an enumeration oracle.

    Same reasoning as `get_customer` and as `authenticate`'s uniform failure message.
    """
    source = inspect.getsource(alerts_route.get_alert)
    # The docstring explains why 403 is wrong, so it legitimately contains the string. Strip it
    # and check the executable body.
    body = source.split('"""', 2)[-1]

    assert "status_code=404" in body
    assert "403" not in body, (
        "a cross-tenant alert read must be indistinguishable from a missing one"
    )


def test_the_route_module_does_not_reach_the_global_feed_unconditionally():
    """`repository.list_alerts(None)` returns every alert; it must be guarded.

    The repository keeps that capability on purpose — the operations dashboard is a real platform
    surface — so the safety property is that the only unguarded call site is inside the
    `permitted is None` branch.
    """
    source = inspect.getsource(alerts_route.list_alerts)
    body = source.split("if permitted is None:")[1]
    guarded_call, _, remainder = body.partition("return await _attach_verdicts(alerts)")

    assert "repository.list_alerts(subscriber_id" in guarded_call, (
        "the unrestricted read must sit inside the platform branch"
    )
    assert "repository.list_alerts(None" not in remainder, (
        "an unconditional global read survives outside the platform branch"
    )
