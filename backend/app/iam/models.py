"""IAM contracts — accounts, sessions and API keys.

**Two account kinds, one instance, and the distinction is a security boundary rather
than a pricing tier:**

| | `individual` | `commercial` |
|---|---|---|
| Onboards via | the web portal, self-service | the portal, then the REST API |
| Authenticates with | a session cookie from email + password | an API key in `X-SHELTER-API-Key` |
| May call the API | **never** | yes, scoped to its own customers |
| Manages | its own single profile | many individuals, shared with other tenants |

**Individuals have no API access at all.** That is enforced structurally: their
credential is a short-lived session token whose audience claim is `portal`, and the
API-key guard does not accept session tokens. A farmer cannot be socially-engineered
into pasting a key that does not exist, and a stolen session cannot be replayed
against the machine API.

**Commercial aggregators create individuals but never see their passwords.** An
aggregator-created individual has `password_hash=None` and cannot log into the portal
until they claim the account by email. Otherwise an aggregator could set a password and
impersonate a farmer, with no audit trail of who acted.

**Tenancy is many-to-many.** A subscriber may be served by several aggregators at once
— see `app/iam/tenancy.py`. The relationship lives in a `memberships` collection, not
as an owner field on the account, because none of those organisations owns the person.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated

from pydantic import (
    BaseModel,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_serializer,
)

from app.models.enums import Channel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AccountKind(str, Enum):
    """Who this account is, which determines how it may authenticate."""

    #: A farmer or household. Portal only, no API, one profile.
    INDIVIDUAL = "individual"
    #: A cooperative, insurer, state agency or NGO acting for many individuals.
    #: Portal *and* REST API, via an API key.
    COMMERCIAL = "commercial"
    #: A machine principal — the Next.js frontend server, a CI job, an ops script.
    #: No password, no portal login, no human owner; authenticates only with a scoped
    #: API key. This is what replaces the single shared `X-SHELTER-Key`.
    SERVICE = "service"


class AccountStatus(str, Enum):
    #: Registered, email not yet confirmed. May log in, cannot receive alerts —
    #: sending to an unverified address is how a service becomes a spam vector.
    PENDING_VERIFICATION = "pending_verification"
    ACTIVE = "active"
    #: Operator-suspended. Distinct from `disabled`, which is self-service.
    SUSPENDED = "suspended"
    #: Self-deactivated. Retained so re-activation keeps the same areas.
    DISABLED = "disabled"


class ApiKeyScope(str, Enum):
    """What an aggregator's key may do.

    Separated so a key embedded in a partner's batch importer cannot also delete
    their customers. Least privilege is the whole point of having scopes at all —
    a single omnipotent key would make rotation the only response to any leak.
    """

    #: Read own customers, their areas and their alerts.
    READ = "customers:read"
    #: Create and update own customers.
    WRITE = "customers:write"
    #: Trigger an immediate scan for an own customer's area.
    SCAN = "scan:trigger"
    #: Manage webhook subscriptions belonging to this aggregator.
    WEBHOOKS = "webhooks:manage"

    # --- Platform scopes: service accounts only ---------------------------
    #
    # Deliberately a separate namespace (`platform:*`) from the tenant scopes above.
    # An aggregator key must never be able to hold one of these, because they are not
    # scoped to a tenant — `platform:subscribers:write` can register a subscriber for
    # anyone, which is exactly the unbounded authority the shared key had.
    # `iam/deps.py` refuses to mint a platform scope on a non-service account.
    #
    #: Register, update and deactivate any subscriber. What the portal's signup flow
    #: needs, and nothing more.
    PLATFORM_SUBSCRIBERS = "platform:subscribers:write"
    #: Read any subscriber, alert or assessment. The dashboard's needs.
    PLATFORM_READ = "platform:read"
    #: Run a synchronous assessment. Costs real catalogue quota, so it is separate
    #: from PLATFORM_READ rather than bundled with it.
    PLATFORM_ASSESS = "platform:assess"
    #: Dispatch an alert to a subscriber's channels — including the NIGCOMSAT
    #: broadcast escalation. **The most dangerous scope in the system**: it reaches
    #: people directly. Never granted by default, and never to the frontend, which
    #: has no legitimate reason to page a district.
    PLATFORM_BROADCAST = "platform:broadcast"
    #: Operational surfaces: verification sweeps, chat economics, webhook engine
    #: administration.
    PLATFORM_OPERATE = "platform:operate"


#: Scopes that only a SERVICE account may hold.
#:
#: Enforced at mint time, not merely at use time. A tenant key carrying an unscoped
#: platform permission would be able to act outside its own customers, which is the
#: precise failure the shared key represented.
PLATFORM_SCOPES: frozenset[ApiKeyScope] = frozenset({
    ApiKeyScope.PLATFORM_SUBSCRIBERS,
    ApiKeyScope.PLATFORM_READ,
    ApiKeyScope.PLATFORM_ASSESS,
    ApiKeyScope.PLATFORM_BROADCAST,
    ApiKeyScope.PLATFORM_OPERATE,
})

#: What the Next.js frontend server actually needs, and no more.
#:
#: Derived by reading `frontend/lib/api.ts`: it calls `/health`, `/risk/assess` and
#: `/subscribers`. So the portal needs to register subscribers, read for the
#: dashboard, and run an assessment — it does **not** need PLATFORM_BROADCAST or
#: PLATFORM_OPERATE. The shared key granted both. This is the least-privilege set
#: a `frontend` service account should be provisioned with.
FRONTEND_SCOPES: tuple[ApiKeyScope, ...] = (
    ApiKeyScope.PLATFORM_SUBSCRIBERS,
    ApiKeyScope.PLATFORM_READ,
    ApiKeyScope.PLATFORM_ASSESS,
)

#: The scopes a newly-created key gets when none are requested.
#:
#: Read + write, but **not** `scan:trigger` and **not** `webhooks:manage`. Scanning
#: costs real catalogue quota and webhook management can redirect another system's
#: alert stream, so both must be asked for deliberately rather than arriving by
#: default in a key someone pasted into a script.
DEFAULT_KEY_SCOPES: tuple[ApiKeyScope, ...] = (ApiKeyScope.READ, ApiKeyScope.WRITE)


Password = Annotated[str, StringConstraints(min_length=12, max_length=200)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=120)]


# --------------------------------------------------------------------------- #
# Onboarding requests
# --------------------------------------------------------------------------- #


class IndividualSignup(BaseModel):
    """The 60-second self-service flow: name, contact, password, plot, language.

    Deliberately flat and small. Every additional required field measurably reduces
    completion, and this is aimed at a farmer on a feature phone over a metered
    connection — so the area and language are collected here rather than in a second
    step, and nothing else is mandatory.
    """

    first_name: ShortText
    last_name: ShortText
    email: EmailStr
    #: E.164. Optional at signup: an email-only subscriber is fully functional, and
    #: demanding a number before the value is demonstrated loses signups.
    phone: str | None = Field(default=None, max_length=20)
    password: Password

    language: str = Field(default="en", min_length=2, max_length=8)
    #: Preferred channel. Email is the default because it needs no gateway
    #: credential and no template approval, so it works on day one everywhere.
    preferred_channel: Channel = Channel.EMAIL

    @field_validator("phone")
    @classmethod
    def _e164(cls, value: str | None) -> str | None:
        """Normalise to E.164 or reject.

        Stored unnormalised, a number is undeliverable on WhatsApp and Signal, which
        both require the `+<country><number>` form — and the failure surfaces only
        when an alert is dispatched, which is the worst possible moment.
        """
        if value is None or not value.strip():
            return None
        cleaned = "".join(c for c in value if c.isdigit() or c == "+")
        if not cleaned.startswith("+"):
            # Nigeria is the launch market, so a bare local number is the common
            # input. 0803... -> +234803...
            cleaned = "+234" + cleaned.lstrip("0")
        if not (8 <= len(cleaned) <= 16):
            raise ValueError("phone must be a valid E.164 number, e.g. +2348031234567")
        return cleaned

    @field_validator("password")
    @classmethod
    def _not_obvious(cls, value: str) -> str:
        """Reject the handful of passwords that appear in every breach corpus.

        A 12-character minimum plus Argon2id is the real defence; this only catches
        `password1234` and friends. A full complexity policy is deliberately absent —
        it pushes people toward `Passw0rd!` and a written note, which is worse.
        """
        lowered = value.lower()
        for bad in ("password", "shelter", "123456", "qwerty", "letmein", "welcome"):
            if bad in lowered:
                raise ValueError(
                    "password contains a commonly-guessed word; "
                    "a passphrase of three unrelated words is stronger and easier"
                )
        return value


class CommercialSignup(BaseModel):
    """Aggregator onboarding: an organisation plus its first operator.

    The operator's own credentials are the *portal* login. The API key is minted
    separately and explicitly (`POST /iam/api-keys`) rather than at signup, so a key
    only ever exists because someone asked for one — which makes an unused
    aggregator account a smaller liability than one holding a forgotten live key.
    """

    organisation: ShortText
    #: The aggregator's kind, used only for reporting. Access is governed by
    #: `AccountKind.COMMERCIAL`, not by this.
    sector: str = Field(default="cooperative", max_length=60)

    contact_first_name: ShortText
    contact_last_name: ShortText
    email: EmailStr
    phone: str | None = Field(default=None, max_length=20)
    password: Password

    _normalise_phone = field_validator("phone")(IndividualSignup._e164.__func__)
    _check_password = field_validator("password")(IndividualSignup._not_obvious.__func__)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


# --------------------------------------------------------------------------- #
# Stored documents
# --------------------------------------------------------------------------- #


class Account(BaseModel):
    """An identity document, as stored in Atlas and returned to the portal.

    **`password_hash` is not a field here.** It is read and written only by
    `iam/store.py`, and every read path that produces an `Account` projects it away.
    A model that could carry a hash would eventually be serialised into a response —
    this makes that mistake impossible rather than merely discouraged.
    """

    id: str
    kind: AccountKind
    status: AccountStatus = AccountStatus.PENDING_VERIFICATION

    email: EmailStr
    first_name: str
    last_name: str
    phone: str | None = None

    #: Set for commercial accounts only.
    organisation: str | None = None
    sector: str | None = None

    language: str = "en"
    preferred_channel: Channel = Channel.EMAIL

    #: The `Subscriber` this account owns, once activated. None until the plot is
    #: bound — an account can exist without a subscription, which is what makes the
    #: signup step independent of the activation step.
    subscriber_id: str | None = None

    #: **Deliberately absent: there is no `managed_by`.** Tenancy is a many-to-many
    #: `memberships` edge (`app/iam/tenancy.py`), because a farmer's cooperative,
    #: insurer and state extension service may all serve them at once and none of them
    #: owns the identity. A single owner field made the first aggregator to register
    #: someone their permanent owner and turned a second aggregator's legitimate
    #: onboarding into "that address is taken".

    email_verified: bool = False
    created_at: datetime = Field(default_factory=_now)
    last_login_at: datetime | None = None

    #: Chosen avatar, or None to use the one derived from `id`.
    #:
    #: Stored as the emoji itself rather than an index, so the meaning survives a change to
    #: `avatar.AVATAR_EMOJI` — an index would silently repoint every avatar if the list were
    #: reordered. Validated against that list on read (`avatar.resolve`), so a value that is
    #: no longer offered degrades to the derived avatar instead of rendering a stray glyph.
    avatar_emoji: str | None = None
    avatar_color: str | None = None

    @model_serializer(mode="wrap")
    def _serialise_with_avatar(self, handler):  # type: ignore[no-untyped-def]
        """Fill `avatar_emoji` / `avatar_color` with the derived values when unset.

        A `@property` would not appear in the JSON at all, and returning `null` to the
        client would push the derivation into the frontend — where it would have to
        reimplement the SHA-256 emoji mapping, and the two could then disagree about what a
        given account looks like. Resolving it here means one definition, on the side that
        owns the curated emoji list.

        `mode="wrap"` so the normal serialisation runs first and this only substitutes the
        two fields, rather than reimplementing the dump.
        """
        from app.iam import avatar as avatar_mod

        data = handler(self)
        art = avatar_mod.resolve(self.id, self.avatar_emoji, self.avatar_color)
        data["avatar_emoji"] = art["emoji"]
        data["avatar_color"] = art["color"]
        return data

    @property
    def avatar(self) -> dict[str, str]:
        """The emoji and colour to display, for server-side callers."""
        from app.iam import avatar as avatar_mod

        return avatar_mod.resolve(self.id, self.avatar_emoji, self.avatar_color)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def aggregator_id(self) -> str | None:
        """This account's id when it is acting as an aggregator, else None.

        An alias rather than a second stored field, deliberately: one identity has one
        id, and duplicating it would create two values that can disagree. The alias
        exists because the *relationship* is what the name describes — `memberships`
        stores `aggregator_id`, partner documentation says "your aggregator id", and a
        reader should not have to know it is the same string as `account.id`.

        None for an individual, so a caller cannot accidentally treat a farmer's id as
        a tenant scope.
        """
        return self.id if self.kind is AccountKind.COMMERCIAL else None

    @property
    def can_use_api(self) -> bool:
        """Only commercial accounts may hold an API key.

        Consulted by the key-mint endpoint. Individuals are portal-only, and this is
        the property that says so in one place rather than as a scattered `if`.
        """
        return self.kind is AccountKind.COMMERCIAL


class ApiKeyPublic(BaseModel):
    """An API key as listed back — **never** including the secret.

    There is deliberately no field that could hold the plaintext or its hash. The
    plaintext is not stored at all, and `key_hash` is projected away at the query, so
    a future serialisation mistake cannot leak what never entered the process.
    """

    id: str
    account_id: str
    name: str
    #: First 8 characters of the body — enough to identify which key a log line or a
    #: leak report refers to, far too few to reconstruct one.
    hint: str
    #: Last 4 characters of the full key. GitHub and Stripe both show this; it is what
    #: lets someone match a key in their own config against a key in this list.
    last_four: str = ""
    scopes: list[ApiKeyScope]
    #: The workspace this key is scoped to, and therefore which activated intelligence tracks
    #: it can reach. None on a SERVICE account key (no workspace) and on keys minted before
    #: workspaces existed — those resolve to the organisation's default, so an older
    #: integration keeps working rather than losing its entitlement at deploy time.
    workspace_id: str | None = None
    #: active | rotating | revoked | expired. `rotating` means superseded but still
    #: valid until `grace_expires_at`, so the state is legible in the portal rather
    #: than a key mysteriously working after being replaced.
    status: str = "active"
    created_at: datetime
    last_used_at: datetime | None = None
    #: Total authenticated requests. Surfaced so an operator can see that a key they
    #: are about to revoke is genuinely unused.
    use_count: int = 0
    expires_at: datetime | None = None
    grace_expires_at: datetime | None = None
    #: Governance flags from `keys.key_health` — never_used, stale, expiring_soon.
    #: Computed rather than stored, so it cannot go stale.
    health: dict = Field(default_factory=dict)


class ApiKeyCreated(ApiKeyPublic):
    """Mint response — **the only time the secret is ever returned.**"""

    key: str = Field(
        description="Store this now. Only a SHA-256 hash is kept, so this value "
        "cannot be retrieved again by anyone, including SHELTER support. A lost key "
        "must be rotated, not recovered."
    )
    warning: str = (
        "This is the only time this key will be shown. Copy it now and store it in "
        "your secret manager. If you lose it, rotate the key — it cannot be recovered."
    )


class ApiKeyCreate(BaseModel):
    name: ShortText = Field(description="What this key is for, e.g. 'batch importer'")
    #: Which workspace this key belongs to. Omitted means the organisation's default.
    #:
    #: The scopes a caller may request are resolved from their role **on this workspace**, not
    #: from the union across all of them — otherwise an owner of one project could mint a
    #: write key naming a project where they are View-Only, and the workspace boundary would
    #: hold everywhere except at the one place that produces a credential.
    workspace_id: str | None = Field(
        default=None,
        description="Workspace this key is scoped to. Defaults to your default workspace. "
        "The scopes you may request depend on your role on this workspace.",
    )
    scopes: list[ApiKeyScope] = Field(
        default_factory=lambda: list(DEFAULT_KEY_SCOPES),
        description="Least privilege. `scan:trigger` and `webhooks:manage` are "
        "excluded by default because both have side effects beyond reading data.",
    )
    #: Expiry in days, or None to never expire.
    #:
    #: **Both are supported choices and the portal makes the user pick**, rather than
    #: defaulting silently. A key that expires unexpectedly breaks a partner's
    #: integration at an unpredictable moment; a key that never expires is a permanent
    #: liability. Neither default is right for everyone, so the trade-off is stated and
    #: chosen — the same approach GitHub takes with PAT expiry.
    expires_in_days: int | None = Field(
        default=None,
        ge=1,
        le=3650,
        description="Days until this key expires. Omit or null for a key that never "
        "expires (revoke or rotate it manually instead).",
    )


class ApiKeyRotate(BaseModel):
    """Rotation request.

    Rotation mints a replacement and puts the current key into a grace window, so the
    partner can deploy the new one before the old one dies. Delete-then-create forces a
    choice between an outage and leaving a compromised key live — and faced with that,
    people leave it live.
    """

    grace_hours: int | None = Field(
        default=None,
        ge=0,
        le=168,
        description="Hours the old key keeps working. 0 kills it immediately (use "
        "this when a key has leaked). Defaults to IAM_KEY_ROTATION_GRACE_HOURS.",
    )


class SessionToken(BaseModel):
    """Portal session, returned on login and signup."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Seconds until this token stops working")
    account: Account
