"""Only channels that have actually delivered a message may be bound.

## Why this restriction exists at all

Seven channels are implemented and registered in `router._DISPATCHERS`. Exactly one of them —
email — has ever delivered a real message. WhatsApp, Telegram, Signal and Slack have code,
config keys and tests, and no credentials; NIGCOMSAT has no gateway yet.

Each of those degrades *honestly* at dispatch: `Dispatcher.available` is False, the receipt comes
back SKIPPED with "credentials not configured", nothing raises. That is correct behaviour and it is
not enough. It happens hours later in a log nobody reads, so a farmer who chose WhatsApp saw the
save succeed and then received nothing, with no way to find out why. For a service whose entire
promise is "we will contact you", a channel that accepts an address and silently delivers silence is
worse than one that is plainly absent.

So the refusal moved to the moment of choosing, where the person is still on the form and can pick
something that works.

## Why `webhook` is in the set and email is the only thing in the UI

`MVP_CHANNELS` is `{email, webhook}`. Webhook delivery works — it is the aggregator relay path, and
`delivery_mode: webhook` depends on it. But it is not a *subscriber preference*: it needs a signing
secret and is configured under Webhooks, so `AAlertDelivery.OFFERED` on the frontend lists email
alone. The two lists therefore differ on purpose, and `test_the_ui_offers_no_channel_the_api_refuses`
pins the direction of the difference: the UI may be narrower than the API, never wider.

## Why the guard is tested per route rather than once on the function

The function was correct from the first commit. What was wrong was the *set of routes calling it* —
three write paths took a `preferred_channel` and bound it unchecked, and the worst of them was
`POST /workspaces/{id}/customers`: an aggregator could onboard a farmer with
`preferred_channel: "whatsapp"`, get a 200, and that farmer would never be contacted. A silent
non-delivery reaching a third party who never chose the channel and cannot see the setting.

An audit that enumerates the routes is what catches the next one, so that is what this file does.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest
from fastapi import HTTPException

from app.api.area_input import reject_unavailable_channel, reject_unavailable_channels
from app.models.enums import MVP_CHANNELS, Channel
from app.models.schemas import ChannelBinding

_ROUTES = pathlib.Path(inspect.getfile(reject_unavailable_channels)).parent / "routes"


def test_the_mvp_set_is_email_and_webhook():
    """Pinned deliberately.

    Widening this is a product decision that must be made in the same change as sending a real
    message on the new channel — not something that drifts in because a dispatcher exists.
    """
    assert MVP_CHANNELS == frozenset({Channel.EMAIL, Channel.WEBHOOK})


@pytest.mark.parametrize("channel", sorted(MVP_CHANNELS, key=lambda c: c.value))
def test_an_mvp_channel_is_accepted(channel: Channel):
    reject_unavailable_channels([ChannelBinding(channel=channel, address="a@b.co")])
    reject_unavailable_channel(channel)


@pytest.mark.parametrize(
    "channel", sorted(set(Channel) - set(MVP_CHANNELS), key=lambda c: c.value)
)
def test_every_other_channel_is_refused_by_both_forms(channel: Channel):
    """A 422, not a 500 and not a silent accept — and the same for one channel or a list."""
    for call in (
        lambda: reject_unavailable_channels(
            [ChannelBinding(channel=channel, address="a@b.co")]
        ),
        lambda: reject_unavailable_channel(channel),
    ):
        with pytest.raises(HTTPException) as caught:
            call()
        assert caught.value.status_code == 422


def test_the_refusal_names_what_does_work():
    """"whatsapp is unavailable" leaves a farmer stuck. Naming the alternative does not."""
    with pytest.raises(HTTPException) as caught:
        reject_unavailable_channel(Channel.WHATSAPP)
    detail = caught.value.detail
    assert "whatsapp" in detail
    for offered in MVP_CHANNELS:
        assert offered.value in detail


def test_no_channel_is_not_a_refusal():
    """`preferred_channel` is optional on some payloads; absent means "use the default"."""
    reject_unavailable_channel(None)
    reject_unavailable_channels([])


def _functions(path: pathlib.Path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node


def _is_write_route(node) -> bool:
    return any(
        isinstance(d, ast.Call) and getattr(d.func, "attr", "") in {"post", "put", "patch"}
        for d in node.decorator_list
    )


def test_every_write_route_that_takes_a_channel_guards_it():
    """The audit that catches the next unguarded path.

    A route is in scope when it is a POST/PUT/PATCH *and* it reads `payload.channels` or
    `payload.preferred_channel`. Such a route must call one of the two guards — otherwise it accepts
    a channel this deployment cannot deliver on, which is exactly the bug three routes had.
    """
    unguarded: list[str] = []
    checked = 0

    for path in sorted(_ROUTES.glob("*.py")):
        for node in _functions(path):
            if not _is_write_route(node):
                continue
            source = ast.unparse(node)
            takes_channel = (
                "payload.channels" in source or "payload.preferred_channel" in source
            )
            if not takes_channel:
                continue
            checked += 1
            if "reject_unavailable_channel" not in source:
                unguarded.append(f"{path.name}::{node.name}")

    assert checked >= 5, (
        f"only found {checked} channel-accepting write routes; the AST walk has probably "
        f"stopped matching and this test is no longer checking anything"
    )
    assert not unguarded, (
        "these write routes accept a delivery channel without refusing the undeliverable "
        f"ones: {unguarded}. Call reject_unavailable_channel(s) before anything is persisted."
    )


def test_the_ui_offers_no_channel_the_api_refuses():
    """The frontend picker must be a subset of `MVP_CHANNELS`.

    A picker listing a channel the API 422s on gives the subscriber an error they cannot act on.
    Narrower is fine — email-only is deliberate, because `webhook` needs a signing secret and is
    configured elsewhere. Wider is a bug.
    """
    delivery = (
        pathlib.Path(__file__).resolve().parents[2]
        / "frontend/app/portal/settings/AlertDelivery.tsx"
    )
    if not delivery.exists():  # pragma: no cover - backend-only checkouts
        pytest.skip("frontend not present in this checkout")

    line = next(
        ln for ln in delivery.read_text().splitlines() if ln.startswith("const OFFERED")
    )
    # Split on `=` first, NOT on `[`: the type annotation is `Channel[]`, so splitting on the first
    # bracket parses the annotation and yields an empty set — which is vacuously a subset of
    # anything and made this assertion pass while the picker offered whatsapp. Caught by tampering.
    array = line.split("=", 1)[1]
    inner = array[array.index("[") + 1 : array.index("]")]
    offered = {token.strip().strip('"').strip("'") for token in inner.split(",")}
    offered.discard("")

    assert offered, f"parsed no channels out of {line!r}; the parser has stopped working"

    allowed = {c.value for c in MVP_CHANNELS}
    assert offered <= allowed, (
        f"the settings page offers {sorted(offered - allowed)}, which the API refuses with a 422"
    )
