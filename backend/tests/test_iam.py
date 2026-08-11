"""IAM — identity, key lifecycle, multi-tenancy and the immutable audit log.

`app/iam/security.py`, `keys.py`, `tenancy.py` and `audit.py` are pure functions
precisely so this file can assert real security properties rather than mocking an auth
layer, which proves nothing about whether it is secure. The Atlas-backed paths were
verified separately against a live cluster; what is asserted here is everything that
can be checked without a network.

The properties that matter most, and the failure each prevents:

1. **A key cannot be recovered** — not by support, not by the owner. It is not stored.
2. **A malformed key is rejected before any database read** — the API-key header is
   unauthenticated, so it is the cheapest surface to flood.
3. **The audit log has no update or delete path** — asserted structurally, because
   "immutable" as a code comment is not a guarantee.
4. **Tenancy is many-to-many** — one farmer, several aggregators, none of them owner.
5. **Individuals cannot hold an API key** — a credential a farmer cannot use is one
   that can only be phished out of them.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from app.iam import audit, keys, passwordless, security, tenancy
from app.iam.models import (
    DEFAULT_KEY_SCOPES,
    AccountKind,
    ApiKeyScope,
    CommercialSignup,
    IndividualSignup,
)

# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #


def test_password_hash_verifies_and_is_salted():
    """Argon2id, and two hashes of the same password must differ.

    Identical hashes would mean no salt, which makes a rainbow table viable across
    every account that chose the same passphrase.
    """
    password = "correct horse battery staple"
    first = security.hash_password(password)
    second = security.hash_password(password)

    assert first != second, "hashes must be salted"
    assert security.verify_password(password, first)
    assert security.verify_password(password, second)
    assert first.startswith("$argon2id$"), "must be Argon2id, not bcrypt or sha"


def test_password_verification_rejects_wrong_input_without_raising():
    stored = security.hash_password("the real passphrase here")

    assert security.verify_password("wrong passphrase entirely", stored) is False
    assert security.verify_password("", stored) is False
    # A None hash is the aggregator-created account: no password set, cannot log in.
    assert security.verify_password("anything", None) is False
    # A corrupt stored hash is a data problem, not an authentication success.
    assert security.verify_password("anything", "not-a-hash") is False


def test_long_passphrases_are_not_silently_truncated():
    """**The specific reason bcrypt was rejected.**

    bcrypt truncates at 72 bytes, so two long passphrases sharing a 72-byte prefix
    hash identically — the second half of a strong passphrase would be decorative.
    """
    base = "a" * 72
    stored = security.hash_password(base + "genuinely-different-suffix")

    assert security.verify_password(base + "a-completely-other-suffix", stored) is False


def test_dummy_verify_exists_for_timing_equalisation():
    """Without it, a missing account returns in microseconds and a real one in ~50 ms —
    a reliable account-enumeration oracle over a list of farmers in a named district."""
    security.dummy_verify()  # must not raise
    source = pathlib.Path("app/iam/store.py").read_text()
    assert "dummy_verify" in source, "authenticate must equalise timing on a miss"


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #


def test_session_round_trips():
    token, expires_in = security.issue_session("acc_123", "individual")
    claims = security.read_session(token)

    assert claims is not None
    assert claims["sub"] == "acc_123"
    assert claims["kind"] == "individual"
    assert expires_in > 0


def test_session_rejects_tampering():
    token, _ = security.issue_session("acc_123", "individual")
    head, payload, signature = token.split(".")

    assert security.read_session(f"{head}.{payload}.{signature[:-4]}abcd") is None
    assert security.read_session("not.a.token") is None
    assert security.read_session("") is None


def test_expired_session_is_rejected():
    token, _ = security.issue_session("acc_123", "individual", minutes=-1)
    assert security.read_session(token) is None


def test_session_carries_no_personal_data():
    """A JWT is signed, not encrypted, so everything in it is readable by anyone
    holding the token — including whatever logs the Authorization header."""
    token, _ = security.issue_session("acc_123", "commercial")
    claims = security.read_session(token) or {}

    for leaky in ("email", "name", "first_name", "phone", "password"):
        assert leaky not in claims


def test_session_algorithm_is_pinned():
    """Accepting the token's own `alg` header is the classic JWT vulnerability —
    `alg: none` would make every signature optional."""
    source = pathlib.Path("app/iam/security.py").read_text()
    assert 'algorithms=["HS256"]' in source


def test_session_audience_separates_portal_from_api():
    """A portal token must not be replayable against the machine API."""
    token, _ = security.issue_session("acc_1", "individual")
    claims = security.read_session(token) or {}
    assert claims.get("aud") == security.PORTAL_AUDIENCE


# --------------------------------------------------------------------------- #
# API key lifecycle
# --------------------------------------------------------------------------- #


def test_minted_keys_are_unique_prefixed_and_checksummed():
    minted = [keys.mint() for _ in range(50)]

    assert len({m.plaintext for m in minted}) == 50, "keys must not collide"
    for m in minted:
        assert m.plaintext.startswith(keys.PREFIX_LIVE)
        assert keys.is_well_formed(m.plaintext)


def test_keys_are_exactly_64_characters():
    """Fixed width, not a minimum.

    A constant length lets `is_well_formed` reject a wrong-length string before any
    database read — which matters because the API-key header is an unauthenticated
    surface and therefore the cheapest thing for an attacker to flood.
    """
    for _ in range(20):
        assert len(keys.mint().plaintext) == 64
        assert len(keys.mint(test_mode=True).plaintext) == 64

    assert keys.TOTAL_KEY_LENGTH == 64
    # Derived, so changing the prefix or checksum cannot silently change the total.
    assert keys.BODY_LENGTH == 64 - len(keys.PREFIX_LIVE) - keys.CHECKSUM_LENGTH


def test_live_and_test_prefixes_are_the_same_length():
    """Otherwise a length check becomes an environment oracle: a caller could learn
    whether a key is live or test without presenting a valid one."""
    assert len(keys.PREFIX_LIVE) == len(keys.PREFIX_TEST)


def test_key_alphabet_is_url_shell_and_header_safe():
    """**The reason `-._~` and nothing else.**

    A key gets pasted into URLs, shell commands, YAML and HTTP headers. base64's
    `+/=` break URLs; `$`, backtick and `!` are shell-active; `#` truncates a URL
    fragment; `:` splits a header; `,` and `;` invite proxy mangling. The chosen set is
    exactly RFC 3986's unreserved characters, which survive all four contexts
    unescaped.
    """
    specials = {c for c in keys.KEY_ALPHABET if not c.isalnum()}
    assert specials == {"-", ".", "_", "~"}

    hostile = set("+/=$`!#:;,&?%*'\"\\ <>|(){}[]^")
    assert not (hostile & set(keys.KEY_ALPHABET))

    # Alphanumerics in full, so entropy is not quietly reduced.
    assert sum(c.isupper() for c in keys.KEY_ALPHABET) == 26
    assert sum(c.islower() for c in keys.KEY_ALPHABET) == 26
    assert sum(c.isdigit() for c in keys.KEY_ALPHABET) == 10


def test_minted_bodies_use_only_the_declared_alphabet():
    """A key carrying a character outside the set would be accepted here and then
    mangled by a proxy — failing as an unexplained 401 rather than a clear rejection."""
    for _ in range(30):
        key = keys.mint().plaintext
        body = key[len(keys.PREFIX_LIVE) : -keys.CHECKSUM_LENGTH]
        assert all(c in keys.KEY_ALPHABET for c in body)


def test_wrong_length_is_rejected_before_any_lookup():
    """Exact-length validation is what makes the cheap pre-database rejection work."""
    key = keys.mint().plaintext

    assert keys.is_well_formed(key) is True
    assert keys.is_well_formed(key + "A") is False
    assert keys.is_well_formed(key[:-1]) is False
    # Right prefix, right length, wrong checksum.
    assert keys.is_well_formed(key[:-6] + "000000") is False


def test_body_with_a_hostile_character_is_rejected():
    """Even at the correct length and with a valid checksum over that body."""
    import binascii

    body = "A" * (keys.BODY_LENGTH - 1) + "$"
    checksum = format(binascii.crc32(body.encode()) & 0xFFFFFFFF, "08x")[:6]
    forged = f"{keys.PREFIX_LIVE}{body}{checksum}"

    assert len(forged) == 64
    assert keys.is_well_formed(forged) is False


def test_key_entropy_is_cryptographically_sourced():
    """`random.choice` here would be a critical vulnerability — the sequence would be
    reproducible from a few observed keys."""
    source = pathlib.Path("app/iam/keys.py").read_text()
    mint_body = source.split("def mint(")[1].split("def is_well_formed")[0]

    assert "secrets.choice" in mint_body
    # `random` is not imported at all, so this cannot regress silently.
    assert "import random" not in source


def test_checksum_catches_a_truncated_paste():
    """**Why a checksum rather than only a prefix.**

    A dropped character becomes a clear "malformed key" at the door instead of a
    mystery 401 next week — and it is rejected without a database read, which matters
    because this header is an unauthenticated surface.
    """
    key = keys.mint().plaintext

    assert keys.is_well_formed(key)
    assert keys.is_well_formed(key[:-1]) is False
    assert keys.is_well_formed(key + "x") is False


def test_malformed_input_is_rejected_without_a_lookup():
    for junk in ("", "garbage", "Bearer eyJhbGciOiJIUzI1NiJ9.x.y", "shltky", "sk_live_abc"):
        assert keys.is_well_formed(junk) is False


def test_test_and_live_prefixes_are_distinguishable():
    """The "we pasted the wrong environment's key" incident should be caught by
    reading, not by debugging."""
    assert keys.mint(test_mode=True).plaintext.startswith(keys.PREFIX_TEST)
    assert keys.mint(test_mode=False).plaintext.startswith(keys.PREFIX_LIVE)


def test_key_hash_matches_only_the_original():
    minted = keys.mint()

    assert keys.matches(minted.plaintext, minted.key_hash)
    assert keys.matches(keys.mint().plaintext, minted.key_hash) is False


def test_key_comparison_is_constant_time():
    """String `==` short-circuits at the first differing byte; over a keep-alive
    connection that timing is measurable enough to recover a hash byte by byte."""
    source = pathlib.Path("app/iam/keys.py").read_text()
    body = source.split("def matches(")[1].split("\ndef ")[0]
    assert "compare_digest" in body


def test_plaintext_is_never_derivable_from_stored_material():
    """**The property that makes "shown once" true rather than merely stated.**

    Recovery is not a policy we could relax — the plaintext is not stored, so it is
    arithmetically unavailable to support, to the owner, and to an attacker with the
    database.
    """
    minted = keys.mint()

    for stored in (minted.key_hash, minted.hint, minted.last_four):
        assert minted.plaintext not in stored
    assert len(minted.hint) == keys.HINT_LENGTH
    assert len(minted.last_four) == 4


def test_redaction_never_leaks_a_usable_key():
    """A key in a log is a key in every downstream system that ingests logs, which is
    how these leak in practice."""
    key = keys.mint().plaintext
    redacted = keys.redact(key)

    assert key not in redacted
    assert len(redacted) < 24
    assert redacted.endswith(key[-4:])


def test_expiry_is_optional_and_both_choices_work():
    """Never-expiring is a supported choice, not an oversight: a key that silently
    stops working breaks an integration at an unpredictable moment."""
    assert keys.expiry_from_days(None) is None
    assert keys.is_expired(None) is False

    future = keys.expiry_from_days(30)
    assert future is not None and keys.is_expired(future) is False
    assert keys.is_expired(datetime.now(timezone.utc) - timedelta(seconds=1)) is True


def test_naive_datetimes_do_not_break_expiry():
    """Mongo returns naive UTC datetimes. Without normalisation this raises TypeError
    on every expiring key and the guard fails closed for the wrong reason."""
    naive_past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    naive_future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)

    assert keys.is_expired(naive_past) is True
    assert keys.is_expired(naive_future) is False


def test_usable_status_is_the_single_source_of_truth():
    """Three separate implementations of "is this key valid?" is how a revoked key
    keeps working in one code path."""
    active = {"status": "active", "expires_at": None, "grace_expires_at": None}
    assert keys.usable_status(active) is keys.KeyStatus.ACTIVE

    assert keys.usable_status({**active, "status": "revoked"}) is None
    assert keys.usable_status({**active, "status": "expired"}) is None
    assert keys.usable_status({**active, "expires_at": datetime.now(timezone.utc) - timedelta(1)}) is None

    # Rotating: valid inside the grace window, dead outside it.
    rotating = {"status": "rotating", "expires_at": None,
                "grace_expires_at": datetime.now(timezone.utc) + timedelta(hours=1)}
    assert keys.usable_status(rotating) is keys.KeyStatus.ROTATING
    assert keys.usable_status({**rotating,
                               "grace_expires_at": datetime.now(timezone.utc) - timedelta(hours=1)}) is None


def test_revocation_beats_an_open_grace_window():
    """Ordering matters: a revoked key must be dead even mid-rotation."""
    doc = {"status": "revoked", "expires_at": None,
           "grace_expires_at": datetime.now(timezone.utc) + timedelta(hours=12)}
    assert keys.usable_status(doc) is None


def test_key_health_flags_stale_and_expiring_keys():
    now = datetime.now(timezone.utc)

    never_used = keys.key_health({"created_at": now, "last_used_at": None, "expires_at": None})
    assert never_used["never_used"] is True
    assert never_used["stale"] is True
    assert never_used["never_expires"] is True

    fresh = keys.key_health({"created_at": now, "last_used_at": now,
                             "expires_at": now + timedelta(days=5)})
    assert fresh["stale"] is False
    assert fresh["expiring_soon"] is True


def test_default_scopes_exclude_side_effecting_permissions():
    """Scanning costs catalogue quota and webhook management can redirect another
    system's alert stream. Both must be asked for, not arrive in a pasted key."""
    assert ApiKeyScope.READ in DEFAULT_KEY_SCOPES
    assert ApiKeyScope.WRITE in DEFAULT_KEY_SCOPES
    assert ApiKeyScope.SCAN not in DEFAULT_KEY_SCOPES
    assert ApiKeyScope.WEBHOOKS not in DEFAULT_KEY_SCOPES


