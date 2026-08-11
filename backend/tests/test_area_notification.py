"""Creating a monitoring area must tell the person whose land it is.

## The gap

Reported by an aggregator: a monitoring area was created for a customer and **nothing was sent to
anyone**. The area was normalised, stored, queued for scanning and written to the audit log — so
the only confirmation was the HTTP 201 the integration received, which the farmer never sees.

There were **three** add-area paths and all three were silent:

| path | used by |
|---|---|
| `POST /subscribers/{id}/areas` | an individual's own portal |
| `POST /iam/customers/{id}/areas` | a partner's API key |
| `POST /iam/workspaces/{ws}/customers/{id}/areas` | the aggregator portal |

Fixing one would have left the other two, which is why these tests enumerate the routes rather
than checking a single handler.

## Why the email restates the location

Same reasoning as the picker's confirmation card, and the same incident behind it: a farm described
as being in Kobape, Ogun State was once activated at Warrington, England. A confirmation naming the
district and country is the last chance to catch a wrong location *before* advisories start
arriving about the wrong ground.
"""

from __future__ import annotations

import inspect

from app.api.routes import iam as iam_routes
from app.api.routes import subscribers as subscriber_routes
from app.iam import mailer


def test_every_add_area_path_sends_a_confirmation():
    """**The enumeration.** A fourth path added later must appear here or fail this test."""
    handlers = (
        ("individual portal", subscriber_routes.create_area),
        ("partner API key", iam_routes.add_customer_area),
        ("aggregator portal", iam_routes.add_workspace_customer_area),
    )
    for label, handler in handlers:
        source = inspect.getsource(handler)
        assert "send_area_added" in source or "_confirm_area_added" in source, (
            f"the {label} add-area path creates and queues an area without telling anyone"
        )


def test_confirmations_are_backgrounded():
    """The area is already durable and already queued when the mail is sent.

    A synchronous send would let a slow provider turn a successful creation into a timeout — and
    the caller would have no way to know whether the area exists.
    """
    for handler in (
        subscriber_routes.create_area,
        iam_routes.add_customer_area,
        iam_routes.add_workspace_customer_area,
    ):
        source = inspect.getsource(handler)
        assert "background.add_task" in source, (
            f"{handler.__name__} sends mail synchronously; a mail outage would fail the request"
        )


def test_the_confirmation_names_the_district_and_country():
    """A plot name alone is not checkable — "Alspecs Farms" is right in Ogun and in England.

    The email carries `admin1`, `admin2` and `country` for the same reason the picker's card does:
    it is the last point at which a wrong location can be noticed before advisories begin.
    """
    signature = inspect.signature(mailer.send_area_added)

    for field in ("area_name", "hectares", "admin1", "admin2", "country"):
        assert field in signature.parameters, f"the confirmation cannot state {field}"

    source = inspect.getsource(mailer.send_area_added)
    assert '("Where", where or "not identified")' in source, (
        "an unresolved district renders as nothing, hiding the fact it is unknown"
    )


def test_a_partner_created_area_names_who_created_it():
    """A farmer who did not press the button should be told who did.

    A silent change to what is monitored on someone's land is not acceptable even when it is
    legitimate — and an unexplained "we are now watching your farm" email reads as a scam.
    """
    signature = inspect.signature(mailer.send_area_added)
    assert "added_by" in signature.parameters

    for handler in (iam_routes.add_customer_area, iam_routes.add_workspace_customer_area):
        source = inspect.getsource(handler)
        assert "added_by=" in source, (
            f"{handler.__name__} does not name the aggregator that created the area"
        )

    # And the individual path must NOT claim a third party — they created it themselves.
    own = inspect.getsource(subscriber_routes._confirm_area_added)
    assert "added_by" not in own, (
        "the individual path names an 'added_by', which would be wrong: they added it themselves"
    )


def test_the_confirmation_goes_to_the_customer_not_the_aggregator():
    """The aggregator already knows — they made the call and got a 201.

    The person who needs telling is the one whose land it is.
    """
    for handler in (iam_routes.add_customer_area, iam_routes.add_workspace_customer_area):
        source = inspect.getsource(handler)
        # The LAST occurrence: `add_workspace_customer_area`'s docstring mentions
        # `send_area_added` while explaining why the notification exists, and splitting on the
        # first match captured the docstring instead of the call.
        send = source.rsplit("send_area_added", 1)[1][:220]
        assert "account.email" in send or "customer.email" in send, (
            f"{handler.__name__} does not address the confirmation to the customer"
        )
        assert "aggregator.account.email" not in send, (
            f"{handler.__name__} emails the aggregator instead of the customer"
        )


def test_a_missing_account_is_silent_rather_than_an_error():
    """A subscriber created straight through the platform API may have no portal account.

    That means "nobody to email", not "something is broken" — and it must not turn a successful
    area creation into a logged failure.
    """
    source = inspect.getsource(subscriber_routes._confirm_area_added)

    assert "if account is None" in source
    assert "return" in source
    assert "except Exception" in source, (
        "the confirmation can raise inside a background task, where nothing would catch it"
    )
