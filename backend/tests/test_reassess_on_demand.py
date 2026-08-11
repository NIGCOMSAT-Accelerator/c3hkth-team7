"""On-demand reassessment from the portal — the scoping that makes it safe to expose.

## What this closes

`POST /risk/assess` existed, was wired into `frontend/lib/api.ts`, and had **no call sites**. So
the only paths to a reading were SHELTER's own clock — the ~6-hour watch loop, or the scan queued
when a plot is added or moved. Correct for the product, but it left a subscriber unable to ask
"what does the satellite say about my field now", and left the service unable to *show* that it
queries live open data rather than replaying something stored.

## Why the scoping is the part worth a test

`/risk/assess` takes a **geometry**, not an `aoi_id`, and spends real catalogue quota on whatever
it is handed. That is right for its other callers — the demo and the Partner API assess ground
that is not registered at all — and it makes a naive portal action dangerous in a specific way: an
action that forwarded a client-supplied bbox would let anyone with a session bill arbitrary COG
reads against coordinates they chose, and read the result back. Not a tenancy leak of *stored*
data, which is what `test_tenancy.py` sweeps for, but a live measurement of someone else's ground
performed on request.

So the invariant is: **the id from the form is a lookup key, never the thing assessed.** The action
re-reads the subscriber server-side and assesses the area found in that record, so a forged id
matches nothing and the action stops. These tests assert that ordering structurally, because it is
invisible at runtime — a wrong version returns a perfectly plausible assessment.

Source assertions: there is no JS test runner in this repo, and the failure being prevented is a
control that spends quota on unverified geometry.
"""

from __future__ import annotations

import pathlib

ACTIONS = pathlib.Path("../frontend/app/portal/areas/actions.ts")
MANAGER = pathlib.Path("../frontend/app/portal/areas/AreaManager.tsx")
API = pathlib.Path("../frontend/lib/api.ts")
CSS = pathlib.Path("../frontend/app/globals.css")


def _read(path: pathlib.Path) -> str | None:
    """None when the frontend is not checked out beside the backend.

    Same tolerance as the other frontend sweeps: the backend image does not ship `frontend/`, so
    these must skip rather than fail there.
    """
    return path.read_text() if path.exists() else None


def _reassess_body(source: str) -> str:
    """Just the `reassessArea` function, so an assertion cannot pass on a neighbour.

    `addArea` also calls `api.` and also reads a form — matching against the whole file would let
    this test pass with `reassessArea` deleted entirely.
    """
    assert "export async function reassessArea" in source, (
        "reassessArea is gone. The portal has no way to request a reading, so the only path to "
        "one is the 6-hour watch loop."
    )
    after = source.split("export async function reassessArea", 1)[1]
    # Up to the next top-level export, which is where the function ends.
    return after.split("\nexport async function ", 1)[0]


def test_the_action_exists_and_is_reachable_from_the_plot_list():
    source = _read(MANAGER)
    if source is None:
        return

    assert "reassessArea" in source, "the action is never imported"
    assert "useActionState(reassessArea" in source, (
        "reassessArea is imported but not bound to an action state, so pressing the button "
        "cannot report a result"
    )
    # Rendered as a form, not an onClick: a Server Action through a form works before hydration,
    # which is the common case on the connections this product serves.
    assert "<form action={reassess}" in source, (
        "the control is not a form, so it is dead until JavaScript hydrates — the portal is SSR "
        "precisely because that wait is long here"
    )


def test_the_assessed_geometry_comes_from_the_account_not_the_form():
    """**The one that matters.** The form's `aoi_id` may only be used to look a plot UP.

    A version that passed the client's own bbox to `/risk/assess` would compile, work, and let any
    session spend catalogue quota measuring arbitrary coordinates.
    """
    source = _read(ACTIONS)
    if source is None:
        return

    body = _reassess_body(source)

    # The plot is re-read server-side, scoped to this account.
    assert "getSubscriber(gate.subscriberId)" in body, (
        "the plot is not re-read from the account's own record, so the geometry being assessed "
        "was supplied by the client"
    )
    assert "areas.find" in body, (
        "the area is not looked up within the subscriber's own areas — a forged aoi_id would not "
        "be rejected"
    )

    # And the thing handed to assess() is that looked-up record.
    assert "api.assess(area" in body, (
        "assess() is not called with the area found on the account. Whatever it IS called with "
        "did not come from a server-side read."
    )

    # No geometry field is accepted from the form. `bbox` is the one that would actually be
    # honoured by the endpoint, so its presence here is the specific bug.
    for field in ("bbox", "geometry", "ring", "hectares"):
        assert f'formData.get("{field}")' not in body, (
            f"`{field}` is read from the form. Geometry must never come from the client here — "
            f"the endpoint assesses what it is given, so this would measure unverified ground."
        )


def test_a_plot_that_is_not_on_the_account_is_indistinguishable_from_one_that_does_not_exist():
    """Same reasoning as the backend's 404-not-403 on cross-tenant reads.

    Area ids appear in portal URLs. An id that fails differently is an id an attacker can confirm.
    """
    source = _read(ACTIONS)
    if source is None:
        return

    body = _reassess_body(source)
    _, _, tail = body.partition("if (!area)")
    assert tail, "there is no not-found branch at all"

    # The user-facing sentence only, not the comment above it — the comment necessarily NAMES the
    # distinction it is preventing, so matching the whole branch would fail on its own rationale.
    message = tail.split("message:", 1)[1].split("\n", 1)[0].lower()
    for leak in ("not yours", "another", "belongs to", "permission", "forbidden"):
        assert leak not in message, (
            f"the not-found message says {leak!r}, which distinguishes 'not yours' from 'does "
            f"not exist' and turns any aoi_id into a membership oracle"
        )