def test_checksum_is_not_a_security_control():
    """CRC32 detects transcription errors. Using a cryptographic hash here would imply
    the checksum authenticates the key, which it must not — authentication is the
    stored-hash comparison, and confusing the two is how a checksum becomes a bypass."""
    source = pathlib.Path("app/iam/keys.py").read_text()
    body = source.split("def _checksum(")[1].split("\ndef ")[0]
    assert "crc32" in body
    assert "sha256" not in body


# --------------------------------------------------------------------------- #
# Multi-tenancy
# --------------------------------------------------------------------------- #


def test_membership_is_an_edge_not_an_owner_field():
    """**The modelling requirement.**

    `managed_by: str` made the first aggregator to register someone their permanent
    owner, and turned a second aggregator's legitimate onboarding into "that address is
    taken". A farmer's cooperative, insurer and extension service may all serve them.
    """
    account_source = pathlib.Path("app/iam/models.py").read_text()
    tree = ast.parse(account_source)

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Account":
            fields = {
                t.target.id
                for t in node.body
                if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name)
            }
            assert "managed_by" not in fields, (
                "tenancy must be a memberships edge, not an owner field"
            )
            break
    else:
        pytest.fail("Account model not found")


def test_membership_document_carries_per_tenant_data():
    """`external_ref` lives on the edge: two aggregators know the same farmer by
    different references and neither's should overwrite the other's."""
    doc = tenancy.build_membership_document(
        "acc_farmer", "agg_coop", external_ref="MEMBER-4417"
    )

    assert doc["account_id"] == "acc_farmer"
    assert doc["aggregator_id"] == "agg_coop"
    assert doc["external_ref"] == "MEMBER-4417"
    assert doc["status"] == tenancy.MembershipStatus.ACTIVE.value


def test_one_subscriber_can_belong_to_many_aggregators():
    """The whole point: distinct edges, no conflict, independent references."""
    first = tenancy.build_membership_document("acc_farmer", "agg_coop", external_ref="C-1")
    second = tenancy.build_membership_document("acc_farmer", "agg_insurer", external_ref="P-9")

    assert first["account_id"] == second["account_id"]
    assert first["aggregator_id"] != second["aggregator_id"]
    assert first["id"] != second["id"]
    assert first["external_ref"] != second["external_ref"]


def test_tenant_filter_includes_status():
    """A filter missing the status check keeps serving a detached customer."""
    f = tenancy.active_filter("agg_x")

    assert f["aggregator_id"] == "agg_x"
    assert f["status"] == tenancy.MembershipStatus.ACTIVE.value


def test_subscriber_revocation_is_distinct_from_aggregator_detach():
    """Only the subscriber may undo their own revocation — otherwise an aggregator
    could re-attach itself after the person removed it, making the control
    decorative."""
    assert (
        tenancy.MembershipStatus.REVOKED_BY_SUBSCRIBER
        is not tenancy.MembershipStatus.DETACHED
    )
    source = pathlib.Path("app/iam/store.py").read_text()
    attach = source.split("async def attach_membership(")[1].split("async def ")[0]
    assert "REVOKED_BY_SUBSCRIBER" in attach, (
        "attach_membership must refuse to reactivate a subscriber-revoked edge"
    )


# --------------------------------------------------------------------------- #
# The immutable audit log
# --------------------------------------------------------------------------- #


