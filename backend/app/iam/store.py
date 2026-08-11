"""MongoDB Atlas identity store.

**Why a fifth datastore, when `docs/data-and-persistence-architecture.md` argued
against adding one.** That argument was against MongoDB for *timeseries* — assessments
and forecast points, which Postgres with Timescale handles better and which need to
be joined spatially. Identity is a different shape and the reasoning inverts:

| | Assessments (Postgres) | Identity (Atlas) |
|---|---|---|
| Access pattern | ranged, joined, spatially filtered | one document by id or email |
| Schema | fixed, migrated | varies by account kind, evolves with onboarding |
| Relationship to alerts | *is* the alert data | governs who may ask for it |

The decisive reason is **blast radius**. A read-replica grant, an analytics user or a
`pg_dump` on the operational database currently exposes assessments and subscriber
addresses. Putting password hashes and API-key hashes in that same database would
extend every one of those grants to credentials. Separate stores mean a Postgres
compromise cannot authenticate as anyone.

**Everything here degrades to unavailable rather than raising.** `available()` is
False without `MONGO_URL`, the IAM endpoints then return 503, and the satellite
pipeline is untouched — identity is an onboarding surface, not something a flood
warning depends on.

**No password hash or key hash ever leaves this module.** Reads that build an
`Account` project those fields away, which is why `Account` has no field for them.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.iam import attribution, identifiers, keys, security, team, tenancy
from app.iam import audit as audit_mod
from app.iam import passwordless as pwless
from app.iam import roles as roles_mod
from app.iam.keys import KeyEvent
from app.iam.models import (
    Account,
    AccountKind,
    AccountStatus,
    ApiKeyPublic,
    ApiKeyScope,
)
from app.logging_config import get_logger

log = get_logger(__name__)

_client: Any = None

#: Projection applied to every account read that produces an `Account`.
#:
#: An exclusion projection rather than an inclusion one, deliberately: a new field
#: added to the document should appear automatically, whereas a new *secret* must be
#: added here explicitly. Getting that backwards would mean the next credential-like
#: field is exposed by default.
_PUBLIC_PROJECTION = {"password_hash": 0, "verification_token_hash": 0}


def available() -> bool:
    return bool(settings.mongo_url)


def _db():
    """Lazily-created Atlas handle.

    Created on first use, not at import: `app/config.py` and the models must import
    with no Mongo configured, or the whole app becomes untestable without a database
    the pipeline does not need.
    """
    global _client
    if not available():
        raise RuntimeError("MONGO_URL is not configured")
    if _client is None:
        from motor.motor_asyncio import AsyncIOMotorClient

        _client = AsyncIOMotorClient(
            settings.mongo_url,
            serverSelectionTimeoutMS=settings.mongo_timeout_ms,
            # Atlas requires TLS; being explicit means a URI missing the query
            # parameter still connects securely rather than failing obscurely.
            tls=True,
            appname="shelter-iam",
        )
    return _client[settings.mongo_database]


async def ping() -> bool:
    if not available():
        return False
    try:
        await _db().command("ping")
        return True
    except Exception as exc:
        log.warning("Atlas unreachable", extra={"error": str(exc)})
        return False


async def close() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None


async def ensure_indexes() -> None:
    """Create the indexes the auth paths depend on. Idempotent.

    Two are **unique**, and that matters for correctness rather than speed:

    * `email` — two accounts with one address would make login ambiguous, and
      whichever document sorted first would silently win.
    * `api_keys.key_hash` — the guard looks a key up by hash, so a duplicate would
      make authorisation non-deterministic.

    Called at startup. A failure is logged and tolerated: the collections still work,
    just without the constraint, and refusing to boot over an index would take the
    warning pipeline down for an identity concern.
    """
    if not available():
        return
    try:
        db = _db()

        # --- accounts ---------------------------------------------------------
        # `id` is our own application identifier, NOT Mongo's `_id`. Nothing indexes
        # it automatically, so `get_account` — which runs on EVERY authenticated
        # request, for both session and API-key auth — was a full collection scan.
        # Verified with an explain plan: COLLSCAN before, IXSCAN after. This is the
        # single most important index in the IAM module.
        await db.accounts.create_index("id", unique=True, name="account_id_unique")
        await db.accounts.create_index("email", unique=True, name="email_unique")
        # `managed_by` no longer exists — tenancy moved to the memberships edge — so
        # its index is dead weight: it costs a write on every account insert and
        # serves no query. Dropped explicitly rather than left behind, because an
        # unused index is invisible cost that nobody later dares remove.
        try:
            await db.accounts.drop_index("managed_by_idx")
            log.info("dropped stale index accounts.managed_by_idx")
        except Exception:
            pass    # already absent on a fresh deployment

        # Both directions of the membership edge are hot: "which customers does this
        # aggregator serve" and "which aggregators serve this subscriber". The unique
        # compound index also makes a duplicate edge impossible, which is what keeps
        # "is this customer mine?" unambiguous and stops a doubled fan-out.
        await db.memberships.create_index(
            [("account_id", 1), ("aggregator_id", 1)], unique=True, name="membership_unique"
        )
        await db.memberships.create_index(
            [("aggregator_id", 1), ("status", 1)], name="tenant_customers_idx"
        )
        # (account_id, status) rather than account_id alone: `list_account_aggregators`
        # filters on both, and the compound form means the status filter is satisfied
        # by the index instead of by re-examining each document.
        await db.memberships.create_index(
            [("account_id", 1), ("status", 1)], name="membership_account_status_idx"
        )
        # Matches `list_tenant_accounts` including its sort.
        await db.memberships.create_index(
            [("aggregator_id", 1), ("status", 1), ("joined_at", -1)],
            name="tenant_customers_sorted_idx",
        )
        await db.key_audit.create_index("expires_at", expireAfterSeconds=0, name="audit_ttl")

        # --- The immutable audit log -----------------------------------------
        # These indexes are what make keyset pagination O(1) per page instead of
        # O(offset). The compound (scope, at DESC, _id DESC) shape matches the query
        # exactly, so Mongo walks a range rather than scanning and discarding.
        #
        # Without them the log gets slower precisely as it becomes more valuable, and
        # a `skip`-based UI would start timing out somewhere past a few hundred
        # thousand entries.
        # Workspaces and organisation members.
        #
        # Both are read on EVERY portal render — the nav needs permissions and the workspace
        # page needs the list — so an unindexed collection scan here is on the hot path of
        # every page view, not an occasional report. The earlier index audit found exactly
        # this class of defect on `get_account`, which is why these are declared with the
        # collection rather than added after someone notices the latency.
        #
        # `account_id` alone rather than a compound key: an organisation has a handful of
        # workspaces, so the sort is free once the scan is bounded to one tenant.
        await db.workspaces.create_index("account_id", name="workspace_by_account")
        # Unique on the public id: it is minted with a retry loop, and a duplicate would mean
        # two workspaces answering the same key's scope.
        await db.workspaces.create_index("id", unique=True, name="workspace_id_unique")

        # One membership document per person per organisation. Unique so an invitation
        # accepted twice cannot produce two roles for the same account — of which the second
        # would silently win, and it might be the more permissive one.
        # Drop the superseded account-level unique index if a previous deployment created it.
        #
        # Left in place it would keep rejecting a second workspace membership for the same
        # person — the requirement this model exists to support — and the failure would look
        # like a mysterious duplicate-key error on invite acceptance rather than like a stale
        # index. `create_index` will not replace an index of the same keys with different
        # options, so this must be an explicit drop.
        try:
            await db.org_members.drop_index("org_member_unique")
            log.info("dropped superseded index org_member_unique (account-level)")
        except Exception:  # noqa: BLE001 — absent on a fresh deployment, which is normal
            pass

        # Unique on the EDGE, not on the account.
        #
        # This index previously covered `account_id` alone, which enforced one role per person
        # and so made "assigned to a workspace or many workspaces with the assigned roles"
        # impossible to represent. The pair is what must be unique: one membership per person
        # per workspace, so an invitation accepted twice cannot produce two roles on the same
        # workspace — of which the second would silently win, and might be the wider one.
        await db.org_members.create_index(
            [("account_id", 1), ("workspace_id", 1)],
            unique=True,
            name="org_member_edge_unique",
        )
        await db.org_members.create_index(
            "organisation_id", name="org_member_by_organisation"
        )
        # Attribution is read by aoi_id (one area) and by owner_id (an invoice).
        await db.area_attribution.create_index(
            "aoi_id", unique=True, name="attribution_aoi_unique"
        )
        await db.area_attribution.create_index(
            [("owner_id", 1), ("ended_at", 1)], name="attribution_by_owner"
        )
        # And by project, for a per-workspace invoice breakdown. `owner_id` leads so this also
        # serves the owner-only query — an aggregator with several projects always filters by
        # themselves first, and a compound index cannot be used from its second field alone.
        await db.area_attribution.create_index(
            [("owner_id", 1), ("workspace_id", 1), ("ended_at", 1)],
            name="attribution_by_workspace",
        )

        # Invitations are looked up by hash on accept and by organisation on the team page.
        await db.org_invitations.create_index(
            "token_hash", unique=True, name="invite_token_unique"
        )
        await db.org_invitations.create_index(
            [("organisation_id", 1), ("status", 1)], name="invite_by_organisation"
        )
        await db.org_invitations.create_index(
            "email", name="invite_by_email"
        )

        await db.audit_log.create_index(
            [("account_id", 1), ("at", -1), ("_id", -1)], name="audit_account_keyset"
        )
        await db.audit_log.create_index(
            [("actor_id", 1), ("at", -1), ("_id", -1)], name="audit_actor_keyset"
        )
        await db.audit_log.create_index(
            [("account_id", 1), ("action", 1), ("at", -1), ("_id", -1)],
            name="audit_action_keyset",
        )
        # Retention by database policy. Deletion happens on Mongo's schedule, which
        # nothing in the application can trigger — that is what keeps the log
        # append-only from the application's point of view.
        await db.audit_log.create_index(
            "expires_at", expireAfterSeconds=0, name="audit_log_ttl"
        )
        # (key_id, account_id, at DESC) — `key_audit_trail` scopes by account so one
        # aggregator cannot read another's trail, and that predicate belongs in the
        # index rather than being applied after the fetch.
        await db.key_audit.create_index(
            [("key_id", 1), ("account_id", 1), ("at", -1)], name="audit_key_scoped_idx"
        )
        await db.accounts.create_index("subscriber_id", sparse=True, name="subscriber_idx")
        # --- api_keys ---------------------------------------------------------
        await db.api_keys.create_index("key_hash", unique=True, name="key_hash_unique")
        # `id` for the same reason as accounts: revoke and rotate look up by it.
        await db.api_keys.create_index("id", unique=True, name="key_id_unique")
        # (account_id, created_at DESC) matches `list_api_keys` including its sort, so
        # the sort is served by the index rather than done in memory.
        await db.api_keys.create_index(
            [("account_id", 1), ("created_at", -1)], name="key_account_created_idx"
        )
        # The expiry sweep filters on (status, expires_at) and was a COLLSCAN. It runs
        # every scheduler cycle, so it would scan every key ever created, forever.
        # Sparse: a never-expiring key has expires_at=None and is not a candidate.
        await db.api_keys.create_index(
            [("status", 1), ("expires_at", 1)], sparse=True, name="key_expiry_sweep_idx"
        )
        # The rotation-grace half of the same sweep.
        await db.api_keys.create_index(
            [("status", 1), ("grace_expires_at", 1)], sparse=True, name="key_grace_sweep_idx"
        )
        # --- auth tokens (magic link, password reset) -------------------------
        # Looked up by hash on every click, so the hash needs an index; and both
        # collections expire themselves via TTL rather than needing a cleanup job.
        await db.auth_tokens.create_index(
            [("token_hash", 1), ("purpose", 1)], name="auth_token_lookup"
        )
        await db.auth_tokens.create_index("account_id", name="auth_token_account_idx")
        await db.auth_tokens.create_index(
            "expires_at", expireAfterSeconds=0, name="auth_token_ttl"
        )
        # Throttle counter: (email, purpose, at) matches the count query exactly.
        await db.auth_token_requests.create_index(
            [("email", 1), ("purpose", 1), ("at", -1)], name="token_request_window"
        )
        await db.auth_token_requests.create_index(
            "expires_at", expireAfterSeconds=0, name="token_request_ttl"
        )

        # --- login_attempts ---------------------------------------------------
        # `is_locked_out` and `register_failed_login` both query by email and were
        # COLLSCANs. That is on the login path, so it degrades exactly when someone is
        # being credential-stuffed — the moment the throttle matters most.
        await db.login_attempts.create_index("email", unique=True, name="attempt_email_unique")
        # Records expire themselves, so there is no cleanup job.
        await db.login_attempts.create_index(
            "expires_at", expireAfterSeconds=0, name="attempt_ttl"
        )
        log.info("IAM indexes ensured")
    except Exception as exc:
        log.warning("could not ensure IAM indexes", extra={"error": str(exc)})


def _to_account(doc: dict | None) -> Account | None:
    if doc is None:
        return None
    doc = dict(doc)
    doc.pop("_id", None)
    # Defensive: even if a caller forgot the projection, these never reach a model.
    doc.pop("password_hash", None)
    doc.pop("verification_token_hash", None)
    try:
        return Account(**doc)
    except Exception as exc:
        # Log the account id and the reason, not just "malformed". A bare message
        # here cost real debugging time: a rejected document gave no indication of
        # *which* field failed, and the caller only saw None.
        #
        # Mongo tolerates any shape, so this is the only place a schema mismatch
        # surfaces — it must say enough to act on. The document itself is never
        # logged: it holds an email address and, if a projection were ever missed, a
        # hash.
        log.warning(
            "account document failed validation",
            extra={
                "account_id": doc.get("id", "<missing>"),
                "fields": sorted(doc.keys()),
                "error": str(exc).replace("\n", " ")[:300],
            },
        )
        return None


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #


async def create_account(
    *,
    kind: AccountKind,
    email: str,
    first_name: str,
    last_name: str,
    password: str | None,
    phone: str | None = None,
    organisation: str | None = None,
    sector: str | None = None,
    language: str = "en",
    preferred_channel: str = "email",
) -> tuple[Account | None, str | None]:
    """`(account, verification_token)`, or `(None, None)` if the email is taken.

    Returns rather than raises on a duplicate, because "this address already has an
    account" is an expected outcome of a public signup form, not an error condition.

    `password=None` is the aggregator-created case: the individual has no portal
    credential until they claim the account by email. An aggregator must never be
    able to set a farmer's password — that would let them impersonate the farmer with
    no trace of who acted.

    **No `managed_by` parameter.** Tenancy is a `memberships` edge, added by
    `attach_membership`, because a subscriber may be served by several aggregators at
    once and none of them owns the identity. Creating the account and attaching the
    first membership are two steps so that the *second* aggregator to reach the same
    person attaches to the existing identity instead of being told the address is
    taken.
    """
    # 10-char alphanumeric, minted through the uniqueness check rather than assumed
    # unique from entropy alone. The unique index on `accounts.id` is the real
    # guarantee; this resolves a collision before the insert so it never surfaces as a
    # duplicate-key error the caller has to distinguish from a duplicate email.
    async def _id_taken(candidate: str) -> bool:
        return await _db().accounts.count_documents({"id": candidate}, limit=1) > 0

    try:
        account_id = await identifiers.mint_unique(_id_taken, label="account id")
    except RuntimeError as exc:
        log.error("could not mint an account id", extra={"error": str(exc)})
        return None, None

    token = security.new_verification_token()

    document = {
        "id": account_id,
        "kind": kind.value,
        "status": AccountStatus.PENDING_VERIFICATION.value,
        "email": email.lower().strip(),
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
        "phone": phone,
        "organisation": organisation,
        "sector": sector,
        "language": language,
        "preferred_channel": preferred_channel,
        "subscriber_id": None,
        "email_verified": False,
        "created_at": datetime.now(timezone.utc),
        "last_login_at": None,
        "password_hash": security.hash_password(password) if password else None,
        "verification_token_hash": security.hash_token(token),
        "verification_expires_at": datetime.now(timezone.utc)
        + timedelta(hours=settings.iam_verification_ttl_hours),
    }

    try:
        await _db().accounts.insert_one(document)
    except Exception as exc:
        # DuplicateKeyError is the common path; anything else is logged the same way
        # because the caller's response is identical and must not leak the reason.
        if "duplicate" in str(exc).lower() or "E11000" in str(exc):
            return None, None
        log.exception("account creation failed")
        return None, None

    log.info("account created", extra={"account_id": account_id, "kind": kind.value})
    return _to_account(document), token


async def get_account(account_id: str) -> Account | None:
    try:
        doc = await _db().accounts.find_one({"id": account_id}, _PUBLIC_PROJECTION)
        return _to_account(doc)
    except Exception as exc:
        log.debug("account read failed", extra={"error": str(exc)})
        return None


async def get_account_by_email(email: str) -> Account | None:
    try:
        doc = await _db().accounts.find_one(
            {"email": email.lower().strip()}, _PUBLIC_PROJECTION
        )
        return _to_account(doc)
    except Exception as exc:
        log.debug("account read failed", extra={"error": str(exc)})
        return None


async def authenticate(email: str, password: str) -> Account | None:
    """Verify credentials. None on any failure, with no hint as to which.

    Three properties this deliberately preserves:

    * **A missing account and a wrong password are indistinguishable**, in both the
      response and the timing (`dummy_verify` burns the same CPU). Otherwise the
      endpoint is an account-enumeration oracle over a list of farmers in a named
      district.
    * **A suspended account cannot log in even with the correct password**, so
      suspension is effective immediately rather than at token expiry.
    * **Hashes are upgraded transparently** when cost parameters are raised.
    """
    normalised = email.lower().strip()
    try:
        doc = await _db().accounts.find_one({"email": normalised})
    except Exception as exc:
        log.warning("authentication lookup failed", extra={"error": str(exc)})
        return None

    if doc is None:
        security.dummy_verify()
        return None

    if not security.verify_password(password, doc.get("password_hash")):
        return None

    if doc.get("status") == AccountStatus.SUSPENDED.value:
        log.warning("suspended account attempted login", extra={"account_id": doc.get("id")})
        return None

    now = datetime.now(timezone.utc)
    updates: dict[str, Any] = {"last_login_at": now}
    if security.needs_rehash(doc["password_hash"]):
        updates["password_hash"] = security.hash_password(password)

    try:
        await _db().accounts.update_one({"id": doc["id"]}, {"$set": updates})
    except Exception:
        # A failed bookkeeping write must not fail an otherwise valid login.
        log.debug("post-login update failed")

    doc["last_login_at"] = now
    return _to_account(doc)


async def verify_email(token: str) -> Account | None:
    """Consume a verification token and activate the account.

    Looked up by *hash*, so a database leak does not let anyone verify arbitrary
    addresses. Single-use: the token fields are unset in the same update, which also
    makes a replayed link a no-op rather than a second activation.
    """
    try:
        doc = await _db().accounts.find_one_and_update(
            {
                "verification_token_hash": security.hash_token(token),
                "verification_expires_at": {"$gt": datetime.now(timezone.utc)},
            },
            {
                "$set": {
                    "email_verified": True,
                    "status": AccountStatus.ACTIVE.value,
                },
                "$unset": {"verification_token_hash": "", "verification_expires_at": ""},
            },
            projection=_PUBLIC_PROJECTION,
            return_document=True,
        )
    except Exception as exc:
        log.warning("email verification failed", extra={"error": str(exc)})
        return None

    if doc is None:
        return None
    log.info("email verified", extra={"account_id": doc.get("id")})
    return _to_account(doc)


async def bind_subscriber(account_id: str, subscriber_id: str) -> bool:
    """Link an activated subscription to its account.

    Separate from account creation because signup and activation are separate steps:
    an account exists after signup, and gains a `subscriber_id` when the plot is
    dropped on the map. Keeping them apart is what lets the portal complete signup in
    one screen and activation in the next.
    """
    try:
        result = await _db().accounts.update_one(
            {"id": account_id},
            {"$set": {"subscriber_id": subscriber_id, "status": AccountStatus.ACTIVE.value}},
        )
        return result.matched_count == 1
    except Exception as exc:
        log.warning("subscriber bind failed", extra={"error": str(exc)})
        return False


# --------------------------------------------------------------------------- #
# Multi-tenancy — a subscriber may belong to many aggregators
#
# See app/iam/tenancy.py for why membership is a join collection rather than a
# `managed_by` field. The short version: a farmer's cooperative, insurer and state
# extension service may all legitimately serve them, and none of them owns them.
# --------------------------------------------------------------------------- #


async def attach_membership(
    account_id: str,
    aggregator_id: str,
    *,
    role: tenancy.MembershipRole = tenancy.MembershipRole.MANAGER,
    external_ref: str | None = None,
    onboarded_by_this_tenant: bool = False,
    workspace_id: str | None = None,
) -> tenancy.Membership | None:
    """Link a subscriber to an aggregator. Idempotent.

    Re-attaching a previously detached edge **reactivates it rather than creating a
    second one**, so the aggregator's own `external_ref` and the original join date
    survive a detach/re-attach cycle. Duplicate edges would make "is this customer
    mine?" ambiguous and double every fan-out.

    Refuses to reactivate a `REVOKED_BY_SUBSCRIBER` edge: only the subscriber may undo
    that, or an aggregator could re-attach itself after the person removed it.
    """
    try:
        existing = await _db().memberships.find_one(
            {"account_id": account_id, "aggregator_id": aggregator_id}
        )
        if existing is not None:
            if existing.get("status") == tenancy.MembershipStatus.REVOKED_BY_SUBSCRIBER.value:
                log.warning(
                    "aggregator attempted to re-attach a subscriber-revoked membership",
                    extra={"account_id": account_id, "aggregator_id": aggregator_id},
                )
                return None
            await _db().memberships.update_one(
                {"id": existing["id"]},
                {"$set": {"status": tenancy.MembershipStatus.ACTIVE.value,
                          "detached_at": None,
                          "role": role.value,
                          # Re-attaching through a different workspace's key moves the customer
                          # into that project. Deliberate: the key used is the statement of which
                          # customer base they belong to, and silently keeping the old one would
                          # make the same customer appear in a project nobody assigned them to.
                          **({"workspace_id": workspace_id} if workspace_id else {}),
                          **({"external_ref": external_ref} if external_ref else {})}},
            )
            refreshed = await _db().memberships.find_one({"id": existing["id"]})
            return tenancy.to_membership(refreshed)

        document = tenancy.build_membership_document(
            account_id, aggregator_id, role=role, external_ref=external_ref,
            onboarded_by_this_tenant=onboarded_by_this_tenant,
            workspace_id=workspace_id,
        )
        await _db().memberships.insert_one(document)
        log.info(
            "membership attached",
            extra={"account_id": account_id, "aggregator_id": aggregator_id,
                   "role": role.value},
        )
        return tenancy.to_membership(document)
    except Exception as exc:
        log.warning("membership attach failed", extra={"error": str(exc)})
        return None


async def detach_membership(
    account_id: str, aggregator_id: str, *, by_subscriber: bool = False
) -> bool:
    """End one aggregator's access without touching the identity or other tenants.

    `by_subscriber=True` records that the *person* revoked it, which the aggregator
    cannot then undo. Retained rather than deleted so the record of who served whom
    survives — which is what an insurance dispute turns on.
    """
    status = (
        tenancy.MembershipStatus.REVOKED_BY_SUBSCRIBER
        if by_subscriber
        else tenancy.MembershipStatus.DETACHED
    )
    try:
        result = await _db().memberships.update_one(
            {"account_id": account_id, "aggregator_id": aggregator_id},
            {"$set": {"status": status.value,
                      "detached_at": datetime.now(timezone.utc)}},
        )
        if result.matched_count == 1:
            log.info(
                "membership detached",
                extra={"account_id": account_id, "aggregator_id": aggregator_id,
                       "by_subscriber": by_subscriber},
            )
        return result.matched_count == 1
    except Exception as exc:
        log.warning("membership detach failed", extra={"error": str(exc)})
        return False


async def get_membership(
    account_id: str, aggregator_id: str, *, workspace_id: str | None = None
) -> tenancy.Membership | None:
    """One edge, or None. The authorisation primitive for every scoped read.

    `workspace_id` narrows to one project, so a key minted for the Bayelsa pilot cannot read a
    customer belonging to the Kebbi season by quoting their id. Omitted means any workspace,
    which is what a pre-workspace key and the portal session both need.
    """
    try:
        doc = await _db().memberships.find_one(
            {
                "account_id": account_id,
                **tenancy.active_filter(aggregator_id, workspace_id=workspace_id),
            }
        )
        return tenancy.to_membership(doc)
    except Exception as exc:
        log.debug("membership read failed", extra={"error": str(exc)})
        return None


async def is_member(
    account_id: str, aggregator_id: str, *, workspace_id: str | None = None
) -> bool:
    """Whether this tenant may see this account, within this workspace. Used by `owned_account`."""
    return (
        await get_membership(account_id, aggregator_id, workspace_id=workspace_id)
        is not None
    )


async def list_tenant_accounts(
    aggregator_id: str,
    *,
    limit: int = 200,
    skip: int = 0,
    workspace_id: str | None = None,
) -> list[Account]:
    """The accounts one aggregator serves, optionally within one workspace.

    Two queries rather than an aggregation `$lookup`, deliberately: the membership
    query establishes the tenant boundary *first*, so the accounts query can only ever
    ask for ids this tenant is entitled to. A `$lookup` starting from accounts would
    put the boundary in a later pipeline stage, where a mistake leaks another tenant's
    rows — the same reason the chat session filter is inside the query.
    """
    try:
        cursor = (
            _db()
            .memberships.find(
                tenancy.active_filter(aggregator_id, workspace_id=workspace_id),
                {"account_id": 1},
            )
            .sort("joined_at", -1)
            .skip(skip)
            .limit(min(limit, 500))
        )
        ids = [d["account_id"] async for d in cursor]
        if not ids:
            return []

        docs = _db().accounts.find(
            tenancy.scoped_account_filter(aggregator_id, ids), _PUBLIC_PROJECTION
        )
        return [a for a in [_to_account(d) async for d in docs] if a is not None]
    except Exception as exc:
        log.debug("tenant account list failed", extra={"error": str(exc)})
        return []


async def count_tenant_accounts(aggregator_id: str) -> int:
    try:
        return int(await _db().memberships.count_documents(tenancy.active_filter(aggregator_id)))
    except Exception:
        return 0


async def list_account_aggregators(account_id: str) -> list[dict]:
    """Which aggregators serve one subscriber — **for the subscriber's own eyes**.

    Exposed on the portal so a farmer can see and revoke who has access to their data.
    Never exposed to an aggregator: A learning that B also serves this farmer is
    commercially sensitive and none of A's business. The route enforces that by taking
    the account id from the session, not from a parameter.
    """
    try:
        cursor = _db().memberships.find(
            {"account_id": account_id,
             "status": tenancy.MembershipStatus.ACTIVE.value},
            {"_id": 0, "external_ref": 0},   # another tenant's reference is private
        )
        out: list[dict] = []
        async for doc in cursor:
            aggregator = await get_account(doc["aggregator_id"])
            out.append({
                "aggregator_id": doc["aggregator_id"],
                "organisation": aggregator.organisation if aggregator else None,
                "role": doc.get("role"),
                "joined_at": doc["joined_at"].isoformat()
                if hasattr(doc.get("joined_at"), "isoformat") else doc.get("joined_at"),
            })
        return out
    except Exception as exc:
        log.debug("aggregator list failed", extra={"error": str(exc)})
        return []


async def mint_subscriber_id() -> str:
    """A checked-unique 10-character subscriber id.

    Called by the IAM activation flow, which is the path a real subscriber takes. The
    `Subscriber` model's own default mints an *unchecked* candidate — same shape, still
    caught by the unique index — so a directly-constructed object is valid but does not
    pay a round trip.

    Checks **both** stores. `subscribers` is a Postgres table while `accounts.subscriber_id`
    lives in Atlas, so an id free in one could be taken in the other; a collision there
    would silently bind an account to somebody else's subscription. Falls back to an
    unchecked mint when either store is unreachable rather than failing activation —
    the unique constraints still hold.
    """
    async def _taken(candidate: str) -> bool:
        if await _db().accounts.count_documents({"subscriber_id": candidate}, limit=1):
            return True
        try:
            from app.db import session as pg

            async with pg.acquire() as conn:
                return bool(
                    await conn.fetchval(
                        "SELECT 1 FROM subscribers WHERE id = $1", candidate
                    )
                )
        except Exception:
            # Postgres unreachable: the Atlas half still ran, and the unique index on
            # subscribers.id remains authoritative.
            return False

    try:
        return await identifiers.mint_unique(_taken, label="subscriber id")
    except RuntimeError as exc:
        log.warning(
            "falling back to an unchecked subscriber id",
            extra={"error": str(exc)},
        )
        return identifiers.mint()


async def list_accounts_by_kind(kind: AccountKind, *, limit: int = 200) -> list[Account]:
    """All accounts of one kind. Used for the service-account inventory.

    Not exposed for INDIVIDUAL: an endpoint that lists every subscriber is a bulk PII
    read, and nothing needs it — the aggregator path is tenant-scoped and the portal
    path is self-scoped.
    """
    try:
        cursor = (
            _db()
            .accounts.find({"kind": kind.value}, _PUBLIC_PROJECTION)
            .sort("created_at", -1)
            .limit(min(limit, 500))
        )
        return [a for a in [_to_account(d) async for d in cursor] if a is not None]
    except Exception as exc:
        log.debug("account list by kind failed", extra={"error": str(exc)})
        return []


async def set_status(account_id: str, status: AccountStatus) -> bool:
    try:
        result = await _db().accounts.update_one(
            {"id": account_id}, {"$set": {"status": status.value}}
        )
        return result.matched_count == 1
    except Exception as exc:
        log.warning("status update failed", extra={"error": str(exc)})
        return False


async def update_preferences(
    account_id: str, *, language: str | None = None, preferred_channel: str | None = None
) -> Account | None:
    updates = {
        k: v
        for k, v in (("language", language), ("preferred_channel", preferred_channel))
        if v is not None
    }
    if not updates:
        return await get_account(account_id)
    try:
        doc = await _db().accounts.find_one_and_update(
            {"id": account_id},
            {"$set": updates},
            projection=_PUBLIC_PROJECTION,
            return_document=True,
        )
        return _to_account(doc)
    except Exception as exc:
        log.warning("preference update failed", extra={"error": str(exc)})
        return None


# --------------------------------------------------------------------------- #
# Login throttling
# --------------------------------------------------------------------------- #


async def register_failed_login(email: str) -> int:
    """Count a failure and return the running total.

    Keyed on email rather than IP: farmers in one district commonly share a NAT, so
    IP-based lockout would let one attacker lock out a whole village. The records
    self-expire via a TTL index, so there is no cleanup job.
    """
    try:
        doc = await _db().login_attempts.find_one_and_update(
            {"email": email.lower().strip()},
            {
                "$inc": {"failures": 1},
                "$set": {
                    "expires_at": datetime.now(timezone.utc)
                    + timedelta(minutes=settings.iam_login_lockout_minutes)
                },
            },
            upsert=True,
            return_document=True,
        )
        return int(doc.get("failures", 1)) if doc else 1
    except Exception:
        # Fail OPEN: if the counter is unreachable we cannot know the count, and
        # locking every login out over a missing collection is the worse failure.
        # Same reasoning as `llm/budget.py`.
        return 0


async def is_locked_out(email: str) -> bool:
    try:
        doc = await _db().login_attempts.find_one({"email": email.lower().strip()})
        if doc is None:
            return False
        return int(doc.get("failures", 0)) >= settings.iam_max_login_attempts
    except Exception:
        return False


async def clear_failed_logins(email: str) -> None:
    try:
        await _db().login_attempts.delete_one({"email": email.lower().strip()})
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #


async def _audit(
    key_id: str,
    account_id: str,
    event: KeyEvent,
    *,
    detail: str | None = None,
    ip: str | None = None,
) -> None:
    """Append an audit entry. Never raises.

    A separate collection rather than an array on the key document: an array grows
    unboundedly inside a document that is read on **every** API request, so a busy
    integration would make its own authentication progressively slower. Bounded by a
    TTL index instead.

    Best-effort by design. Losing an audit line must not fail the request it
    describes — but every state change attempts one, so the record is complete in
    practice and the gaps are visible as missing `used` events rather than as silence.
    """
    try:
        await _db().key_audit.insert_one(
            {
                "key_id": key_id,
                "account_id": account_id,
                "event": event.value,
                "detail": detail,
                # Truncated: an IP is enough to correlate an incident, and storing a
                # full request context here would turn the audit log into a second
                # PII store.
                "ip": (ip or "")[:45] or None,
                "at": datetime.now(timezone.utc),
                "expires_at": datetime.now(timezone.utc)
                + timedelta(days=settings.iam_key_audit_retention_days),
            }
        )
    except Exception:
        log.debug("key audit write failed", extra={"key_id": key_id, "event": event.value})


async def create_api_key(
    account_id: str,
    name: str,
    scopes: list[ApiKeyScope],
    *,
    workspace_id: str | None = None,
    expires_in_days: int | None = None,
    test_mode: bool = False,
    ip: str | None = None,
) -> tuple[str, ApiKeyPublic] | None:
    """`(plaintext_key, public_record)`, or None if the per-account cap is reached.

    **The plaintext exists only in this return value.** It is never written to Atlas,
    never logged (`keys.redact` is used wherever a key could reach a log line), and
    never included in the notification email. Recovery is therefore not a policy we
    could relax — it is arithmetically impossible, which is what makes "shown once"
    true rather than merely stated.
    """
    try:
        live = await _db().api_keys.count_documents(
            {"account_id": account_id, "status": {"$in": [keys.KeyStatus.ACTIVE.value,
                                                          keys.KeyStatus.ROTATING.value]}}
        )
        if live >= settings.iam_max_api_keys_per_account:
            return None
    except Exception as exc:
        log.warning("key count failed", extra={"error": str(exc)})
        return None

    # Platform scopes are unbounded by tenant, so only a SERVICE account may hold
    # one. Enforced HERE, at mint time, rather than only at use time: a tenant key
    # carrying `platform:subscribers:write` could register a subscriber for anyone,
    # which is exactly the unbounded authority the shared key represented.
    from app.iam.models import PLATFORM_SCOPES

    account = await get_account(account_id)
    requested_platform = [s for s in scopes if s in PLATFORM_SCOPES]
    if requested_platform and (account is None or account.kind is not AccountKind.SERVICE):
        log.error(
            "refused to mint a platform scope on a non-service account",
            extra={
                "account_id": account_id,
                "kind": account.kind.value if account else "unknown",
                "scopes": [s.value for s in requested_platform],
            },
        )
        return None

    minted = keys.mint(test_mode=test_mode)
    now = datetime.now(timezone.utc)
    expires = keys.expiry_from_days(expires_in_days)
    key_id = f"key_{uuid.uuid4().hex[:20]}"

    document = {
        "id": key_id,
        "account_id": account_id,
        "name": name.strip(),
        "key_hash": minted.key_hash,
        "hint": minted.hint,
        "last_four": minted.last_four,
        "prefix": minted.prefix,
        "scopes": [s.value for s in scopes],
        # The workspace this key is scoped to, and therefore which activated intelligence
        # tracks it can reach. None for a SERVICE account, which has no workspace.
        "workspace_id": workspace_id,
        "status": keys.KeyStatus.ACTIVE.value,
        "created_at": now,
        "last_used_at": None,
        "use_count": 0,
        "expires_at": expires,
        "grace_expires_at": None,
        "rotated_to": None,
        "rotated_from": None,
    }

    try:
        await _db().api_keys.insert_one(document)
    except Exception:
        log.exception("api key creation failed")
        return None

    await _audit(key_id, account_id, KeyEvent.CREATED,
                 detail=f"scopes={[s.value for s in scopes]} "
                        f"expires={expires.isoformat() if expires else 'never'}",
                 ip=ip)
    log.info(
        "api key created",
        extra={
            "account_id": account_id,
            "key_id": key_id,
            # The redacted form, never the key.
            "key": keys.redact(minted.plaintext),
            "expires": expires.isoformat() if expires else "never",
        },
    )
    return minted.plaintext, _to_public(document)


def _to_public(doc: dict) -> ApiKeyPublic:
    """Build the public view. `key_hash` is structurally absent from the model."""
    return ApiKeyPublic(
        id=doc["id"],
        account_id=doc["account_id"],
        name=doc["name"],
        hint=doc.get("hint", ""),
        last_four=doc.get("last_four", ""),
        scopes=[ApiKeyScope(s) for s in doc.get("scopes", [])
                if s in ApiKeyScope._value2member_map_],
        workspace_id=doc.get("workspace_id"),
        status=keys.KeyStatus(doc.get("status", "active")),
        created_at=doc["created_at"],
        last_used_at=doc.get("last_used_at"),
        use_count=int(doc.get("use_count", 0)),
        expires_at=doc.get("expires_at"),
        grace_expires_at=doc.get("grace_expires_at"),
        health=keys.key_health(doc),
    )


async def resolve_api_key(
    presented: str, *, ip: str | None = None
) -> tuple[Account, list[ApiKeyScope], str | None] | None:
    """`(account, scopes, workspace_id)` for a valid key, else None.

    The **workspace** travels with the key because an aggregator runs several projects and each
    has its own key: a customer onboarded with the Bayelsa key belongs to the Bayelsa workspace,
    and must not appear when the Kebbi key lists customers. Resolving it here means every Partner
    API route gets the scope for free rather than each having to look the key up again.

    `None` for a key minted before workspaces existed, and for a SERVICE account, which has no
    workspace. Callers treat that as "unscoped" rather than refusing — an older integration must
    keep working.

    Five conditions must hold, each rejecting a distinct case:

    1. **Well-formed** — checked *before* any database read, so a flood of junk on
       this unauthenticated surface costs no I/O.
    2. The hash exists.
    3. `usable_status` is not None — covers revoked, expired, and a rotation whose
       grace window has closed, in one place so the guard and the portal cannot
       disagree about whether a key is live.
    4. The owning account is ACTIVE — suspending an aggregator disables every key it
       holds, without revoking them one by one.
    5. Not expired.

    A rejection is audited when the key is *recognisable* (well-formed but unusable),
    because a burst of those against one account is the signal that a key has leaked
    and is being probed.
    """
    if not keys.is_well_formed(presented):
        return None

    try:
        doc = await _db().api_keys.find_one({"key_hash": keys.hash_key(presented)})
    except Exception as exc:
        log.warning("api key lookup failed", extra={"error": str(exc)})
        return None

    if doc is None:
        return None

    status = keys.usable_status(doc)
    if status is None:
        await _audit(doc["id"], doc["account_id"], KeyEvent.REJECTED,
                     detail=f"status={doc.get('status')}", ip=ip)
        log.warning(
            "unusable api key presented",
            extra={"key_id": doc["id"], "status": doc.get("status"),
                   "key": keys.redact(presented)},
        )
        return None

    account = await get_account(doc["account_id"])
    if account is None or account.status is not AccountStatus.ACTIVE:
        await _audit(doc["id"], doc["account_id"], KeyEvent.REJECTED,
                     detail="account not active", ip=ip)
        return None

    # Usage bookkeeping. `$inc` so concurrent requests do not lose counts, and
    # best-effort so a failed write never fails the request it describes.
    try:
        await _db().api_keys.update_one(
            {"id": doc["id"]},
            {"$set": {"last_used_at": datetime.now(timezone.utc)}, "$inc": {"use_count": 1}},
        )
    except Exception:
        pass

    if status is keys.KeyStatus.ROTATING:
        # Loud, because the partner is still using a key that is about to die and the
        # only way they find out otherwise is an outage.
        log.warning(
            "rotated key still in use; it will stop working at the grace deadline",
            extra={"key_id": doc["id"], "grace_expires_at": str(doc.get("grace_expires_at"))},
        )

    scopes = [ApiKeyScope(s) for s in doc.get("scopes", [])
              if s in ApiKeyScope._value2member_map_]
    return account, scopes, doc.get("workspace_id")


async def list_api_keys(account_id: str) -> list[ApiKeyPublic]:
    """An account's keys, without secrets.

    `key_hash` is projected away at the query, so it never enters the process — a
    later serialisation bug cannot leak what was never fetched.
    """
    try:
        cursor = _db().api_keys.find(
            {"account_id": account_id}, {"key_hash": 0, "_id": 0}
        ).sort("created_at", -1)
        out: list[ApiKeyPublic] = []
        async for doc in cursor:
            try:
                out.append(_to_public(doc))
            except Exception as exc:
                log.warning("malformed key document",
                            extra={"key_id": doc.get("id"), "error": str(exc)[:200]})
        return out
    except Exception as exc:
        log.debug("key list failed", extra={"error": str(exc)})
        return []


async def revoke_api_key(
    account_id: str, key_id: str, *, reason: str = "", ip: str | None = None
) -> bool:
    """Revoke immediately. Effective on the very next request.

    Scoped by `account_id` in the query, so one aggregator cannot revoke another's key
    by guessing an id. Marked `revoked` rather than deleted: the audit trail of what
    that key did must survive its revocation, which is the first thing an incident
    review asks for.
    """
    try:
        result = await _db().api_keys.update_one(
            {"id": key_id, "account_id": account_id,
             "status": {"$ne": keys.KeyStatus.REVOKED.value}},
            {"$set": {"status": keys.KeyStatus.REVOKED.value,
                      "revoked_at": datetime.now(timezone.utc),
                      "revoke_reason": (reason or "")[:200]}},
        )
    except Exception as exc:
        log.warning("key revoke failed", extra={"error": str(exc)})
        return False

    if result.matched_count != 1:
        return False

    await _audit(key_id, account_id, KeyEvent.REVOKED, detail=reason or None, ip=ip)
    log.info("api key revoked", extra={"key_id": key_id, "account_id": account_id})
    return True


async def rotate_api_key(
    account_id: str,
    key_id: str,
    *,
    grace_hours: int | None = None,
    ip: str | None = None,
) -> tuple[str, ApiKeyPublic] | None:
    """Mint a replacement and put the old key into its grace window.

    **Not delete-then-create.** Both keys work until the deadline, so a partner can
    deploy the replacement and verify it before the old one dies. Delete-then-create
    forces a choice between an outage and leaving a compromised key live — and faced
    with that, people leave it live, which defeats the purpose of rotation.

    `grace_hours=0` is the incident path: the old key dies immediately.

    The replacement inherits the original's scopes and name, so a rotation cannot
    silently widen or narrow what the integration can do — the only thing that changes
    is the secret.
    """
    try:
        old = await _db().api_keys.find_one({"id": key_id, "account_id": account_id})
    except Exception as exc:
        log.warning("rotation lookup failed", extra={"error": str(exc)})
        return None

    if old is None or old.get("status") == keys.KeyStatus.REVOKED.value:
        return None

    scopes = [ApiKeyScope(s) for s in old.get("scopes", [])
              if s in ApiKeyScope._value2member_map_]

    # Preserve the remaining lifetime rather than resetting it: a key rotated one day
    # before expiry should not silently gain a fresh year.
    remaining_days: int | None = None
    if old.get("expires_at") is not None:
        expires_at = old["expires_at"]
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        remaining_days = max(1, (expires_at - datetime.now(timezone.utc)).days)

    minted = await create_api_key(
        account_id, old["name"], scopes, expires_in_days=remaining_days, ip=ip
    )
    if minted is None:
        return None
    plaintext, public = minted

    grace = keys.rotation_deadline(
        grace_hours if grace_hours is not None else settings.iam_key_rotation_grace_hours
    )
    try:
        await _db().api_keys.update_one(
            {"id": key_id},
            {"$set": {"status": keys.KeyStatus.ROTATING.value,
                      "grace_expires_at": grace,
                      "rotated_to": public.id}},
        )
        await _db().api_keys.update_one(
            {"id": public.id}, {"$set": {"rotated_from": key_id}}
        )
    except Exception as exc:
        log.warning("rotation bookkeeping failed", extra={"error": str(exc)})

    await _audit(key_id, account_id, KeyEvent.ROTATED,
                 detail=f"replaced_by={public.id} grace_until={grace.isoformat()}", ip=ip)
    log.info(
        "api key rotated",
        extra={"old_key_id": key_id, "new_key_id": public.id,
               "grace_expires_at": grace.isoformat()},
    )
    return plaintext, public


async def expire_due_keys() -> int:
    """Mark passed-expiry and passed-grace keys terminal. Returns how many.

    Called by the scheduler. `usable_status` already refuses them at request time, so
    this is bookkeeping rather than enforcement — but without it the portal would show
    an expired key as "active", and an operator reading a stale status is how a key
    gets trusted after it stopped working.
    """
    now = datetime.now(timezone.utc)
    total = 0
    try:
        for query in (
            {"status": keys.KeyStatus.ACTIVE.value, "expires_at": {"$lte": now}},
            {"status": keys.KeyStatus.ROTATING.value, "grace_expires_at": {"$lte": now}},
        ):
            result = await _db().api_keys.update_many(
                query, {"$set": {"status": keys.KeyStatus.EXPIRED.value}}
            )
            total += int(result.modified_count or 0)
    except Exception as exc:
        log.debug("key expiry sweep failed", extra={"error": str(exc)})
        return 0

    if total:
        log.info("api keys expired", extra={"count": total})
    return total


async def key_audit_trail(account_id: str, key_id: str, *, limit: int = 100) -> list[dict]:
    """Audit entries for one key, newest first.

    Scoped by `account_id` so one aggregator cannot read another's audit trail — which
    would otherwise expose their integration's usage pattern.
    """
    try:
        cursor = (
            _db()
            .key_audit.find({"key_id": key_id, "account_id": account_id}, {"_id": 0})
            .sort("at", -1)
            .limit(min(limit, 500))
        )
        return [
            {**d, "at": d["at"].isoformat() if hasattr(d.get("at"), "isoformat") else d.get("at")}
            async for d in cursor
        ]
    except Exception as exc:
        log.debug("audit read failed", extra={"error": str(exc)})
        return []


async def record_scope_denial(key_id: str, account_id: str, scope: str,
                              *, ip: str | None = None) -> None:
    """Audit a scope refusal.

    Worth recording separately from a rejection: repeated denials mean an integration
    is attempting something its key was not granted, which is either a
    misconfiguration to help them fix or a compromised key being probed for reach.
    """
    await _audit(key_id, account_id, KeyEvent.SCOPE_DENIED, detail=scope, ip=ip)


# --------------------------------------------------------------------------- #
# The immutable audit log
#
# APPEND-ONLY. There is deliberately no update or delete function in this section,
# and `tests/test_iam.py` asserts that structurally. Retention is a TTL index, i.e.
# deletion by database policy on Mongo's own schedule — not an application code path.
# See app/iam/audit.py for why each event is its own document (BSON 16 MB) and why
# pagination is keyset rather than skip/limit.
# --------------------------------------------------------------------------- #


async def record_audit(
    *,
    account_id: str,
    action: audit_mod.AuditAction,
    outcome: audit_mod.AuditOutcome = audit_mod.AuditOutcome.SUCCESS,
    actor_id: str | None = None,
    actor_kind: str = "self",
    target_id: str | None = None,
    detail: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
) -> bool:
    """Append one audit event. Never raises.

    Best-effort on purpose. An audit write must not fail the action it describes —
    refusing a farmer's login because the log was briefly unreachable would be a worse
    outcome than a gap in the record. The gap is visible (a login with no entry) rather
    than silent, and a `log.error` marks it.

    `insert_one` only. Nothing in this module updates or deletes an entry, which is
    what "immutable" means in practice rather than as an aspiration.
    """
    if not available():
        return False

    event = audit_mod.build_event(
        account_id=account_id, action=action, outcome=outcome, actor_id=actor_id,
        actor_kind=actor_kind, target_id=target_id, detail=detail, ip=ip,
        user_agent=user_agent, request_id=request_id,
    )
    try:
        await _db().audit_log.insert_one(event)
        return True
    except Exception as exc:
        log.error(
            "AUDIT WRITE FAILED — the action proceeded but is not in the log",
            extra={"account_id": account_id, "action": action.value, "error": str(exc)},
        )
        return False


async def audit_page(
    *,
    account_id: str,
    cursor: str | None = None,
    page_size: int | None = None,
    action: str | None = None,
    as_actor: bool = False,
) -> audit_mod.Page:
    """One page of audit entries, keyset-paginated.

    `as_actor=False` (the default) answers **"what happened to my account?"** — which
    includes actions an aggregator performed on the subscriber's behalf. That is the
    question a subscriber and a regulator ask, and it is why `account_id` and
    `actor_id` are separate fields.

    `as_actor=True` answers **"what did my organisation do?"** — the aggregator's own
    activity across all its customers.

    Fetches `page_size + 1` rows so "is there more?" needs no second query and no
    `count_documents`, which would be an unindexed scan that slows down as the log
    grows.
    """
    size = audit_mod.clamp_page_size(page_size)
    if not available():
        return audit_mod.Page(entries=[], next_cursor=None, has_more=False, page_size=size)

    base: dict = {"actor_id": account_id} if as_actor else {"account_id": account_id}
    if action:
        base["action"] = action

    try:
        cursor_doc = (
            _db()
            .audit_log.find(audit_mod.cursor_query(base, cursor))
            # Sort must match the index exactly, or Mongo does an in-memory sort and
            # the O(1)-per-page property is lost.
            .sort([("at", -1), ("_id", -1)])
            .limit(size + 1)
        )
        docs = [d async for d in cursor_doc]
    except Exception as exc:
        log.warning("audit read failed", extra={"error": str(exc)})
        return audit_mod.Page(entries=[], next_cursor=None, has_more=False, page_size=size)

    return audit_mod.make_page(docs, size)


async def audit_summary(account_id: str, *, days: int = 30) -> dict:
    """Counts by action over a window, for the portal's activity widget.

    An aggregation with a `$match` on the indexed prefix and a `$limit` before
    grouping, so the cost is bounded by the window rather than by the collection. A
    naive `count_documents` per action would be one full scan per action type.
    """
    if not available():
        return {}
    since = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        pipeline = [
            {"$match": {"account_id": account_id, "at": {"$gte": since}}},
            # Bounded so one very busy account cannot make this aggregation expensive.
            {"$limit": 10_000},
            {"$group": {"_id": {"action": "$action", "outcome": "$outcome"},
                        "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
        ]
        rows = [r async for r in _db().audit_log.aggregate(pipeline)]
    except Exception as exc:
        log.debug("audit summary failed", extra={"error": str(exc)})
        return {}

    return {
        "window_days": days,
        "by_action": [
            {"action": r["_id"]["action"], "outcome": r["_id"]["outcome"],
             "count": int(r["count"])}
            for r in rows
        ],
        # Stated explicitly so a reader does not mistake a capped aggregation for a
        # complete total on a very active account.
        "capped_at": 10_000,
    }


# --------------------------------------------------------------------------- #
# Passwordless sign-in, password reset, and TOTP
#
# See app/iam/passwordless.py for why magic links are the primary path for
# individuals and TOTP is opt-in rather than mandatory.
# --------------------------------------------------------------------------- #


async def _invited_member(account_id: str) -> bool:
    """Whether this account was created by redeeming a team invitation.

    Read from the account document rather than inferred from having a membership: a founder
    who later joins their own workspace has memberships too, and refusing them the magic link
    would be a silent behaviour change for an account that never opted into it.

    Fails **open for the magic link** — an unreadable account returns False, so sign-in still
    works. The alternative would lock a colleague out over a transient Mongo blip, and the
    magic link is itself an authenticated path proving mailbox control; it is not the
    dangerous direction.
    """
    try:
        doc = await _db().accounts.find_one({"id": account_id}, {"invited_member": 1})
    except Exception as exc:  # noqa: BLE001
        log.warning("invited-member lookup failed", extra={"error": str(exc)})
        return False
    return bool(doc and doc.get("invited_member"))


async def issue_single_use_token(
    email: str, purpose: pwless.TokenPurpose, *, next_path: str | None = None
) -> tuple[str, Account] | None:
    """Mint a token for an existing account. None when the address is unknown.

    **The caller must return an identical response either way.** A different response,
    or a materially different latency, turns this into an account-enumeration oracle
    over a list of farmers in named districts.

    Any outstanding token for the same purpose is replaced, so a link intercepted from
    an older email is already dead by the time a newer one is requested.
    """
    if not available():
        return None

    account = await get_account_by_email(email)
    if account is None:
        return None

    if account.status is AccountStatus.SUSPENDED:
        # Suspension must be effective immediately, including against a
        # passwordless path that never touches the password.
        log.warning("token requested for a suspended account", extra={"account_id": account.id})
        return None

    # An invited team member signs in with their password and nothing else.
    #
    # One credential path per account, so "how did they get in?" has exactly one answer in an
    # audit — which matters more for someone holding write access to an aggregator's whole
    # customer base than for an individual farmer, who keeps the magic link precisely because
    # typing a strong password on a low-end phone is painful.
    #
    # Password RESET stays available: locking a colleague out of recovery would make a
    # forgotten password an administrator ticket.
    if (
        purpose is pwless.TokenPurpose.MAGIC_LINK
        and (await _invited_member(account.id))
    ):
        log.info(
            "magic link refused for an invited member",
            extra={"account_id": account.id},
        )
        return None

    token = pwless.mint_token(purpose)
    try:
        await _db().auth_tokens.delete_many(
            {"account_id": account.id, "purpose": purpose.value}
        )
        await _db().auth_tokens.insert_one(
            {
                "account_id": account.id,
                "purpose": purpose.value,
                "token_hash": token.token_hash,
                "next_path": pwless.safe_next_path(next_path),
                "created_at": datetime.now(timezone.utc),
                "expires_at": token.expires_at,
                "consumed_at": None,
            }
        )
    except Exception as exc:
        log.warning("token issue failed", extra={"error": str(exc)})
        return None

    return token.plaintext, account


async def redeem_single_use_token(
    plaintext: str, purpose: pwless.TokenPurpose
) -> tuple[Account, str] | None:
    """`(account, next_path)` for a valid token, else None.

    `find_one_and_delete` rather than a read-then-delete: the token is removed in the
    same atomic operation that validates it, so two concurrent clicks on the same link
    cannot both succeed. A read-then-delete has a window where both pass.

    Matched on the **hash**, so a database leak yields no working links. The purpose is
    part of the query, so a password-reset token cannot be redeemed as a sign-in.
    """
    if not available():
        return None

    try:
        doc = await _db().auth_tokens.find_one_and_delete(
            {
                "token_hash": pwless.token_hash(plaintext),
                "purpose": purpose.value,
                "expires_at": {"$gt": datetime.now(timezone.utc)},
            }
        )
    except Exception as exc:
        log.warning("token redemption failed", extra={"error": str(exc)})
        return None

    if doc is None:
        return None

    account = await get_account(doc["account_id"])
    if account is None or account.status is AccountStatus.SUSPENDED:
        return None

    # A magic-link click proves control of the mailbox, which is exactly what email
    # verification asks — so redeeming one confirms the address rather than leaving the
    # account stuck pending. Password reset does NOT imply this: the user may be
    # resetting precisely because they never received the original email.
    if purpose is pwless.TokenPurpose.MAGIC_LINK and not account.email_verified:
        await _db().accounts.update_one(
            {"id": account.id},
            {"$set": {"email_verified": True, "status": AccountStatus.ACTIVE.value}},
        )
        account = await get_account(account.id) or account

    return account, doc.get("next_path") or "/dashboard"


#: Sign-in actions that establish a device as one this person actually used.
#:
#: A FAILED login is excluded deliberately. Someone guessing a password from a machine the owner
#: has never touched would otherwise appear in their own "trusted devices" list, which inverts the
#: meaning of the table: it must answer "where have I signed in from", not "who has tried".
#: `LOGIN_SUCCEEDED` alone, and that is deliberate — it now covers every real sign-in:
#:
#:   * password;
#:   * password plus second factor;
#:   * a redeemed magic link, which is the primary path for farmers.
#:
#: All three carry their IP and user agent, which they did not before this table existed.
#: `MAGIC_LINK_REQUESTED` is excluded because requesting a link is not signing in — the link may
#: never be opened, and including it would list a device that never authenticated.
_DEVICE_ACTIONS: tuple[str, ...] = (audit_mod.AuditAction.LOGIN_SUCCEEDED.value,)


async def trusted_devices(account_id: str, *, limit: int = 12) -> list[dict]:
    """Devices this account has signed in from, most recent first.

    ## Why this is derived rather than stored

    Every field the table needs — user agent, IP, resolved location, timestamp — is *already*
    written on every sign-in by `record_audit`. A separate `devices` collection would be a second
    copy of facts we hold, kept in sync by a write on the login path, and it would show nothing at
    all for accounts that already exist until each signed in again.

    Deriving it means the history is correct retroactively and there is no new failure mode. The
    cost is that "trusted" is inferred from recency rather than asserted — which is exactly the
    semantics the page states: a device stays listed unless the account goes unused.

    ## What one row is

    Grouped by `(user_agent, ip)`, not by user agent alone. The same browser on the same laptop
    seen from home and from a market's wifi are two different facts, and collapsing them would hide
    a sign-in from a place the owner does not recognise — the one thing this table exists to
    surface.

    Returns plain dicts rather than a model because the route shapes them into its own response —
    including resolving the **location**, which is deliberately not aggregated here. Audit rows
    store the IP only; the geo database is consulted at read time (`iam.geo.lookup`), so a row
    written before the database was installed still resolves, and a location is never a stale copy
    of a lookup made months ago.
    """
    if not available():
        return []

    try:
        cursor = _db().audit_log.aggregate(
            [
                {"$match": {"account_id": account_id, "action": {"$in": list(_DEVICE_ACTIONS)}}},
                # Newest first BEFORE grouping, so `$first` inside each group is the latest
                # sign-in from that device rather than an arbitrary one.
                {"$sort": {"at": -1}},
                {
                    "$group": {
                        "_id": {"ua": "$user_agent", "ip": "$ip"},
                        "last_seen": {"$first": "$at"},
                        "first_seen": {"$last": "$at"},
                        "sign_ins": {"$sum": 1},
                    }
                },
                {"$sort": {"last_seen": -1}},
                # Bounded: an account signing in from a new IP each day would otherwise render an
                # unbounded table, and past a dozen rows nobody is auditing anything.
                {"$limit": max(1, min(limit, 50))},
            ]
        )
        rows = [row async for row in cursor]
    except Exception as exc:  # noqa: BLE001
        log.warning("trusted device lookup failed", extra={"error": str(exc)})
        return []

    return [
        {
            "user_agent": row["_id"].get("ua"),
            "ip": row["_id"].get("ip"),
            "last_seen": row.get("last_seen"),
            "first_seen": row.get("first_seen"),
            "sign_ins": row.get("sign_ins", 0),
        }
        for row in rows
    ]


async def issue_password_change_code(account_id: str) -> str | None:
    """Mint and store a 6-character confirmation code. Returns the plaintext to email.

    Keyed on `account_id` rather than on an email address, unlike `issue_single_use_token`: the
    caller is already authenticated, so there is no address to look up and no enumeration oracle to
    protect against. The code always goes to the account's *registered* address — never to one
    supplied in the request — which is what makes it a proof of mailbox control rather than a
    formality the requester can redirect.

    Any outstanding code is replaced, so requesting a second one immediately invalidates the first.
    A subscriber who asks again because the mail was slow should not end up with two live codes.
    """
    if not available():
        return None

    code = pwless.mint_password_change_code()
    try:
        await _db().auth_tokens.delete_many(
            {
                "account_id": account_id,
                "purpose": pwless.TokenPurpose.PASSWORD_CHANGE_CODE.value,
            }
        )
        await _db().auth_tokens.insert_one(
            {
                "account_id": account_id,
                "purpose": pwless.TokenPurpose.PASSWORD_CHANGE_CODE.value,
                "token_hash": code.token_hash,
                "created_at": datetime.now(timezone.utc),
                "expires_at": code.expires_at,
                "consumed_at": None,
                # Counted up on each wrong guess. This — not the code's 30 bits — is what makes a
                # short code safe; see `passwordless.PASSWORD_CODE_LENGTH`.
                "attempts": 0,
            }
        )
    except Exception as exc:
        log.warning("password change code issue failed", extra={"error": str(exc)})
        return None

    return code.plaintext


async def redeem_password_change_code(account_id: str, code: str) -> bool:
    """True when the code is correct, consuming it. False on wrong, expired, or spent.

    ## Why this is not `redeem_single_use_token`

    That one is `find_one_and_delete` on the hash, which is exactly right for a 256-bit token: any
    guess is hopeless, so counting failures is pointless. Here the secret is ~30 bits and the
    attempt ceiling is the actual security control, which needs the row to *survive* a wrong guess
    long enough to be counted. So the lookup is by account and the deletion is explicit.

    ## Why a wrong guess consumes the attempt but not the code, and the fifth consumes both

    Burning the code on the first mistake would make a typo cost a fresh email — on a 6-character
    code retyped from a phone, that is the common case, not the adversarial one. Burning it on the
    fifth caps an attacker at five tries out of 32^6 regardless of how long the window is.

    Deleted rather than locked out: a lockout keyed on an account would let anyone who knows an
    email address deny its owner a password change.

    Scoped by `account_id`, taken from the session and never from the request body, so one
    subscriber's code cannot be redeemed against another's account.
    """
    if not available():
        return False

    normalised = pwless.normalise_password_change_code(code)

    try:
        doc = await _db().auth_tokens.find_one(
            {
                "account_id": account_id,
                "purpose": pwless.TokenPurpose.PASSWORD_CHANGE_CODE.value,
                "expires_at": {"$gt": datetime.now(timezone.utc)},
            }
        )
    except Exception as exc:
        log.warning("password change code lookup failed", extra={"error": str(exc)})
        return False

    if doc is None:
        return False

    # Constant-time comparison of the hashes. `secrets.compare_digest` rather than `==` because a
    # short code is exactly the case where a timing side channel could narrow the search — the very
    # thing the attempt ceiling assumes cannot happen.
    import secrets as _secrets

    if _secrets.compare_digest(doc["token_hash"], pwless.token_hash(normalised)):
        try:
            await _db().auth_tokens.delete_one({"_id": doc["_id"]})
        except Exception as exc:  # noqa: BLE001
            # The password change itself is about to proceed. Failing to delete leaves a code that
            # expires on its own within minutes, which is better than refusing a correct code.
            log.warning("password change code delete failed", extra={"error": str(exc)})
        return True

    attempts = int(doc.get("attempts", 0)) + 1
    try:
        if attempts >= pwless.PASSWORD_CODE_MAX_ATTEMPTS:
            await _db().auth_tokens.delete_one({"_id": doc["_id"]})
            log.warning(
                "password change code burned after too many attempts",
                extra={"account_id": account_id, "attempts": attempts},
            )
        else:
            await _db().auth_tokens.update_one(
                {"_id": doc["_id"]}, {"$set": {"attempts": attempts}}
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("password change attempt count failed", extra={"error": str(exc)})

    return False


async def count_recent_token_requests(email: str, purpose: pwless.TokenPurpose) -> int:
    """Requests in the throttle window. 0 when unknown, so it fails OPEN.

    Fails open deliberately: if the counter is unreachable we cannot know the count, and
    refusing sign-in over a missing collection is worse than allowing an extra email.
    Same reasoning as `llm/budget.py`.
    """
    if not available():
        return 0
    since = datetime.now(timezone.utc) - timedelta(
        minutes=pwless.LINK_REQUEST_WINDOW_MINUTES
    )
    try:
        return int(
            await _db().auth_token_requests.count_documents(
                {"email": email.lower().strip(), "purpose": purpose.value,
                 "at": {"$gte": since}}
            )
        )
    except Exception:
        return 0


async def record_token_request(email: str, purpose: pwless.TokenPurpose) -> None:
    """Log a request for throttling. Records regardless of whether the account exists,
    so the throttle itself cannot be used to probe for valid addresses."""
    if not available():
        return
    try:
        now = datetime.now(timezone.utc)
        await _db().auth_token_requests.insert_one(
            {
                "email": email.lower().strip(),
                "purpose": purpose.value,
                "at": now,
                # Self-expiring, so there is no cleanup job.
                "expires_at": now + timedelta(hours=1),
            }
        )
    except Exception:
        pass


async def set_password(account_id: str, password: str) -> bool:
    """Replace an account's password hash.

    Also clears any outstanding reset tokens: completing a reset must invalidate every
    other link that could set the password again, or a second forwarded email stays live
    after the user thinks they have secured the account.
    """
    if not available():
        return False
    try:
        result = await _db().accounts.update_one(
            {"id": account_id},
            {"$set": {"password_hash": security.hash_password(password)}},
        )
        await _db().auth_tokens.delete_many(
            {"account_id": account_id,
             "purpose": pwless.TokenPurpose.PASSWORD_RESET.value}
        )
        return result.matched_count == 1
    except Exception as exc:
        log.warning("password set failed", extra={"error": str(exc)})
        return False


# --- TOTP ------------------------------------------------------------------ #


async def begin_totp_enrolment(account_id: str) -> tuple[str, str] | None:
    """`(secret, provisioning_uri)`. Staged, not yet active.

    Stored under `totp_pending_secret` rather than `totp_secret`, because a secret is
    only trustworthy once the user has proved their app is generating matching codes. If
    enrolment activated immediately, a mistyped QR scan would lock the account out —
    which for an aggregator means losing access to every customer they manage.
    """
    if not available():
        return None
    account = await get_account(account_id)
    if account is None:
        return None

    secret = pwless.new_totp_secret()
    try:
        await _db().accounts.update_one(
            {"id": account_id}, {"$set": {"totp_pending_secret": secret}}
        )
    except Exception as exc:
        log.warning("TOTP enrolment failed", extra={"error": str(exc)})
        return None

    return secret, pwless.totp_provisioning_uri(secret, account.email)


async def confirm_totp_enrolment(account_id: str, code: str) -> list[str] | None:
    """Activate TOTP once a code verifies. Returns recovery codes, shown once.

    Recovery codes are issued *here* rather than at `begin`, so they only exist for an
    account that has actually completed enrolment — and are returned only once, hashed
    at rest, for the same reason as an API key.
    """
    if not available():
        return None
    try:
        doc = await _db().accounts.find_one({"id": account_id})
        if doc is None:
            return None
        pending = doc.get("totp_pending_secret")
        if not pending or not pwless.verify_totp(pending, code):
            return None

        plain, hashed = pwless.new_recovery_codes()
        await _db().accounts.update_one(
            {"id": account_id},
            {
                "$set": {
                    "totp_secret": pending,
                    "totp_enabled": True,
                    "totp_recovery_hashes": hashed,
                    "totp_enrolled_at": datetime.now(timezone.utc),
                },
                "$unset": {"totp_pending_secret": ""},
            },
        )
        return plain
    except Exception as exc:
        log.warning("TOTP confirmation failed", extra={"error": str(exc)})
        return None


async def totp_state(account_id: str) -> dict:
    """Whether TOTP is active, and how many recovery codes remain unused."""
    if not available():
        return {"enabled": False, "recovery_codes_remaining": 0}
    try:
        doc = await _db().accounts.find_one(
            {"id": account_id},
            {"totp_enabled": 1, "totp_recovery_hashes": 1, "totp_enrolled_at": 1},
        )
    except Exception:
        return {"enabled": False, "recovery_codes_remaining": 0}
    if doc is None:
        return {"enabled": False, "recovery_codes_remaining": 0}
    return {
        "enabled": bool(doc.get("totp_enabled")),
        "recovery_codes_remaining": len(doc.get("totp_recovery_hashes") or []),
        "enrolled_at": (
            doc["totp_enrolled_at"].isoformat() if doc.get("totp_enrolled_at") else None
        ),
    }


async def verify_second_factor(account_id: str, code: str) -> bool:
    """Check a TOTP code or a recovery code.

    A recovery code is **consumed** on use — `$pull` removes it in the same update, so a
    written-down code cannot be replayed by anyone who later sees the paper.
    """
    if not available():
        return False
    try:
        doc = await _db().accounts.find_one({"id": account_id})
        if doc is None or not doc.get("totp_enabled"):
            return False

        if pwless.verify_totp(doc.get("totp_secret", ""), code):
            return True

        # Fall back to a recovery code.
        candidate = security.hash_token(pwless.normalise_recovery_code(code))
        if candidate in (doc.get("totp_recovery_hashes") or []):
            await _db().accounts.update_one(
                {"id": account_id}, {"$pull": {"totp_recovery_hashes": candidate}}
            )
            log.warning(
                "recovery code consumed; remaining codes reduced",
                extra={"account_id": account_id},
            )
            return True
        return False
    except Exception as exc:
        log.warning("second factor check failed", extra={"error": str(exc)})
        return False


async def disable_totp(account_id: str) -> bool:
    """Turn TOTP off and discard the secret and recovery codes.

    Discarded rather than retained: keeping a disabled secret means re-enabling would
    silently reuse material the user may have exported to a device they no longer
    control.
    """
    if not available():
        return False
    try:
        result = await _db().accounts.update_one(
            {"id": account_id},
            {
                "$set": {"totp_enabled": False},
                "$unset": {
                    "totp_secret": "",
                    "totp_pending_secret": "",
                    "totp_recovery_hashes": "",
                },
            },
        )
        return result.matched_count == 1
    except Exception as exc:
        log.warning("TOTP disable failed", extra={"error": str(exc)})
        return False


# --------------------------------------------------------------------------- #
# Roles and workspace membership
#
# An organisation's own people, as distinct from `memberships`, which is the SUBSCRIBER
# relationship — "this aggregator serves this farmer". Two different edges that would be
# confusing to share a collection: one is a commercial relationship with a customer, the
# other is a colleague's access level.
# --------------------------------------------------------------------------- #


async def member_edges(account_id: str) -> list[dict]:
    """Every membership edge this account holds, one per workspace. Never raises.

    The founder of an organisation has no edge document — ownership is derived, the same way
    `member_role` derived it before workspaces existed — so this synthesises an owner edge on
    each of their own workspaces. Writing them at signup instead would leave every
    organisation created before this feature with no access to its own settings, which is not
    recoverable without an operator.
    """
    try:
        cursor = _db().org_members.find({"account_id": account_id})
        edges = [doc async for doc in cursor]
    except Exception as exc:  # noqa: BLE001 — a read must never break a request
        log.warning("member edges lookup failed", extra={"error": str(exc)})
        return []

    if edges:
        return edges

    account = await get_account(account_id)
    if account is None or account.kind.value != "commercial":
        return []

    # The founder. Owner on every workspace their own account owns.
    workspaces = await list_workspaces(account_id)
    if not workspaces:
        default = await ensure_default_workspace(account_id)
        workspaces = [default] if default else []

    return [
        {
            "organisation_id": account_id,
            "account_id": account_id,
            "workspace_id": workspace["id"],
            "role": roles_mod.Role.OWNER.value,
            "permissions": [],
            "status": team.MemberStatus.ACTIVE.value,
            "derived": True,
        }
        for workspace in workspaces
    ]


async def organisation_for(account_id: str) -> str:
    """The organisation this account belongs to.

    An invited member's workspaces belong to the FOUNDER's account id, not to their own, so
    every workspace query has to resolve this first. Getting it wrong is silent: the member
    signs in successfully and sees an empty workspace list, which reads as data loss rather
    than as a scoping bug.

    Falls back to the account's own id, which is correct for a founder and harmless for
    anyone else — they simply see their own (empty) organisation rather than someone else's.
    """
    edges = await member_edges(account_id)
    for edge in edges:
        organisation = edge.get("organisation_id")
        if organisation:
            return organisation
    return account_id


async def member_role(account_id: str) -> str | None:
    """One role name for display, or None when it varies across workspaces.

    None is an honest answer, not a failure: a member who is Engineering on one project and
    View-Only on another has no single role, and picking one would misrepresent the other.
    """
    edges = await member_edges(account_id)
    return team.display_role(edges)


async def member_permissions(account_id: str) -> frozenset:
    """Union of permissions across every workspace. Empty on any failure — fail closed.

    The union drives the side-nav and org-wide routes. It is deliberately NOT what a
    workspace-scoped route checks: use `member_permissions_in`, or an owner of one project
    would inherit authority over every other one.
    """
    try:
        return team.effective_permissions(await member_edges(account_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("member permissions lookup failed", extra={"error": str(exc)})
        return frozenset()


async def member_permissions_in(account_id: str, workspace_id: str) -> frozenset:
    """Permissions on ONE workspace. The authority for every workspace-scoped route."""
    try:
        return team.permissions_in(await member_edges(account_id), workspace_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("workspace permissions lookup failed", extra={"error": str(exc)})
        return frozenset()


# --------------------------------------------------------------------------- #
# Workspaces
#
# An organisation's container for activated intelligence tracks. API keys are scoped to one,
# so a key can only reach the tracks its workspace has activated.
#
# Mongo rather than Postgres, alongside the rest of IAM: a workspace is identity and
# entitlement, not measured data. The blast-radius separation CLAUDE.md describes — Atlas
# holding accounts and keys, Postgres holding assessments — applies here too.
# --------------------------------------------------------------------------- #


async def list_workspaces(account_id: str) -> list[dict]:
    """Every workspace belonging to this organisation. Never raises.

    Takes the ORGANISATION's account id. Callers holding a member's own id must resolve it
    through `organisation_for` first — an invited member's workspaces are owned by the
    founder's account, so filtering on the member's own id returns nothing, which reads as
    data loss rather than as a scoping bug.
    """
    try:
        cursor = _db().workspaces.find({"account_id": account_id}).sort("created_at", 1)
        return [_workspace_public(doc) async for doc in cursor]
    except Exception as exc:  # noqa: BLE001 — a read must not break the portal
        log.warning("workspace list failed", extra={"error": str(exc)})
        return []


def _workspace_public(doc: dict) -> dict:
    """Strip the Mongo `_id`, which is an implementation detail and not a stable id.

    The public identifier is `id` — a minted 10-character code, quotable on a support call.
    Returning `_id` would leak an ObjectId that encodes a creation timestamp and a machine
    identifier.
    """
    return {
        "id": doc.get("id"),
        "name": doc.get("name"),
        "tracks": doc.get("tracks", []),
        "created_at": (
            doc["created_at"].isoformat() if doc.get("created_at") else None
        ),
        "is_default": bool(doc.get("is_default")),
    }


async def create_workspace(
    account_id: str, name: str, tracks: list[str], *, is_default: bool = False
) -> dict | None:
    """Create a workspace. None on failure.

    The id is minted through the same uniqueness check as an account id, because it is
    public-facing: a partner stores it, and it appears on a key.
    """
    from app.iam import identifiers

    try:
        workspace_id = await identifiers.mint_unique(
            lambda candidate: _db().workspaces.find_one({"id": candidate})
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("workspace id mint failed", extra={"error": str(exc)})
        return None

    doc = {
        "id": workspace_id,
        "account_id": account_id,
        "name": name,
        "tracks": tracks,
        "is_default": is_default,
        "created_at": datetime.now(timezone.utc),
    }

    try:
        await _db().workspaces.insert_one(doc)
    except Exception as exc:  # noqa: BLE001
        log.warning("workspace insert failed", extra={"error": str(exc)})
        return None

    return _workspace_public(doc)


async def ensure_default_workspace(account_id: str) -> dict | None:
    """The organisation's default workspace, created on first read if absent.

    Lazy rather than at signup, for the same reason `member_role` derives ownership lazily: an
    organisation created before this feature existed must still work, and a migration that
    back-filled every account would be a one-off script that a fresh deployment then does not
    need.

    Idempotent under concurrency by re-reading after the insert: two tabs opening the workspace
    page simultaneously must not produce two defaults.
    """
    existing = await list_workspaces(account_id)
    if existing:
        return next((w for w in existing if w["is_default"]), existing[0])

    from app.iam.tracks import DEFAULT_TRACKS

    created = await create_workspace(
        account_id,
        "Default workspace",
        [t.value for t in DEFAULT_TRACKS],
        is_default=True,
    )
    if created is None:
        # The insert may have lost a race with another request. Re-read rather than reporting
        # failure — the workspace probably exists now.
        again = await list_workspaces(account_id)
        return again[0] if again else None
    return created


async def update_workspace(
    account_id: str, workspace_id: str, *, name: str | None = None,
    tracks: list[str] | None = None,
) -> dict | None:
    """Rename a workspace or change its activated tracks.

    Scoped by `account_id` in the FILTER, not checked afterwards: another organisation's
    workspace is never a candidate document, so a guessed id cannot reach it. The same
    discipline the `memberships` queries use.
    """
    updates: dict = {}
    if name is not None:
        updates["name"] = name
    if tracks is not None:
        updates["tracks"] = tracks
    if not updates:
        return None

    try:
        doc = await _db().workspaces.find_one_and_update(
            {"id": workspace_id, "account_id": account_id},
            {"$set": updates},
            return_document=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("workspace update failed", extra={"error": str(exc)})
        return None

    return _workspace_public(doc) if doc else None


async def delete_workspace(account_id: str, workspace_id: str) -> bool:
    """Delete a workspace. Refuses the default one.

    The default cannot be deleted because API keys are scoped to a workspace: removing the last
    one would leave existing keys pointing at nothing, and a key that resolves to no workspace
    is a key with undefined entitlement. Deleting a non-default workspace with live keys is
    equally a problem — the caller checks that before calling this.
    """
    try:
        doc = await _db().workspaces.find_one_and_delete(
            {"id": workspace_id, "account_id": account_id, "is_default": {"$ne": True}}
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("workspace delete failed", extra={"error": str(exc)})
        return False
    return doc is not None


# --------------------------------------------------------------------------- #
# Area attribution — which audience is billed for a monitored area
#
# Keyed by `aoi_id`, in Mongo, so Postgres stays tenant-blind. See `app/iam/attribution.py`
# for the business model this encodes and why ownership is recorded rather than derived.
# --------------------------------------------------------------------------- #


async def record_attribution(
    *,
    aoi_id: str,
    owner_kind: attribution.OwnerKind,
    owner_id: str,
    subscriber_id: str,
    subject_account_id: str | None = None,
    external_ref: str | None = None,
    workspace_id: str | None = None,
) -> bool:
    """Map an area to the party billed for it. Idempotent on `aoi_id`.

    Called from every path that creates an area — direct activation, aggregator onboarding, and
    the area lifecycle routes. Missing it on one path would leave those areas unattributed and
    therefore unbillable, which is a silent revenue hole rather than an error.

    Upsert rather than insert: re-activating or re-creating an area with the same id should
    correct the attribution, not fail. `recorded_at` is preserved on an update so a re-write
    does not reset the billing clock.
    """
    if not available():
        return False

    document = attribution.build_attribution(
        aoi_id=aoi_id,
        owner_kind=owner_kind,
        owner_id=owner_id,
        subscriber_id=subscriber_id,
        subject_account_id=subject_account_id,
        external_ref=external_ref,
        workspace_id=workspace_id,
    )
    first_seen = document.pop("recorded_at")

    try:
        await _db().area_attribution.update_one(
            {"aoi_id": aoi_id},
            {
                "$set": document,
                # Only on insert, so a correction does not restart the billed period.
                "$setOnInsert": {"recorded_at": first_seen},
            },
            upsert=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — attribution must not fail an activation
        log.warning(
            "area attribution write failed",
            extra={"aoi_id": aoi_id, "error": str(exc)},
        )
        return False


async def end_attribution(aoi_id: str) -> bool:
    """Mark an area as no longer monitored. The row is KEPT.

    Deleting it would make a past invoice unexplainable — the charge would reference an area
    with no record of who owned it or when. Stamping `ended_at` closes the billable period
    while preserving the answer.
    """
    if not available():
        return False
    try:
        result = await _db().area_attribution.update_one(
            {"aoi_id": aoi_id, "ended_at": None},
            {"$set": {"ended_at": datetime.now(timezone.utc)}},
        )
        return result.modified_count == 1
    except Exception as exc:  # noqa: BLE001
        log.warning("attribution close failed", extra={"aoi_id": aoi_id, "error": str(exc)})
        return False


async def attribution_for(aoi_id: str) -> dict | None:
    """Who is billed for this area, or None when it was never attributed."""
    if not available():
        return None
    try:
        doc = await _db().area_attribution.find_one({"aoi_id": aoi_id}, {"_id": 0})
    except Exception as exc:  # noqa: BLE001
        log.warning("attribution lookup failed", extra={"error": str(exc)})
        return None
    return doc


async def owned_aoi_ids(
    owner_id: str, *, include_ended: bool = False, workspace_id: str | None = None
) -> list[str]:
    """Every area billed to this owner.

    **This is the billing join.** Postgres aggregates assessment counts by `aoi_id` and knows
    nothing about tenants; this resolves which ids belong to whom. Two queries, no tenant column
    in Postgres, and the risk layer stays unable to care who is paying.

    `include_ended` prices a historical period: an area removed last week was still monitored
    for part of the month and must appear on that invoice.

    `workspace_id` narrows to one project. Note the filter is applied **in addition to**
    `owner_id`, never instead of it: a workspace id is not a capability, and scoping on it alone
    would make a guessed id readable by anyone.
    """
    if not available():
        return []

    query: dict = {"owner_id": owner_id}
    if not include_ended:
        query["ended_at"] = None
    if workspace_id is not None:
        query["workspace_id"] = workspace_id

    try:
        cursor = _db().area_attribution.find(query, {"aoi_id": 1, "_id": 0})
        return [doc["aoi_id"] async for doc in cursor]
    except Exception as exc:  # noqa: BLE001
        log.warning("owned aoi lookup failed", extra={"error": str(exc)})
        return []


async def _backfill_workspace(record: dict) -> bool:
    """Fill in a missing `workspace_id` on an already-attributed aggregator row.

    True when a project was written, False when nothing needed doing — which is the common case
    and not a failure. Returning a bool rather than raising keeps `reconcile_attribution` able to
    treat "no repair needed" and "repaired" as two ordinary outcomes it counts separately.

    Deliberately narrow: it writes ONE field on a row that already exists, and only when that
    field is absent. It cannot change who is billed, cannot move `recorded_at`, and cannot touch
    an individual's row — see the caller's docstring for why each of those matters.
    """
    if record.get("workspace_id"):
        return False
    if record.get("owner_kind") != attribution.OwnerKind.AGGREGATOR.value:
        # None is the correct value for an individual, not a gap to be filled.
        return False

    subject = record.get("subject_account_id")
    if not subject:
        return False

    try:
        edge = await _db().memberships.find_one(
            {
                "account_id": subject,
                "aggregator_id": record.get("owner_id"),
                "status": tenancy.MembershipStatus.ACTIVE.value,
            },
            {"workspace_id": 1},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("workspace backfill lookup failed", extra={"error": str(exc)})
        return False

    workspace_id = (edge or {}).get("workspace_id")
    if not workspace_id:
        # The customer sits in no project. Left alone rather than assigned to a default: an
        # invoice line under a project nobody put them in is worse than one marked unassigned.
        return False

    try:
        await _db().area_attribution.update_one(
            {"aoi_id": record["aoi_id"]},
            # `workspace_derived` rather than the row-wide `derived` flag: the OWNER on this row
            # was stated at creation and is still trustworthy. Marking the whole record derived
            # would discard that, and make a correctly-attributed area look reconstructed.
            {"$set": {"workspace_id": workspace_id, "workspace_derived": True}},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "workspace backfill write failed",
            extra={"aoi_id": record.get("aoi_id"), "error": str(exc)},
        )
        return False

    return True


async def reconcile_attribution() -> dict:
    """Attribute any area that has none. Returns a count per outcome.

    Two situations need this, and neither is an error:

      * areas created before attribution existed — including every area on a deployment
        upgrading to this feature;
      * an area whose attribution write failed transiently, since those are best-effort and must
        never block monitoring.

    Ownership is derived HERE — and only here — from the `memberships` edge, because that is the
    only evidence available retrospectively. That is exactly the derivation this module exists to
    avoid at creation time, and the difference matters: at creation the truth is known and
    recorded, whereas a repair can only reconstruct. `derived: true` marks the difference so a
    disputed invoice can be told apart from a stated one.

    Idempotent: an already-attributed area is skipped, so this is safe to run on a schedule.

    ## Backfilling the project on an existing row

    An area attributed before `workspace_id` was recorded is "already attributed", so the skip
    above would leave it without a project **forever** — and a per-project invoice would silently
    under-report, which is the failure mode this whole module exists to prevent.

    So an aggregator-owned row with no `workspace_id` is repaired in place rather than skipped,
    counted as `backfilled` to keep it distinct from a fresh attribution. Only the missing field
    is touched: `recorded_at` and `owner_id` are never rewritten, because a repair that moved the
    billing clock or the billed party would be a data-loss bug wearing a maintenance function's
    clothes.

    An INDIVIDUAL row is never backfilled — None is the correct value there, not a missing one.
    """
    if not available():
        return {
            "checked": 0,
            "attributed": 0,
            "backfilled": 0,
            "skipped": 0,
            "unresolved": 0,
        }

    from app.store import repository

    counts = {
        "checked": 0,
        "attributed": 0,
        "backfilled": 0,
        "skipped": 0,
        "unresolved": 0,
    }

    try:
        pairs = await repository.all_area_ids()
    except Exception as exc:  # noqa: BLE001
        log.warning("attribution reconcile could not list areas", extra={"error": str(exc)})
        return counts

    for aoi_id, subscriber_id in pairs:
        counts["checked"] += 1

        existing = await attribution_for(aoi_id)
        if existing is not None:
            if await _backfill_workspace(existing):
                counts["backfilled"] += 1
            else:
                counts["skipped"] += 1
            continue

        # Which account holds this subscriber?
        try:
            holder = await _db().accounts.find_one(
                {"subscriber_id": subscriber_id}, {"id": 1, "kind": 1}
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("reconcile account lookup failed", extra={"error": str(exc)})
            counts["unresolved"] += 1
            continue

        if holder is None:
            # A subscriber with no account: created through the platform endpoint directly, so
            # there is nobody to bill. Left unattributed rather than guessed at.
            counts["unresolved"] += 1
            continue

        # An ACTIVE membership means an aggregator onboarded this farmer, so the aggregator is
        # the billable party. No membership means a direct B2C subscriber.
        try:
            edge = await _db().memberships.find_one(
                {"account_id": holder["id"], "status": tenancy.MembershipStatus.ACTIVE.value},
                {"aggregator_id": 1, "external_ref": 1, "workspace_id": 1},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("reconcile membership lookup failed", extra={"error": str(exc)})
            counts["unresolved"] += 1
            continue

        if edge is not None:
            written = await record_attribution(
                aoi_id=aoi_id,
                owner_kind=attribution.OwnerKind.AGGREGATOR,
                owner_id=edge["aggregator_id"],
                subscriber_id=subscriber_id,
                subject_account_id=holder["id"],
                external_ref=edge.get("external_ref"),
                # The customer's CURRENT project, which is the best available answer
                # retrospectively and may not be the one they were in when the area was created.
                # `derived: true` below is what marks the difference — see this function's
                # docstring — so a per-project invoice line can be told apart from a stated one.
                workspace_id=edge.get("workspace_id"),
            )
        else:
            written = await record_attribution(
                aoi_id=aoi_id,
                owner_kind=attribution.OwnerKind.INDIVIDUAL,
                owner_id=holder["id"],
                subscriber_id=subscriber_id,
            )

        if written:
            try:
                await _db().area_attribution.update_one(
                    {"aoi_id": aoi_id}, {"$set": {"derived": True}}
                )
            except Exception:  # noqa: BLE001
                pass
            counts["attributed"] += 1
        else:
            counts["unresolved"] += 1

    log.info("attribution reconciled", extra=counts)
    return counts


#: What an owner with no attributed areas looks like. Named so every failure path returns the
#: same shape — a caller reading `["areas"]` must never have to guard first.
_EMPTY_SUMMARY: dict = {"areas": 0, "subjects": 0, "owner_kind": None, "workspaces": []}


async def attribution_summary(owner_id: str) -> dict:
    """Billable shape for one owner: area count, distinct farmers, audience, and per-project split.

    Aggregated in Mongo rather than assembled in Python because an aggregator at scale has
    thousands of areas, and pulling them all back to count them is the kind of thing that works
    in testing and times out on the Anchor Scheme.

    `workspaces` breaks the same totals down per project, which is the granularity a partner
    reconciles at — "what did the Kano rollout cost" is the question actually being asked, and an
    aggregator-level total cannot answer it. Empty for an individual, who has no projects.

    Two `$group` stages over one `$match` rather than two round trips: the totals and the
    breakdown must describe the same instant, and two queries could straddle an area being added.
    """
    if not available():
        return dict(_EMPTY_SUMMARY)

    try:
        pipeline_stages = [
            {"$match": {"owner_id": owner_id, "ended_at": None}},
            {
                "$facet": {
                    "totals": [
                        {
                            "$group": {
                                "_id": "$owner_kind",
                                "areas": {"$sum": 1},
                                "subjects": {"$addToSet": "$subject_account_id"},
                            }
                        }
                    ],
                    "by_workspace": [
                        # An unattributed-to-project area groups under null rather than being
                        # dropped: those are pre-workspace or repaired rows, and silently omitting
                        # them would make the breakdown sum to less than the total — which reads
                        # as missing revenue rather than as an unassigned area.
                        {
                            "$group": {
                                "_id": "$workspace_id",
                                "areas": {"$sum": 1},
                                "subjects": {"$addToSet": "$subject_account_id"},
                            }
                        },
                        {"$sort": {"areas": -1}},
                    ],
                }
            },
        ]
        cursor = _db().area_attribution.aggregate(pipeline_stages)
        facets = [row async for row in cursor]
    except Exception as exc:  # noqa: BLE001
        log.warning("attribution summary failed", extra={"error": str(exc)})
        return dict(_EMPTY_SUMMARY)

    if not facets:
        return dict(_EMPTY_SUMMARY)

    totals = facets[0].get("totals") or []
    if not totals:
        return dict(_EMPTY_SUMMARY)

    row = totals[0]
    return {
        "areas": row["areas"],
        "subjects": len(row.get("subjects", [])),
        "owner_kind": row["_id"],
        "workspaces": [
            {
                "workspace_id": group["_id"],
                "areas": group["areas"],
                "subjects": len(group.get("subjects", [])),
            }
            for group in facets[0].get("by_workspace") or []
        ],
    }


# --------------------------------------------------------------------------- #
# Team membership and invitations
#
# One document per (account, workspace) in `org_members`; invitations live separately in
# `org_invitations` until accepted, so an unaccepted invitation grants nothing.
# --------------------------------------------------------------------------- #


async def list_team(organisation_id: str) -> list[dict]:
    """Every member of this organisation, with the workspaces and roles they hold.

    Grouped by account so the portal renders one row per person rather than one per edge — a
    member on four workspaces is one colleague, not four.
    """
    try:
        cursor = _db().org_members.find({"organisation_id": organisation_id})
        edges = [doc async for doc in cursor]
    except Exception as exc:  # noqa: BLE001
        log.warning("team list failed", extra={"error": str(exc)})
        return []

    known_roles = {r.value for r in roles_mod.Role}
    grouped: dict[str, dict] = {}
    for edge in edges:
        account_id = edge.get("account_id", "")
        entry = grouped.setdefault(
            account_id,
            {"account_id": account_id, "email": None, "full_name": None, "grants": []},
        )
        role = edge.get("role", "")
        entry["grants"].append(
            {
                "workspace_id": edge.get("workspace_id"),
                "role": role,
                "role_label": (
                    roles_mod.ROLE_LABELS[roles_mod.Role(role)][0]
                    if role in known_roles
                    else role
                ),
                "permissions": sorted(
                    p.value
                    for p in roles_mod.permissions_for(role, edge.get("permissions"))
                ),
                "status": edge.get("status"),
            }
        )

    # Names come from the accounts collection rather than being denormalised onto the edge: a
    # colleague who corrects the spelling of their own name must not have to be re-invited for
    # the team page to show it.
    for account_id, entry in grouped.items():
        account = await get_account(account_id)
        if account is not None:
            entry["email"] = account.email
            entry["full_name"] = f"{account.first_name} {account.last_name}".strip()

    return list(grouped.values())


async def set_member_grants(
    organisation_id: str,
    account_id: str,
    grants: list[dict],
    *,
    invited_by: str | None = None,
) -> int:
    """Replace this member's workspace roles with exactly `grants`. Returns edges written.

    Replace rather than merge, and scoped to `organisation_id` in every filter: an
    administrator editing a colleague's access is stating the full intended set, so a role
    removed from the form must actually be removed. A merge would make revoking access through
    this path impossible — the most dangerous kind of no-op.
    """
    wanted = {
        grant["workspace_id"]: grant for grant in grants if grant.get("workspace_id")
    }

    try:
        for workspace_id, grant in wanted.items():
            document = team.build_member_document(
                organisation_id=organisation_id,
                account_id=account_id,
                workspace_id=workspace_id,
                role=grant.get("role", roles_mod.Role.VIEW_ONLY.value),
                permissions=grant.get("permissions"),
                invited_by=invited_by,
            )
            await _db().org_members.update_one(
                {
                    "organisation_id": organisation_id,
                    "account_id": account_id,
                    "workspace_id": workspace_id,
                },
                {"$set": document},
                upsert=True,
            )

        # Anything not named is revoked rather than deleted, so the audit log's target still
        # resolves to a document months later.
        await _db().org_members.update_many(
            {
                "organisation_id": organisation_id,
                "account_id": account_id,
                "workspace_id": {"$nin": list(wanted)},
            },
            {"$set": {"status": team.MemberStatus.REVOKED.value}},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("set member grants failed", extra={"error": str(exc)})
        return 0

    return len(wanted)


async def revoke_member(organisation_id: str, account_id: str) -> bool:
    """Remove a colleague's access to every workspace in this organisation."""
    try:
        result = await _db().org_members.update_many(
            {"organisation_id": organisation_id, "account_id": account_id},
            {"$set": {"status": team.MemberStatus.REVOKED.value}},
        )
        return result.matched_count > 0
    except Exception as exc:  # noqa: BLE001
        log.warning("revoke member failed", extra={"error": str(exc)})
        return False


