"""`POST /iam/customers/{id}/areas/{aoi}/scan` — the route that gives `scan:trigger` a home.

## What this closes

`ApiKeyScope.SCAN` was an **orphan scope**: grantable in the portal's key minter, documented in the
developer reference as "Request an immediate assessment for one of your customers' areas", mapped in
`roles.py`, and carrying an audit action (`CUSTOMER_SCAN_TRIGGERED`) that nothing ever emitted.
**No route enforced it.** An aggregator could read the checkbox, grant it deliberately, and receive
a capability that did nothing.

That is the same failure `test_schema_contract.py` was built to catch for tables — "an orphan is
worse than a missing table" — one level up, at the API surface: a missing route 404s loudly, whereas
an advertised scope reviews as done and quietly is not.

## Why these are behavioural rather than source assertions

Most frontend-facing tests in this suite grep source, because there is no JS runner. This route is
Python and its handler is directly callable, so these drive it with a faked store and assert what it
*does*. That matters for the two properties worth having: a cross-tenant id must not queue work, and
the throttle must not be bypassable by guessing ids. Neither is visible in a signature, and a wrong
version of either returns a perfectly ordinary 202.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi import HTTPException

from app.api.routes import iam as iam_routes
from app.iam.deps import Aggregator
from app.iam.models import Account, ApiKeyScope
from app.models.schemas import AreaOfInterest, BBox, Subscriber

# --------------------------------------------------------------------------- #
# Fixtures — the smallest graph the route needs.
# --------------------------------------------------------------------------- #

OWNER_SUB = "sub_owner"
OTHER_SUB = "sub_other"
AOI_MINE = "aoi_mine"
AOI_THEIRS = "aoi_theirs"


def _area(aoi_id: str = AOI_MINE) -> AreaOfInterest:
    return AreaOfInterest(
        id=aoi_id,
        name="Riverside field",
        bbox=BBox(west=3.3, south=6.9, east=3.4, north=7.0),
        hectares=120.0,
    )


def _subscriber(active: bool = True) -> Subscriber:
    return Subscriber(
        id=OWNER_SUB,
        name="Ada",
        language="en",
        active=active,
        areas=[_area()],
        channels=[],
    )


def _aggregator(*scopes: ApiKeyScope) -> Aggregator:
    return Aggregator(
        account=Account(
            id="acc_agg",
            email="ops@coop.example",
            first_name="Co-op",
            last_name="Ops",
            kind="commercial",
        ),
        scopes=list(scopes),
        workspace_id="ws_1",
    )


class _Recorder:
    """Captures what the route did, so a test can assert on absence as well as presence."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str]] = []
        self.audits: list[str] = []
        self.incr_calls: list[str] = []
        self.counter = 0


@pytest.fixture
def wired(monkeypatch):
    """Route wired to fakes: IAM store up, one customer, one area, a working queue."""
    rec = _Recorder()

    monkeypatch.setattr(iam_routes.store, "available", lambda: True)

    async def fake_owned_account(account_id, aggregator):
        # The real one 404s for another tenant; here every id resolves so the tests below can
        # isolate the AREA ownership check specifically.
        return Account(
            id=account_id,
            email="ada@example.com",
            first_name="Ada",
            last_name="Nwosu",
            kind="individual",
            subscriber_id=OWNER_SUB,
        )

    monkeypatch.setattr(iam_routes, "owned_account", fake_owned_account)

    async def fake_get_area(aoi_id):
        if aoi_id == AOI_MINE:
            return OWNER_SUB, _area()
        if aoi_id == AOI_THEIRS:
            # Exists, but under a different subscriber — the cross-tenant case.
            return OTHER_SUB, _area(AOI_THEIRS)
        return None

    monkeypatch.setattr(iam_routes.repository, "get_area", fake_get_area)

    async def fake_get_subscriber(subscriber_id):
        return _subscriber()

    monkeypatch.setattr(iam_routes.repository, "get_subscriber", fake_get_subscriber)

    async def fake_incr(key, ttl):
        rec.incr_calls.append(key)
        rec.counter += 1
        return rec.counter

    monkeypatch.setattr(iam_routes.cache, "incr", fake_incr)

    async def fake_audit(**kwargs):
        rec.audits.append(str(kwargs.get("action")))

    monkeypatch.setattr(iam_routes.store, "record_audit", fake_audit)

    # `enqueue_scan` is imported inside the handler, so patch it on the module it comes from.
    from app.agents import pipeline

    async def fake_enqueue(subscriber, aoi):
        rec.enqueued.append((subscriber.id, aoi.id))
        return "job_abc123"

    monkeypatch.setattr(pipeline, "enqueue_scan", fake_enqueue)

    return rec