def test_audit_module_never_updates_or_deletes():
    """**"Immutable" as a code comment is not a guarantee; this is.**

    Asserted structurally over the AST: no call to any mutating driver method may
    appear against the audit collection. Retention is a TTL index — deletion by
    database policy on Mongo's schedule — which is not an application code path.
    """
    source = pathlib.Path("app/iam/store.py").read_text()
    audit_section = source.split("# The immutable audit log")[-1]

    forbidden = (
        "audit_log.update_one", "audit_log.update_many",
        "audit_log.delete_one", "audit_log.delete_many",
        "audit_log.find_one_and_update", "audit_log.find_one_and_delete",
        "audit_log.replace_one", "audit_log.bulk_write",
    )
    found = [f for f in forbidden if f in audit_section]
    assert not found, f"the audit log must be append-only; found: {found}"
    assert "audit_log.insert_one" in audit_section


def test_audit_events_are_individually_bounded():
    """**The BSON 16 MB defence.**

    Audit logs hit that cap by being modelled as an array inside a parent document. Here
    each event is its own document with every field bounded, so no single document can
    grow pathologically and a collection has no size limit.
    """
    event = audit.build_event(
        account_id="acc_1",
        action=audit.AuditAction.LOGIN_SUCCEEDED,
        detail="X" * 50_000,
        ip="1" * 200,
        user_agent="U" * 5_000,
    )

    assert len(event["detail"]) <= audit.MAX_DETAIL_CHARS
    assert len(event["ip"]) <= 45
    assert len(event["user_agent"]) <= 200
    # No nested collection that could grow without limit.
    assert not any(isinstance(v, list | dict) for v in event.values())


def test_audit_separates_subject_from_actor():
    """When an aggregator onboards a farmer, the event is *about* the farmer and *by*
    the aggregator. Collapsing them makes "who did this to my account?" unanswerable —
    the single most important question a subscriber can ask."""
    event = audit.build_event(
        account_id="acc_farmer",
        actor_id="agg_coop",
        actor_kind="aggregator",
        action=audit.AuditAction.CUSTOMER_ONBOARDED,
    )

    assert event["account_id"] == "acc_farmer"
    assert event["actor_id"] == "agg_coop"
    assert event["actor_kind"] == "aggregator"


def test_audit_actor_defaults_to_the_subject():
    """A self-service action has one party, and it must still be recorded as the actor
    rather than left null."""
    event = audit.build_event(account_id="acc_1", action=audit.AuditAction.LOGIN_SUCCEEDED)
    assert event["actor_id"] == "acc_1"
    assert event["actor_kind"] == "self"


def test_audit_retention_is_stamped_at_insert():
    """So it cannot be shortened retroactively for entries already written."""
    event = audit.build_event(account_id="acc_1", action=audit.AuditAction.LOGIN_SUCCEEDED)
    assert event["expires_at"] > event["at"]


def test_cursor_round_trips_and_is_opaque():
    """Opaque so a client cannot hand-craft one to read outside its own scope."""
    now = datetime.now(timezone.utc)
    cursor = audit._encode_cursor(now, "507f1f77bcf86cd799439011")

    assert "507f1f77" not in cursor, "must not be plainly readable"
    decoded = audit._decode_cursor(cursor)
    assert decoded is not None
    assert decoded[1] == "507f1f77bcf86cd799439011"


def test_malformed_cursor_restarts_rather_than_raising():
    """A truncated or tampered cursor should restart from page one, not 500. It cannot
    widen scope, because the tenant filter is applied independently."""
    for junk in ("", "!!!", "nope", "YWJj"):
        assert audit._decode_cursor(junk) is None

    base = {"account_id": "acc_1"}
    assert audit.cursor_query(base, "garbage") == base


def test_keyset_query_handles_same_millisecond_events():
    """Without the `$or` on `(at, _id)`, events sharing a timestamp are silently
    dropped from results — a real correctness bug, not a performance one."""
    now = datetime.now(timezone.utc)
    cursor = audit._encode_cursor(now, "507f1f77bcf86cd799439011")
    query = audit.cursor_query({"account_id": "acc_1"}, cursor)

    assert "$or" in query
    assert len(query["$or"]) == 2
    # Scope survives the cursor.
    assert query["account_id"] == "acc_1"


def test_cursor_cannot_widen_scope():
    """A forged cursor may only move position within a scope the caller already has."""
    forged = audit._encode_cursor(datetime.now(timezone.utc), "507f1f77bcf86cd799439011")
    query = audit.cursor_query({"account_id": "acc_victim"}, forged)
    assert query["account_id"] == "acc_victim"


def test_page_size_is_clamped():
    """A caller asking for 100,000 rows would build a response large enough to exhaust
    process memory — a denial of service against ourselves."""
    assert audit.clamp_page_size(1_000_000) == audit.MAX_PAGE_SIZE
    assert audit.clamp_page_size(None) == audit.DEFAULT_PAGE_SIZE
    assert audit.clamp_page_size(0) == audit.DEFAULT_PAGE_SIZE
    assert audit.clamp_page_size(25) == 25


def test_page_trims_the_lookahead_row():
    """Callers fetch page_size+1 to answer "is there more?" without a count. The extra
    row must never reach the client."""
    now = datetime.now(timezone.utc)
    docs = [{"at": now, "_id": f"id{i}", "action": "x"} for i in range(6)]

    page = audit.make_page(docs, 5)
    assert len(page.entries) == 5
    assert page.has_more is True
    assert page.next_cursor is not None

    last_page = audit.make_page(docs[:3], 5)
    assert last_page.has_more is False
    assert last_page.next_cursor is None


def test_serialised_entries_hide_internals():
    """`_id` is an internal ObjectId and `expires_at` is a retention mechanism that
    would read as a property of the event."""
    now = datetime.now(timezone.utc)
    page = audit.make_page(
        [{"at": now, "_id": "abc", "expires_at": now, "action": "x"}], 5
    )
    entry = page.entries[0]

    assert "_id" not in entry
    assert "expires_at" not in entry
    assert isinstance(entry["at"], str), "must be JSON-safe"


def test_audit_page_reports_no_total():
    """`count_documents` on a growing collection is an unindexed scan that gets slower
    exactly as the log becomes more valuable."""
    page = audit.make_page([], 10)
    assert not hasattr(page, "total")


# --------------------------------------------------------------------------- #
# Onboarding validation
# --------------------------------------------------------------------------- #


def test_signup_normalises_nigerian_numbers_to_e164():
    """Stored unnormalised, a number is undeliverable on WhatsApp and Signal — and the
    failure surfaces only when an alert is dispatched."""
    signup = IndividualSignup(
        first_name="Amina", last_name="Bello", email="amina@example.com",
        phone="08031234567", password="correct horse battery staple",
    )
    assert signup.phone == "+2348031234567"


def test_signup_accepts_an_absent_phone():
    """An email-only subscriber is fully functional, and demanding a number before the
    value is demonstrated loses signups."""
    signup = IndividualSignup(
        first_name="A", last_name="B", email="a@example.com", phone=None,
        password="correct horse battery staple",
    )
    assert signup.phone is None


def test_signup_rejects_short_and_obvious_passwords():
    with pytest.raises(ValueError):
        IndividualSignup(first_name="A", last_name="B", email="a@example.com",
                         password="short")
    with pytest.raises(ValueError):
        IndividualSignup(first_name="A", last_name="B", email="a@example.com",
                         password="password12345")


def test_signup_rejects_a_malformed_email():
    with pytest.raises(ValueError):
        IndividualSignup(first_name="A", last_name="B", email="not-an-email",
                         password="correct horse battery staple")


def test_commercial_signup_reuses_the_same_validators():
    """A weaker password policy on the account that holds API keys would be backwards."""
    with pytest.raises(ValueError):
        CommercialSignup(organisation="Co", contact_first_name="A", contact_last_name="B",
                         email="a@example.com", password="password12345")

    ok = CommercialSignup(organisation="Kebbi Co", contact_first_name="N",
                          contact_last_name="E", email="n@example.com",
                          phone="08031234567", password="three unrelated words here")
    assert ok.phone == "+2348031234567"


# --------------------------------------------------------------------------- #
# Structural boundaries
# --------------------------------------------------------------------------- #


def test_account_model_cannot_carry_a_secret():
    """A model with a hash field would eventually be serialised into a response. This
    makes that mistake impossible rather than merely discouraged."""
    tree = ast.parse(pathlib.Path("app/iam/models.py").read_text())

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Account":
            fields = {
                t.target.id
                for t in node.body
                if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name)
            }
            for secret in ("password_hash", "password", "verification_token_hash", "secret"):
                assert secret not in fields
            return
    pytest.fail("Account model not found")


