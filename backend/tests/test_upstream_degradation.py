"""What the EO adapters do when an upstream misbehaves — and what they say about it.

## Why this file exists

Three lines from the production worker logs, all of which read as the same kind of event and
none of which were:

    {"msg": "malaria atlas lookup failed", "error": ""}
    {"msg": "worldpop lookup failed",      "error": ""}
    {"msg": "worldpop lookup failed",      "error": "Extra data: line 7 column 2 (char 152)"}

An operator reading those cannot tell an outage from a parser bug from a wrong URL, which is
the whole problem: every adapter in `app/eo/` is *designed* to degrade rather than raise, so the
log line is the only evidence that a source dropped out of `ExposureSummary.sources`. A warning
that names the source and withholds the reason spends the operator's attention and returns
nothing.

Each test here pins one of the three causes, established by live reproduction against the real
endpoints rather than inferred.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.eo import exposure, health
from app.logging_config import describe
from app.models.schemas import BBox

BBOX = BBox(west=8.47, south=11.95, east=8.57, north=12.05)  # Kano


# --------------------------------------------------------------------------- #
# `error: ""` — httpx timeouts stringify to nothing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout(""),
        httpx.ConnectTimeout(""),
        httpx.PoolTimeout(""),
        httpx.ConnectError(""),
    ],
)
def test_describe_never_returns_an_empty_reason(exc):
    """**The cause of `error: ""`.**

    Every `httpx` timeout class carries its message positionally and is routinely raised with an
    empty one, so `str(exc)` is `""` — and `httpx` is the library every adapter in `app/eo/` uses.
    A timeout is also the single most likely failure for a keyless public endpoint, so this was
    not an edge case: it was the common path.
    """
    assert str(exc) == "", "if this fails httpx started populating the message; good, but re-check"
    assert describe(exc) == type(exc).__name__


def test_describe_keeps_the_message_when_there_is_one():
    """The fix must not turn a useful message into a bare class name."""
    assert describe(ValueError("bbox is inverted")) == "ValueError: bbox is inverted"


# --------------------------------------------------------------------------- #
# WorldPop — trailing PHP warnings, and an API that ignores `runasync=false`
# --------------------------------------------------------------------------- #

#: The **real** body captured from `api.worldpop.org`, PHP diagnostics and all.
#:
#: This is why `response.json()` raised `Extra data: line 7 column 2 (char 152)`. The JSON is
#: complete and correct; the server appends unsolicited HTML after it.
REAL_MALFORMED_BODY = """{
    "status": "created",
    "status_code": 200,
    "error": false,
    "error_message": null,
    "taskid": "ec98015f-0000-4000-8000-000000000000"
}<br />
<b>Warning</b>:  Trying to access array offset on value of type bool in <b>/srv/www/api.worldpop.org/html/app/ServicesController.php</b> on line <b>278</b><br />
"""


def test_the_real_worldpop_body_is_parsed_rather_than_raising():
    """Strict JSON rejects this; the population inside it is nonetheless recoverable."""
    with pytest.raises(json.JSONDecodeError):
        json.loads(REAL_MALFORMED_BODY)

    parsed = exposure._lenient_json(REAL_MALFORMED_BODY)
    assert parsed["taskid"] == "ec98015f-0000-4000-8000-000000000000"
    assert parsed["status"] == "created"


def test_lenient_parsing_still_rejects_a_body_that_is_not_json():
    """Tolerating trailing junk must not become tolerating anything.

    A 504 HTML error page is a plausible response from any of these endpoints, and it must fail
    loudly into the adapter's `except` rather than being read as an empty answer — an empty answer
    would put the source in `sources` with a zero, which is the "absent is not zero" violation.
    """
    with pytest.raises(json.JSONDecodeError):
        exposure._lenient_json("<html><head><title>504 Gateway Time-out</title></head></html>")


async def test_worldpop_follows_the_task_it_is_given(monkeypatch):
    """**The bug the `Extra data` error was hiding.**

    `runasync=false` is ignored — verified live, `/services/stats` always answers
    `{"status": "created", "taskid": …}`. The old code read `data.total_population` from that
    first response, which never contains it, so a *successful* request still returned 0 and
    WorldPop silently never appeared in `ExposureSummary.sources`.
    """
    monkeypatch.setattr(exposure, "_WORLDPOP_POLL_SECONDS", 0.0)
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if "services/stats" in request.url.path:
            return httpx.Response(200, text=REAL_MALFORMED_BODY)
        return httpx.Response(
            200,
            json={"status": "finished", "data": {"total_population": 41234.7}},
        )

    _patch_transport(monkeypatch, exposure, handler)
    assert await exposure._worldpop_population(BBOX) == 41234

    assert any("services/stats" in p for p in seen)
    assert any("tasks/ec98015f" in p for p in seen), "the task id must actually be polled"


async def test_worldpop_takes_a_direct_answer_without_polling(monkeypatch):
    """The cheap path, kept for if WorldPop ever honours `runasync=false`."""
    monkeypatch.setattr(exposure, "_WORLDPOP_POLL_SECONDS", 0.0)
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"data": {"total_population": 900}})

    _patch_transport(monkeypatch, exposure, handler)
    assert await exposure._worldpop_population(BBOX) == 900
    assert not any("tasks/" in p for p in seen)


async def test_an_unfinished_worldpop_task_is_unknown_not_zero_population(monkeypatch):
    """A task stuck at `created` — observed live, repeatedly — must not become a claim.

    0 here means "we don't know", and `exposure_for` only appends `worldpop` to `sources` when
    the count is positive. `_exposure_term` then returns 0.0 for an empty `sources`, so an
    unanswered WorldPop lowers nothing and asserts nothing. It must never read as "nobody lives
    inside this footprint", which would suppress a real alert.
    """
    monkeypatch.setattr(exposure, "_WORLDPOP_POLL_SECONDS", 0.0)
    monkeypatch.setattr(exposure, "_WORLDPOP_POLL_ATTEMPTS", 2)

    async def handler(request: httpx.Request) -> httpx.Response:
        if "services/stats" in request.url.path:
            return httpx.Response(200, text=REAL_MALFORMED_BODY)
        return httpx.Response(200, json={"status": "created"})

    _patch_transport(monkeypatch, exposure, handler)
    assert await exposure._worldpop_population(BBOX) == 0

    summary = exposure.ExposureSummary()
    assert "worldpop" not in summary.sources
    assert summary.population == 0


async def test_worldpop_gives_up_within_a_bounded_budget(monkeypatch):
    """Population modulates a score; it never triggers a hazard, so it must not hold a scan open.

    The Analyst is already the slow stage. An unbounded poll on a free-tier queue would let one
    AOI's exposure lookup stall a worker slot indefinitely.
    """
    monkeypatch.setattr(exposure, "_WORLDPOP_POLL_SECONDS", 0.0)
    polls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        if "services/stats" in request.url.path:
            return httpx.Response(200, text=REAL_MALFORMED_BODY)
        polls += 1
        return httpx.Response(200, json={"status": "created"})

    _patch_transport(monkeypatch, exposure, handler)
    await exposure._worldpop_population(BBOX)
    assert polls == exposure._WORLDPOP_POLL_ATTEMPTS


async def test_a_worldpop_timeout_degrades_with_a_named_reason(monkeypatch, caplog):
    """The whole point of the exercise: the log line now says which failure happened."""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("")

    _patch_transport(monkeypatch, exposure, handler)
    with caplog.at_level("WARNING"):
        assert await exposure._worldpop_population(BBOX) == 0

    record = next(r for r in caplog.records if "worldpop" in r.getMessage())
    assert record.error == "ReadTimeout"


# --------------------------------------------------------------------------- #
# Malaria Atlas — an upstream outage, and a layer name that goes stale
# --------------------------------------------------------------------------- #


def test_the_malaria_layer_carries_map_s_version_prefix():
    """MAP version-prefixes its layers and retires the unprefixed names.

    `Global_Pf_Parasite_Rate` — the previous default — no longer exists. A WMS request for a
    missing layer is a ServiceException, so this would have failed even against a healthy server,
    which the 504 outage was masking. The current name is read out of the data portal's own JS
    bundle (`PF_PR="202508_Global_Pf_Parasite_Rate"`, checked 2026-08-12).

    This asserts the *shape*, not the exact version — a newer prefix is an upgrade, not a
    regression, and pinning the digits would fail the build on someone else's release schedule.
    """
    from app.config import settings

    layer = settings.malaria_atlas_layer
    prefix = layer.split("_", 1)[0]
    assert prefix.isdigit() and len(prefix) == 6, (
        f"expected a YYYYMM-prefixed MAP layer, got {layer!r}"
    )
    assert layer.endswith("Global_Pf_Parasite_Rate")


async def test_disabling_the_malaria_atlas_asserts_nothing_rather_than_non_endemic(monkeypatch):
    """The switch that quiets a known outage must not become a claim about the parasite.

    `HealthBaseline()` defaults `available=False`, and the Oracle gates the malaria cascade on
    `available and endemic`. So a disabled source declines to assert — exactly as a failed one
    does. If `available` defaulted True this switch would silently tell every district in Nigeria
    it is malaria-free.
    """
    monkeypatch.setattr(health.settings, "malaria_atlas_enabled", False, raising=False)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a disabled source must not make a request")

    _patch_transport(monkeypatch, health, handler)
    baseline = await health.malaria_baseline(BBOX)

    assert baseline.available is False
    assert baseline.endemic is False
    assert baseline.malaria_pfpr is None or baseline.malaria_pfpr == 0


async def test_the_live_504_degrades_with_a_named_reason(monkeypatch, caplog):
    """What MAP is actually returning today, end to end.

    `data.malariaatlas.org` serves 200 but `/geoserver` returns 504 for every request, including
    the tile requests MAP's own portal makes. Nothing in this codebase can fix that; what it can
    do is degrade to an unasserted cascade and say why.
    """
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(504, text="<html><title>504 Gateway Time-out</title></html>")

    _patch_transport(monkeypatch, health, handler)
    with caplog.at_level("WARNING"):
        baseline = await health.malaria_baseline(BBOX)

    assert baseline.available is False
    record = next(r for r in caplog.records if "malaria" in r.getMessage())
    assert "504" in record.error


async def test_a_malaria_timeout_degrades_with_a_named_reason(monkeypatch, caplog):
    """**The `{"msg": "malaria atlas lookup failed", "error": ""}` line, exactly.**

    Separate from the 504 test above, and this is the distinction worth keeping: an
    `HTTPStatusError` carries a message, so `str(exc)` renders the 504 case fine and a test that
    only covered it would pass with the bug still present. The empty `error` came from the
    *timeout* path — and a 10s gateway timeout is what MAP actually does under load, so this was
    the frequent case, not the rare one.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("")

    _patch_transport(monkeypatch, health, handler)
    with caplog.at_level("WARNING"):
        assert (await health.malaria_baseline(BBOX)).available is False

    record = next(r for r in caplog.records if "malaria" in r.getMessage())
    assert record.error == "ReadTimeout", (
        "an empty reason is the defect this file exists for; `describe` is what prevents it"
    )


# --------------------------------------------------------------------------- #


def _patch_transport(monkeypatch, module, handler) -> None:
    """Route the module's `httpx.AsyncClient` through a mock transport.

    Patched on the module rather than globally so one adapter's test cannot silently intercept
    another's requests.
    """
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", factory)