async def _call(aggregator, aoi_id=AOI_MINE, account_id="A7K2M9P4QX"):
    return await iam_routes.trigger_customer_area_scan(
        account_id=account_id, aoi_id=aoi_id, aggregator=aggregator
    )


# --------------------------------------------------------------------------- #
# The scope is actually enforced — the whole point of the change.
# --------------------------------------------------------------------------- #


def test_the_route_exists_and_is_gated_on_the_scan_scope():
    """The orphan is adopted: `ApiKeyScope.SCAN` now has exactly one enforcing route."""
    source = inspect.getsource(iam_routes.trigger_customer_area_scan)
    assert "require_scope(ApiKeyScope.SCAN)" in source, (
        "the route does not require `scan:trigger`, so the scope is still an orphan — grantable "
        "in the portal and enforced nowhere"
    )


def test_the_scope_is_not_granted_by_default():
    """It spends real catalogue quota, so it must be asked for deliberately."""
    from app.iam.models import DEFAULT_KEY_SCOPES

    assert ApiKeyScope.SCAN not in DEFAULT_KEY_SCOPES, (
        "scan:trigger is granted by default, so every existing key silently gains the ability to "
        "spend satellite quota"
    )


async def test_a_key_without_the_scope_is_refused(wired):
    """`require_scope` is a dependency, so this asserts the guard itself rather than the route.

    Worth pinning: `customers:write` is the scope a partner already holds to manage areas, and it
    must NOT imply the ability to trigger scans.
    """
    guard = iam_routes.require_scope(ApiKeyScope.SCAN)
    with pytest.raises(HTTPException) as exc:
        await guard(_aggregator(ApiKeyScope.READ, ApiKeyScope.WRITE))

    assert exc.value.status_code == 403
    assert "scan:trigger" in exc.value.detail
    assert not wired.enqueued


# --------------------------------------------------------------------------- #
# Tenancy — an area id is not an authorisation.
# --------------------------------------------------------------------------- #


async def test_an_area_belonging_to_another_subscriber_queues_nothing(wired):
    """**The one that matters.**

    `AOI_THEIRS` exists, so the lookup succeeds — only the ownership comparison stops it. Without
    that comparison this route would scan any area id on request. The caller learns nothing
    directly, since the response carries no reading, but it would let one tenant spend another's
    quota and fire advisories at a stranger's plot at a chosen moment.
    """
    with pytest.raises(HTTPException) as exc:
        await _call(_aggregator(ApiKeyScope.SCAN), aoi_id=AOI_THEIRS)

    assert exc.value.status_code == 404
    assert wired.enqueued == [], "a cross-tenant area id queued a scan"


async def test_a_cross_tenant_area_is_indistinguishable_from_a_missing_one(wired):
    """404 with the SAME message either way.

    Area ids appear in webhook payloads. A 403, or a differently-worded 404, would turn any id into
    a membership oracle — the same reasoning as `get_customer` and `latest_assessment`.
    """
    with pytest.raises(HTTPException) as theirs:
        await _call(_aggregator(ApiKeyScope.SCAN), aoi_id=AOI_THEIRS)
    with pytest.raises(HTTPException) as absent:
        await _call(_aggregator(ApiKeyScope.SCAN), aoi_id="aoi_nope")

    assert theirs.value.status_code == absent.value.status_code == 404
    assert theirs.value.detail == absent.value.detail, (
        "an area under another tenant reports differently from one that does not exist, which "
        "confirms the id"
    )