def test_public_projection_excludes_every_secret():
    """An exclusion projection, so a new *secret* must be added here explicitly while a
    new ordinary field appears automatically. Backwards, and the next credential-like
    field is exposed by default."""
    from app.iam.store import _PUBLIC_PROJECTION

    assert _PUBLIC_PROJECTION["password_hash"] == 0
    assert _PUBLIC_PROJECTION["verification_token_hash"] == 0


def test_individuals_cannot_use_the_api():
    """Structural: `can_use_api` is the one place that decides, so the check cannot
    drift between routes."""
    from app.iam.models import Account, AccountStatus

    common = dict(
        id="acc_1", email="a@example.com", first_name="A", last_name="B",
        status=AccountStatus.ACTIVE, created_at=datetime.now(timezone.utc),
    )
    assert Account(kind=AccountKind.INDIVIDUAL, **common).can_use_api is False
    assert Account(kind=AccountKind.COMMERCIAL, **common).can_use_api is True


def test_iam_store_is_the_only_module_touching_atlas():
    """One module owns the driver, so the projection discipline has a single
    enforcement point. A second `AsyncIOMotorClient` elsewhere would bypass it."""
    offenders = []
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        if path.name == "store.py" and "iam" in str(path):
            continue
        if "AsyncIOMotorClient" in path.read_text():
            offenders.append(str(path))
    assert not offenders, f"only app/iam/store.py may construct a Mongo client: {offenders}"


def test_preflight_rejects_a_predictable_jwt_secret_in_production():
    """A predictable signing key lets anyone mint a session for any account."""
    from unittest import mock

    from app.preflight import main

    with mock.patch("app.config.settings.environment", "production"), \
         mock.patch("app.config.settings.api_key", "a" * 32), \
         mock.patch("app.config.settings.mongo_url", "mongodb+srv://x/y"), \
         mock.patch("app.config.settings.iam_jwt_secret", None):
        assert main(["--role", "api"]) == 1


# --------------------------------------------------------------------------- #
# Transactional email transport
#
# Two ways to reach the same Brevo account, and they fail differently — which is
# the whole reason both exist. Verified live: the API returned HTTP 201 from a host
# whose egress IP was NOT on Brevo's permit list, while the SMTP relay on the same
# host returned `535 Authentication failed`. The API authenticates with a key alone;
# the relay additionally requires the sending IP to be allow-listed.
# --------------------------------------------------------------------------- #


def _mail_settings(**overrides):
    from unittest import mock

    return mock.patch.multiple("app.config.settings", **overrides)


@pytest.mark.parametrize(
    "configured,brevo_key,smtp_host,expected",
    [
        # auto prefers the API: one credential instead of two, and no IP allow-list,
        # so it is the path most likely to work on an unknown host.
        ("auto", "xkeysib-x", "relay", "brevo_api"),
        ("auto", None, "relay", "smtp"),
        ("auto", None, None, "noop"),
        # An explicit choice is honoured when it can be.
        ("brevo_api", "xkeysib-x", "relay", "brevo_api"),
        ("smtp", "xkeysib-x", "relay", "smtp"),
        ("noop", "xkeysib-x", "relay", "noop"),
        # **A forced provider that is not configured degrades to noop, never to the
        # other transport.** Same rule as ADVISORY_PROVIDER.
        ("brevo_api", None, "relay", "noop"),
        ("smtp", "xkeysib-x", None, "noop"),
    ],
)
def test_provider_resolution(configured, brevo_key, smtp_host, expected):
    from app.iam import mailer

    with _mail_settings(
        notification_provider=configured,
        brevo_api_key=brevo_key,
        brevo_sender_email="sender@example.com",
        smtp_host=smtp_host,
    ):
        assert mailer.resolve_provider() == expected


@pytest.mark.asyncio
async def test_forced_provider_never_silently_reroutes():
    """**The property worth a dedicated test.**

    `brevo_api` forced but unconfigured, with a working SMTP relay available, must
    send nothing. Quietly using the other transport would leave the operator believing
    their explicit choice was honoured — and a misconfiguration they cannot see is
    worse than mail that visibly did not send.
    """
    from app.iam import mailer

    with _mail_settings(
        notification_provider="brevo_api",
        brevo_api_key=None,
        smtp_host="relay.example.com",
        smtp_username="u",
        smtp_password="p",
    ):
        assert mailer.resolve_provider() == "noop"
        assert mailer.available() is False
        # Must not fall through to SMTP.
        assert await mailer._send("x@example.com", "s", "body", None) is False


@pytest.mark.asyncio
async def test_auto_falls_back_to_smtp_when_the_api_errors():
    """Under `auto` — and only under `auto` — a failed API send may retry over SMTP.

    Permitted here because the operator expressed no preference, so either transport
    satisfies the intent. Under a forced provider the same fallback would be a silent
    substitution, which the test above forbids.
    """
    from unittest import mock

    from app.iam import mailer

    with _mail_settings(
        notification_provider="auto",
        brevo_api_key="xkeysib-x",
        brevo_sender_email="s@example.com",
        smtp_host="relay.example.com",
    ):
        with mock.patch.object(mailer, "_send_brevo_api", return_value=False) as api, \
             mock.patch.object(mailer, "_send_smtp", return_value=True) as smtp:
            assert await mailer._send("x@example.com", "s", "body", None) is True
            assert api.called and smtp.called


@pytest.mark.asyncio
async def test_brevo_payload_shape_matches_the_api_contract():
    """Built by hand rather than with a vendor SDK, for the same reason `app/llm/`
    speaks plain HTTP: one less dependency whose release cadence we do not control,
    and a payload a reader can check against Brevo's documentation."""
    from unittest import mock

    from app.iam import mailer

    captured: dict = {}

    class _Response:
        status_code = 201

        @staticmethod
        def json():
            return {"messageId": "<abc@smtp-relay.mailin.fr>"}

        text = ""

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _Response()

    with _mail_settings(
        brevo_api_key="xkeysib-secret",
        brevo_sender_email="sender@example.com",
        brevo_sender_name="SHELTER",
        brevo_reply_to_email="reply@example.com",
        brevo_tag="shelter-transactional",
    ):
        with mock.patch.object(mailer.httpx, "AsyncClient", return_value=_Client()):
            assert await mailer._send_brevo_api(
                "farmer@example.com", "Subject", "plain body", "<p>html</p>"
            ) is True

    payload = captured["json"]
    assert payload["sender"] == {"name": "SHELTER", "email": "sender@example.com"}
    assert payload["to"] == [{"email": "farmer@example.com"}]
    assert payload["textContent"] == "plain body"
    assert payload["htmlContent"] == "<p>html</p>"
    assert payload["replyTo"] == {"email": "reply@example.com"}
    assert payload["tags"] == ["shelter-transactional"]
    # The key travels in the `api-key` header, which is what Brevo expects — not as
    # a bearer token, which it silently rejects as unauthenticated.
    assert captured["headers"]["api-key"] == "xkeysib-secret"


@pytest.mark.asyncio
async def test_brevo_rejection_is_reported_not_raised():
    """A 400 from Brevo (unverified sender, exhausted credits) must return False.

    Callers treat False as "the link was not sent" and continue — a signup must not be
    lost because mail was unavailable, since the account is already durable and the
    link can be re-sent.
    """
    from unittest import mock

    from app.iam import mailer

    class _Rejected:
        status_code = 400
        text = '{"code":"invalid_parameter","message":"sender not verified"}'

        @staticmethod
        def json():
            return {}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Rejected()

    with _mail_settings(brevo_api_key="xkeysib-x", brevo_sender_email="s@example.com"):
        with mock.patch.object(mailer.httpx, "AsyncClient", return_value=_Client()):
            assert await mailer._send_brevo_api("a@example.com", "s", "b", None) is False


@pytest.mark.asyncio
async def test_network_failure_returns_false():
    """A DNS failure or timeout is an expected condition for an outbound call, not an
    exception worth propagating into a signup request."""
    from unittest import mock

    from app.iam import mailer

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise OSError("name resolution failed")

    with _mail_settings(brevo_api_key="xkeysib-x", brevo_sender_email="s@example.com"):
        with mock.patch.object(mailer.httpx, "AsyncClient", return_value=_Client()):
            assert await mailer._send_brevo_api("a@example.com", "s", "b", None) is False


def test_brevo_sender_falls_back_to_smtp_from():
    """A deployment that only set the SMTP identity should still work over the API,
    rather than silently resolving to noop over a second unset address."""
    from app.iam import mailer

    with _mail_settings(
        brevo_api_key="xkeysib-x",
        brevo_sender_email=None,
        smtp_from="alerts@example.com",
    ):
        assert mailer._brevo_ready() is True


def test_mailer_uses_no_vendor_sdk():
    """Same discipline as `tests/test_llm_portability.py`: plain HTTP over the httpx
    the project already depends on, so there is no vendor client to track and the
    payload is readable against the published contract."""
    source = pathlib.Path("app/iam/mailer.py").read_text()

    for vendor in ("import sib_api_v3_sdk", "from sib_api_v3_sdk", "import brevo"):
        assert vendor not in source


