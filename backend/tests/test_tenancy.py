"""Every read route is either tenant-scoped or deliberately public. No third case.

## Why this test is a sweep rather than a list of cases

Three unauthenticated cross-tenant leaks were found in one session, and only the first was reported
by a user:

| Route | Returned to a bare curl |
|---|---|
| `GET /alerts` | Every subscriber's advisories, plot names, delivery receipts |
| `GET /subscribers` | Full name, email address, and every plot's exact bounding box |
| `GET /risk/areas/{aoi_id}` | A named plot's severity, score and full evidence list |
| `GET /verification/{assessment_id}` | The verdict on a named plot's hazard |

Each had the same shape: a GET with no dependency, where scoping was either absent or a query
parameter the caller supplied. Fixing them one at a time is how the second, third and fourth came to
exist while the first was being patched.

So this enumerates **every** GET route on the app and requires each to be in exactly one of two
sets: it depends on `resolve_audience` / a platform scope, or it is named in `PUBLIC_READS` with a
reason. A new unguarded read fails the build; a deliberately public one is a one-line, reviewed
addition.
"""

from __future__ import annotations

import ast
import pathlib

ROUTES = pathlib.Path("app/api/routes")

#: Reads that are public ON PURPOSE. Each entry states why, because "it was already like that" is
#: how the leaks above survived review.
PUBLIC_READS: dict[str, str] = {
    # --- service description: no subscriber, area or contact appears in any of these ---
    "health": "liveness for load balancers and uptime checks",
    "ready": "readiness probe; the container healthcheck calls it before the port opens",
    "bootstrap": "reports whether a fresh deployment has anything configured yet",
    "search_probe": "whether web search is reachable; returns no results",
    # --- the API contract, ungated so an integrator can generate a client first ---
    "partner_openapi": "the partner OpenAPI document; a contract is not data",
    "developer_docs": "the partner documentation page",
    "docs_favicon": "a favicon",
    "webhook_info": "webhook delivery semantics — documentation",
    "event_schema": "webhook payload shapes — documentation",
    # --- aggregates with no tenant in them ---
    "metrics": (
        "verdict counts and precision across the platform. Deliberately readable without a "
        "credential: it is the one number that says whether the service works, and it names no "
        "subscriber, area or hazard instance"
    ),
    "training_set": (
        "row counts and feature NAMES for retraining readiness — never a subscriber's assessment. "
        "Its own docstring commits to this"
    ),
    # --- geocoding: a public gazetteer, not our data ---
    "search_places": "forward geocoding over public place names",
    "reverse_place": "reverse geocoding of a coordinate the caller already holds",
}