async def test_authorisation_runs_before_the_throttle(wired):
    """Otherwise a caller could burn another area's hourly budget by guessing ids.

    The counter is keyed on `aoi_id`, so incrementing it before the ownership check would let an
    unauthorised request throttle a *legitimate* one — a denial of service against another tenant,
    delivered through our own rate limiter.
    """
    with pytest.raises(HTTPException):
        await _call(_aggregator(ApiKeyScope.SCAN), aoi_id=AOI_THEIRS)

    assert wired.incr_calls == [], (
        "the rate-limit counter was incremented for an area the caller may not see"
    )


# --------------------------------------------------------------------------- #
# The happy path, and what it promises.
# --------------------------------------------------------------------------- #


async def test_a_permitted_scan_is_queued_and_audited(wired):
    result = await _call(_aggregator(ApiKeyScope.SCAN))

    assert wired.enqueued == [(OWNER_SUB, AOI_MINE)]
    assert result.aoi_id == AOI_MINE
    assert result.job_id == "job_abc123"
    # The audit action that existed with no emitter.
    assert any("CUSTOMER_SCAN_TRIGGERED" in a for a in wired.audits), (
        "the scan is not audited, so 'who asked us to spend this quota' is unanswerable"
    )


def test_the_response_is_202_and_carries_no_assessment():
    """Queued, not awaited — so the body must not imply a reading exists.

    A 200 with an assessment-shaped body would be a lie: the scan has not run. `ScanQueued` exists
    to say what actually happened, and the route status is 202 to match.
    """
    source = inspect.getsource(iam_routes)
    decorator = source.split('"/customers/{account_id}/areas/{aoi_id}/scan"', 1)[1].split(
        "async def", 1
    )[0]
    assert "HTTP_202_ACCEPTED" in decorator, (
        "the route returns 200, which claims the work is done when it has only been queued"
    )

    fields = set(iam_routes.ScanQueued.model_fields)
    for leaked in ("severity", "score", "confidence", "assessment", "evidence"):
        assert leaked not in fields, (
            f"ScanQueued carries `{leaked}` — a queue acknowledgement must not look like a reading"
        )


def test_the_queued_path_is_the_same_one_the_scheduler_uses():
    """`pipeline.enqueue_scan`, not a second implementation.

    A hand-rolled envelope here would compile, run, and silently truncate the run_id trace — and
    would drift from the scheduled path over time. `worker.py` may construct `JobEnvelope` exactly
    once for the same reason.
    """
    source = inspect.getsource(iam_routes.trigger_customer_area_scan)
    assert "pipeline.enqueue_scan" in source
    assert "JobEnvelope" not in source, (
        "the route builds its own job envelope instead of going through enqueue_scan"
    )


def test_no_run_id_is_promised_that_cannot_be_supplied():
    """`enqueue_scan` returns `job.id` and binds the run id inside a context manager.

    So `tracing.current_run_id()` after the call is None. A `run_id` field here would be null on
    every response, which is worse than its absence — a partner would quote it in a support request
    and it would identify nothing.
    """
    assert "run_id" not in iam_routes.ScanQueued.model_fields, (
        "ScanQueued advertises a run_id the route cannot populate"
    )


# --------------------------------------------------------------------------- #
# Rate limiting — a courtesy to shared free upstreams.
# --------------------------------------------------------------------------- #


async def test_repeated_scans_of_one_area_are_throttled(wired, monkeypatch):
    """Sentinel-1 revisits every ~6 days, so a per-minute loop re-reads one scene for one answer.

    The limit is a real ceiling, not advisory: past it, nothing is queued.
    """
    monkeypatch.setattr(
        iam_routes.settings, "scan_trigger_rate_limit_per_hour", 2, raising=False
    )
    agg = _aggregator(ApiKeyScope.SCAN)

    await _call(agg)
    await _call(agg)
    with pytest.raises(HTTPException) as exc:
        await _call(agg)

    assert exc.value.status_code == 429
    assert len(wired.enqueued) == 2, "a throttled request still queued satellite work"


async def test_the_refusal_states_when_to_come_back(wired, monkeypatch):
    """A partner integration retries on a schedule.

    A 429 with no interval invites a tight retry loop, which is the behaviour being limited.
    """
    monkeypatch.setattr(
        iam_routes.settings, "scan_trigger_rate_limit_per_hour", 1, raising=False
    )
    agg = _aggregator(ApiKeyScope.SCAN)
    await _call(agg)

    with pytest.raises(HTTPException) as exc:
        await _call(agg)

    assert exc.value.headers and "Retry-After" in exc.value.headers, (
        "429 carries no Retry-After, so the correct retry interval is guesswork"
    )