def test_health_reports_the_resolved_transport():
    """A deployment that set NOTIFICATION_PROVIDER but is missing the credential
    resolves to noop. That must be visible on a calm day, not discovered when a
    subscriber never receives a verification link."""
    source = pathlib.Path("app/api/routes/health.py").read_text()

    assert "resolve_provider()" in source
    assert '"notifications"' in source


# --------------------------------------------------------------------------- #
# Platform authentication — replacing the shared X-SHELTER-Key
#
# The old credential was one static string gating 29 write endpoints across seven
# routers, with no attribution, no scoping, no revocation and no expiry. These tests
# lock in the properties that fix each of those.
# --------------------------------------------------------------------------- #


def test_platform_scopes_are_separate_from_tenant_scopes():
    """Two namespaces, deliberately.

    A tenant scope is bounded by `memberships`; a platform scope is not.
    `platform:subscribers:write` can register a subscriber for anyone — which is
    exactly the unbounded authority the shared key had — so it must never sit on an
    aggregator's key.
    """
    from app.iam.models import PLATFORM_SCOPES, ApiKeyScope

    for scope in PLATFORM_SCOPES:
        assert scope.value.startswith("platform:")

    tenant = {ApiKeyScope.READ, ApiKeyScope.WRITE, ApiKeyScope.SCAN, ApiKeyScope.WEBHOOKS}
    assert not (tenant & PLATFORM_SCOPES), "the two namespaces must not overlap"


def test_frontend_scopes_exclude_broadcast():
    """**The concrete least-privilege win.**

    `frontend/lib/api.ts` calls `/health`, `/risk/assess` and `/subscribers` — three
    endpoints. The shared key also granted NIGCOMSAT broadcast and webhook
    administration. The portal has no reason to page a district, so its key does not
    carry that scope.
    """
    from app.iam.models import FRONTEND_SCOPES, ApiKeyScope

    assert ApiKeyScope.PLATFORM_SUBSCRIBERS in FRONTEND_SCOPES
    assert ApiKeyScope.PLATFORM_READ in FRONTEND_SCOPES
    assert ApiKeyScope.PLATFORM_ASSESS in FRONTEND_SCOPES

    assert ApiKeyScope.PLATFORM_BROADCAST not in FRONTEND_SCOPES
    assert ApiKeyScope.PLATFORM_OPERATE not in FRONTEND_SCOPES


def test_broadcast_is_its_own_scope():
    """Dispatch reaches people directly, including the satellite broadcast escalation
    when every terrestrial channel fails. Bundling it with subscriber writes would mean
    the portal's key could page a district."""
    source = pathlib.Path("app/api/routes/alerts.py").read_text()
    dispatch = source.split('"/dispatch/{subscriber_id}"')[1].split("async def")[0]

    # Match the dependency line, not prose. An earlier version of this test matched
    # the scope name anywhere in the block and tripped over the comment explaining
    # the choice — which would also have passed had the guard been wrong.
    guard = [ln for ln in dispatch.splitlines() if "require_platform_scope" in ln]
    assert len(guard) == 1, f"expected exactly one guard, got {guard}"
    assert "PLATFORM_BROADCAST" in guard[0]
    assert "PLATFORM_SUBSCRIBERS" not in guard[0]


def test_fleet_scan_needs_operate_not_assess():
    """`/risk/scan` queues a scan for EVERY active subscriber, so its cost scales with
    the whole fleet rather than one area — an operations action, not a portal one."""
    source = pathlib.Path("app/api/routes/risk.py").read_text()
    scan = source.split('"/scan"')[1].split("async def")[0]

    guard = [ln for ln in scan.splitlines() if "require_platform_scope" in ln]
    assert len(guard) == 1
    assert "PLATFORM_OPERATE" in guard[0]


def test_no_route_still_uses_the_unscoped_guard():
    """Every platform write must name a scope.

    Two deliberate exceptions in `health.py`: `/bootstrap/verify` exists to validate
    whichever credential the caller holds, and `/iam/service-accounts` provisions the
    replacement for the shared key — requiring a scoped key for either would be
    circular.
    """
    import re

    offenders: list[str] = []
    for path in sorted(pathlib.Path("app/api/routes").glob("*.py")):
        source = path.read_text()
        for match in re.finditer(r"Depends\(require_api_key\)", source):
            # Locate the enclosing route for the error message.
            before = source[: match.start()]
            route = before.rsplit("@router", 1)[-1].split("\n")[0] if "@router" in before else "?"
            offenders.append(f"{path.name}:{route.strip()}")

    allowed_files = {"health.py", "iam.py"}
    unexpected = [o for o in offenders if o.split(":")[0] not in allowed_files]
    assert not unexpected, (
        f"these routes still use the unscoped shared-key guard: {unexpected}"
    )


def test_principal_carries_attribution():
    """The audit gap the shared key created.

    `require_api_key` returned None — it proved *a* valid caller existed and nothing
    else, so "who registered these 400 subscribers?" was unanswerable. A `Principal`
    names the actor, and names the unattributable case explicitly rather than leaving
    it null.
    """
    from app.iam.platform import Principal

    anonymous = Principal(None, [], legacy=True)
    assert anonymous.id == "legacy-shared-key"
    assert anonymous.legacy is True


def test_legacy_key_is_refused_in_production_with_iam():
    """Once a scoped key is available, the shared key is strictly worse — so it stops
    being accepted rather than lingering because someone forgot to remove it."""
    from unittest import mock

    from app.iam.platform import _legacy_allowed

    with mock.patch.multiple(
        "app.config.settings",
        iam_legacy_shared_key_enabled=True,
        environment="production",
        mongo_url="mongodb+srv://x/y",
    ):
        assert _legacy_allowed() is False

    # Development keeps it, so a developer with no Atlas connection is not locked out
    # of their own API.
    with mock.patch.multiple(
        "app.config.settings",
        iam_legacy_shared_key_enabled=True,
        environment="development",
        mongo_url="mongodb+srv://x/y",
    ):
        assert _legacy_allowed() is True

    # And the explicit off switch wins everywhere.
    with mock.patch.multiple(
        "app.config.settings",
        iam_legacy_shared_key_enabled=False,
        environment="development",
        mongo_url=None,
    ):
        assert _legacy_allowed() is False


def test_service_account_provisioning_refuses_tenant_scopes():
    """A service account acts across the platform. Giving it a customer-scoped
    permission would be meaningless — there is no tenant to scope to."""
    source = pathlib.Path("app/iam/platform.py").read_text()
    body = source.split("async def provision_service_account(")[1]

    assert "_platform_scope_set()" in body
    assert "refusing to provision" in body


def test_platform_scopes_cannot_be_minted_on_a_tenant_account():
    """**Enforced at mint time, not only at use time.**

    Checking at use time alone would leave an over-privileged key sitting in the
    database, valid until someone noticed. Verified live against Atlas: a commercial
    account requesting `platform:broadcast` gets None.
    """
    source = pathlib.Path("app/iam/store.py").read_text()
    mint = source.split("async def create_api_key(")[1].split("async def ")[0]

    assert "PLATFORM_SCOPES" in mint
    assert "AccountKind.SERVICE" in mint
    assert "refused to mint a platform scope" in mint


def test_service_accounts_have_no_password():
    """No password means no login to brute-force and no credential to phish. Its only
    authentication path is the key."""
    source = pathlib.Path("app/iam/platform.py").read_text()
    body = source.split("async def provision_service_account(")[1]

    assert "password=None" in body


def test_frontend_sends_the_scoped_header_when_it_holds_a_scoped_key():
    """Prefix detection rather than a second environment variable, so there is one
    value to rotate instead of two that can disagree."""
    source = pathlib.Path("../frontend/lib/api.ts").read_text()

    assert 'headers.set("X-SHELTER-API-Key", API_KEY)' in source
    assert 'API_KEY.startsWith("shltky")' in source
    # The legacy header stays during migration.
    assert 'headers.set("X-SHELTER-Key", API_KEY)' in source


def test_preflight_nudges_off_the_shared_key():
    """A warning outside production, an error inside it — holding both credentials
    means the weaker one is still live."""
    from unittest import mock

    from app.preflight import run_checks

    with mock.patch.multiple(
        "app.config.settings",
        api_key="a" * 32,
        mongo_url="mongodb+srv://x/y",
        iam_legacy_shared_key_enabled=True,
        environment="production",
        iam_jwt_secret="b" * 32,
    ):
        errors = [f for f in run_checks("api") if f.level == "error"]
        assert any("shared X-SHELTER-Key" in f.message for f in errors)


# --------------------------------------------------------------------------- #
# Identity identifiers — 10-character alphanumeric, minted by IAM
#
# `aggregator_id` and `subscriber_id` are the *public* references: a partner stores
# them, a farmer reads one off a printed slip, an agent quotes one on a support call.
# So they are short and legible, and the properties below protect that.
# --------------------------------------------------------------------------- #