async def redeem_team_invitation(token: str) -> tuple[Account, dict] | None:
    """Redeem an invitation link. `(account, invitation)` or None.

    ## What this does that the first accept-only flow did not

    The earlier flow required the invited person to already hold a verified account, so the
    real journey was: read invite → discover you need an account → sign up → verify that
    email → return to the link → accept. Two emails and a dead end if they clicked the invite
    first.

    This creates the account itself, **with no password hash at all** (`password=None`, the
    path `create_account` already supports for aggregator-created subscribers). The caller
    then issues a session scoped to setting one.

    ## Why no temporary password

    A generated one-time password would be valid at `POST /iam/login` — public, reachable by
    anyone, and so an online guessing target for the whole 14 days — and would have to be
    short enough to type, perhaps 50-60 bits against this token's 256. It would also survive
    being forwarded in a reply-all. The token is usable only by someone holding the URL and is
    destroyed by `find_one_and_delete` on first redemption.

    ## Email is verified by the redemption itself

    Clicking a link sent to an address proves control of that mailbox — the same reasoning
    `redeem_single_use_token` applies to a magic link. So the account is created ACTIVE and
    verified, and no separate confirmation email is sent. An invited colleague who then had to
    verify their address would be proving the same fact twice.

    Atomic on the token: two concurrent clicks cannot both create an account, because the
    delete and the validation are one operation.
    """
    if not available():
        return None

    try:
        document = await _db().org_invitations.find_one_and_delete(
            {
                "token_hash": security.hash_token(token),
                "status": team.MemberStatus.INVITED.value,
                "expires_at": {"$gt": datetime.now(timezone.utc)},
            }
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("invitation redemption failed", extra={"error": str(exc)})
        return None

    if document is None:
        return None

    email = document["email"]
    account = await get_account_by_email(email)

    if account is None:
        # First time: create the identity. No password, and ACTIVE rather than
        # PENDING_VERIFICATION because redeeming this token already proved the mailbox.
        account, _ = await create_account(
            kind=AccountKind.COMMERCIAL,
            email=email,
            first_name=document.get("first_name") or "",
            last_name=document.get("last_name") or "",
            password=None,
            organisation=document.get("organisation_name"),
        )
        if account is None:
            log.error("invitation redeemed but account creation failed",
                      extra={"email": keys.redact(email)})
            return None
        try:
            await _db().accounts.update_one(
                {"id": account.id},
                {"$set": {"email_verified": True,
                          "status": AccountStatus.ACTIVE.value,
                          # Invited members sign in with a password only — no magic link.
                          # Recorded on the account so `issue_single_use_token` can refuse
                          # rather than the rule living only in a route.
                          "invited_member": True}},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not activate invited account", extra={"error": str(exc)})
        account = await get_account(account.id) or account

    written = await set_member_grants(
        document["organisation_id"],
        account.id,
        document.get("grants", []),
        invited_by=document.get("invited_by"),
    )
    if not written:
        return None

    return account, document


async def create_invitation(
    organisation_id: str,
    email: str,
    grants: list[dict],
    invited_by: str,
    *,
    first_name: str = "",
    last_name: str = "",
    organisation_name: str = "",
) -> tuple[dict, str] | None:
    """Store an invitation and return it with its single-use plaintext token.

    Any earlier pending invitation to the same address in the same organisation is superseded,
    so re-inviting someone after correcting their role does not leave a stale invitation
    carrying the old grant — whichever arrived first would otherwise still be redeemable.
    """
    document, plaintext = team.build_invitation(
        organisation_id=organisation_id,
        email=email,
        grants=grants,
        invited_by=invited_by,
        first_name=first_name,
        last_name=last_name,
        organisation_name=organisation_name,
    )
    try:
        await _db().org_invitations.update_many(
            {
                "organisation_id": organisation_id,
                "email": document["email"],
                "status": team.MemberStatus.INVITED.value,
            },
            {"$set": {"status": team.MemberStatus.REVOKED.value}},
        )
        await _db().org_invitations.insert_one(dict(document))
    except Exception as exc:  # noqa: BLE001
        log.warning("invitation insert failed", extra={"error": str(exc)})
        return None
    return document, plaintext


async def find_invitation(organisation_id: str, email: str) -> dict | None:
    """The most recent outstanding invitation for this address, expired or not.

    **Expired ones are included deliberately.** An expired invitation is exactly what a resend
    is for, and filtering on `expires_at` here would make the common case — "their link lapsed
    over a holiday" — indistinguishable from "you never invited them", which sends an
    administrator hunting for a mistake they did not make.

    Scoped by `organisation_id` in the filter, so one aggregator cannot probe another's
    invitation list by address.
    """
    if not available():
        return None
    try:
        return await _db().org_invitations.find_one(
            {
                "organisation_id": organisation_id,
                "email": email.strip().lower(),
                "status": team.MemberStatus.INVITED.value,
            },
            sort=[("created_at", -1)],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("invitation lookup failed", extra={"error": str(exc)})
        return None


async def resend_invitation(
    organisation_id: str, email: str, resent_by: str
) -> tuple[dict, str] | None:
    """Reissue an outstanding invitation with a fresh token and a new 14-day window.

    ## A new token, not a new expiry on the old one

    Extending `expires_at` would leave the *original* link live — and that link has been
    sitting in a mailbox, possibly forwarded, for two weeks. Reissuing supersedes it: the old
    token hash is gone, so only the newly emailed link works. That is the same reasoning
    `create_invitation` applies when re-inviting the same address.

    ## The grants are carried over, not re-chosen

    A resend is "send that again", so the workspaces and roles are whatever the original
    invitation named. Silently re-deriving them from the resender's current permissions could
    quietly widen or narrow what the colleague was offered — an Operations member resending an
    Owner's invitation must not change its contents. To change the role, revoke and invite
    afresh.

    Returns None when there is nothing outstanding to resend, which the route reports as 404.
    """
    existing = await find_invitation(organisation_id, email)
    if existing is None:
        return None

    return await create_invitation(
        organisation_id,
        existing["email"],
        existing.get("grants", []),
        # Attributed to whoever resent it, so the audit trail names the person who acted
        # rather than the original inviter who may have left the organisation.
        resent_by,
        first_name=existing.get("first_name", ""),
        last_name=existing.get("last_name", ""),
        organisation_name=existing.get("organisation_name", ""),
    )


async def list_invitations(organisation_id: str) -> list[dict]:
    """Pending invitations. The token hash is never returned."""
    try:
        cursor = _db().org_invitations.find(
            {
                "organisation_id": organisation_id,
                "status": team.MemberStatus.INVITED.value,
            }
        ).sort("created_at", -1)
        now = datetime.now(timezone.utc)
        out = []
        async for doc in cursor:
            expires = doc.get("expires_at")
            if expires is not None and expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            out.append(
                {
                    "email": doc.get("email"),
                    "grants": doc.get("grants", []),
                    "created_at": (
                        doc["created_at"].isoformat() if doc.get("created_at") else None
                    ),
                    "expires_at": expires.isoformat() if expires else None,
                    # Computed here rather than in the frontend so both surfaces agree on
                    # when a link is dead. A UI comparing dates in the browser would use the
                    # visitor's clock, and a skewed laptop would offer "Resend" on a live
                    # invitation or hide it on a lapsed one.
                    "expired": bool(expires and expires < now),
                }
            )
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("invitation list failed", extra={"error": str(exc)})
        return []




async def account_for_subscriber(subscriber_id: str) -> Account | None:
    """The account that owns this subscription, or None.

    ## Why this is needed outside the IAM routes

    `app/api/routes/subscribers.py` handles the individual add-area path and had no way to reach an
    account, so it could not send a confirmation — an area was created, queued and logged with
    nobody told. The aggregator path had the same gap from the other direction.

    Looked up by `subscriber_id` because that is the only link the subscribers router holds. The
    field is unique in practice (one account binds one subscription) and indexed for the
    `/alerts` audience lookup, so this is a point read rather than a scan.

    Returns None rather than raising: a missing account is possible for a subscriber created
    directly through the platform API with no portal account behind it, and that is a legitimate
    state — it means "nobody to email", not "something is broken".
    """
    if not available() or not subscriber_id:
        return None
    try:
        doc = await _db().accounts.find_one(
            {"subscriber_id": subscriber_id}, _PUBLIC_PROJECTION
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "account lookup by subscriber failed",
            extra={"subscriber_id": subscriber_id, "error": str(exc)},
        )
        return None
    return _to_account(doc) if doc else None
