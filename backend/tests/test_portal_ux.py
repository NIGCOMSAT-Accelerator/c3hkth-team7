"""The portal's create pattern, and the 404 that revealed it was missing.

## The bug

The Webhooks page's only call-to-action linked to `/portal/webhooks/new` — a route that was never
built, so "+ Create" produced a **404**. Reported during MVP review.

The missing route was a symptom rather than the cause. Every webhook endpoint required
`platform:operate`, a scope only the operations team's key holds, so a form at that route would have
returned **403** instead. And `webhooks:manage`, the scope documented as "manage webhook
subscriptions belonging to this aggregator", is grantable only on an API key — which is minted in the
portal. So the only path to asynchronous delivery ran through the programmatic channel that
asynchronous delivery exists to serve.

`webhook_caller` now accepts a portal session carrying `integration:manage`, checked **before** the
key paths for the same reason as `resolve_audience`: the frontend attaches its service key to every
request, so checking a key first would resolve a browser request as the *platform* and hand a
signed-in aggregator unrestricted scope over every subscription on the deployment.

## The pattern

An empty state with one action, a modal for creation, and the list once records exist. A create form
that is always open occupies the page before you have done anything — on a page with four such cards
the content someone came for is below the fold.

These tests assert the pattern structurally, because it is the kind of thing that decays one page at
a time.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

from app.api.routes import webhooks as webhook_routes

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"
WEBHOOK_SOURCE = pathlib.Path(inspect.getfile(webhook_routes)).read_text()


def _read(relative: str) -> str | None:
    path = FRONTEND / relative
    return path.read_text() if path.exists() else None


# --------------------------------------------------------------------------- #
# The 404, and the scope that made it unfixable at the frontend
# --------------------------------------------------------------------------- #


def test_no_portal_page_links_to_a_route_that_does_not_exist():
    """**The reported bug, generalised.**

    "+ Create" pointed at `/portal/webhooks/new`, which nobody had built. A dead primary action is
    worse than a missing feature: it looks available, and the 404 arrives after the click.

    Sweeps every internal `/portal/...` href in the portal and requires a matching `page.tsx`, so the
    next dangling link fails the build rather than being found by a reviewer.
    """
    portal = FRONTEND / "app/portal"
    if not portal.exists():  # pragma: no cover - backend-only checkout
        return

    import re

    dangling: list[str] = []
    for file in sorted(portal.rglob("*.tsx")):
        for match in re.finditer(r'href="(/portal/[^"?#]*)"', file.read_text()):
            target = match.group(1).rstrip("/")
            segments = [s for s in target.split("/") if s][1:]  # drop "portal"

            # A dynamic segment cannot be resolved statically; a directory containing `[id]`
            # satisfies it. Walk the tree allowing bracketed directories to match anything.
            node = portal
            ok = True
            for segment in segments:
                if (node / segment).is_dir():
                    node = node / segment
                    continue
                dynamic = [d for d in node.iterdir() if d.is_dir() and d.name.startswith("[")]
                if dynamic:
                    node = dynamic[0]
                    continue
                ok = False
                break

            if not ok or not (node / "page.tsx").exists():
                dangling.append(f"{file.relative_to(FRONTEND)} -> {target}")

    assert not dangling, (
        f"these portal links point at routes with no page: {dangling}. A dead call-to-action "
        f"looks available and 404s after the click — build the route or open a modal instead."
    )


def test_a_portal_session_can_manage_its_own_webhooks():
    """Without this the Webhooks page cannot work at all.

    `webhooks:manage` is grantable only on an API key, keys are minted in the portal, and the portal
    is where an aggregator sets up the webhook they need *before* writing any integration code.
    Requiring a key to register an endpoint made asynchronous delivery reachable only through the
    programmatic channel it serves.
    """
    caller = next(
        ast.unparse(node)
        for node in ast.walk(ast.parse(WEBHOOK_SOURCE))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == "webhook_caller"
    )

    assert "portal_session" in caller, (
        "webhook_caller accepts no session, so the portal's Webhooks page cannot create an endpoint"
    )
    assert "MANAGE_INTEGRATION" in caller, (
        "the session path does not check `integration:manage` — the same permission that gates the "
        "page must gate the write"
    )


def test_the_session_is_resolved_before_the_platform_key():
    """**Order matters, and getting it wrong is a privilege escalation.**

    `frontend/lib/api.ts` attaches the platform service key to every request. If the key were
    checked first, a browser request carrying both would resolve as *unrestricted* — and a signed-in
    aggregator would see and be able to delete every subscription on the deployment.

    The same trap `resolve_audience` documents, where it was a real write vulnerability.
    """
    caller = next(
        ast.unparse(node)
        for node in ast.walk(ast.parse(WEBHOOK_SOURCE))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == "webhook_caller"
    )

    session_at = caller.index("read_session")
    platform_at = caller.index("PLATFORM_OPERATE")
    assert session_at < platform_at, (
        "the platform key is resolved before the portal session, so a browser request carrying "
        "both resolves as unrestricted — a signed-in aggregator would reach every tenant's webhooks"
    )


# --------------------------------------------------------------------------- #
# The create pattern
# --------------------------------------------------------------------------- #

#: Client components that own a create flow, and must follow the pattern.
MANAGERS = (
    "app/portal/webhooks/WebhookManager.tsx",
    "app/portal/api-keys/KeyMinter.tsx",
    "app/portal/team/TeamManager.tsx",
    "app/portal/workspace/WorkspaceEditor.tsx",
)

#: Pages with an empty state but deliberately NO modal, each for a stated reason.
#:
#: `areas` — the picker carries a map and a state → LGA → ward cascade. A map inside a scrolling
#: dialog on a phone competes for the same drag gesture, which is materially worse than a dedicated
#: panel. It is already collapsed behind "+ Add a plot", which is what the modal pattern is for.
EMPTY_STATE_ONLY = ("app/portal/areas/AreaManager.tsx",)


def test_every_create_flow_uses_the_shared_modal_and_empty_state():
    """One pattern, not one per page.

    A page-local overlay would have to reimplement focus trapping, Escape, the top layer and
    `aria-modal` — four things `<dialog>.showModal()` gives for free and that hand-rolled overlays
    get wrong. A page-local empty state drifts in tone and in whether it offers an action at all.
    """
    for relative in MANAGERS:
        source = _read(relative)
        if source is None:  # pragma: no cover
            continue
        assert "@/components/Modal" in source, f"{relative} does not use the shared Modal"
        assert "@/components/EmptyState" in source, (
            f"{relative} does not use the shared EmptyState, so a page with no records offers no "
            f"way to create one"
        )


def test_no_create_form_is_rendered_unconditionally():
    """The busy-page complaint, asserted.

    A form reachable only through `Modal` cannot occupy the page at rest. If a manager renders a
    `<form action={create}>` outside the modal subtree, the page is back to being a wall of open
    cards.
    """
    for relative in MANAGERS:
        source = _read(relative)
        if source is None:  # pragma: no cover
            continue
        # The create form is built into a `form` variable and handed to `<Modal>`; nothing else may
        # render it. A second `action={create...}` outside that assignment is the regression.
        creates = source.count("action={create")
        assert creates <= 1, (
            f"{relative} renders the create form in {creates} places; it belongs only inside the "
            f"modal"
        )


def test_the_modal_uses_a_native_dialog():
    """`showModal()` is what makes it modal.

    Rendering `<dialog open>` produces a NON-modal dialog: no focus trap, no top layer, no Escape.
    The imperative call is load-bearing, not stylistic.
    """
    source = _read("components/Modal.tsx")
    if source is None:  # pragma: no cover
        return

    assert "<dialog" in source
    assert "showModal()" in source, (
        "the modal is opened declaratively, which yields a non-modal dialog with no focus trap"
    )
    assert "onCancel" in source, "Escape does not route through onClose, so state can desync"
    # Body scroll lock: without it a phone user scrolling a long form scrolls the page behind.
    assert "overflow" in source


# --------------------------------------------------------------------------- #
# Alerts — the collapsible
# --------------------------------------------------------------------------- #


def test_the_alert_queue_is_collapsible():
    """Fifty alerts at ~120 lines of markup each was a wall.

    Finding Tuesday's WARNING meant scrolling past everything quiet since.
    """
    page = _read("app/portal/alerts/page.tsx")
    card = _read("components/AlertCard.tsx")
    if page is None or card is None:  # pragma: no cover
        return

    assert "AlertCard" in page, "the alert queue no longer uses the collapsible card"
    assert "aria-expanded" in card, "the toggle is not announced to a screen reader"
    assert "aria-controls" in card


def test_the_collapsed_row_supports_triage_without_opening():
    """It has to carry enough to decide whether to open.

    Severity (badge, with icon and label — never colour alone), the headline, which plot, when, and
    whether it reached anybody. An alert that reached nobody is the one to open first and would
    otherwise be invisible until expanded.
    """
    card = _read("components/AlertCard.tsx")
    if card is None:  # pragma: no cover
        return

    for required in ("SeverityBadge", "headline", "aoi_name", "reached nobody"):
        assert required in card, f"the collapsed row omits {required!r}"


def test_a_pending_receipt_is_not_reported_as_undelivered():
    """A receipt still in flight is neither a success nor a failure.

    Treating `pending` as a failure would label every just-dispatched alert as broken for as long as
    the queue takes — on a busy cycle, that is most of the queue.
    """
    card = _read("components/AlertCard.tsx")
    if card is None:  # pragma: no cover
        return

    assert '!== "pending"' in card, (
        "pending receipts are not excluded from the delivery check, so a queued alert reads as "
        "having reached nobody"
    )


def test_the_alert_body_is_unmounted_when_collapsed():
    """Not hidden with CSS.

    Each body holds a TrackModules subtree; fifty of those mounted is real memory and real
    reconciliation cost for markup nobody is looking at.
    """
    card = _read("components/AlertCard.tsx")
    if card is None:  # pragma: no cover
        return

    assert "{open && (" in card, (
        "the alert body renders unconditionally and is presumably hidden with CSS; unmount it"
    )


# --------------------------------------------------------------------------- #
# Client/server boundary
# --------------------------------------------------------------------------- #


def test_no_client_component_imports_the_server_only_links_module():
    """`lib/links.ts` reads `SHELTER_API_URL` and is marked `server-only`.

    Importing it from a client component fails the production build — which is the marker doing its
    job, and cost a build to discover. The URLs are passed in as props instead: the API console is
    served by FastAPI on a different origin, so the href must be absolute and the origin is server
    knowledge.
    """
    offenders: list[str] = []
    for directory in ("components", "app"):
        root = FRONTEND / directory
        if not root.exists():  # pragma: no cover
            continue
        for file in sorted(root.rglob("*.tsx")):
            source = file.read_text()
            if '"use client"' not in source:
                continue
            if "@/lib/links" in source:
                offenders.append(str(file.relative_to(FRONTEND)))

    assert not offenders, (
        f"these client components import the server-only links module and will fail the "
        f"production build: {offenders}. Pass the URL in as a prop."
    )


def test_the_list_pages_all_have_an_empty_state():
    """A page rendering "0 records" above an empty list reads as broken rather than as new.

    Every module a subscriber or aggregator can create things in needs the same treatment — that was
    the review comment, and it decays one page at a time without a sweep.
    """
    for relative in MANAGERS + EMPTY_STATE_ONLY:
        source = _read(relative)
        if source is None:  # pragma: no cover
            continue
        assert "EmptyState" in source, (
            f"{relative} has no empty state, so a fresh account sees an empty list with no "
            f"call-to-action"
        )


def test_the_areas_picker_is_not_moved_into_a_modal():
    """The one deliberate exception, asserted so it is not "fixed" into consistency.

    A map and an admin cascade inside a scrolling dialog fight the same drag gesture on a phone. The
    inline panel is already collapsed by default, which is the outcome the modal pattern exists for.
    """
    source = _read("app/portal/areas/AreaManager.tsx")
    if source is None:  # pragma: no cover
        return

    assert "@/components/Modal" not in source, (
        "the area picker was moved into a modal; a map inside a scrolling dialog competes with the "
        "dialog's own scroll on a phone. Keep the collapsible panel."
    )
    # And it must still be collapsed rather than always open.
    assert "setAdding(" in source


def test_settings_forms_are_not_modals():
    """Settings edits existing records; it has nothing to create.

    A modal is for creation — an edit form for a record that already exists belongs on the page,
    where the current values are visible beside the fields that change them.
    """
    for relative in (
        "app/portal/settings/AlertDelivery.tsx",
        "app/portal/settings/PreferencesForm.tsx",
    ):
        source = _read(relative)
        if source is None:  # pragma: no cover
            continue
        assert "@/components/Modal" not in source, (
            f"{relative} puts an edit form in a modal; the current values belong beside the fields "
            f"that change them"
        )


# --------------------------------------------------------------------------- #
# Workspace scoping in the create modals
# --------------------------------------------------------------------------- #


def test_the_webhook_modal_scopes_by_workspace():
    """An aggregator running two programmes needs each endpoint to receive only its own alerts.

    The form had no workspace field at all, and `WebhookCreate` had no such parameter — so every
    endpoint received every project's events. For an aggregator whose Bayelsa pilot and Kebbi season
    are separate commercial relationships, that is a cross-programme data leak inside one account.
    """
    manager = _read("app/portal/webhooks/WebhookManager.tsx")
    if manager is None:  # pragma: no cover
        return

    assert 'name="workspace_id"' in manager, "the webhook form cannot scope to a workspace"
    assert "All projects" in manager, (
        "no explicit all-workspaces option — an empty select reads as a missing choice"
    )

    action = _read("app/portal/webhooks/actions.ts")
    assert action and "workspace_id" in action, "the action does not forward the workspace"


def test_the_invite_modal_assigns_a_role_per_workspace():
    """Already correct, and asserted so it stays that way.

    Reported as missing during MVP review; it is not. `RoleGrid` renders one role dropdown per
    workspace with "No access" as the default, which is how a colleague can administer one project
    and be view-only on another. Verified in the delivered HTML, not just the source.
    """
    manager = _read("app/portal/team/TeamManager.tsx")
    if manager is None:  # pragma: no cover
        return

    assert "RoleGrid" in manager
    assert 'name={`role-${workspace.id}`}' in manager, (
        "roles are not assigned per workspace, so a colleague gets one role everywhere"
    )
    assert "No access" in manager, (
        "no way to grant nothing on a workspace — the default must be no access, not the first role"
    )


def test_the_api_refuses_a_workspace_the_caller_does_not_own():
    """A supplied id is a FILTER, not an authorisation.

    Without the check, a signed-in aggregator could register an endpoint against another tenant's
    workspace id and start receiving their alert events — plot locations, contact addresses,
    severity. A cross-tenant read achieved entirely through a write.

    404, not 403: a 403 confirms the id exists and turns the endpoint into a workspace-enumeration
    oracle across tenants.
    """
    from app.api.routes import webhooks as webhook_routes

    source = pathlib.Path(inspect.getfile(webhook_routes)).read_text()
    guard = next(
        ast.unparse(node)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == "_resolve_workspace"
    )

    assert "list_workspaces" in guard, "membership is not verified before the id is used"
    assert "HTTP_404_NOT_FOUND" in guard
    assert "HTTP_403_FORBIDDEN" not in guard, (
        "a 403 confirms the workspace exists, making this a cross-tenant enumeration oracle"
    )


def test_the_workspace_refusal_is_not_laundered_into_a_503():
    """**A bug found by running it, not by reading it.**

    `_resolve_workspace` was called INSIDE the route's `try`, whose bare `except Exception` re-raised
    everything as 503. So a deliberate cross-tenant refusal came back as
    `HTTP 503  Could not create the subscription: 404: No such workspace.` — an authorisation
    decision reported as an infrastructure failure, which both misleads the caller and invites a
    retry.
    """
    from app.api.routes import webhooks as webhook_routes

    source = pathlib.Path(inspect.getfile(webhook_routes)).read_text()
    create = next(
        ast.unparse(node)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == "create_subscription"
    )

    resolve_at = create.index("_resolve_workspace")
    try_at = create.index("try:")
    assert resolve_at < try_at, (
        "the workspace is resolved inside the try block, so its 404 is re-raised as a 503"
    )


def test_the_create_response_echoes_the_stored_workspace():
    """A create response that omits a field it accepted is indistinguishable from one that
    discarded it.

    Observed live: the row stored `VDK6N8GGPY` correctly while the response reported `null`, so a
    working write looked like a silently ignored parameter. Read back from the ROW, not the request,
    so the caller can tell an applied scope from an accepted-and-dropped one.
    """
    from app.api.routes import webhooks as webhook_routes

    source = pathlib.Path(inspect.getfile(webhook_routes)).read_text()
    assert "workspace_id=row.get(\"owner_workspace_id\")" in source, (
        "the response does not echo the stored workspace, so an applied scope is unverifiable"
    )


def test_the_dashboard_alert_queue_is_collapsible():
    """The same 74-line-per-record wall as the portal's queue.

    Extended from `/portal/alerts` on review. The Activity page deliberately is NOT: its rows are
    already two lines and it is cursor-paginated, so collapsing would add interaction for nothing.
    """
    page = _read("app/dashboard/page.tsx")
    if page is None:  # pragma: no cover
        return

    assert "AlertCard" in page, "the dashboard alert queue is not collapsible"

    activity = _read("app/portal/activity/page.tsx")
    if activity:
        assert "AlertCard" not in activity, (
            "the activity log was made collapsible; its rows are already compact and it is "
            "cursor-paginated, so this adds interaction without reducing scroll"
        )