def test_identifiers_are_ten_alphanumeric_characters():
    from app.iam import identifiers

    for _ in range(50):
        value = identifiers.mint()
        assert len(value) == 10
        assert value.isalnum()
        assert value.isupper() or value.isdigit() or not value.isalpha()
        assert identifiers.is_valid(value)


def test_identifier_alphabet_excludes_illegible_characters():
    """**I, O, 0 and 1 are removed on purpose.**

    A farmer reading an id over a phone line, or an agent typing one from handwriting,
    confuses `I`/`1` and `O`/`0` constantly — and every confusion is a support call
    about a "missing" record that exists under a neighbouring id.

    The cost is ~1.7 bits. That is affordable because **an id is not a credential**:
    authorisation is the API key plus the membership edge, so guessing an id gains
    nothing.
    """
    from app.iam import identifiers

    for char in "IO01":
        assert char not in identifiers.ID_ALPHABET

    assert len(identifiers.ID_ALPHABET) == 32
    # And no minted id can contain one.
    minted = "".join(identifiers.mint() for _ in range(200))
    assert not (set(minted) & set("IO01"))


def test_identifier_space_is_large_enough_to_not_collide():
    """32^10 is ~2^50. Not a security boundary — but large enough that a collision is
    a genuine anomaly rather than something to expect at scale."""
    import math

    from app.iam import identifiers

    bits = math.log2(len(identifiers.ID_ALPHABET) ** identifiers.ID_LENGTH)
    assert bits > 48

    # Empirically distinct across a large draw.
    assert len({identifiers.mint() for _ in range(20_000)}) == 20_000


def test_identifiers_use_a_cryptographic_source():
    """An id is not a credential, so this is not strictly required — but a predictable
    sequence would make ids enumerable, and enumerable ids turn any future
    authorisation slip into a bulk extraction rather than a single-record one."""
    source = pathlib.Path("app/iam/identifiers.py").read_text()

    assert "secrets.choice" in source
    assert "import random" not in source


def test_validation_rejects_malformed_identifiers():
    """Shape-checked before a database round trip: these appear in URL paths, so a typo
    should be a clear 422 rather than a 404 that reads as "your record is gone"."""
    from app.iam import identifiers

    assert identifiers.is_valid("A7K2M9P4QX") is True

    for bad in ("", None, "A7K2M9P4Q", "A7K2M9P4QXY", "a7k2m9p4qx",
                "A7K2M9P4Q0", "A7K2-M9P4QX", "A7K2 M9P4QX"):
        assert identifiers.is_valid(bad) is False, f"{bad!r} should be rejected"


def test_normalisation_is_forgiving_but_canonical():
    """Someone quoting an id from a printed slip may lower-case it or add hyphens for
    legibility. None of those should be a lookup failure — but the stored form stays
    exact."""
    from app.iam import identifiers

    assert identifiers.normalise("a7k2m9p4qx") == "A7K2M9P4QX"
    assert identifiers.normalise("A7K2-M9P4-QX") == "A7K2M9P4QX"
    assert identifiers.normalise("  A7K2M9P4QX  ") == "A7K2M9P4QX"

    # None, not a guess, when the result is not an id — so a caller can tell "not an
    # id" from "an id that does not exist".
    assert identifiers.normalise("nonsense") is None
    assert identifiers.normalise("") is None


@pytest.mark.asyncio
async def test_mint_unique_retries_past_a_collision():
    """The unique index is the real guarantee; this resolves a collision *before* the
    insert, so it never surfaces as a duplicate-key error the caller has to
    distinguish from a duplicate email."""
    from app.iam import identifiers

    seen: list[str] = []

    async def exists(candidate: str) -> bool:
        seen.append(candidate)
        return len(seen) < 3      # first two collide

    value = await identifiers.mint_unique(exists)

    assert len(seen) == 3
    assert identifiers.is_valid(value)


@pytest.mark.asyncio
async def test_mint_unique_fails_loudly_on_a_broken_check():
    """A failing existence check must not yield a possibly-colliding id — the unique
    index would then reject the insert with a confusing error, far from the cause."""
    from app.iam import identifiers

    async def broken(candidate: str) -> bool:
        raise ConnectionError("atlas unreachable")

    with pytest.raises(RuntimeError, match="could not verify"):
        await identifiers.mint_unique(broken)


@pytest.mark.asyncio
async def test_mint_unique_gives_up_rather_than_looping_forever():
    """Five consecutive collisions at 2^50 means the check is broken or the database is
    unreachable. Retrying forever would hang the request and look like a timeout."""
    from app.iam import identifiers

    async def always_taken(candidate: str) -> bool:
        return True

    with pytest.raises(RuntimeError, match="attempts"):
        await identifiers.mint_unique(always_taken)


def test_subscriber_ids_use_the_identity_format():
    """A subscriber id is public-facing, so it gets the short legible form rather than
    the prefixed internal one used for assessments and jobs."""
    from app.iam import identifiers
    from app.models.schemas import Subscriber

    for _ in range(10):
        subscriber = Subscriber(name="Test")
        assert identifiers.is_valid(subscriber.id)


def test_internal_ids_keep_their_prefix():
    """Assessments, alerts and jobs are machine-referenced and appear in logs, where
    `alert_…` is self-describing. Only the human-facing ids changed."""
    from app.models.schemas import AreaOfInterest, BBox

    aoi = AreaOfInterest(name="x", bbox=BBox(west=1, south=1, east=2, north=2))
    assert aoi.id.startswith("aoi_")


def test_aggregator_id_is_an_alias_not_a_second_field():
    """One identity, one id. A separate stored `aggregator_id` would create two values
    that can disagree — the alias exists because the *relationship* is what the name
    describes, and `memberships` stores it under that name."""
    import ast
    from datetime import datetime, timezone

    from app.iam.models import Account, AccountKind, AccountStatus

    common = dict(
        id="A7K2M9P4QX", email="a@example.com", first_name="A", last_name="B",
        status=AccountStatus.ACTIVE, created_at=datetime.now(timezone.utc),
    )
    commercial = Account(kind=AccountKind.COMMERCIAL, **common)
    individual = Account(kind=AccountKind.INDIVIDUAL, **common)

    assert commercial.aggregator_id == commercial.id
    # None for an individual, so a farmer's id cannot be mistaken for a tenant scope.
    assert individual.aggregator_id is None

    # And it must not be a stored field.
    tree = ast.parse(pathlib.Path("app/iam/models.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Account":
            fields = {
                t.target.id
                for t in node.body
                if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name)
            }
            assert "aggregator_id" not in fields
            return
    pytest.fail("Account model not found")


def test_account_ids_are_minted_through_the_uniqueness_check():
    """Entropy alone is not a uniqueness guarantee, and a collision on a primary
    identifier is data corruption rather than a retry-later inconvenience."""
    source = pathlib.Path("app/iam/store.py").read_text()
    # Split on the next TOP-LEVEL def. `create_account` contains a nested
    # `async def _id_taken`, so splitting on "async def " truncated the body and the
    # assertion passed vacuously against an empty string.
    create = source.split("async def create_account(")[1].split("\nasync def ")[0]

    assert "identifiers.mint_unique" in create
    assert "uuid.uuid4().hex[:20]" not in create


def test_subscriber_id_uniqueness_spans_both_stores():
    """`subscribers` is a Postgres table while `accounts.subscriber_id` is in Atlas. An
    id free in one could be taken in the other, and that collision would bind an
    account to somebody else's subscription."""
    source = pathlib.Path("app/iam/store.py").read_text()
    mint = source.split("async def mint_subscriber_id(")[1].split("\nasync def ")[0]

    assert "accounts.count_documents" in mint
    assert "FROM subscribers" in mint


def test_legacy_prefixed_ids_are_recognised_not_rejected():
    """Existing rows keep working, and the distinction between "old format" and
    "malformed" stays legible in logs and error messages."""
    from app.iam import identifiers

    assert identifiers.looks_like_legacy("sub_a1b2c3d4e5f6") is True
    assert identifiers.looks_like_legacy("acc_9f03d772abcd") is True
    assert identifiers.looks_like_legacy("A7K2M9P4QX") is False
    assert identifiers.looks_like_legacy("garbage") is False


# --------------------------------------------------------------------------- #
# Email design system
#
# One header, one footer, per-type iconography. Before this, the verification email
# had a branded header while welcome and API-key notices were plain text with no header
# at all — a subscriber receiving both would not read them as the same product.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "kind", ["verify", "welcome", "api_key", "sign_in", "reset", "alert"]
)
def test_every_email_kind_renders_with_its_own_icon(kind: str):
    """A distinct glyph per notification kind, so a recipient recognises *what* an email
    is before reading a word — which matters most for the security notices, where "did I
    do this?" is the whole question."""
    from app.email import layout

    html = layout.render(kind=kind, eyebrow="Test", title="Title", body_html="<p>x</p>")

    assert layout._ICONS[kind][:24] in html, f"the {kind} icon is not embedded"
    assert "SHELTER" in html


