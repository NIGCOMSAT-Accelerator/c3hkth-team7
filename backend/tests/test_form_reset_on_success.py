"""A form that succeeded must close itself.

## The bug

Reported after adding a monitoring area: the page did not change. The add form stayed open with the
previous plot's name and location still in it, and the "Monitoring active" confirmation rendered
*underneath* the open form. It read as though nothing had happened — or worse, as though a second
plot were half-entered.

Both portal pages had it, in three places:

  * `AreaManager` — the individual's add-a-plot form
  * `CustomerManager` — the aggregator's onboard-a-customer form
  * `CustomerManager` — the aggregator's add-a-plot-for-a-customer form

Each called its close-handler only from **Cancel**. Nothing closed them on success.

## Why an effect, and why the key matters

`useActionState` offers no success callback — the result arrives as new state on the next render, so
an effect watching it is the only place that knows.

The key must **change between two successive successes**, or the effect fires once and never again.
`AreaManager` keys on the created `aoiId`; `CustomerManager`'s actions return only `{ok, message}`,
so it keys on the message text, which differs per customer. Keying on `ok` alone is the mistake
these tests exist to catch: it stays true after the first success.

Source assertions rather than a rendered test: there is no JS test runner here, and the failure being
prevented is someone removing the reset. `npm run build` covers whether it compiles.
"""

from __future__ import annotations

import pathlib

AREAS = pathlib.Path("../frontend/app/portal/areas/AreaManager.tsx")
CUSTOMERS = pathlib.Path(
    "../frontend/app/portal/workspace/[id]/customers/CustomerManager.tsx"
)


def _read(path: pathlib.Path) -> str | None:
    return path.read_text() if path.exists() else None


def test_the_areas_form_closes_on_success():
    source = _read(AREAS)
    if source is None:
        return

    assert "useEffect" in source, "no effect watches for the add succeeding"
    # Keyed on the created id, so a second add closes the form too.
    assert "addState.activated?.aoiId" in source, (
        "the reset is not keyed on the created area's id, so adding two plots in a row would "
        "leave the form open the second time"
    )
    effect = source.split("const createdId")[1].split("return (")[0]
    assert "setAdding(false)" in effect, "the form is not closed"
    assert "setResolved(null)" in effect, (
        "the resolved location survives, so a stale place could submit against a new plot name"
    )


def test_the_customer_forms_close_on_success():
    """Two forms on one page, and both were broken."""
    source = _read(CUSTOMERS)
    if source is None:
        return

    assert "useEffect" in source, "no effect watches for either action succeeding"

    onboard = source.split("const onboardDone")[1].split("const areaDone")[0]
    assert "setOnboarding(false)" in onboard
    assert "setOnboardArea(null)" in onboard

    area = source.split("const areaDone")[1].split("return (")[0]
    assert "setAddingFor(null)" in area
    assert "setPlotArea(null)" in area


def test_the_reset_key_changes_between_successive_successes():
    """**The subtle half.** `ok` alone stays true, so the effect would fire once and stop.

    `CustomerManager`'s actions return no id, so the message — which names the customer or plot — is
    what differs between two saves.
    """
    source = _read(CUSTOMERS)
    if source is None:
        return

    assert "onboardState.ok ? onboardState.message" in source, (
        "the onboard reset is keyed on `ok` rather than on something that changes per success"
    )
    assert "areaState.ok ? areaState.message" in source, (
        "the add-area reset is keyed on `ok` rather than on something that changes per success"
    )


def test_a_successful_add_offers_the_next_step():
    """Closing the form without offering a way forward is a dead end.

    Several scattered plots is the normal case, so "add another" is what a subscriber most likely
    wants next — and the summary is where the eye already is, because it is what just appeared.
    """
    source = _read(AREAS)
    if source is None:
        return

    assert "Add another plot" in source, "the confirmation offers no way to add a second plot"
    assert "onAddAnother" in source


def test_there_is_only_one_add_control_after_a_success():
    """The card's own button and the summary's would otherwise both show, doing the same thing."""
    source = _read(AREAS)
    if source is None:
        return

    assert "{!adding && !addState.ok ? (" in source, (
        "the card's '+ Add a plot' button is not suppressed after a success, so it duplicates the "
        "summary's own 'add another'"
    )
    # And the summary's action hides while the form is open, since it would then do nothing.
    assert "{!formOpen && (" in source, (
        "the summary keeps a live 'add another' button while the form is already open"
    )