async def test_the_counter_is_keyed_per_area_not_per_key(wired):
    """The cost falls per footprint.

    A per-key cap would let one aggregator with many customers starve the queue, while a small one
    is throttled over a single plot.
    """
    await _call(_aggregator(ApiKeyScope.SCAN))
    assert wired.incr_calls, "no counter was incremented at all"
    assert AOI_MINE in wired.incr_calls[0], (
        f"the rate-limit key {wired.incr_calls[0]!r} is not keyed on the area"
    )


async def test_a_zero_limit_disables_the_throttle(wired, monkeypatch):
    """Consistent with the other `0 disables` settings in config, and needed for a load test."""
    monkeypatch.setattr(
        iam_routes.settings, "scan_trigger_rate_limit_per_hour", 0, raising=False
    )
    agg = _aggregator(ApiKeyScope.SCAN)
    for _ in range(5):
        await _call(agg)

    assert len(wired.enqueued) == 5


async def test_the_throttle_fails_open_when_the_cache_is_unreachable(wired, monkeypatch):
    """`cache.incr` returns 0 on failure, and 0 must not read as "over the limit".

    Same judgement as chat: this ceiling protects shared upstreams, and refusing a legitimate
    flood-season scan over a missing Redis key is the worse failure.
    """
    monkeypatch.setattr(
        iam_routes.settings, "scan_trigger_rate_limit_per_hour", 1, raising=False
    )

    async def dead_cache(key, ttl):
        return 0

    monkeypatch.setattr(iam_routes.cache, "incr", dead_cache)

    agg = _aggregator(ApiKeyScope.SCAN)
    for _ in range(3):
        await _call(agg)

    assert len(wired.enqueued) == 3, (
        "a cache outage closed the route, so an unreachable counter stops scans entirely"
    )


# --------------------------------------------------------------------------- #
# Refusals that prevent a promise we cannot keep.
# --------------------------------------------------------------------------- #


async def test_a_customer_with_no_subscription_is_refused(wired, monkeypatch):
    async def no_subscription(account_id, aggregator):
        return Account(
            id=account_id,
            email="new@example.com",
            first_name="New",
            last_name="Customer",
            kind="individual",
            subscriber_id=None,
        )

    monkeypatch.setattr(iam_routes, "owned_account", no_subscription)

    with pytest.raises(HTTPException) as exc:
        await _call(_aggregator(ApiKeyScope.SCAN))

    assert exc.value.status_code == 409
    assert not wired.enqueued


async def test_a_paused_subscription_is_refused_rather_than_scanned_silently(
    wired, monkeypatch
):
    """The scheduler skips inactive subscribers, so the work would be measured and never dispatched.

    A 202 promising a webhook that cannot arrive is indistinguishable from a broken integration.
    Naming the paused subscription is what makes it actionable.
    """

    async def paused(subscriber_id):
        return _subscriber(active=False)

    monkeypatch.setattr(iam_routes.repository, "get_subscriber", paused)

    with pytest.raises(HTTPException) as exc:
        await _call(_aggregator(ApiKeyScope.SCAN))

    assert exc.value.status_code == 409
    assert "paused" in exc.value.detail.lower()
    assert not wired.enqueued, "a scan was queued for a subscription that cannot be alerted"


# --------------------------------------------------------------------------- #
# The setting is configured and documented, per the config contract.
# --------------------------------------------------------------------------- #


def test_the_rate_limit_is_configurable_and_documented():
    """`test_config.py` enforces this globally; asserted here so the failure names this feature."""
    import pathlib

    from app.config import settings

    assert isinstance(settings.scan_trigger_rate_limit_per_hour, int)

    env = pathlib.Path("../.env.example")
    if env.exists():
        assert "SCAN_TRIGGER_RATE_LIMIT_PER_HOUR" in env.read_text(), (
            "the setting is not in .env.example, so an operator cannot discover the ceiling"
        )