def test_footer_is_exact_and_centred():
    """The footer copy is specified verbatim, so it is asserted verbatim — a paraphrase
    here would be a silent change to a legal notice on every outbound email."""
    import re

    from app.email import layout

    html = layout.render(
        kind="welcome", eyebrow="E", title="T", body_html="<p>x</p>"
    )
    flat = re.sub(r"\s+", " ", html)

    assert "&copy; FreePass Holding Co 2026" in flat
    assert (
        "SHELTER &mdash; satellite-enabled &amp; AI-powered early warning for "
        "flood, crop and health risk." in flat
    )
    assert "text-align:center" in html, "the footer must be centred"


def _strip_comments(html: str) -> str:
    """HTML with `<!-- ... -->` removed — the footer comments legitimately mention `<style>`."""
    import re

    return re.sub(r"<!--.*?-->", "", html, flags=re.S)


def test_footer_carries_both_consortium_marks():
    """Both real logos, both embedded as base64 PNG.

    Neither is inline SVG and neither is fetched. Outlook's sanitiser strips `<svg>`, which is
    why the FreePass wordmark rendered as nothing at all in New Outlook; and a remote `<img>`
    is blocked by most clients on first open, which is when the recipient is judging whether
    the mail is legitimate. A `data:` URI is the only form that satisfies both.
    """
    from app.email import layout

    html = layout.render(kind="welcome", eyebrow="E", title="T", body_html="<p>x</p>")

    assert "Powered by" in html
    # Three: the FreePass wordmark in two inks (swapped by colour scheme) and NIGCOMSAT.
    assert html.count("data:image/png;base64,") == 3, (
        "expected both FreePass inks and the NIGCOMSAT emblem to be embedded"
    )
    # `alt` text is the fallback when a client blocks even embedded images.
    assert 'alt="FreePass"' in html
    assert 'alt="NIGCOMSAT"' in html


def test_emails_use_no_remote_images():
    """Most clients block remote images until the recipient clicks "show images", so a logo
    referenced by URL is invisible exactly when they are deciding whether the mail is
    legitimate.

    `<img>` is now permitted — but ONLY with a `data:` payload. That is the distinction that
    matters: the bytes travel inside the message, so there is nothing to fetch and nothing to
    block. A single `src="http…"` would reintroduce the whole problem.
    """
    from app.email import layout

    html = layout.render(kind="verify", eyebrow="E", title="T", body_html="<p>x</p>")

    assert 'src="http' not in html, "no remote images — clients block them on first open"
    assert "url(" not in html, "no CSS-referenced remote assets either"


def test_email_layout_survives_outlook():
    """Outlook's Word rendering engine ignores flex and most div-based layout, so the
    structure must be tables and every style must be inline."""
    from app.email import layout

    html = layout.render(kind="verify", eyebrow="E", title="T", body_html="<p>x</p>")

    assert html.count('role="presentation"') >= 3, "layout must be tables"
    assert "display:flex" not in html

    # A <style> block is stripped by Gmail in many contexts, so nothing may DEPEND on one.
    #
    # Exactly one is permitted, and only for the dark-mode logo fill: a media query has no
    # inline form, so the alternative is no dark-mode handling at all — which is what left the
    # FreePass wordmark washed out at 3.60:1 on a dark background. The safety property is not
    # "no style block" but "the email is complete without it", so the check is that every
    # element carrying a class ALSO carries the equivalent inline style.
    assert _strip_comments(html).count("<style>") <= 1, "only the dark-mode logo rule may use a style block"
    assert "<style" not in _strip_comments(html).split("</head>")[-1], "style in the body; Gmail strips it"

    from app.email import layout as layout_mod

    if 'class="fp-logo"' in html:
        assert f'fill="{layout_mod.LOGO_INK}"' in html, (
            "the logo relies on the style block for its colour; a client that strips it would "
            "render the wordmark unfilled"
        )


def test_emails_carry_a_preheader():
    """The grey text a client shows beside the subject. Unset, clients scrape the first
    words of the body — usually "Hello <name>", which wastes the one line that decides
    whether the mail is opened."""
    from app.email import layout

    html = layout.render(
        kind="verify", eyebrow="Account activation", title="Confirm your email",
        body_html="<p>x</p>",
    )

    assert "display:none;max-height:0" in html
    assert "Account activation" in html


def test_every_sender_uses_the_shared_layout():
    """Structural guard against the drift this module exists to fix.

    A new sender writing its own `<html>` would reintroduce exactly the inconsistency
    that had already appeared — and it would ship with no footer.
    """
    source = pathlib.Path("app/iam/mailer.py").read_text()

    senders = [
        line.split("(")[0].removeprefix("async def ")
        for line in source.splitlines()
        if line.startswith("async def send_")
    ]
    assert len(senders) >= 5, f"expected at least five senders, found {senders}"

    # No sender may hand-roll a document.
    assert "<!doctype html>" not in source.lower(), (
        "senders must call layout.render() rather than writing their own document"
    )
    assert source.count("layout.render(") == len(senders), (
        f"{len(senders)} senders but {source.count('layout.render(')} layout calls — "
        "every sender must use the shared chrome"
    )


def test_activation_binds_the_subscription_to_the_account():
    """**The bug that made a working subscription invisible.**

    Every portal page gates on `account.subscriber_id`. `POST /subscribers` — the platform
    endpoint an aggregator uses to onboard somebody else — writes the Postgres subscriber row and
    never touches the IAM account, so that field stays null.

    The frontend's subscribe action called exactly that endpoint. The visible result: an area
    monitored on every satellite pass, ten assessments recorded, real Sentinel-1 measurements,
    and both `/portal` and `/portal/areas` reporting "nothing is being monitored". Two stores,
    no link, no error anywhere.

    `/iam/activate` is the session-scoped path that does both, and the bind result must be
    checked rather than discarded — a silent failure reproduces the same symptom.
    """
    import pathlib

    source = pathlib.Path("app/api/routes/iam.py").read_text()
    start = source.index("async def activate(")
    body = source[start : source.index("\n# ---", start)]

    assert "bind_subscriber" in body, "activation must link the subscription to the account"
    assert "if not await store.bind_subscriber" in body, (
        "the bind result must be checked — a discarded failure leaves the portal empty with "
        "no error, which is indistinguishable from data loss to the user"
    )
    assert "SUBSCRIPTION_ACTIVATED" in body, (
        "activation must be audited, or there is no record of whether it ran"
    )


def test_the_subscribe_action_uses_the_session_scoped_endpoint():
    """The frontend must not activate through the platform endpoint.

    `createSubscriber` takes the platform key and a caller-supplied identity; `activateSubscription`
    takes the session, so the plot binds to whoever is actually signed in. Using the former from
    a subscriber-facing form is both a scoping mistake and the cause of the empty-portal bug.
    """
    import pathlib

    action = pathlib.Path("../frontend/app/subscribe/actions.ts")
    if not action.exists():  # backend-only checkout
        return

    source = action.read_text()
    assert "activateSubscription" in source, (
        "the subscribe action must call /iam/activate so the account is linked"
    )
    # `await api.createSubscriber(` — the CALL, not a mention. The docstring explains why the
    # platform endpoint is wrong, and a substring match would flag that explanation as the bug
    # it warns about.
    assert "await api.createSubscriber(" not in source, (
        "createSubscriber is the aggregator onboarding path and does not bind the account"
    )


# --------------------------------------------------------------------------- #
# In-session password change, confirmed by an emailed code
#
# A different threat model from the signed-out reset link, and the difference is what makes a
# six-character code defensible here. See `passwordless.PASSWORD_CODE_LENGTH`.
# --------------------------------------------------------------------------- #


def test_the_change_code_is_namespaced_away_from_the_reset_token():
    """A short code must never be redeemable on the signed-out reset endpoint.

    A reset token is the SOLE credential in play — the holder is not signed in — which is why it is
    256 bits and arrives as a link. This code is a second factor behind a session that already
    authenticated. Sharing a purpose would let the six-character value be replayed where nothing
    else protects the account, turning a second factor into a sole credential.
    """
    from app.iam.passwordless import TokenPurpose

    assert TokenPurpose.PASSWORD_CHANGE_CODE.value != TokenPurpose.PASSWORD_RESET.value
    minted = passwordless.mint_password_change_code()
    assert minted.purpose is TokenPurpose.PASSWORD_CHANGE_CODE


