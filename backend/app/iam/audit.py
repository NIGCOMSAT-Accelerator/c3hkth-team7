"""Immutable audit log — every action by every account, at any scale.

## The three hard requirements, and how each is met

**1. Indelible and immutable.** Append-only by construction: this module exposes
`record()` and read functions, and *no* update or delete path. Enforced defensively at
the driver call site (only `insert_one`), asserted structurally by
`tests/test_iam.py::test_audit_module_never_updates_or_deletes`, and — where the
deployment supports it — by an Atlas role that grants `insert` and `find` on this
collection but not `update` or `remove`. Application-level discipline plus a database
grant, because either alone is one mistake away from being bypassed.

**2. No BSON size limit, ever.** MongoDB caps a single document at 16 MB. The way audit
logs hit that cap is by being modelled as an *array inside a parent document* —
`account.audit_events[]` grows until the account becomes unwritable, and the failure
arrives as a mysterious write error on a busy account months later. So every event is
**its own document** with a fixed, small shape. A collection has no size limit; a
document does. The `detail` field is truncated at `MAX_DETAIL_CHARS` so no single event
can be pathological either.

**3. Fast pagination as the collection grows.** `skip(n)` is O(n) in Mongo: the server
walks and discards n documents on every page. At page 10,000 that is a timeout, and it
is also *incorrect* — a document inserted during paging shifts every subsequent page,
so entries are silently duplicated or skipped. This module uses **keyset (cursor)
pagination** on `(at, _id)` instead: each page carries an opaque cursor naming its last
position, and the next query is an indexed range scan. Constant cost per page at any
depth, and stable under concurrent writes.

## Retention

Audit is immutable but not infinite: an `expires_at` TTL index lets Mongo drop entries
past the retention window. That is deletion by *policy*, executed by the database on a
schedule nothing in the application can influence — not an application delete path,
which is what "immutable" excludes. Retention is long (`IAM_AUDIT_RETENTION_DAYS`) and
the TTL is set at insert time, so it cannot be shortened retroactively for existing
entries.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from app.config import settings
from app.logging_config import get_logger

log = get_logger(__name__)

#: Hard cap on a single event's free-text detail.
#:
#: Every field here is bounded, so one document cannot approach the 16 MB BSON limit
#: even pathologically. An unbounded `detail` is how a well-meaning "log the whole
#: request body" turns into an unwritable document.
MAX_DETAIL_CHARS = 500

#: Page size ceiling. A caller asking for 100,000 rows would build a response large
#: enough to exhaust process memory, which is a denial of service against ourselves.
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


class AuditAction(str, Enum):
    """What happened. A closed vocabulary, deliberately.

    Free-text actions make the log unqueryable within a year — nobody remembers
    whether it was "login" or "logged_in", so dashboards silently miss half the data.
    Adding a member here is a one-line change; the closed set is what keeps the log
    aggregatable.
    """

    # Identity
    ACCOUNT_CREATED = "account.created"
    ACCOUNT_VERIFIED = "account.verified"
    ACCOUNT_SUSPENDED = "account.suspended"
    ACCOUNT_REACTIVATED = "account.reactivated"
    LOGIN_SUCCEEDED = "auth.login.succeeded"
    LOGIN_FAILED = "auth.login.failed"
    LOGIN_LOCKED = "auth.login.locked"
    PASSWORD_CHANGED = "auth.password.changed"
    #: A password change STARTED from inside a session — the confirmation code was sent.
    #:
    #: Recorded on the request rather than only on completion, because the abandoned and failed
    #: attempts are the interesting ones. A log that holds only successful changes cannot answer
    #: "did somebody with her session try to change her password last Tuesday?", which is precisely
    #: what a compromise looks like from the outside.
    PASSWORD_CHANGE_REQUESTED = "auth.password.change_requested"
    #: A wrong, expired or already-spent confirmation code. Repeated entries on one account are the
    #: signature of someone guessing a code they did not receive.
    PASSWORD_CHANGE_FAILED = "auth.password.change_failed"
    #: Session lifecycle. `SESSION_ENDED_IDLE` is distinct from `LOGOUT` on purpose —
    #: "walked away" and "chose to leave" are different facts, and only the first
    #: suggests a shared or unattended device worth asking about.
    LOGOUT = "auth.logout"
    SESSION_ENDED_IDLE = "auth.session.idle_timeout"
    SESSION_EXTENDED = "auth.session.extended"
    #: A sign-in link was emailed. **Not a sign-in** — the link may never be opened.
    #:
    #: This was previously recorded as `LOGIN_SUCCEEDED`, which was wrong in a way that mattered
    #: once `store.trusted_devices` started deriving the Security page's device table from sign-in
    #: rows: a device that merely *asked* for a link would have appeared as one the account had
    #: authenticated from. The redemption is the sign-in, and it is recorded separately.
    MAGIC_LINK_REQUESTED = "auth.magic_link.requested"

    # Preferences and subscription
    PREFERENCES_UPDATED = "account.preferences.updated"
    #: A page view on a surface that shows subscriber data. Recorded because "who looked
    #: at this, and when" is the question an incident review actually asks, and it cannot
    #: be reconstructed after the fact from anything else.
    #:
    #: Deliberately NOT every navigation — only the gated surfaces. Logging every route
    #: change would bury the entries that matter under noise, and grow the collection for
    #: no investigative gain.
    DASHBOARD_VIEWED = "portal.dashboard.viewed"
    SUBSCRIPTION_ACTIVATED = "subscription.activated"
    AREA_ADDED = "subscription.area.added"
    #: An area renamed or re-cropped. Distinct from ADDED so "who changed this plot's name"
    #: is answerable — an aggregator correcting a typo and one adding a plot are different acts.
    AREA_UPDATED = "subscription.area.updated"
    AREA_REMOVED = "subscription.area.removed"
    CHANNEL_UPDATED = "subscription.channel.updated"

    # API keys — the highest-value entries in the log
    KEY_CREATED = "apikey.created"
    KEY_ROTATED = "apikey.rotated"
    KEY_REVOKED = "apikey.revoked"
    KEY_REJECTED = "apikey.rejected"
    KEY_SCOPE_DENIED = "apikey.scope_denied"

    # Workspaces — an aggregator's projects. Track activation is a commercial change, so it
    # belongs in the permanent record alongside key creation.
    WORKSPACE_CREATED = "workspace.created"
    WORKSPACE_UPDATED = "workspace.updated"
    WORKSPACE_DELETED = "workspace.deleted"

    # Team membership. Distinct from the `tenancy.*` actions below: those are an aggregator's
    # commercial relationship with a customer, these are a colleague's access level. Sharing a
    # label would make "who granted this access" unanswerable, because the two edges mean
    # different things.
    MEMBER_INVITED = "team.member.invited"
    #: Distinct from `MEMBER_INVITED`: a resend means the first attempt did not land, which is
    #: worth being able to count. Several resends to one address is a deliverability problem,
    #: and an entry that looked like a fresh invitation would hide that.
    MEMBER_INVITE_RESENT = "team.member.invite_resent"
    MEMBER_JOINED = "team.member.joined"
    MEMBER_ROLE_CHANGED = "team.member.role_changed"
    MEMBER_REMOVED = "team.member.removed"

    # Multi-tenancy
    MEMBERSHIP_ATTACHED = "tenancy.membership.attached"
    MEMBERSHIP_DETACHED = "tenancy.membership.detached"
    MEMBERSHIP_REVOKED_BY_SUBSCRIBER = "tenancy.membership.revoked_by_subscriber"

    # Aggregator actions on customers
    CUSTOMER_ONBOARDED = "aggregator.customer.onboarded"
    CUSTOMER_READ = "aggregator.customer.read"
    CUSTOMER_SCAN_TRIGGERED = "aggregator.customer.scan_triggered"


class AuditOutcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


@dataclass(frozen=True)
class Page:
    """One page of audit entries plus the cursor for the next.

    `next_cursor is None` means the end. `total` is deliberately **absent**: a
    `count_documents` on a growing collection is an unindexed scan that gets slower
    exactly as the log gets more valuable, and no UI needs an exact total to render
    "load more".
    """

    entries: list[dict]
    next_cursor: str | None
    has_more: bool
    page_size: int


def _encode_cursor(at: datetime, doc_id: str) -> str:
    """Opaque cursor over `(at, _id)`.

    Base64 of JSON, not a raw timestamp: opaque means a client cannot hand-craft one to
    read outside its own scope, and the tuple makes ordering total — two events in the
    same millisecond would otherwise make a timestamp-only cursor skip one.
    """
    payload = json.dumps({"at": at.isoformat(), "id": str(doc_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str] | None:
    """Parse a cursor, or None if it is malformed.

    None rather than raising: a truncated or tampered cursor should restart from the
    first page, not 500. A client cannot use a bad cursor to see anything it should
    not, because the tenant filter is applied independently of the cursor.
    """
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded).decode())
        at = datetime.fromisoformat(data["at"])
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return at, str(data["id"])
    except (ValueError, KeyError, TypeError, binascii.Error, json.JSONDecodeError):
        return None


def build_event(
    *,
    account_id: str,
    action: AuditAction,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    actor_id: str | None = None,
    actor_kind: str = "self",
    target_id: str | None = None,
    detail: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
) -> dict:
    """One audit document. Fixed shape, every field bounded.

    **`actor_id` and `account_id` are separate, and that separation is the point.** When
    an aggregator onboards a farmer, the event is *about* the farmer (`account_id`) and
    *by* the aggregator (`actor_id`). Collapsing them would make it impossible to answer
    "who did this to my account?" — the single most important question a subscriber can
    ask of an audit log, and the one a regulator asks on their behalf.
    """
    now = datetime.now(timezone.utc)
    return {
        "account_id": account_id,
        "actor_id": actor_id or account_id,
        # "self" | "aggregator" | "operator" | "system"
        "actor_kind": actor_kind,
        "action": action.value,
        "outcome": outcome.value,
        "target_id": target_id,
        "detail": (detail or "")[:MAX_DETAIL_CHARS] or None,
        # Truncated to fit IPv6. An IP is enough to correlate an incident; a full
        # request dump would make the audit log a second, unmanaged PII store.
        "ip": (ip or "")[:45] or None,
        "user_agent": (user_agent or "")[:200] or None,
        # Ties an audit entry to the application log line for the same request.
        "request_id": (request_id or "")[:64] or None,
        "at": now,
        # Retention by database policy, set at insert so it cannot be shortened
        # retroactively for entries already written.
        "expires_at": now + timedelta(days=settings.iam_audit_retention_days),
    }


def cursor_query(base: dict, cursor: str | None) -> dict:
    """Add the keyset predicate to a scoped filter.

    `(at, _id) < (cursor_at, cursor_id)` expressed as an `$or`, which is the standard
    compound-key keyset form: strictly-earlier timestamps, plus same-timestamp
    documents with a smaller id. Without the second clause, events sharing a
    millisecond are silently dropped from the results.

    **The tenant filter in `base` is applied independently of the cursor**, so a
    forged cursor cannot widen scope — it can only move the position within a scope
    the caller already has.
    """
    if not cursor:
        return base
    decoded = _decode_cursor(cursor)
    if decoded is None:
        return base
    at, doc_id = decoded
    from bson import ObjectId

    try:
        oid = ObjectId(doc_id)
    except Exception:
        return base

    return {
        **base,
        "$or": [{"at": {"$lt": at}}, {"at": at, "_id": {"$lt": oid}}],
    }


def make_page(docs: list[dict], page_size: int) -> Page:
    """Turn a fetched batch into a page.

    Callers fetch `page_size + 1` documents; the extra one answers "is there more?"
    without a second query and without a `count`. It is trimmed off before returning,
    so the client never sees it.
    """
    has_more = len(docs) > page_size
    visible = docs[:page_size]

    next_cursor = None
    if has_more and visible:
        last = visible[-1]
        next_cursor = _encode_cursor(last["at"], last["_id"])

    return Page(
        entries=[_serialise(d) for d in visible],
        next_cursor=next_cursor,
        has_more=has_more,
        page_size=page_size,
    )


def _serialise(doc: dict) -> dict:
    """JSON-safe view of one entry.

    `_id` and `expires_at` are dropped: the former is an internal ObjectId whose only
    external use is inside an opaque cursor, and the latter is a retention mechanism
    that would read as a meaningful property of the event.
    """
    out = {k: v for k, v in doc.items() if k not in ("_id", "expires_at")}
    at = out.get("at")
    if hasattr(at, "isoformat"):
        out["at"] = at.isoformat()
    return out


def clamp_page_size(requested: int | None) -> int:
    if not requested or requested < 1:
        return DEFAULT_PAGE_SIZE
    return min(requested, MAX_PAGE_SIZE)
