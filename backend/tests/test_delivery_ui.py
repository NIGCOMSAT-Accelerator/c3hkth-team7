"""The alert-delivery editor, and the setting that lets an aggregator be the only voice.

## What this closes

Channels were settable **only at signup** — no endpoint, no UI. A farmer who mistyped a phone
number had alerts going nowhere permanently, and the Settings page could display the preferred
channel without changing it.

And an aggregator who onboarded a farmer had SHELTER's SMS arriving alongside their own about the
same flood, in two voices, with no way to switch ours off.

Source assertions: there is no JS test runner here, and the failures being prevented are a control
that offers something the backend refuses, or a control that silently cannot work.
"""

from __future__ import annotations

import inspect
import pathlib

EDITOR = pathlib.Path("../frontend/app/portal/settings/AlertDelivery.tsx")
PAGE = pathlib.Path("../frontend/app/portal/settings/page.tsx")
ACTIONS = pathlib.Path("../frontend/app/portal/settings/actions.ts")


def _read(path: pathlib.Path) -> str | None:
    return path.read_text() if path.exists() else None


def test_the_editor_only_offers_channels_a_subscriber_can_set():
    """Not every `Channel` is a preference, and not every preference is deliverable yet.

    **Two independent reasons a channel is absent**, and conflating them is how this test went
    stale. It used to assert `"whatsapp" in offered`, which was right when the only question was
    "is this a personal preference?" — and became wrong once `MVP_CHANNELS` restricted the API to
    channels that have actually delivered a message. The assertion contradicted a 422.

      * **Not a preference at all** — checked here, permanently. `nigcomsat_broadcast` is an
        escalation reaching a district rather than a person; `webhook` needs a signing secret and
        belongs under Webhooks, where a free-text field would invite an unsigned URL; `slack` needs
        an app install rather than an address. None of these become pickers no matter what ships.
      * **Not deliverable yet** — that is `MVP_CHANNELS`, and the subset check lives in
        `tests/test_mvp_channels.py` so there is exactly one place asserting it. Naming individual
        channels here would go stale again the day one of them starts working.
    """
    source = _read(EDITOR)
    if source is None:
        return

    offered = source.split("const OFFERED: Channel[] = [")[1].split("]")[0]
    for excluded in ("nigcomsat_broadcast", "webhook", "slack"):
        assert excluded not in offered, (
            f"{excluded!r} is offered as a self-serve channel; it is not a preference"
        )
    # Email is the floor: with nothing offered, the page is a form that cannot be filled in.
    assert '"email"' in offered


def test_the_relay_option_is_hidden_from_individuals():
    """`webhook` mode needs an aggregator to relay.

    Offering it to an individual would present a control the backend refuses with a 422 — and if it
    ever succeeded it would silence their alerts entirely, with nobody to deliver them.
    """
    source = _read(EDITOR)
    if source is None:
        return

    assert "canRelay" in source, "no gate on the relay section"
    assert "{canRelay && (" in source, (
        "the relay section is not conditional, so an individual is offered a setting that can "
        "only fail"
    )

    page = _read(PAGE)
    if page is None:
        return
    assert 'account?.kind === "commercial"' in page, (
        "canRelay is not derived from the account kind"
    )


def test_clearing_an_address_removes_the_channel():
    """Removal has to be expressible, and the API takes the full set rather than a diff.

    A cleared field dropping the row is how "stop using WhatsApp" works without a delete verb — and
    the UI says so, because an empty field that silently means "remove" gets misunderstood.
    """
    actions = _read(ACTIONS)
    if actions is None:
        return

    block = actions.split("export async function replaceChannels")[1]
    assert "if (!address) continue;" in block, (
        "an empty address is submitted rather than dropped, so a channel can never be removed"
    )

    editor = _read(EDITOR)
    if editor is None:
        return
    assert "A cleared address is removed when you save" in editor, (
        "the removal behaviour is not explained, so it will be misread"
    )


def test_an_empty_final_set_is_refused_with_a_reason():
    """A subscriber with no channels is monitored and unreachable.

    The backend refuses it too — this is the message that explains *why* rather than surfacing a
    422 about list length.
    """
    actions = _read(ACTIONS)
    if actions is None:
        return

    block = actions.split("export async function replaceChannels")[1]
    assert "channels.length === 0" in block
    assert "never told" in block, (
        "the empty-set message does not explain the consequence"
    )


def test_the_row_loop_is_bounded():
    """A hostile or malformed submission must not make the action allocate unboundedly."""
    actions = _read(ACTIONS)
    if actions is None:
        return

    block = actions.split("export async function replaceChannels")[1]
    assert "i < 24" in block, (
        "the form-field loop has no upper bound; iterate a fixed range, not the submitted keys"
    )


def test_channels_and_delivery_mode_are_separate_forms():
    """They fail separately.

    A rejected address must not discard a delivery-mode change the subscriber already made — and
    one combined form would submit both and roll back both.
    """
    source = _read(EDITOR)
    if source is None:
        return

    assert source.count("<form action=") >= 2, "the two controls share one form"
    assert "replaceChannels" in source and "setAreaDelivery" in source


def test_the_backend_still_owns_validation():
    """The action deliberately does not re-check the address format or the area ownership.

    Both are the backend's job — it owns the subscriber's area list and rejects an unknown id with
    a readable 422. A second implementation here could disagree with the one guarding the write.
    """
    actions = _read(ACTIONS)
    if actions is None:
        return

    block = actions.split("export async function replaceChannels")[1].split(
        "export async function setAreaDelivery"
    )[0]
    assert "NOT validated here" in actions or "backend's job" in actions, (
        "the deliberate absence of client-side validation is undocumented, so someone will add it"
    )
    # And it must not have crept in.
    assert "@" not in block.split("placeholder")[0].replace("aoi_id", ""), (
        "an email-format check appeared in the action"
    )


def test_dispatch_honours_the_mode():
    """The UI is only meaningful if the router acts on it."""
    from app.dispatch import router

    source = inspect.getsource(router.deliver)
    assert "DeliveryMode.WEBHOOK" in source
    assert "return []" in source, (
        "webhook mode does not suppress direct dispatch, so the setting does nothing"
    )