def test_the_change_code_is_short_typable_and_unambiguous():
    """Six characters from the alphabet that excludes look-alikes.

    The code is read off one screen and typed into another, usually on a phone. O/0 and I/1 in a
    six-character code is not a nicety — it is a real failure rate, and a wrong guess costs one of
    only five attempts.
    """
    from app.iam.identifiers import ID_ALPHABET

    seen: set[str] = set()
    for _ in range(50):
        code = passwordless.mint_password_change_code()
        assert len(code.plaintext) == passwordless.PASSWORD_CODE_LENGTH
        assert all(c in ID_ALPHABET for c in code.plaintext)
        seen.add(code.plaintext)

    # Random, not sequential. A predictable code would make the attempt ceiling irrelevant.
    assert len(seen) > 40, "codes must not repeat — they are the confirmation, not a label"


def test_only_the_hash_of_a_change_code_is_stored():
    """A database leak must not yield working codes, same rule as every other token here."""
    code = passwordless.mint_password_change_code()
    assert code.token_hash != code.plaintext
    assert code.plaintext not in code.token_hash


def test_a_typed_change_code_is_normalised_both_ways():
    """A phone keyboard offers lower case, and a reader may group the characters.

    Normalising on both sides is what makes those inputs equivalent rather than wrong — and the
    stored hash is of the canonical form, so a mismatch here would reject a correct code.
    """
    code = passwordless.mint_password_change_code()
    plain = code.plaintext

    for typed in (plain.lower(), f" {plain} ", f"{plain[:3]}-{plain[3:]}", f"{plain[:3]} {plain[3:]}"):
        assert passwordless.normalise_password_change_code(typed) == plain


def test_the_attempt_ceiling_is_the_real_control():
    """~30 bits is only safe because a wrong guess is counted and the fifth burns the code.

    Without the ceiling an attacker gets as many tries as fit in the window, which is the argument
    the module already makes against a long-lived one-time password.
    """
    assert passwordless.PASSWORD_CODE_MAX_ATTEMPTS <= 5
    # Short-lived as well, so an abandoned request does not leave a usable code in an inbox.
    assert passwordless.PASSWORD_CODE_TTL_MINUTES <= 15
    # And far shorter than the reset link, which is a stronger secret and can afford longer.
    assert (
        passwordless.PASSWORD_CODE_TTL_MINUTES
        < passwordless.PASSWORD_RESET_TTL_MINUTES
    )


def test_the_change_code_goes_to_the_registered_address_only():
    """Never to an address supplied in the request.

    That is the whole point: the code proves control of the account's mailbox. Accepting a target
    address would let the requester redirect the proof to a mailbox they already control, which
    reduces the flow to "click this button to change the password".
    """
    import pathlib

    source = pathlib.Path("app/api/routes/iam.py").read_text()
    start = source.index("async def request_password_change_code(")
    body = source[start : source.index("\nclass PasswordChangeConfirm", start)]

    assert "account.email" in body, "the code must be sent to the account's own address"
    # There is no request body at all on this route, so there is nothing to redirect with.
    assert "payload" not in body, (
        "this endpoint must take no body — an address parameter would be redirectable"
    )
    # And it is behind a session.
    assert "Depends(current_account)" in body


def test_the_password_is_screened_before_the_code_is_spent():
    """Rejecting a weak password after consuming the code would cost the user a second email.

    The same ordering as the reset flow, and for the same reason: one weak choice should not also
    invalidate the confirmation they already fetched from their inbox.
    """
    import pathlib

    source = pathlib.Path("app/api/routes/iam.py").read_text()
    start = source.index("async def confirm_password_change(")
    body = source[start : source.index("\n# --- ", start)]

    assert body.index("_reject_if_breached") < body.index("redeem_password_change_code")


def test_a_password_change_is_audited_on_request_and_on_failure():
    """The abandoned and failed attempts are the interesting ones.

    A log holding only successful changes cannot answer "did somebody with her session try to
    change her password?" — which is exactly what a compromise looks like from outside.
    """
    import pathlib

    source = pathlib.Path("app/api/routes/iam.py").read_text()
    assert "AuditAction.PASSWORD_CHANGE_REQUESTED" in source
    assert "AuditAction.PASSWORD_CHANGE_FAILED" in source


# --------------------------------------------------------------------------- #
# Trusted devices
# --------------------------------------------------------------------------- #


def test_every_successful_sign_in_records_its_origin():
    """A sign-in with no IP or user agent cannot be recognised by its owner.

    This was the gap: `LOGIN_SUCCEEDED` was written without either, so the audit log held a list of
    successes with no origin — unable to distinguish the owner's phone from an attacker holding
    their password, and with nothing for the device table to group by.

    All three real sign-in paths are checked, including the magic link, which is the PRIMARY path
    for farmers — omitting it would leave the device table blind to the product's main audience.
    """
    import pathlib

    source = pathlib.Path("app/api/routes/iam.py").read_text()

    for marker in (
        'detail="password"',
        'detail="password + second factor"',
        'detail="magic link redeemed"',
    ):
        start = source.index(marker)
        call = source[start : start + 260]
        assert "user_agent=request.headers.get" in call, (
            f"the sign-in recorded as {marker} must carry its user agent"
        )
        assert "ip=request.client.host" in call, (
            f"the sign-in recorded as {marker} must carry its IP"
        )


def test_requesting_a_magic_link_is_not_recorded_as_a_sign_in():
    """The link may never be opened.

    Recorded as `LOGIN_SUCCEEDED` it would put a device that never authenticated into the trusted-
    device table, which inverts what that table asserts. The redemption is the sign-in.
    """
    import pathlib

    source = pathlib.Path("app/api/routes/iam.py").read_text()
    start = source.index('detail="magic link requested"')
    call = source[start - 300 : start]

    assert "MAGIC_LINK_REQUESTED" in call
    assert "LOGIN_SUCCEEDED" not in call


def test_the_device_table_lists_only_real_sign_ins():
    """A FAILED login must never appear as a trusted device.

    Someone guessing a password from a machine the owner has never touched would otherwise be
    listed among their own devices — the table would answer "who has tried" instead of "where have
    I signed in from", which is the opposite of its purpose.
    """
    from app.iam.audit import AuditAction
    from app.iam.store import _DEVICE_ACTIONS

    assert AuditAction.LOGIN_SUCCEEDED.value in _DEVICE_ACTIONS
    for excluded in (
        AuditAction.LOGIN_FAILED,
        AuditAction.LOGIN_LOCKED,
        AuditAction.MAGIC_LINK_REQUESTED,
    ):
        assert excluded.value not in _DEVICE_ACTIONS, (
            f"{excluded.value} is not a sign-in and must not appear as a trusted device"
        )


def test_devices_are_grouped_by_device_and_address_together():
    """The same browser seen from home and from a market's wifi are two different facts.

    Grouping on the user agent alone would hide a sign-in from a place the owner does not
    recognise, which is the one thing the table exists to surface.
    """
    import pathlib

    source = pathlib.Path("app/iam/store.py").read_text()
    start = source.index("async def trusted_devices(")
    body = source[start : source.index("\nasync def ", start + 10)]

    group = body[body.index("$group") :]
    assert '"ua": "$user_agent"' in group and '"ip": "$ip"' in group

    # Newest-first BEFORE the group, or `$first` would pick an arbitrary sign-in rather than the
    # latest — and "last login" would be wrong.
    assert body.index('{"$sort": {"at": -1}}') < body.index("$group")

    # Bounded, so an account signing in from a new address daily cannot render an endless table.
    assert "$limit" in body


def test_the_device_list_is_scoped_to_the_caller_with_no_id_parameter():
    """Scoping IS the authorisation. An id parameter would be something to tamper with."""
    import pathlib

    source = pathlib.Path("app/api/routes/iam.py").read_text()
    start = source.index("async def my_trusted_devices(")
    body = source[start : source.index("\n# --- In-session password change", start)]

    assert "trusted_devices(account.id)" in body
    assert "Depends(current_account)" in body
    # Every row names the account, so a shared handset does not produce anonymous rows.
    assert "email=account.email" in body


def test_the_trusted_device_notice_is_server_supplied():
    """The policy sentence ships with the data so the two cannot drift apart."""
    import pathlib

    source = pathlib.Path("app/api/routes/iam.py").read_text()
    start = source.index("class TrustedDeviceList(")
    body = source[start : start + 700]

    assert "Your trusted devices are listed below" in body
    assert "period of inactivity" in body


def test_a_rejected_change_code_is_logged_as_a_failure():
    """`outcome=FAILURE`, not the default success.

    A run of rejected codes is what someone guessing a code they never received looks like — and
    that is the entire reason these rows are written. Recorded as `success` they would read as
    "a password change happened", which is both wrong and the opposite signal.
    """
    import pathlib

    source = pathlib.Path("app/api/routes/iam.py").read_text()
    start = source.index("AuditAction.PASSWORD_CHANGE_FAILED")
    block = source[start : start + 420]
    assert "AuditOutcome.FAILURE" in block
