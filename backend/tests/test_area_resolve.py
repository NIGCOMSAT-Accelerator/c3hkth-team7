"""The partner address-to-monitored-area flow, and the guard that makes it two calls.

## What this covers

    POST /iam/customers/{account_id}/areas/resolve   address -> geometry + resolution_token
    POST /iam/customers/{account_id}/areas           token   -> monitored area, scan queued

The interesting assertions are not about geocoding — `test_place_precision.py` covers that. They are
about the **token**, because it is the thing standing between a plausible-but-wrong geocode and a
monitored area with a scan queued and a customer already emailed.

**Why the flow is two calls at all.** Address resolution is the one AOI input whose errors are
silent: a bad size is caught by `check_monitorable`, a bad shape by `validate_ring`, but nothing
catches a plausible coordinate in the wrong place. This platform has already registered a Nigerian
farm in England down that path. Splitting the call means an integration can see and reject a wrong
resolution rather than discovering it afterwards.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.api import resolution

BACKEND = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def store(monkeypatch):
    """An in-memory stand-in for Dragonfly, so token LOGIC is what is under test.

    The real cache is absent in unit tests, and `resolution.issue` correctly degrades to returning
    None when it cannot store — which would make every assertion below vacuously pass. Substituting
    a working store is what keeps these tests about the code rather than about the environment.
    """
    kv: dict[str, str] = {}

    async def set_text(key, value, ttl_seconds):
        assert ttl_seconds > 0, "a resolution token must never be stored without a TTL"
        kv[key] = value

    async def get_text(key):
        return kv.get(key)

    async def delete(*keys):
        for key in keys:
            kv.pop(key, None)

    monkeypatch.setattr(resolution.cache, "set_text", set_text)
    monkeypatch.setattr(resolution.cache, "get_text", get_text)
    monkeypatch.setattr(resolution.cache, "delete", delete)
    return kv


AREA = {
    "name": "Wuse Market",
    "bbox": {"west": 7.46, "south": 9.06, "east": 7.47, "north": 9.07},
    "hectares": 7.74,
}


# --------------------------------------------------------------------------- #
# The token
# --------------------------------------------------------------------------- #


async def test_a_token_round_trips_the_exact_area(store):
    """**The guarantee the token exists to provide.**

    What gets monitored is byte-for-byte what was resolved and shown to the partner, rather than
    something their code rebuilt from the response fields. A shifted spreadsheet column cannot
    change the geometry, only which token is presented.
    """
    token = await resolution.issue(area=AREA, account_id="ACC1", aggregator_id="AGG1")
    assert token and token.startswith("shltres_")

    got = await resolution.redeem(token, account_id="ACC1", aggregator_id="AGG1")
    assert got == AREA


async def test_a_token_is_single_use(store):
    """A second redemption must fail.

    Not for replay-attack reasons — the token authorises nothing on its own and the caller is
    already authenticated by their API key — but because a re-redeemable token invites a retry loop
    that creates duplicate areas for one plot.
    """
    token = await resolution.issue(area=AREA, account_id="ACC1", aggregator_id="AGG1")
    assert await resolution.redeem(token, account_id="ACC1", aggregator_id="AGG1") is not None
    assert await resolution.redeem(token, account_id="ACC1", aggregator_id="AGG1") is None


async def test_a_token_is_bound_to_its_customer(store):
    """**The failure nothing downstream could detect.**

    An aggregator legitimately holds many customers. If a token minted for one were usable against
    another, a spreadsheet with a shifted column would quietly attach every plot to the wrong
    farmer — and every number afterwards would still look correct, because the geometry is valid,
    just attributed to the wrong person.
    """
    token = await resolution.issue(area=AREA, account_id="ACC1", aggregator_id="AGG1")
    assert await resolution.redeem(token, account_id="ACC2", aggregator_id="AGG1") is None


async def test_a_token_is_bound_to_its_aggregator(store):
    """Cross-tenant, the same argument one level up."""
    token = await resolution.issue(area=AREA, account_id="ACC1", aggregator_id="AGG1")
    assert await resolution.redeem(token, account_id="ACC1", aggregator_id="AGG9") is None


@pytest.mark.parametrize("bad", ["", "nope", "shltky_wrongprefix", "shltres_"])
async def test_a_malformed_token_is_refused(store, bad):
    assert await resolution.redeem(bad, account_id="ACC1", aggregator_id="AGG1") is None


async def test_an_unreachable_cache_yields_no_token_rather_than_a_broken_one(monkeypatch):
    """**Resolve must still answer.** The geometry is correct even when the token cannot be stored.

    `resolution_token: null` is reported rather than omitted, so the partner learns the confirmation
    guarantee is unavailable instead of discovering it on the write. Failing the resolve entirely
    would be worse: the geocoding succeeded and that answer has value.
    """

    async def boom(*args, **kwargs):
        raise RuntimeError("cache down")

    monkeypatch.setattr(resolution.cache, "set_text", boom)
    assert await resolution.issue(area=AREA, account_id="A", aggregator_id="B") is None


async def test_the_ttl_is_bounded_and_short():
    """Ten minutes, and the reason is not arbitrary.

    The token pins a geometry. One pinned overnight could be committed against a customer whose
    subscription was cancelled in between, or after the underlying place data changed. Long enough
    for an operator to eyeball a map or an importer to commit a page of rows; not longer.
    """
    assert 60 <= resolution.TTL_SECONDS <= 3600


# --------------------------------------------------------------------------- #
# The routes
# --------------------------------------------------------------------------- #


def _route_source(name: str) -> str:
    """The source of one handler in `api/routes/iam.py`, via AST rather than a text search."""
    tree = ast.parse((BACKEND / "app/api/routes/iam.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return ast.get_source_segment(
                (BACKEND / "app/api/routes/iam.py").read_text(), node
            ) or ""
    raise AssertionError(f"handler {name} not found")


def _body_source(name: str) -> str:
    """One handler's source with its DOCSTRING removed.

    Necessary for any ordering assertion: these docstrings explain the ordering they enforce, so
    `source.index("owned_account")` matched the prose rather than the call and the test passed with
    the code reordered. Found by mutation testing — the mutant escaped.
    """
    path = BACKEND / "app/api/routes/iam.py"
    text = path.read_text()
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body = body[1:]  # drop the docstring
            return "\n".join(ast.get_source_segment(text, stmt) or "" for stmt in body)
    raise AssertionError(f"handler {name} not found")


def test_resolve_requires_the_write_scope():
    """`customers:write`, the same scope as the write it precedes.

    A caller who cannot create an area has no use for a token that commits one — and a read-only
    key being able to mint them would make the scope boundary meaningless.
    """
    source = _route_source("resolve_customer_area")
    assert "require_scope(ApiKeyScope.WRITE)" in source


def test_resolve_authorises_before_it_geocodes():
    """Order matters, for two reasons.

    An unknown customer id must not be usable to run free lookups against our rate-limited
    upstream. And `owned_account` 404s on another tenant's customer with the same message as a
    genuine miss — doing the geocode first would leak existence through timing.
    """
    source = _body_source("resolve_customer_area")
    owned_at = source.index("owned_account")
    resolve_at = source.index("await resolve(")
    assert owned_at < resolve_at, "authorise before spending an upstream call"


def test_resolve_writes_nothing():
    """A dry run. No area, no scan, no email — that is the whole point of the split."""
    source = _route_source("resolve_customer_area")

    for forbidden in ("add_area", "enqueue_scan", "send_area_added", "record_attribution"):
        assert forbidden not in source, f"resolve must not {forbidden}"


def test_the_write_still_accepts_a_bare_area():
    """**Backward compatibility, asserted.**

    Every existing integration posts a bare `AreaOfInterest` to this route. `AreaCreateRequest`
    subclasses it precisely so that body stays valid unchanged — a wrapper (`{"area": {...}}`) would
    have broken all of them.
    """
    from app.api.routes.iam import AreaCreateRequest
    from app.models.schemas import AreaOfInterest

    assert issubclass(AreaCreateRequest, AreaOfInterest)

    # The pre-existing shape must validate with no token.
    body = AreaCreateRequest.model_validate(
        {"name": "Direct plot", "bbox": {"west": 3.4, "south": 7.1, "east": 3.5, "north": 7.2}}
    )
    assert body.resolution_token is None
    assert body.bbox is not None


def test_geometry_cannot_be_overridden_alongside_a_token():
    """The token pins the geometry; only labels may be changed.

    Letting a caller supply a token *and* a bbox would reintroduce exactly the drift the token
    exists to prevent — and silently, because both inputs would look valid.
    """
    source = _route_source("_area_from_request")

    # On the token path, the area comes from the stored payload. Only name and crop are re-applied.
    token_branch = source[source.index("if payload.resolution_token:") : source.index("if payload.bbox is None")]
    assert "AreaOfInterest.model_validate(stored)" in token_branch
    assert "payload.name" in token_branch and "payload.crop" in token_branch
    for geometry_field in ("payload.bbox", "payload.geometry"):
        assert geometry_field not in token_branch, (
            f"{geometry_field} must not influence a token-committed area"
        )


def test_an_invalid_token_is_422_and_names_the_recovery():
    """Not 403 or 404, and the message says what to do.

    Unknown, expired, reused and issued-for-another-customer are deliberately indistinguishable:
    the recovery is identical in every case, and distinguishing them would confirm a token existed.
    """
    source = _route_source("_area_from_request")
    assert "HTTP_422_UNPROCESSABLE_ENTITY" in source
    assert "areas/resolve" in source, "the error must name the endpoint that issues a new token"


def test_neither_form_supplied_explains_both_forms():
    """A field-level 422 would read as though `name` and `bbox` were always mandatory."""
    source = _route_source("_area_from_request")
    tail = source[source.index("if payload.bbox is None") :]
    assert "resolution_token" in tail and "bbox" in tail


def test_the_resolve_endpoint_is_in_the_partner_reference():
    """A partner-facing route absent from the filtered spec is a route integrators cannot find.

    `devdocs.PARTNER_PREFIXES` matches on `/iam/customers`, so this needed no allow-list change —
    which is exactly the kind of assumption worth verifying rather than trusting, because a prefix
    that silently fails to match produces a reference missing the endpoint.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    spec = TestClient(app).get("/shelter/v1/api/dev-docs/openapi.json").json()
    assert any("areas/resolve" in path for path in spec["paths"]), (
        "the partner reference must document the resolve endpoint"
    )