def test_nothing_is_dispatched_by_a_subscriber_pressing_the_button():
    """`assess` runs Scout -> Analyst -> Oracle and stops. The Herald never sees it.

    This is what makes the control safe in front of an end user: it cannot page anybody, and it
    cannot consume the 18-hour dedupe window that a real alert later depends on.
    """
    source = _read(ACTIONS)
    if source is None:
        return

    body = _reassess_body(source)
    for dispatching in ("dispatch", "broadcast", "sendAlert", "herald"):
        assert dispatching not in body.lower(), (
            f"reassessArea references {dispatching!r}. On-demand assessment must not deliver "
            f"anything — a subscriber refreshing a reading would otherwise message themselves, "
            f"or suppress a genuine alert through the dedupe window."
        )

    # And the promise is made to the user, not only kept in code.
    manager = _read(MANAGER)
    if manager is not None:
        assert "sends no alert" in manager, (
            "the UI does not say the button sends no alert. That is the fact which makes it "
            "safe to press repeatedly, so it belongs on screen."
        )


def test_the_reading_is_revalidated_so_the_panel_cannot_show_a_stale_severity():
    """A success message above an unchanged severity reads as a broken button.

    `revalidatePath` re-renders the route and returns the new payload in the action's own
    response, so the panel updates in the same roundtrip.
    """
    source = _read(ACTIONS)
    if source is None:
        return

    body = _reassess_body(source)
    assert 'revalidatePath("/portal/areas")' in body, (
        "the plot list is not revalidated, so the subscriber is told the scan succeeded while "
        "still looking at the previous reading"
    )
    assert 'revalidatePath("/portal")' in body, (
        "/portal is not revalidated. Its highest-severity tile derives from the same "
        "assessments, so two pages of the portal would disagree about one plot."
    )


def test_the_slow_call_is_bounded_and_a_timeout_is_not_reported_as_an_outage():
    """10-40s normally, and unbounded when a catalogue hangs.

    Without a limit the platform kills the render first and the subscriber gets an opaque 502. And
    a timeout must not read as "the service is down": the scan was accepted and is very likely
    still running, so the honest instruction is to wait rather than to retry — retrying spends a
    second set of COG reads on the same plot.
    """
    actions = _read(ACTIONS)
    api = _read(API)
    if actions is None or api is None:
        return

    assert "timeoutMs" in api, "the client has no timeout option, so /risk/assess is unbounded"
    assert "TimeoutError" in api, (
        "a timeout is not distinguished from a connection failure, so a slow scan is reported as "
        "the service being unreachable"
    )
    assert "504" in api, (
        "the timeout does not get its own status, so a caller must string-match the message to "
        "tell it from an outage"
    )

    body = _reassess_body(actions)
    assert "ASSESS_TIMEOUT_MS" in body, "the action does not bound the call"
    assert "504" in body, (
        "the action does not handle the timeout case separately, so a still-running scan is "
        "reported as a failure"
    )


def test_the_result_is_attributed_to_the_plot_it_concerns():
    """One `useActionState` serves every row, so an unkeyed message is ambiguous.

    A page of five plots showing a bare "reassessed just now" does not say which one was.
    """
    actions = _read(ACTIONS)
    manager = _read(MANAGER)
    if actions is None or manager is None:
        return

    body = _reassess_body(actions)
    assert "savedAoiId" in body, (
        "the action does not return which plot it assessed, so no row can claim the result"
    )
    assert "reassessState.savedAoiId === area.id" in manager, (
        "the result is not filtered to the row it belongs to — every plot would render the same "
        "message, or it would appear detached at the foot of the card"
    )


def test_the_control_is_offered_before_the_first_scan_has_landed():
    """The state where it is wanted most, and the honest demonstration of live querying.

    A subscriber who just added a plot wants to see monitoring work now, not on a cycle boundary
    hours away — and with nothing cached for that plot, a result can only have come from a live
    query.
    """
    source = _read(MANAGER)
    if source is None:
        return

    assert "monpanel--pending" in source, "the pending branch is gone"

    # The early return for `!assessment`, bounded at its own `);` — not at the first `</div>`,
    # which closes an inner element and would truncate the branch before the control.
    branch = source.split("monpanel--pending", 1)[1].split("\n    );", 1)[0]
    assert "<CheckNow" in branch, (
        "the reassess control is absent from the not-yet-assessed state, which is where a new "
        "subscriber most wants it — and where a returned reading can only have come from a live "
        "query, since nothing is cached for the plot yet"
    )

    # And in the assessed branch too, so the two states do not diverge.
    assert source.count("<CheckNow") >= 2, (
        "the control renders in only one of the two panel states"
    )


def test_the_control_meets_the_touch_minimum_and_the_breakpoint_is_mobile_first():
    """`.btn--small` inherits `height: 44px` from `.btn`, so the button itself is already fine.

    What needs asserting is the row that holds it: the hint beside it is a full sentence, and on a
    phone a side-by-side layout would compress the button to a few characters.
    """
    css = _read(CSS)
    if css is None:
        return

    assert ".monpanel__refresh {" in css, "the reassess row has no styles"
    block = css.split(".monpanel__refresh {", 1)[1].split("}", 1)[0]
    assert "column" in block, (
        "the row is not a column at the base size, so the button and its explanatory sentence "
        "compete for width on a phone"
    )
    # Mobile-first, matching test_mobile_responsive's rule for the whole sheet.
    assert "@media (min-width: 620px)" in css, (
        "the horizontal layout is not gated behind a min-width query"
    )
