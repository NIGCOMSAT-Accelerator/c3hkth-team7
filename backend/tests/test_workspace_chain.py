"""`Workspace > Customer > Area > Alerts`, and the API key that lets a partner use it.

## The bug

A real aggregator test account (`WBMLMQ4J5Z`) had two active monitoring areas and a workspace
reporting **zero customers**. Both true, and together they made no sense — which is what a broken
association looks like from outside.

`POST /iam/activate` hard-coded `owner_kind=INDIVIDUAL`, and it is reached by BOTH kinds of account:
an aggregator activating its own monitoring goes through exactly there. So every plot it created was
recorded `owner_kind=individual, owner_id=<the aggregator>, workspace_id=None` — attributed to
nobody's workspace, belonging to no customer.

It compounds, which is why it survived several rounds of testing. `_inherit_attribution` copies the
owner from the subscriber's **first** attributed area, so one wrong row at activation made every
later plot wrong too — silently, and consistently enough to look deliberate.

## Why a wrong row was invisible

No view would have shown it. `CustomerManager` iterates customers, so an area belonging to the
aggregator itself, or one whose attribution names no workspace, appears in no list at all. The data
was wrong and unobservable at the same time, and fixing only the write path would leave the next
wrong row equally unobservable.

Hence two halves here: the attribution is correct on write, and `GET /workspaces/{id}/areas` makes
the whole chain readable — including the rows that are broken.

## The API key half

`POST /iam/api-keys` always worked and no portal surface called it, so the page documented a curl
command. That made the Partner API unreachable in practice: the key is the only way to authenticate
against it, and obtaining one required hand-crafting a request with a session token the aggregator
had no way to read.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

from app.api.routes import iam as iam_routes
from app.iam.attribution import OwnerKind  # noqa: F401  (documents the enum under test)
from app.iam.models import AccountKind, ApiKeyScope

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"
IAM_SOURCE = pathlib.Path(inspect.getfile(iam_routes)).read_text()


def _function(name: str) -> ast.AST:
    for node in ast.walk(ast.parse(IAM_SOURCE)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in app/api/routes/iam.py")


# --------------------------------------------------------------------------- #
# The write half: activation must attribute a commercial account to its workspace
# --------------------------------------------------------------------------- #


def test_activation_branches_on_the_account_kind():
    """**The bug, asserted structurally.**

    `activate` serves individuals and aggregators alike. A single unconditional
    `OwnerKind.INDIVIDUAL` is therefore wrong for half its callers, and produces an area that
    belongs to no workspace and no customer.
    """
    source = ast.unparse(_function("activate"))

    assert "AccountKind.COMMERCIAL" in source, (
        "activate does not branch on the account kind, so a commercial account's own plot is "
        "attributed as an individual's — no workspace, no customer, and invisible in every "
        "workspace view"
    )
    assert "OwnerKind.AGGREGATOR" in source, (
        "activate never records an AGGREGATOR-owned area, so an aggregator's own monitoring is "
        "not attributed to its organisation"
    )
    assert "OwnerKind.INDIVIDUAL" in source, (
        "the B2C path must still attribute the individual — they are their own billable owner"
    )


def test_a_commercial_activation_resolves_a_workspace():
    """An aggregator's own plot belongs to a project, not to no project.

    `workspace_id=None` on an aggregator-owned area is what made the workspace report zero
    customers while holding two areas: the row existed and matched no workspace filter.
    """
    source = ast.unparse(_function("activate"))
    assert "ensure_default_workspace" in source, (
        "activation records an aggregator-owned area without resolving a workspace, so the "
        "per-workspace totals under-sum and the area appears in no workspace view"
    )


def test_every_attribution_write_names_an_owner_kind_explicitly():
    """No caller may rely on a default.

    `record_attribution` has no default for `owner_kind` deliberately — a default would be right
    for one caller and silently wrong for the other, which is the shape of the original defect.
    """
    for node in ast.walk(ast.parse(IAM_SOURCE)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None)
        if name != "record_attribution":
            continue
        kwargs = {kw.arg for kw in node.keywords}
        assert "owner_kind" in kwargs, (
            "a record_attribution call omits owner_kind; it must be stated at every write"
        )
        assert "owner_id" in kwargs, "a record_attribution call omits owner_id"


# --------------------------------------------------------------------------- #
# The read half: the chain must be observable, including where it is broken
# --------------------------------------------------------------------------- #


def test_the_workspace_areas_route_exists_and_is_permission_scoped():
    """One call answering `Workspace > Customer > Area > Alerts`.

    Scoped by `require_workspace_permission`, which resolves the caller's role **on this
    workspace** — so a member who is View-Only here cannot read it by holding a wider role on
    another project. That is the same boundary `workspace_customers` holds; a new route that
    forgot it would be a cross-project read.
    """
    node = _function("workspace_areas")
    source = ast.unparse(node)

    assert "require_workspace_permission" in source, (
        "workspace_areas is not scoped to the caller's role on the named workspace"
    )
    assert "VIEW_CUSTOMERS" in source


def test_the_row_carries_the_customer_the_channels_and_the_alert_queue():
    """All four links, or the view cannot answer the question it exists for."""
    fields: set[str] = set()
    for node in ast.walk(ast.parse(IAM_SOURCE)):
        if isinstance(node, ast.ClassDef) and node.name == "WorkspaceAreaRow":
            fields = {
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            }
            break

    for required in (
        "aoi_id",
        "customer_account_id",
        "customer_name",
        "customer_email",
        "channels",
        "alert_count",
        "delivery_mode",
        "is_own_plot",
    ):
        assert required in fields, f"WorkspaceAreaRow is missing {required!r}"


def test_an_unattributed_area_is_surfaced_rather_than_filtered_out():
    """**The invisibility half of the bug.**

    A row whose `workspace_id` is None is included, because that is precisely the broken state the
    aggregator needs to see. Filtering it would restore the condition where the data is wrong and no
    view can show it.

    A row naming a *different* workspace is correctly excluded — that is another project's business.
    """
    # Asserting the BEHAVIOUR of the filter, not the shape of the source. An earlier version of
    # this test string-matched the unparsed AST for `"None,"`, which is both brittle and not
    # actually a test of anything — it would pass on source that read `if x is None: continue`.
    #
    # The rule under test, extracted verbatim from the route: a record is skipped only when it
    # names a workspace that is not this one.
    def skipped(record: dict | None, workspace_id: str) -> bool:
        return record is not None and record.get("workspace_id") not in (None, workspace_id)

    ws = "VDK6N8GGPY"

    assert not skipped(None, ws), "an area with NO attribution row must be listed"
    assert not skipped({"workspace_id": None}, ws), (
        "an area attributed with workspace_id=None must be listed — that is the broken state the "
        "aggregator needs to see, and hiding it is what made the original bug invisible"
    )
    assert not skipped({"workspace_id": ws}, ws), "this workspace's own area must be listed"
    assert skipped({"workspace_id": "OTHERWS123"}, ws), (
        "another project's area must NOT appear in this workspace's view"
    )

    # And the route really does apply that rule.
    source = ast.unparse(_function("workspace_areas"))
    assert "get('workspace_id') not in (None, workspace_id)" in source, (
        "the route's workspace filter no longer matches the rule asserted above; update both "
        "together or the assertion is testing nothing"
    )


def test_the_channels_are_resolved_per_area():
    """A per-plot binding REPLACES the general ones rather than adding to them.

    Listing every binding on file would tell an aggregator their farmer receives two emails when
    they receive one. `channels_for(aoi_id=...)` owns that rule; re-deriving it here would duplicate
    it.
    """
    source = ast.unparse(_function("workspace_areas"))
    assert "channels_for" in source
    assert "aoi_id=area.id" in source, (
        "channels are resolved without the area, so per-plot overrides are not reflected"
    )


# --------------------------------------------------------------------------- #
# The API key half — the Partner API is unreachable without it
# --------------------------------------------------------------------------- #


def test_the_portal_can_mint_a_key():
    """It could not, which made the Partner API unusable for the accounts it is for.

    `POST /iam/api-keys` worked the whole time; nothing in the portal called it, so the page
    documented a curl command requiring a session token the aggregator had no way to read.
    """
    actions = FRONTEND / "app/portal/api-keys/actions.ts"
    assert actions.exists(), "no server action for key creation"

    source = actions.read_text()
    assert "createApiKey" in source, "the action does not call the create endpoint"
    assert "revokeApiKey" in source, "no way to revoke a key that leaked"

    client = (FRONTEND / "lib/api.ts").read_text()
    assert "createApiKey:" in client
    assert "workspace_id" in client, (
        "createApiKey does not send a workspace; a key silently landing on the default workspace "
        "is only discovered when it returns the wrong customers"
    )


def test_the_key_form_offers_only_real_scopes():
    """A scope the API does not know is a checkbox that produces a 422 on submit."""
    minter = FRONTEND / "app/portal/api-keys/KeyMinter.tsx"
    if not minter.exists():  # pragma: no cover - backend-only checkout
        return

    source = minter.read_text()
    offered = {
        line.split('value: "')[1].split('"')[0]
        for line in source.splitlines()
        if 'value: "' in line and ":" in line
    }
    real = {s.value for s in ApiKeyScope}

    assert offered, "parsed no scopes out of KeyMinter; the parser has stopped working"
    assert offered <= real, (
        f"the key form offers scopes the API does not accept: {sorted(offered - real)}"
    )


def test_the_action_filters_scopes_server_side():
    """A checkbox name is user input.

    The API refuses anything above the caller's role regardless, but the form must not be the thing
    relying on that — a crafted POST should not be able to request a scope the picker never showed.
    """
    source = (FRONTEND / "app/portal/api-keys/actions.ts").read_text()
    assert "GRANTABLE" in source and ".filter(" in source, (
        "the create action forwards submitted scope names unfiltered"
    )


def test_the_secret_is_never_persisted():
    """It is returned once and stored as a hash.

    So the action must not put it in the audit detail or anywhere else — an audit row is exactly the
    place a plaintext credential would survive unnoticed.
    """
    source = (FRONTEND / "app/portal/api-keys/actions.ts").read_text()

    audit_lines = [ln for ln in source.splitlines() if "recordPortalEvent" in ln]
    assert audit_lines, "key creation is not audited at all"
    for line in audit_lines:
        assert "secret" not in line and "created.key" not in line, (
            f"the plaintext key reaches the audit log: {line.strip()}"
        )


def test_individuals_cannot_mint_keys():
    """A farmer has nothing to integrate, and a credential they cannot use can only be phished.

    Enforced by `can_use_api` on the route; asserted here so a refactor that drops the check fails.
    """
    source = ast.unparse(_function("create_api_key"))
    assert "can_use_api" in source
    assert str(AccountKind.COMMERCIAL.value) in source or "commercial" in source.lower()


# --------------------------------------------------------------------------- #
# Webhooks — the async channel the Partner API flow depends on
# --------------------------------------------------------------------------- #


def _webhook_functions():
    from app.api.routes import webhooks as webhook_routes

    source = pathlib.Path(inspect.getfile(webhook_routes)).read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            decorators = " ".join(ast.unparse(d) for d in node.decorator_list)
            if "router." in decorators and "/subscriptions" in decorators:
                yield node.name, decorators, ast.unparse(node)


def test_an_aggregator_key_can_manage_its_own_webhooks():
    """**The bug that made `webhooks:manage` inert.**

    Every webhook route required `platform:operate`, which only the operations team's key holds. So
    the scope was offered when minting a key, documented as "manage webhook subscriptions belonging
    to this aggregator", and refused every aggregator that used it.

    That is worse than an unusable endpoint. An aggregator relaying alerts itself
    (`delivery_mode='webhook'`) has SHELTER contact nobody directly — so with no registered endpoint
    the alert has nowhere to go at all, and the farmer is watched while nobody is told.
    """
    for name, decorators, body in _webhook_functions():
        assert "PLATFORM_OPERATE" not in decorators + body, (
            f"{name} is still platform-only, so an aggregator cannot manage its own webhooks"
        )
        assert "webhook_caller" in body, (
            f"{name} does not resolve a webhook caller; it is either unauthenticated or "
            f"platform-only"
        )


def test_every_per_subscription_route_proves_ownership():
    """**Why the scope could not simply be widened.**

    `webhook_subscriptions` had no owner column, so admitting aggregator keys without tenancy would
    have let any aggregator list, rotate, deactivate and DELETE every other aggregator's endpoint. A
    cross-tenant write, not merely a read — and the platform-only scope was the only thing holding
    that line.

    So each route addressing one subscription by id must pass it through `_owned_or_404` before
    acting.
    """
    unguarded: list[str] = []
    checked = 0

    for name, decorators, body in _webhook_functions():
        if "{subscription_id}" not in decorators:
            continue
        checked += 1
        if "_owned_or_404" not in body:
            unguarded.append(name)

    assert checked >= 6, (
        f"only {checked} per-subscription routes found; the AST walk has stopped matching and "
        f"this test is no longer checking anything"
    )
    assert not unguarded, (
        f"these routes act on a subscription by id without proving the caller owns it: "
        f"{unguarded}. One aggregator could rotate or delete another's endpoint."
    )


def test_a_cross_tenant_subscription_is_404_not_403():
    """Same reasoning as `get_alert` and `get_customer`.

    A 403 confirms the id exists, turning these routes into an enumeration oracle over other
    aggregators' integrations. Subscription ids appear in delivery logs and support threads.
    """
    from app.api.routes import webhooks as webhook_routes

    source = pathlib.Path(inspect.getfile(webhook_routes)).read_text()
    guard = next(
        ast.unparse(n)
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        and n.name == "_owned_or_404"
    )

    assert "HTTP_404_NOT_FOUND" in guard
    assert "HTTP_403_FORBIDDEN" not in guard, (
        "a cross-tenant subscription returns 403, which confirms the id exists"
    )


def test_the_owner_filter_distinguishes_unrestricted_from_empty():
    """`owner_account_id=None` means UNRESTRICTED, not "platform-owned rows only".

    The same trap as `Audience.permitted_subscriber_ids`, where conflating a falsy scope with "no
    filter" is exactly how a brand-new aggregator saw an unrelated farmer's data. Asserted on the
    store's actual query construction.
    """
    from app.webhooks import store as webhook_store

    source = inspect.getsource(webhook_store.list_subscriptions)

    assert "owner_account_id is not None" in source, (
        "the owner filter uses a truthiness test, so an empty-string owner would silently read as "
        "unrestricted"
    )


def test_the_migration_leaves_existing_rows_platform_owned():
    """Pre-existing subscriptions belong to the operations team, not to a guessed aggregator.

    NULL is the honest value. Inventing an owner would hand a live integration — and its signing
    secret's lifecycle — to whichever account was guessed.
    """
    migrations = pathlib.Path(__file__).resolve().parents[1] / "app/db/migrations"
    sql = (migrations / "016_webhook_owner.sql").read_text()

    assert "ADD COLUMN IF NOT EXISTS owner_account_id" in sql
    # No backfill: a DEFAULT or an UPDATE here would assign ownership by guess.
    assert "UPDATE webhook_subscriptions" not in sql, (
        "the migration backfills an owner, which assigns someone else's integration by guess"
    )