def _get_routes() -> list[tuple[str, str, str]]:
    """`(module, function, signature)` for every `@router.get` handler."""
    found: list[tuple[str, str, str]] = []
    for path in sorted(ROUTES.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            gets = [
                d
                for d in node.decorator_list
                if isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "get"
            ]
            if not gets:
                continue
            found.append(
                (path.name, node.name, ast.unparse(gets[0]) + ast.unparse(node.args))
            )
    return found


def test_every_read_route_is_scoped_or_declared_public():
    """**The sweep.** A new unguarded GET fails here rather than in production.

    "Guarded" means the route resolves who is calling — through `resolve_audience` for tenant data,
    or `require_platform_scope` / `current_account` / `current_aggregator` for operator and
    account-management surfaces. Any of those is a deliberate decision about authority; no
    dependency at all is not.
    """
    guards = (
        "resolve_audience",
        "require_platform_scope",
        "current_account",
        "current_aggregator",
        "verified_account",
        "current_key_holder",
        "require_permission",
        "require_workspace_permission",
        "require_scope",
        "password_setup_session",
        "current_session",
        # The legacy shared key. A real credential, and being retired
        # (`IAM_LEGACY_SHARED_KEY_ENABLED`) — but a route behind it is a decision about
        # authority, which is what this sweep tests for.
        "require_api_key",
        # Webhook management: a platform key with `platform:operate`, or an aggregator key with
        # `webhooks:manage` scoped to its OWN subscriptions. Raises 403 with neither, and every
        # per-subscription route additionally proves ownership through `_owned_or_404` — which
        # returns 404 rather than 403 for another tenant's id, so the routes are not a membership
        # oracle over other aggregators' integrations.
        "webhook_caller",
    )

    unguarded: list[str] = []
    for module, name, blob in _get_routes():
        if any(guard in blob for guard in guards):
            continue
        if name in PUBLIC_READS:
            continue
        unguarded.append(f"{module}::{name}")

    assert not unguarded, (
        "these GET routes resolve no caller and are not declared public — each one returns "
        f"whatever it holds to an anonymous request: {unguarded}. Either add a dependency or add "
        "an entry to PUBLIC_READS stating why it is safe."
    )


def test_the_public_read_list_does_not_go_stale():
    """An entry for a route that no longer exists hides the fact that nothing checks it.

    Same reasoning as `KNOWN_UNREACHED` in `test_schema_contract.py`: an allow-list is only as good
    as its accuracy, and a stale one silently widens.
    """
    live = {name for _module, name, _blob in _get_routes()}
    stale = sorted(set(PUBLIC_READS) - live)

    assert not stale, f"PUBLIC_READS names routes that no longer exist: {stale}"


def test_the_tenant_data_routes_specifically_use_the_audience_resolver():
    """The four routes from the incident, pinned by name.

    The sweep above would accept any guard, including a platform scope — which for these would be
    wrong: they are subscriber-facing, so a portal session has to work. Only `resolve_audience`
    handles all three caller types, so these four must use it specifically.
    """
    required = {
        ("alerts.py", "list_alerts"),
        ("alerts.py", "get_alert"),
        ("subscribers.py", "list_subscribers"),
        ("subscribers.py", "get_subscriber"),
        ("subscribers.py", "list_areas"),
        ("risk.py", "latest_assessment"),
        ("verification.py", "get_verification"),
    }
    seen = {
        (module, name)
        for module, name, blob in _get_routes()
        if "resolve_audience" in blob
    }
    missing = sorted(required - seen)

    assert not missing, (
        f"these routes serve one tenant's data and must resolve the audience: {missing}"
    )


def test_unrestricted_access_is_reachable_only_through_the_platform_scope():
    """`Audience.permitted_subscriber_ids is None` is the global-read key. One door only.

    Asserted against the resolver's source because the danger is a *new* branch returning an
    unrestricted audience — a plausible-looking "if the account is an admin" would reintroduce the
    leak for a whole class of caller.
    """
    source = pathlib.Path("app/api/audience.py").read_text()
    unrestricted = [
        line
        for line in source.splitlines()
        if "permitted_subscriber_ids=None" in line and not line.strip().startswith("#")
    ]

    assert len(unrestricted) == 1, (
        f"exactly one branch may grant an unrestricted audience; found {len(unrestricted)}"
    )


def test_an_empty_audience_is_never_treated_as_unrestricted():
    """The precise bug: an empty set is falsy, and `if not permitted` would catch both.

    A brand-new aggregator has zero customers. Under the original code that meant "no filter",
    which is why `WBMLMQ4J5Z` saw an unrelated individual's alerts on first login.
    """
    for module in ("alerts.py", "subscribers.py"):
        source = (ROUTES / module).read_text()
        assert "if permitted is None:" in source, (
            f"{module} must test unrestricted with `is None`; a falsy check also matches the "
            f"empty set, which is what leaked to a customerless aggregator"
        )


def test_cross_tenant_reads_are_indistinguishable_from_missing_records():
    """404, never 403 — a 403 confirms the id exists.

    Subscriber ids, area ids and alert ids all appear in URLs, logs and webhook payloads. They are
    not secrets, so the response must not turn one into a membership oracle.
    """
    for module, marker in (
        ("subscribers.py", "cross-tenant subscriber read refused"),
        ("risk.py", "cross-tenant assessment read refused"),
        ("verification.py", "cross-tenant verdict read refused"),
        ("alerts.py", "cross-tenant alert read refused"),
    ):
        source = (ROUTES / module).read_text()
        assert marker in source, f"{module} does not log a refused cross-tenant read"
        refusal = source.split(marker)[1][:400]
        assert "404" in refusal, f"{module} refuses with something other than 404"
        assert "403" not in refusal, f"{module} leaks existence via 403"
