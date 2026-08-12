"""IAM endpoints — `/shelter/v1/api/iam/*`.

Three audiences on one router, and which guard protects a route is the whole design:

**1. Public (no credential).** Signup, login, email verification. These must be open —
they are how a credential is obtained in the first place. Login is throttled per
email; signup is idempotent-ish in that a duplicate address returns the same generic
response rather than confirming the address is taken.

**2. Portal session (`Authorization: Bearer`).** Self-service: read your own profile,
change language and channels, activate a plot, mint an API key if you are commercial.
Every route is scoped to *the caller's own* account by construction — no route takes an
account id from the client for its own data.

**3. Aggregator API key (`X-SHELTER-API-Key`).** The B2B surface: create and read your
own customers. Tenancy is a many-to-many `memberships` edge, so **a subscriber may be
served by several aggregators at once** — their cooperative, their insurer and the state
extension service. Every scoped query resolves that edge **inside** the query, so
another tenant's customer is never a candidate result.

**Individuals never receive an API key.** `POST /iam/api-keys` checks
`account.can_use_api` and returns 403 for an individual. That is deliberate product
design as much as security: a farmer has nothing to integrate, and a credential they
cannot use is a credential that can only be phished out of them.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from pydantic import BaseModel, EmailStr, Field, field_validator

from app.api.area_input import (
    normalise_area,
    reject_unavailable_channel,
    reject_unavailable_channels,
)
from app.api.deps import require_api_key
from app.config import settings
from app.iam import (
    attribution,
    breached,
    geo,
    idle,
    mailer,
    passwordless,
    platform,
    security,
    store,
    useragent,
)
from app.iam import roles as roles_mod
from app.iam import team as team_mod
from app.iam import tracks as tracks_mod
from app.iam.audit import AuditAction, AuditOutcome
from app.iam.deps import (
    Aggregator,
    Session,
    current_account,
    current_aggregator,
    current_session,
    owned_account,
    password_setup_session,
    require_permission,
    require_scope,
    require_workspace_permission,
    verified_account,
)
from app.iam.models import (
    FRONTEND_SCOPES,
    PLATFORM_SCOPES,
    Account,
    AccountKind,
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyPublic,
    ApiKeyRotate,
    ApiKeyScope,
    CommercialSignup,
    IndividualSignup,
    LoginRequest,
    Password,
    SessionToken,
)
from app.iam.roles import (
    ROLE_LABELS,
    Permission,
    Role,
    permissions_for,
    scopes_for,
)
from app.logging_config import get_logger
from app.models.enums import Channel, DeliveryMode, Severity, SubscriberKind
from app.models.schemas import AreaOfInterest, ChannelBinding, Subscriber
from app.store import cache, repository

log = get_logger(__name__)
router = APIRouter(prefix="/iam", tags=["iam"])


def _require_store() -> None:
    if not store.available():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The IAM service is not configured (MONGO_URL is unset). The satellite "
            "pipeline is unaffected.",
        )


# --------------------------------------------------------------------------- #
# Public — signup, login, verification
# --------------------------------------------------------------------------- #


class ActivationRequest(BaseModel):
    """Bind a plot and delivery channels to the calling account.

    Step two of the 60-second flow: signup created the identity, this makes it
    autonomous. Separate endpoints because the portal collects them on separate
    screens — and because an account without a plot is a valid state (someone who
    signed up and has not chosen their field yet).
    """

    area: AreaOfInterest
    #: Extra channels beyond the account's preferred one. Email is always included
    #: from the account, so an empty list still yields a working subscription.
    channels: list[ChannelBinding] = Field(default_factory=list)


class SignupResponse(BaseModel):
    account: Account
    session: SessionToken | None = None
    verification_email_sent: bool
    next_step: str


@router.post(
    "/signup/individual",
    response_model=SignupResponse,
    status_code=status.HTTP_201_CREATED,
)
async def signup_individual(
    payload: IndividualSignup, request: Request
) -> SignupResponse:
    """Self-service signup. No API key required — this is how a farmer starts.

    A session is issued immediately, before email verification, so the portal can
    continue straight to dropping a pin. Verification gates *alert delivery*
    (`verified_account`), not navigation — blocking the flow on an inbox round trip
    is where signup funnels die, and an unverified account can do nothing that
    reaches a third party.
    """
    _require_store()

    await _reject_if_breached(payload.password)

    # Refuse an undeliverable channel before the account exists, so a rejected choice leaves no
    # half-created identity behind — and so the person is told while they are still on the form.
    reject_unavailable_channel(payload.preferred_channel)

    account, token = await store.create_account(
        kind=AccountKind.INDIVIDUAL,
        email=payload.email,
        first_name=payload.first_name,
        last_name=payload.last_name,
        password=payload.password,
        phone=payload.phone,
        language=payload.language,
        preferred_channel=payload.preferred_channel.value,
    )

    if account is None:
        # Deliberately generic: confirming "that address is registered" turns this
        # endpoint into an account-existence oracle over a public form.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "That email address cannot be registered. If it is yours, try logging in "
            "or resetting your password.",
        )

    sent = await mailer.send_verification(
        account.email,
        account.first_name,
        token or "",
        context=_request_context(request),
    )
    access_token, expires_in = security.issue_session(account.id, account.kind.value)

    # ACCOUNT_CREATED was a declared-but-never-written audit action: the enum member
    # existed, the portal's activity page had a label for it, and no code ever emitted
    # one — so a subscriber's log began at their first sign-in with no record of the
    # account being created at all. The first entry in an audit trail should be its
    # own creation.
    await store.record_audit(
        account_id=account.id,
        action=AuditAction.ACCOUNT_CREATED,
        actor_kind="self",
        detail=f"self-service signup ({account.kind.value})",
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    log.info("individual signed up", extra={"account_id": account.id})
    return SignupResponse(
        account=account,
        session=SessionToken(
            access_token=access_token, expires_in=expires_in, account=account
        ),
        verification_email_sent=sent,
        next_step="POST /iam/activate with your plot to start autonomous monitoring",
    )


@router.post(
    "/signup/commercial",
    response_model=SignupResponse,
    status_code=status.HTTP_201_CREATED,
)
async def signup_commercial(
    payload: CommercialSignup, request: Request
) -> SignupResponse:
    """Aggregator onboarding. Also public — a cooperative self-serves too.

    No API key is minted here. Keys are created explicitly afterwards, so an
    aggregator that signs up and never integrates is not left holding a live
    credential nobody is watching.
    """
    _require_store()
    await _reject_if_breached(payload.password)

    account, token = await store.create_account(
        kind=AccountKind.COMMERCIAL,
        email=payload.email,
        first_name=payload.contact_first_name,
        last_name=payload.contact_last_name,
        password=payload.password,
        phone=payload.phone,
        organisation=payload.organisation,
        sector=payload.sector,
    )

    if account is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "That email address cannot be registered. If it is yours, try logging in.",
        )

    sent = await mailer.send_verification(
        account.email,
        account.first_name,
        token or "",
        context=_request_context(request),
    )
    access_token, expires_in = security.issue_session(account.id, account.kind.value)

    await store.record_audit(
        account_id=account.id,
        action=AuditAction.ACCOUNT_CREATED,
        actor_kind="self",
        detail=f"self-service signup (commercial: {account.organisation})",
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    log.info(
        "commercial account signed up",
        extra={"account_id": account.id, "organisation": account.organisation},
    )
    return SignupResponse(
        account=account,
        session=SessionToken(
            access_token=access_token, expires_in=expires_in, account=account
        ),
        verification_email_sent=sent,
        next_step=(
            "Confirm your email, then POST /iam/api-keys to mint a key and "
            "POST /iam/customers to onboard your first customer"
        ),
    )


class LoginChallenge(BaseModel):
    """Returned when a password is correct but a second factor is still required.

    A distinct response shape rather than a 401, because the two mean different things:
    a 401 says "those credentials are wrong", this says "they were right, now prove the
    second factor". Conflating them makes a 2FA prompt indistinguishable from a typo.
    """

    mfa_required: bool = True
    #: Short-lived, single-purpose token proving the password step passed. NOT a
    #: session: it carries a distinct audience so `current_account` rejects it, which
    #: means an intercepted challenge token cannot read data without the code.
    challenge_token: str
    methods: list[str] = Field(default_factory=lambda: ["totp", "recovery_code"])
    detail: str = "Enter the 6-digit code from your authenticator app."


class MfaVerifyRequest(BaseModel):
    challenge_token: str = Field(min_length=10, max_length=500)
    code: str = Field(min_length=6, max_length=12)


@router.post("/login", response_model=SessionToken | LoginChallenge)
async def login(
    payload: LoginRequest, request: Request
) -> SessionToken | LoginChallenge:
    """Exchange credentials for a portal session.

    Throttled per email address rather than per IP: farmers in one district commonly
    share a NAT, so IP-based lockout would let one attacker lock out a whole village.
    Argon2 makes each attempt cost ~50 ms, and the lockout stops that cost being
    turned back on us as a denial of service.
    """
    _require_store()

    if await store.is_locked_out(payload.email):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many failed attempts. Try again in "
            f"{settings.iam_login_lockout_minutes} minutes.",
        )

    account = await store.authenticate(payload.email, payload.password)
    if account is None:
        failures = await store.register_failed_login(payload.email)
        log.info(
            "failed login",
            extra={"failures": failures, "ip": request.client.host if request.client else None},
        )
        # One message for "no such account" and "wrong password" — see
        # `store.authenticate` for why the timing matches too.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Email or password is incorrect."
        )

    await store.clear_failed_logins(payload.email)

    # --- Second factor ------------------------------------------------------
    # Enforced when the account has enrolled, and — if the deployment requires it —
    # for commercial accounts regardless. A commercial key can read hundreds of
    # farmers' records, so a password alone is a weaker gate than that data deserves.
    #
    # Never forced on an individual: mandatory 2FA on a farmer with one handset and no
    # authenticator app locks them out of their own flood warnings.
    totp = await store.totp_state(account.id)
    must_challenge = totp.get("enabled") or (
        settings.iam_totp_required_for_commercial
        and account.kind is AccountKind.COMMERCIAL
        and totp.get("enabled")
    )

    if must_challenge:
        # A short-lived token scoped to the MFA step. Deliberately NOT a session: it
        # carries a distinct audience so `current_account` rejects it, meaning an
        # intercepted challenge token cannot read data without the code.
        challenge, _ = security.issue_session(
            account.id, f"mfa:{account.kind.value}", minutes=5
        )
        return LoginChallenge(challenge_token=challenge)

    if (
        settings.iam_totp_required_for_commercial
        and account.kind is AccountKind.COMMERCIAL
        and not totp.get("enabled")
    ):
        # Required by policy but not yet enrolled. A session is still issued, because
        # enrolling *needs* one — refusing here would make the requirement
        # unsatisfiable. The flag tells the portal to route straight to enrolment.
        token, expires_in = security.issue_session(account.id, account.kind.value)
        return SessionToken(
            access_token=token, expires_in=expires_in, account=account
        )

    token, expires_in = security.issue_session(account.id, account.kind.value)
    # The device and address are recorded on a SUCCESSFUL sign-in, not only on a failed one.
    #
    # They were missing here, which made the audit log unable to answer the question it exists for:
    # "where have I signed in from?" A log of successes with no origin cannot distinguish the
    # owner's phone from an attacker holding their password, and `store.trusted_devices` — which
    # derives the Security page's device table from these rows — had nothing to group by.
    await store.record_audit(
        account_id=account.id,
        action=AuditAction.LOGIN_SUCCEEDED,
        detail="password",
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return SessionToken(access_token=token, expires_in=expires_in, account=account)


@router.post("/auth/mfa/verify", response_model=SessionToken)
async def verify_mfa(payload: MfaVerifyRequest, request: Request) -> SessionToken:
    """Exchange a challenge token plus a TOTP or recovery code for a session.

    The challenge token proves the password step already passed, so this endpoint never
    sees a password — which means a leaked challenge token grants nothing without the
    second factor, and a leaked code grants nothing without the token.
    """
    _require_store()

    claims = security.read_session(payload.challenge_token)
    if claims is None or not str(claims.get("kind", "")).startswith("mfa:"):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "That sign-in attempt has expired. Start again.",
        )

    account_id = claims.get("sub", "")
    if not await store.verify_second_factor(account_id, payload.code):
        await store.record_audit(
            account_id=account_id,
            action=AuditAction.LOGIN_FAILED,
            outcome=AuditOutcome.FAILURE,
            detail="second factor rejected",
        )
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "That code did not match. Check your device's clock, or use a recovery code.",
        )

    account = await store.get_account(account_id)
    if account is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign-in failed.")

    token, expires_in = security.issue_session(account.id, account.kind.value)
    await store.record_audit(
        account_id=account.id, action=AuditAction.LOGIN_SUCCEEDED,
        detail="password + second factor",
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return SessionToken(access_token=token, expires_in=expires_in, account=account)


async def _reject_if_breached(password: str) -> None:
    """Refuse a password known to be exposed in a public breach.

    ## Why the check is enforced here as well as in the browser

    `POST /iam/password/check` is what makes the signup form warn while someone types. It is
    a convenience, and a convenience is bypassable — a client that skips it, or a direct API
    call, would otherwise set a password from a cracking dictionary. So the authority lives
    on the write path.

    Only applied to password *creation* (signup, reset), never to login. Blocking a sign-in
    because an existing password later appeared in a breach would lock a subscriber out of
    their own flood warnings with no way back in — the right response there is to prompt a
    change after they are safely inside, not to bar the door.

    Fails open: `breached.check` returns `(False, 0)` when HIBP is unreachable, so an outage
    cannot block signups.
    """
    is_breached, times_seen = await breached.check(password)
    if not is_breached:
        return

    # The wording is the user-facing contract, and it is careful on two points: it says what
    # we compared (part of a hash, not the password) so the message does not read as "we
    # have your password", and it explains why a unique password matters rather than just
    # refusing.
    seen = f" It appears {times_seen:,} times in known breaches." if times_seen else ""
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "Please choose a different password. We compared part of a hash of your password "
        "with data from Have I Been Pwned, and it appears the password you entered may "
        "have been exposed on another website." + seen + " For the best security, we want "
        "you to use a unique password.",
    )


class PasswordCheckRequest(BaseModel):
    """A candidate password to screen. Never stored, never logged."""

    #: Not the `Password` type: this endpoint deliberately accepts anything the user has
    #: typed so far, including a 3-character fragment. Rejecting short input with a 422
    #: would make the live check silent until the field was already valid — exactly when it
    #: stops being useful.
    password: str = Field(min_length=1, max_length=200)


class PasswordCheckResponse(BaseModel):
    breached: bool
    #: HIBP's occurrence count. Surfaced because it changes the appropriate message: 3 is a
    #: coincidence, 3 million means the password is in every cracking dictionary.
    times_seen: int
    #: False when screening is disabled or HIBP was unreachable. The UI must not claim a
    #: password is safe on the strength of a check that did not happen.
    checked: bool


@router.post("/password/check", response_model=PasswordCheckResponse)
async def check_password(payload: PasswordCheckRequest) -> PasswordCheckResponse:
    """Screen a password against Have I Been Pwned, without disclosing it.

    ## Why this is a backend endpoint rather than a direct call from the browser

    The browser *could* call `api.pwnedpasswords.com` itself — the k-anonymity protocol is
    designed for exactly that. Routing it through here buys three things:

      * **No third-party request from a subscriber's device.** A direct call would put every
        signup in HIBP's (and any intermediary's) logs with the subscriber's own IP. Here it
        carries the server's.
      * **A shared cache.** Prefix buckets are public ranges; caching them server-side means
        a burst of signups costs one upstream request rather than one per person, which
        matters on a metered connection at the subscriber's end too.
      * **One place to disable it.** `HIBP_ENABLED=false` stops it everywhere.

    ## Unauthenticated, deliberately

    It has to be: the check runs on the signup form, before an account exists. That is safe
    because the endpoint is a pure function of its input and reveals nothing an attacker
    could not learn by querying HIBP directly — which they can, without us.

    **The password is never stored, logged or audited.** No `record_audit` call, and no
    `log` line receives the value. An audit entry here would create a permanent record of
    candidate passwords, which is precisely the thing this feature exists to protect against.
    """
    if not settings.hibp_enabled:
        return PasswordCheckResponse(breached=False, times_seen=0, checked=False)

    is_breached, times_seen = await breached.check(payload.password)
    # `checked` distinguishes "we looked and it is clean" from "we could not look". The
    # frontend must not show a green tick for the second case.
    return PasswordCheckResponse(
        breached=is_breached, times_seen=times_seen, checked=True
    )


# --------------------------------------------------------------------------- #
# Session state — idle tracking, activity, audited events
#
# The idle window is enforced in `deps.current_account`, which reads but never writes.
# These are the only endpoints that change session state, and the split is the whole
# design: if any authenticated request extended the window, a dashboard polling its own
# status endpoint would keep a session alive with nobody in the room.
# --------------------------------------------------------------------------- #


class SessionStateResponse(BaseModel):
    """What the browser needs to run an honest countdown.

    `seconds_remaining` is computed server-side deliberately. A client counting down from
    its own stored timestamp drifts, and is simply wrong after the machine sleeps — which
    is the main case an idle timeout exists to handle.
    """

    account_id: str
    #: Seconds of idle time left before the session is refused.
    seconds_remaining: int
    #: The full window, so the UI can show "15 minutes" without hardcoding it.
    idle_window_seconds: int
    #: When to start warning. Server-owned so the two ends cannot disagree.
    warning_at_seconds: int
    #: False when no last-seen record exists — the cache is unavailable and the idle
    #: window is not being enforced. Surfaced rather than hidden: an operator watching
    #: this can tell "not enforced" from "plenty of time", which otherwise look the same.
    tracked: bool

    #: Who, so the portal sidebar can show a name and avatar without a second request.
    full_name: str = ""
    email: str = ""
    avatar_emoji: str = ""
    avatar_color: str = ""

    #: What they are signed in from — "Mobile · Android 14 · Chrome 141".
    #:
    #: Parsed from THIS request's User-Agent rather than stored at login, so it describes
    #: the device actually in use. A session resumed on a different device would otherwise
    #: keep reporting the first one.
    device: str = ""

    #: Approximate location, or None. `location_source` says how it was derived so the UI
    #: can attach the right caveat — "From your IP address" is an honest hedge, and a
    #: confident city name with no hedge is not.
    location: str | None = None
    location_source: str | None = None


@router.get("/session", response_model=SessionStateResponse)
async def session_state(
    request: Request, session: Session = Depends(current_session)
) -> SessionStateResponse:
    """Current idle state. **Read-only — calling this does not extend the session.**

    That is the load-bearing property. The frontend polls this to drive its countdown, and
    if polling refreshed the window the timeout could never fire on a dashboard that is
    open but unattended.
    """
    _require_store()

    state = await idle.check(session.jti)
    return _session_response(session, state, request)


def _session_response(
    session: Session, state: idle.IdleState, request: Request
) -> SessionStateResponse:
    """Build the session payload once, so every session endpoint agrees.

    Three endpoints return this shape. Composing it separately in each was how the idle
    fields and the identity fields could drift apart — and a sidebar that shows a different
    name from the account menu is the kind of bug nobody notices until it matters.
    """
    account = session.account
    ua = useragent.parse(request.headers.get("user-agent"))
    client_ip = request.client.host if request.client else None
    location = geo.lookup(client_ip)
    art = account.avatar

    return SessionStateResponse(
        account_id=account.id,
        seconds_remaining=state.seconds_remaining,
        idle_window_seconds=idle.window_seconds(),
        warning_at_seconds=idle.warning_seconds(),
        tracked=state.tracked,
        full_name=account.full_name,
        email=account.email,
        avatar_emoji=art["emoji"],
        avatar_color=art["color"],
        device=ua.summary,
        # Falls back to the raw IP when GeoLite2 is not installed. Showing the address is
        # less friendly than a city but strictly more useful than showing nothing — a
        # subscriber can still recognise "that is not my network".
        location=location.label if location else client_ip,
        location_source="ip" if location else ("ip_raw" if client_ip else None),
    )


@router.post("/session/activity", response_model=SessionStateResponse)
async def session_activity(
    request: Request, session: Session = Depends(current_session)
) -> SessionStateResponse:
    """Record real user activity and reset the idle window.

    **The only endpoint that extends a session.** The browser calls it in response to
    genuine input — pointer, keyboard, scroll, touch, tab focus — debounced so a scrolling
    farmer does not generate a request per frame.

    Not audited. An activity ping is not a user action worth a permanent record, and one
    entry per minute per session would swamp the collection that holds logins and key
    creations — the entries an investigation actually needs. The `SESSION_EXTENDED` action
    exists for the deliberate "keep me signed in" click from the warning modal, which *is*
    a decision someone made.
    """
    _require_store()

    await idle.touch(session.jti)
    state = await idle.check(session.jti)
    return _session_response(session, state, request)


@router.post("/session/extend", response_model=SessionStateResponse)
async def session_extend(
    request: Request,
    session: Session = Depends(current_session),
) -> SessionStateResponse:
    """Explicitly keep the session alive — the warning modal's "I'm still here" button.

    Functionally the same refresh as `/session/activity`, but audited: dismissing a timeout
    warning is a decision a person made at a moment in time, and on a shared machine it is
    exactly the kind of entry a later review wants to see.
    """
    _require_store()

    await idle.touch(session.jti)
    await store.record_audit(
        account_id=session.account.id,
        action=AuditAction.SESSION_EXTENDED,
        detail="idle warning dismissed by user",
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    state = await idle.check(session.jti)
    return _session_response(session, state, request)


class SessionEndRequest(BaseModel):
    """Why the session is ending. Shapes the audit entry, nothing else."""

    #: `idle` when the countdown ran out, `user` for a deliberate sign-out.
    reason: str = Field(default="user", max_length=16)


@router.post("/session/end")
async def session_end(
    payload: SessionEndRequest,
    request: Request,
    session: Session = Depends(current_session),
) -> dict:
    """End a session, recording *why*.

    `idle` and `user` are separate audit actions on purpose. "Walked away from a machine"
    and "chose to sign out" are different facts about a person's behaviour, and only the
    first is a signal worth reviewing — a pattern of idle timeouts on one account suggests
    a shared or unattended device.

    Dropping the idle key here rather than letting it lapse means a deliberate sign-out
    takes effect at once, so the same token cannot be replayed from elsewhere inside the
    remaining TTL.
    """
    _require_store()

    idle_timeout = payload.reason == "idle"
    await idle.end(session.jti)
    await store.record_audit(
        account_id=session.account.id,
        action=(
            AuditAction.SESSION_ENDED_IDLE if idle_timeout else AuditAction.LOGOUT
        ),
        detail=(
            f"no activity for {idle.window_seconds() // 60} minutes"
            if idle_timeout
            else "signed out from the portal"
        ),
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return {"ended": True}


class PortalEventRequest(BaseModel):
    """A frontend action worth a permanent record."""

    #: Closed vocabulary, mapped to `AuditAction` below. A free-text action would make the
    #: log unqueryable within a year — the same reasoning as `AuditAction` itself.
    event: str = Field(max_length=48)
    detail: str | None = Field(default=None, max_length=280)


#: Which frontend events may be logged, and as what.
#:
#: An allow-list, not a pass-through. Letting the browser name its own audit action would
#: let anyone holding a session write arbitrary entries into an append-only log that is
#: meant to be evidence — forging a `apikey.revoked` entry for someone else's account, for
#: instance. The mapping is server-side so the client can only ever select from this set.
_PORTAL_EVENTS = {
    "dashboard.viewed": AuditAction.DASHBOARD_VIEWED,
    # The portal's own surfaces. Only the ones that display or change subscriber data —
    # navigation alone is not worth a permanent entry, and logging every route change
    # would bury the sign-ins and key creations an investigation needs.
    "portal.viewed": AuditAction.DASHBOARD_VIEWED,
    "preferences.updated": AuditAction.PREFERENCES_UPDATED,
    "channel.updated": AuditAction.CHANNEL_UPDATED,
    "area.added": AuditAction.AREA_ADDED,
}


@router.post("/session/event")
async def session_event(
    payload: PortalEventRequest,
    request: Request,
    session: Session = Depends(current_session),
) -> dict:
    """Record a frontend action against the caller's **own** account.

    `account_id` comes from the session, never from the body. That is what stops a session
    holder writing audit entries attributed to someone else — the same reasoning as chat's
    `subscriber_id` being closed over rather than a tool argument.

    Unknown events are rejected rather than silently dropped: a typo in a frontend event
    name should surface in development, not vanish and leave a gap discovered during an
    incident review.
    """
    _require_store()

    action = _PORTAL_EVENTS.get(payload.event)
    if action is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown portal event {payload.event!r}. Known events: "
            f"{', '.join(sorted(_PORTAL_EVENTS))}.",
        )

    recorded = await store.record_audit(
        account_id=session.account.id,
        action=action,
        detail=payload.detail,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return {"recorded": recorded}


@router.post("/verify-email", response_model=Account)
async def verify_email(
    request: Request,
    background: BackgroundTasks,
    token: str = Query(min_length=10, max_length=200),
) -> Account:
    """Consume a verification token. Single-use, and looked up by hash.

    ## Also where the welcome email is sent, for EVERY account type

    `send_team_welcome` was reachable only from the team-invitation path, so an aggregator
    owner who signed up directly never received it — and neither did an individual farmer.
    Both are exactly the readers who most need to know why the service caps severity when
    confidence is low, because they read advisories unaided.

    This is the right place for it because it is the **single** transition to active for every
    account type, and `store.verify_email` is a `find_one_and_update` on the token hash: the
    token fields are unset in the same atomic update, so a replayed link returns None and
    never reaches this point. That gives exactly-once delivery with no "welcome_sent" flag to
    keep consistent.
    """
    _require_store()

    account = await store.verify_email(token)
    if account is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That link is invalid or has expired. Request a new one from "
            "POST /iam/resend-verification.",
        )

    # The transition from pending_verification to active is the single most consequential
    # event on an account — it is the point at which SHELTER will start sending to this
    # address. It was previously unrecorded.
    await store.record_audit(
        account_id=account.id,
        action=AuditAction.ACCOUNT_VERIFIED,
        detail="email address confirmed by link",
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    # The welcome, once, for every account type.
    #
    # `organisation_name` is None for an individual and the email adapts — see
    # `send_team_welcome`. A COMMERCIAL account uses its own organisation name; a member who
    # arrives through a team invitation is verified on that path instead and gets the same
    # email with their workspace count, so nobody receives it twice.
    #
    # Backgrounded: a mail provider being slow or down must not fail a verification that has
    # already been committed. The account is active either way, and a missing welcome is a
    # cosmetic loss where a failed verification is a locked-out user.
    background.add_task(
        mailer.send_team_welcome,
        account.email,
        account.first_name,
        organisation_name=(
            account.organisation if account.kind is AccountKind.COMMERCIAL else None
        ),
    )

    return account


@router.post("/resend-verification")
async def resend_verification(
    request: Request, account: Account = Depends(current_account)
) -> dict:
    """Re-send the confirmation link to the caller's own address.

    Requires a session, so it cannot be used to spray mail at arbitrary addresses —
    an unauthenticated resend endpoint is a free email cannon pointed at anyone.
    """
    _require_store()

    if account.email_verified:
        return {"sent": False, "detail": "This address is already confirmed."}

    token = security.new_verification_token()
    try:
        from datetime import datetime, timedelta, timezone

        await store._db().accounts.update_one(
            {"id": account.id},
            {
                "$set": {
                    "verification_token_hash": security.hash_token(token),
                    "verification_expires_at": datetime.now(timezone.utc)
                    + timedelta(hours=settings.iam_verification_ttl_hours),
                }
            },
        )
    except Exception as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not issue a new link."
        ) from exc

    sent = await mailer.send_verification(
        account.email,
        account.first_name,
        token,
        context=_request_context(request),
    )
    return {"sent": sent, "expires_in_hours": settings.iam_verification_ttl_hours}


# --------------------------------------------------------------------------- #
# Portal session — self-service
# --------------------------------------------------------------------------- #


class MyAccessResponse(BaseModel):
    """What this member may do — the answer both the portal nav and the API guard against.

    Returned as a single call so the portal never has to derive permissions from a role name.
    Duplicating `ROLE_PERMISSIONS` in TypeScript is precisely how a UI ends up showing a
    button the API refuses, or hiding one it would have allowed.
    """

    kind: str
    #: None for an individual — the concept does not apply, and returning a role like
    #: "owner" would imply a team that does not exist.
    role: str | None = None
    role_label: str | None = None
    #: Permission strings, e.g. `workspace:manage`. The nav filters on these directly.
    permissions: list[str]
    #: The scopes this member may put on a key they mint. Empty for an individual, who cannot
    #: mint one at all.
    grantable_scopes: list[str]


# --------------------------------------------------------------------------- #
# Workspaces — an aggregator's projects
#
# Multiple workspaces per organisation, each activating its own intelligence tracks. A
# cooperative running a rice programme in Kebbi and a flood-response pilot in Bayelsa keeps
# them separate: different tracks, different API keys, different customers.
#
# All five routes require `workspace:manage`, which Operations deliberately does not hold —
# activating a track changes what the organisation is buying.
# --------------------------------------------------------------------------- #


class TrackInfo(BaseModel):
    """One intelligence track, and whether activating it delivers anything yet."""

    value: str
    label: str
    summary: str
    #: The honest caveat. For Public Health this says plainly that nothing arrives yet.
    notes: str
    #: False when the risk model has no primary hazard for this track. The portal must not
    #: present such a switch as though it changes delivery.
    deliverable: bool
    #: The hazards this track alerts on. Empty for an undeliverable track — which is the
    #: machine-readable form of the same fact.
    hazards: list[str]


class WorkspacePublic(BaseModel):
    id: str
    name: str
    tracks: list[str]
    created_at: str | None = None
    is_default: bool = False
    #: Activated tracks that produce nothing yet. Surfaced per workspace so the portal can
    #: label the row rather than relying on the reader knowing which track is which.
    undeliverable_tracks: list[str] = Field(default_factory=list)


class WorkspaceWrite(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    #: One or all. An empty list is refused by the route — a workspace with no track is a
    #: project that receives nothing, which is almost certainly a mistake rather than an
    #: intent.
    tracks: list[str] = Field(default_factory=list)


class WorkspaceGrant(BaseModel):
    """One workspace, and the role this person holds on it.

    A list of these is what an invitation carries and what an edit replaces. Modelling it as
    (workspace, role) pairs rather than one role plus a list of workspaces is deliberate: an
    aggregator running several projects needs a colleague who is Engineering on the pilot and
    View-Only on the live season, and a single role cannot say that.
    """

    workspace_id: str
    role: str
    #: Consulted only when `role` is `custom`. Ignored otherwise — `permissions_for` will not
    #: merge a stray list into a named role, so storing one would imply access it lacks.
    permissions: list[str] = Field(default_factory=list)


class TeamMember(BaseModel):
    account_id: str
    email: str | None = None
    full_name: str | None = None
    #: One entry per workspace, each with the resolved permission list so the portal never
    #: re-derives it from the role name.
    grants: list[dict] = Field(default_factory=list)


class InviteWrite(BaseModel):
    email: EmailStr
    grants: list[WorkspaceGrant] = Field(min_length=1)
    #: Optional, and only a starting value.
    #:
    #: Redeeming the invitation CREATES the account, so without these the inviting
    #: organisation sees a blank row in its own team list until the colleague edits their
    #: profile. The invitee can correct them; this is a sensible default, not a claim.
    first_name: str = Field(default="", max_length=80)
    last_name: str = Field(default="", max_length=80)


class RoleOption(BaseModel):
    """A role an administrator may assign, with what it grants."""

    value: str
    label: str
    description: str
    permissions: list[str]
    scopes: list[str]


def _workspace_out(doc: dict) -> WorkspacePublic:
    return WorkspacePublic(
        **doc,
        undeliverable_tracks=[t.value for t in tracks_mod.undeliverable(doc.get("tracks", []))],
    )


@router.get("/tracks", response_model=list[TrackInfo])
async def list_tracks(account: Account = Depends(current_account)) -> list[TrackInfo]:
    """The intelligence tracks a workspace can activate.

    Includes `deliverable: false` for Public Health rather than omitting it. Hiding the track
    would lose the demand signal that sequences the roadmap; showing it without the flag would
    let an aggregator activate something that delivers nothing while reading as enabled.
    """
    _require_store()

    out: list[TrackInfo] = []
    for track in tracks_mod.Track:
        info = tracks_mod.TRACK_INFO[track]
        out.append(
            TrackInfo(
                value=track.value,
                label=info["label"],
                summary=info["summary"],
                notes=info["notes"],
                deliverable=tracks_mod.TRACK_DELIVERABLE[track],
                hazards=sorted(h.value for h in tracks_mod.TRACK_HAZARDS[track]),
            )
        )
    return out


@router.get("/workspaces", response_model=list[WorkspacePublic])
async def get_workspaces(
    # VIEW_DASHBOARD, not MANAGE_WORKSPACE: **reading** the list is not the same authority as
    # changing it, and every member needs it. An Engineering member must see which workspaces
    # exist to scope a key to one; the team page renders a role selector per workspace. Gating
    # the read on `workspace:manage` made both impossible and looked like an empty organisation.
    #
    # Not sensitive: these are the caller's own organisation's projects. Create, update and
    # delete below remain `MANAGE_WORKSPACE`, and the per-workspace ones use the workspace guard.
    account: Account = Depends(require_permission(Permission.VIEW_DASHBOARD)),
) -> list[WorkspacePublic]:
    """This organisation's workspaces.

    Creates a default one on first read rather than at signup, so an organisation that existed
    before workspaces did still resolves — and a fresh deployment needs no back-fill script.
    """
    _require_store()

    # Resolved through the organisation, not the caller's own id. An invited member's
    # workspaces belong to the founder's account, so listing by `account.id` would return an
    # empty list for every colleague — which reads as data loss, not as a scoping bug.
    organisation = await store.organisation_for(account.id)
    await store.ensure_default_workspace(organisation)
    return [_workspace_out(w) for w in await store.list_workspaces(organisation)]


@router.post(
    "/workspaces", response_model=WorkspacePublic, status_code=status.HTTP_201_CREATED
)
async def create_workspace_route(
    payload: WorkspaceWrite,
    request: Request,
    background: BackgroundTasks,
    account: Account = Depends(require_permission(Permission.MANAGE_WORKSPACE)),
) -> WorkspacePublic:
    """Create a workspace — a separate project with its own tracks and keys."""
    _require_store()

    if not payload.tracks:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Activate at least one intelligence track. A workspace with none receives "
            "nothing.",
        )

    organisation = await store.organisation_for(account.id)
    created = await store.create_workspace(organisation, payload.name, payload.tracks)
    if created is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not create the workspace."
        )

    await store.record_audit(
        account_id=account.id,
        action=AuditAction.WORKSPACE_CREATED,
        target_id=created["id"],
        detail=f"{payload.name} · tracks: {', '.join(payload.tracks)}",
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    # Backgrounded and non-fatal: the workspace exists and is usable, so a slow mail provider
    # must not turn a successful creation into a 5xx the caller will retry.
    background.add_task(
        mailer.send_workspace_notice,
        account.email,
        account.first_name,
        workspace_name=payload.name,
        created=True,
        context=_request_context(request),
    )
    return _workspace_out(created)


@router.patch("/workspaces/{workspace_id}", response_model=WorkspacePublic)
async def update_workspace_route(
    workspace_id: str,
    payload: WorkspaceWrite,
    request: Request,
    account: Account = Depends(
        require_workspace_permission(Permission.MANAGE_WORKSPACE)
    ),
) -> WorkspacePublic:
    """Rename a workspace, or change which tracks it runs.

    Guarded per workspace rather than on the union: a member who owns one project and is
    View-Only on another must not be able to change the tracks on the second one.
    """
    _require_store()

    if not payload.tracks:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Activate at least one intelligence track. A workspace with none receives "
            "nothing.",
        )

    organisation = await store.organisation_for(account.id)
    updated = await store.update_workspace(
        organisation, workspace_id, name=payload.name, tracks=payload.tracks
    )
    if updated is None:
        # 404 rather than 403 for a workspace belonging to another organisation: a 403 would
        # confirm the id exists, letting one aggregator enumerate another's workspaces.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such workspace.")

    await store.record_audit(
        account_id=account.id,
        action=AuditAction.WORKSPACE_UPDATED,
        target_id=workspace_id,
        detail=f"tracks: {', '.join(payload.tracks)}",
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return _workspace_out(updated)


@router.delete("/workspaces/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_workspace_route(
    workspace_id: str,
    request: Request,
    background: BackgroundTasks,
    account: Account = Depends(
        require_workspace_permission(Permission.MANAGE_WORKSPACE)
    ),
) -> None:
    """Delete a workspace. The default one cannot be deleted.

    Refused for the default because API keys are scoped to a workspace: removing the last one
    would leave live keys resolving to nothing, which is a key with undefined entitlement
    rather than a revoked key.
    """
    _require_store()

    organisation = await store.organisation_for(account.id)

    # Read the NAME before deleting it. The notice has to say which workspace went — an id is
    # not something a recipient can recognise, and after the delete there is nothing left to
    # look it up from. Falls back to the id rather than failing: a nameless notice still beats
    # no notice, and this must not be able to block the deletion.
    doomed = next(
        (
            w
            for w in await store.list_workspaces(account.id)
            if w.get("id") == workspace_id
        ),
        None,
    )
    workspace_name = (doomed or {}).get("name") or workspace_id

    if not await store.delete_workspace(organisation, workspace_id):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No such workspace, or it is your default one — a default cannot be deleted "
            "because API keys are scoped to a workspace.",
        )

    await store.record_audit(
        account_id=account.id,
        action=AuditAction.WORKSPACE_DELETED,
        target_id=workspace_id,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    background.add_task(
        mailer.send_workspace_notice,
        account.email,
        account.first_name,
        workspace_name=workspace_name,
        created=False,
        context=_request_context(request),
    )


class RoleGuide(BaseModel):
    """One role, with the consequences of choosing it."""

    value: str
    label: str
    description: str
    permissions: list[str]
    #: Scopes a member with this role may put on a key. What makes "permissions extend to the
    #: API" visible to the administrator choosing the role.
    scopes: list[str]


@router.get("/roles", response_model=list[RoleGuide])
async def list_roles(
    account: Account = Depends(current_account),
) -> list[RoleGuide]:
    """The roles an administrator can assign, and what each one grants.

    Served from `roles.ROLE_PERMISSIONS` — the same table `require_permission` enforces — so
    the team screen cannot describe a role differently from how it behaves. A hardcoded list
    in the frontend is how "Operations" ends up documented as having an access it does not.

    Requires a session but no permission: someone deciding whether to ask for a wider role
    needs to see what the roles are.
    """
    _require_store()

    out: list[RoleGuide] = []
    for role in Role:
        label, description = ROLE_LABELS[role]
        permissions = permissions_for(role)
        out.append(
            RoleGuide(
                value=role.value,
                label=label,
                description=description,
                permissions=sorted(p.value for p in permissions),
                scopes=sorted(s.value for s in scopes_for(permissions)),
            )
        )
    return out


@router.get("/me/access", response_model=MyAccessResponse)
async def my_access(account: Account = Depends(current_account)) -> MyAccessResponse:
    """The caller's own role, permissions and grantable scopes.

    ## Why the portal asks rather than computes

    The nav needs to know which sections to show; the key-minting screen needs to know which
    scopes may be requested. Both are functions of the same role table, and that table lives
    on the backend because it is also what `require_permission` enforces. One definition,
    asked for — not two definitions kept in step by hand.

    An **individual** gets `role: null`, no permissions beyond viewing, and no grantable
    scopes. That is the shape the portal branches on to hide every organisation section:
    absence rather than a role that happens to deny everything.
    """
    _require_store()

    if account.kind is not AccountKind.COMMERCIAL:
        # An individual manages their own areas and channels. There is no team to divide
        # access among, so there is no role — and inventing one would make the portal think
        # organisation sections might apply.
        return MyAccessResponse(
            kind=account.kind.value,
            role=None,
            role_label=None,
            permissions=[Permission.VIEW_DASHBOARD.value],
            grantable_scopes=[],
        )

    role = await store.member_role(account.id)
    permissions = await store.member_permissions(account.id)
    label = ROLE_LABELS.get(Role(role)) if role else None

    return MyAccessResponse(
        kind=account.kind.value,
        role=role,
        role_label=label[0] if label else None,
        permissions=sorted(p.value for p in permissions),
        grantable_scopes=sorted(s.value for s in scopes_for(permissions)),
    )


@router.get("/me", response_model=Account)
async def me(account: Account = Depends(current_account)) -> Account:
    """The caller's own profile.

    Takes no id parameter, deliberately: the account comes from the session, so there
    is no id for a caller to substitute. That is the same reasoning as closing over
    `subscriber_id` in the chat tools rather than accepting it as an argument.
    """
    return account


class PreferencesUpdate(BaseModel):
    language: str | None = Field(default=None, min_length=2, max_length=8)
    preferred_channel: Channel | None = None


@router.patch("/me/preferences", response_model=Account)
async def update_preferences(
    payload: PreferencesUpdate, account: Account = Depends(current_account)
) -> Account:
    """Change language or preferred channel. Scoped to the caller's own account."""
    _require_store()

    reject_unavailable_channel(payload.preferred_channel)

    updated = await store.update_preferences(
        account.id,
        language=payload.language,
        preferred_channel=payload.preferred_channel.value if payload.preferred_channel else None,
    )
    if updated is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Could not update preferences.")
    return updated


#: Shared with the area lifecycle routes — one implementation, so a new write path cannot
#: quietly skip the check. See `app/api/area_input.py`.
_normalise_area = normalise_area


@router.post("/activate", response_model=Subscriber, status_code=status.HTTP_201_CREATED)
async def activate(
    request: Request,
    payload: ActivationRequest,
    account: Account = Depends(verified_account),
) -> Subscriber:
    """Bind a plot and go autonomous. The second half of the 60-second flow.

    Requires a **verified** account, because this is the step that starts sending
    mail. Creating a subscription for an unconfirmed address is how someone else's
    inbox gets subscribed to alerts they never asked for.

    Registration queues an immediate first scan — someone signing up during a
    developing flood must not wait up to six hours for the next cycle.
    """
    _require_store()

    if account.subscriber_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This account already has an active subscription. Use the portal to add "
            "or change areas.",
        )

    # The account's own address is always a channel, so an activation with no
    # explicit channels still produces a working subscription.
    channels = [ChannelBinding(channel=Channel.EMAIL, address=account.email)]
    # An aggregator onboarding a customer is subject to the same limit — a partner who binds a
    # channel we cannot deliver on would be promising their farmer contact that never arrives.
    reject_unavailable_channels(payload.channels)
    channels += [c for c in payload.channels if c.channel is not Channel.EMAIL]

    # Normalised BEFORE the subscriber is built, so a bad ring is a 422 rather than a
    # half-created identity.
    area = _normalise_area(payload.area)

    subscriber = Subscriber(
        # Minted through the uniqueness check rather than relying on the model's
        # default: this is the path a real subscriber takes, and the id is what a
        # partner stores and a farmer quotes on a support call.
        id=await store.mint_subscriber_id(),
        name=account.full_name,
        kind=SubscriberKind.FARMER
        if account.kind is AccountKind.INDIVIDUAL
        else SubscriberKind.COOPERATIVE,
        language=account.language,
        areas=[area],
        channels=channels,
    )

    try:
        await repository.save_subscriber(subscriber)
    except Exception as exc:
        log.exception("subscriber persist failed during activation")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Could not save your subscription. Nothing was charged and no account "
            "change was made; please try again.",
        ) from exc

    # The return value is CHECKED, not discarded.
    #
    # Every portal page gates on `account.subscriber_id`, so a failed bind produces the exact
    # symptom this flow is meant to prevent: a subscription that runs, accumulates assessments,
    # and reports "nothing is being monitored" because the two stores were never linked.
    #
    # Loud rather than silent. The subscriber row is already durable and the watch loop will
    # scan it, so this is recoverable by re-running activation — but the user must be told,
    # because from their side a green success message and an empty dashboard is indistinguishable
    # from data loss.
    if not await store.bind_subscriber(account.id, subscriber.id):
        log.error(
            "subscription persisted but could not be bound to the account",
            extra={"account_id": account.id, "subscriber_id": subscriber.id},
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Your area was saved and monitoring has started, but we could not link it to "
            "your account, so it will not appear in your portal yet. Please try again — "
            "nothing will be duplicated.",
        )

    # Who is billed for this first plot, and which project it sits in.
    #
    # ## Why this branches on the account kind
    #
    # This route hard-coded `INDIVIDUAL`, and it is reached by BOTH kinds of account — an
    # aggregator activating its own monitoring goes through exactly here. The consequence was
    # visible on a real test account (`WBMLMQ4J5Z`): every plot it created was recorded
    # `owner_kind=individual, workspace_id=None`, so the workspace showed **zero customers** while
    # holding two active monitoring areas, and no portal view could say which customer a plot
    # belonged to — because, as recorded, none did.
    #
    # It compounds, which is why it took a while to see. `subscribers._inherit_attribution` copies
    # the owner from the subscriber's FIRST attributed area, so one wrong row at activation makes
    # every later plot wrong too, silently and consistently.
    #
    # ## An aggregator's own plots still belong to a workspace
    #
    # They are `AGGREGATOR`-owned with `subject_account_id` pointing at the aggregator itself: the
    # organisation is both the billed party and the subject, because these are the aggregator's own
    # fields rather than a customer's. That is a real and distinct case from an onboarded farmer,
    # and recording it correctly is what makes the per-workspace totals sum.
    if account.kind is AccountKind.COMMERCIAL:
        organisation = await store.organisation_for(account.id)
        default_workspace = await store.ensure_default_workspace(organisation)
        await store.record_attribution(
            aoi_id=area.id,
            owner_kind=attribution.OwnerKind.AGGREGATOR,
            owner_id=organisation,
            subscriber_id=subscriber.id,
            # The aggregator is its own subject here — this plot is not a customer's.
            subject_account_id=account.id,
            workspace_id=default_workspace["id"] if default_workspace else None,
        )
    else:
        # B2C: the individual is the billable owner and there is no aggregator. Recorded at
        # creation rather than derived later — see `app/iam/attribution.py`.
        await store.record_attribution(
            aoi_id=area.id,
            owner_kind=attribution.OwnerKind.INDIVIDUAL,
            owner_id=account.id,
            subscriber_id=subscriber.id,
        )

    await store.record_audit(
        account_id=account.id,
        action=AuditAction.SUBSCRIPTION_ACTIVATED,
        target_id=subscriber.id,
        detail=f"{area.name} · {area.hectares or '?'} ha",
    )

    # Queue the first scan and send the welcome note. Neither may fail activation:
    # the subscription is already durable, and the scheduler will pick the area up on
    # its next cycle regardless.
    try:
        from app.agents import pipeline

        await pipeline.enqueue_scan(subscriber, area)
    except Exception:
        log.warning("first scan could not be queued; the watch loop will pick it up")

    await mailer.send_welcome(
        account.email,
        account.first_name,
        area_name=area.name,
        context=_request_context(request),
    )

    log.info(
        "subscription activated",
        extra={"account_id": account.id, "subscriber_id": subscriber.id},
    )
    return subscriber


# --------------------------------------------------------------------------- #
# API keys — commercial accounts only
# --------------------------------------------------------------------------- #


@router.post("/api-keys", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    request: Request,
    payload: ApiKeyCreate,
    account: Account = Depends(verified_account),
) -> ApiKeyCreated:
    """Mint an API key. **Commercial accounts only.**

    An individual gets a 403 with an explanation rather than a silent failure: they
    have nothing to integrate, and a credential a farmer cannot use is a credential
    that can only be phished out of them.

    The plaintext is in this response and nowhere else — not in the database, not in
    the notification email.
    """
    _require_store()

    if not account.can_use_api:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "API keys are for commercial (aggregator) accounts. Individual accounts "
            "use the web portal, which needs no key.",
        )

    # The workspace this key belongs to, and the role that bounds it.
    #
    # Resolved before the scope check because the check must use the caller's role ON THIS
    # WORKSPACE. Using the union would leave the workspace boundary holding everywhere except
    # at the one operation that produces a lasting credential — an owner of one project could
    # mint a write key naming a project where they are View-Only.
    organisation = await store.organisation_for(account.id)
    workspace_id = payload.workspace_id
    if workspace_id is None:
        default = await store.ensure_default_workspace(organisation)
        workspace_id = default["id"] if default else None
    else:
        known = {w["id"] for w in await store.list_workspaces(organisation)}
        if workspace_id not in known:
            # 404 rather than 403, matching the workspace routes: a 403 would confirm the id
            # exists in another organisation.
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such workspace.")

    # A member cannot mint a key wider than their own role.
    #
    # THIS is what makes "side-nav permission extended to the API scopes" true rather than a
    # UI convention. Without it a View-Only member — who cannot edit a customer in the portal —
    # could mint a `customers:write` key and do exactly that through the API. The nav hiding
    # the button would be theatre.
    #
    # Resolved from the same `ROLE_PERMISSIONS` table `require_permission` uses, so the two
    # can never disagree about what a role allows.
    # SERVICE accounts have no workspace and no membership — their scopes are bounded by
    # `PLATFORM_SCOPES` inside `create_api_key` instead, so the role check does not apply.
    grantable = (
        scopes_for(await store.member_permissions_in(account.id, workspace_id))
        if workspace_id
        else scopes_for(await store.member_permissions(account.id))
    )
    requested = set(payload.scopes)
    refused = sorted(s.value for s in requested - grantable)
    if refused:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Your role on this workspace cannot grant {', '.join(refused)}. A key can never "
            "be wider than the role that created it, and a role you hold on another workspace "
            "does not apply here — ask an Organization Owner of this workspace.",
        )

    minted = await store.create_api_key(
        account.id,
        payload.name,
        payload.scopes,
        workspace_id=workspace_id,
        expires_in_days=payload.expires_in_days,
    )
    if minted is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"You already have {settings.iam_max_api_keys_per_account} active keys. "
            "Revoke one before creating another.",
        )

    key, public = minted
    # A security notice, not a delivery mechanism — the key is not in the email.
    # Scopes included so a later 403 is self-diagnosable — "my key lacks that
    # scope" rather than "the API is broken" — and so the partner has the spec links
    # in their inbox at the moment they start integrating.
    await mailer.send_api_key_notice(
        account.email,
        account.first_name,
        public.name,
        public.hint,
        scopes=[s.value for s in public.scopes],
        context=_request_context(request),
    )

    # The audit module calls key events "the highest-value entries in the log" and then
    # never wrote one. A minted credential that leaves no trace is the worst gap of the
    # fourteen: an aggregator asking "who created this key and when?" had no answer.
    #
    # The key itself is NOT logged, only its id, name and scopes — an audit log holding
    # live credentials would be a second, unmanaged secret store.
    await store.record_audit(
        account_id=account.id,
        action=AuditAction.KEY_CREATED,
        target_id=public.id,
        detail=f"{public.name} · scopes: {', '.join(s.value for s in public.scopes)}",
    )
    return ApiKeyCreated(**public.model_dump(), key=key)


@router.get("/api-keys", response_model=list[ApiKeyPublic])
async def list_api_keys(account: Account = Depends(current_account)) -> list[ApiKeyPublic]:
    """The caller's own keys, without secrets."""
    _require_store()
    return await store.list_api_keys(account.id)


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def revoke_api_key(key_id: str, account: Account = Depends(current_account)) -> None:
    """Revoke a key. Effective on the next request, not at expiry."""
    _require_store()
    if not await store.revoke_api_key(account.id, key_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such key")


# --------------------------------------------------------------------------- #
# Aggregator API — manage your own customers
# --------------------------------------------------------------------------- #


class CustomerCreate(BaseModel):
    """An individual onboarded by an aggregator on their customer's behalf.

    **No password field, and that is not an omission.** An aggregator-created account
    has `password_hash=None` and cannot log in until the person claims it by email. If
    an aggregator could set the password they could impersonate the farmer, and no
    audit trail would show who acted.
    """

    first_name: str = Field(min_length=1, max_length=120)
    last_name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=20)
    language: str = Field(default="en", min_length=2, max_length=8)
    preferred_channel: Channel = Channel.EMAIL
    #: Optional: bind the plot in the same call, so a bulk importer needs one
    #: request per customer rather than two.
    area: AreaOfInterest | None = None
    #: The aggregator's own identifier for this person — member number, policy number,
    #: loan id. **Stored on the membership edge, not the account**, because two
    #: aggregators know the same farmer by different references and neither's should
    #: overwrite the other's.
    external_ref: str | None = Field(default=None, max_length=120)


class CustomerRecord(BaseModel):
    account: Account
    subscriber_id: str | None = None


@router.post(
    "/customers",
    response_model=CustomerRecord,
    status_code=status.HTTP_201_CREATED,
)
async def create_customer(
    request: Request,
    payload: CustomerCreate,
    aggregator: Aggregator = Depends(require_scope(ApiKeyScope.WRITE)),
) -> CustomerRecord:
    """Onboard one individual under the calling aggregator.

    Requires `customers:write`. Creates the identity if the address is new, then
    attaches a `memberships` edge to the calling aggregator. **An existing address is
    not an error** — this aggregator may legitimately also serve that person, which is
    what multi-tenancy means here.

    Optionally binds the plot in the same call, so a bulk importer makes one request
    per customer rather than two — which matters when a cooperative is onboarding
    several hundred members.
    """
    _require_store()

    # A key with no workspace cannot create a customer.
    #
    # Under strict workspace filtering an unscoped membership matches nothing, so such a customer
    # would be invisible to every key the aggregator holds — including the one that just created
    # them. Refused with an explanation, which is recoverable, rather than silently producing a
    # customer nobody can see.
    #
    # Only reachable with a key minted before workspaces existed; new keys always carry one
    # (defaulting to the organisation's default workspace).
    if not aggregator.workspace_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This API key is not scoped to a workspace, so a customer created with it would "
            "belong to no project and be invisible to your other keys. Mint a new key for the "
            "workspace this customer belongs to, in the portal under Developers → API keys.",
        )

    # No breach screening, because there is no password to screen: `CustomerCreate` deliberately
    # has no password field — an aggregator who could set a farmer's password could impersonate
    # them with no audit trail showing who acted. The farmer claims the account by email and
    # chooses their own, which IS screened at that point.
    #
    # This route called `_reject_if_breached(payload.password)`, which raised `AttributeError` on
    # every request and made the entire B2B onboarding path return 500. Invisible until now
    # because no test exercised the route end to end and the Partner API had never been called
    # with a real key — the exact gap CLAUDE.md flagged as unexercised.
    reject_unavailable_channel(payload.preferred_channel)

    account, token = await store.create_account(
        kind=AccountKind.INDIVIDUAL,
        email=payload.email,
        first_name=payload.first_name,
        last_name=payload.last_name,
        # No password: the customer claims the account by email. An aggregator that
        # could set it could impersonate the farmer.
        password=None,
        phone=payload.phone,
        language=payload.language,
        preferred_channel=payload.preferred_channel.value,
    )

    # **Multi-tenancy.** An address that already exists is not an error: this
    # aggregator may legitimately also serve that person. Attach to the existing
    # identity rather than refusing, which is what makes a farmer reachable through
    # their cooperative *and* their insurer without duplicate accounts.
    existing = False
    if account is None:
        account = await store.get_account_by_email(payload.email)
        if account is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "That email address cannot be registered.",
            )
        if account.kind is not AccountKind.INDIVIDUAL:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "That address belongs to a commercial account and cannot be onboarded "
                "as a customer.",
            )
        existing = True

    membership = await store.attach_membership(
        account.id,
        aggregator.account.id,
        external_ref=payload.external_ref,
        onboarded_by_this_tenant=not existing,
        # The key used IS the statement of which project this customer belongs to.
        workspace_id=aggregator.workspace_id,
    )
    if membership is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This subscriber previously removed your organisation's access. They must "
            "re-authorise you from their own portal — an aggregator cannot re-attach "
            "itself after the person revoked it.",
        )

    subscriber_id: str | None = None
    if payload.area is not None:
        # Same normalisation as the self-service path. A bulk importer is MORE likely to send
        # a malformed ring — shapefile exports arrive clockwise, unclosed, or with thousands
        # of vertices — so a readable 422 per row is what makes a 400-row batch debuggable.
        area = _normalise_area(payload.area)

        subscriber = Subscriber(
            id=await store.mint_subscriber_id(),
            name=account.full_name,
            kind=SubscriberKind.FARMER,
            language=account.language,
            areas=[area],
            channels=[ChannelBinding(channel=Channel.EMAIL, address=account.email)],
        )
        try:
            await repository.save_subscriber(subscriber)
            await store.bind_subscriber(account.id, subscriber.id)
            subscriber_id = subscriber.id

            # B2B: the AGGREGATOR is billed, the farmer is monitored.
            #
            # `owner_id` is the aggregator's account — the commercial customer, e.g. the Anchor
            # Scheme — and `subject_account_id` is the farmer whose land this is. Keeping both
            # is what lets an invoice read "1,204 areas across 890 farmers", and lets the farmer
            # still ask who sees their data.
            #
            # `external_ref` is the aggregator's own identifier (loan id, member number), copied
            # here so reconciliation against their system needs no join through `memberships`.
            await store.record_attribution(
                aoi_id=area.id,
                owner_kind=attribution.OwnerKind.AGGREGATOR,
                owner_id=aggregator.account.id,
                subscriber_id=subscriber.id,
                subject_account_id=account.id,
                external_ref=payload.external_ref,
                # The project the PRESENTED KEY belongs to. The key is the statement of which
                # customer base this onboarding is for — the same reasoning that makes
                # `attach_membership` scope the membership to it just above.
                workspace_id=aggregator.workspace_id,
            )

            from app.agents import pipeline

            await pipeline.enqueue_scan(subscriber, area)
        except Exception:
            # The account exists and is durable. The plot can be bound in a follow-up
            # call, so this degrades rather than failing the onboarding.
            log.warning(
                "customer created but area binding failed",
                extra={"account_id": account.id},
            )

    # The customer is told an account was made for them, and how to claim it. Sending
    # this is what makes aggregator onboarding transparent to the person it is about
    # rather than something done silently on their behalf.
    # The AGGREGATOR's device, not the customer's — the customer has no session yet. It tells
    # the recipient which partner onboarded them, from where, which is the one check available
    # against an unexpected 'confirm your address' mail from a service they never signed up to.
    await mailer.send_verification(
        account.email,
        account.first_name,
        token or "",
        context=_request_context(request),
    )

    log.info(
        "customer onboarded by aggregator",
        extra={
            "aggregator_id": aggregator.account.id,
            "account_id": account.id,
            "area_bound": subscriber_id is not None,
        },
    )
    return CustomerRecord(account=account, subscriber_id=subscriber_id)


@router.get("/customers", response_model=list[Account])
async def list_customers(
    limit: int = Query(default=100, ge=1, le=500),
    skip: int = Query(default=0, ge=0),
    aggregator: Aggregator = Depends(current_aggregator),
) -> list[Account]:
    """The aggregator's own customers.

    The membership query establishes the tenant boundary *before* the accounts
    collection is touched, so another aggregator's customer is never a candidate row —
    a bug in later filtering could not leak one. Same reasoning as the session filter
    in chat retrieval.
    """
    _require_store()
    if not aggregator.has(ApiKeyScope.READ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This key lacks `customers:read`.")
    # Scoped to the workspace whose key was presented, so each project keeps its own customer
    # base. A key minted before workspaces existed carries None and sees everything, which is
    # what keeps an older integration working.
    return await store.list_tenant_accounts(
        aggregator.account.id,
        limit=limit,
        skip=skip,
        workspace_id=aggregator.workspace_id,
    )


@router.get("/customers/{account_id}", response_model=Account)
async def get_customer(
    account_id: str, aggregator: Aggregator = Depends(current_aggregator)
) -> Account:
    """One customer. 404 — not 403 — when it belongs to another aggregator.

    A 403 would confirm the id exists, letting one aggregator enumerate another's
    customer ids.
    """
    _require_store()
    if not aggregator.has(ApiKeyScope.READ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This key lacks `customers:read`.")
    return await owned_account(account_id, aggregator)


# --------------------------------------------------------------------------- #
# Key lifecycle — rotation and audit
# --------------------------------------------------------------------------- #


@router.post("/api-keys/{key_id}/rotate", response_model=ApiKeyCreated)
async def rotate_key(
    key_id: str,
    payload: ApiKeyRotate,
    account: Account = Depends(current_account),
) -> ApiKeyCreated:
    """Mint a replacement and put the old key into a grace window.

    **Not delete-then-create.** Both keys work until the deadline, so the partner can
    deploy the replacement and verify it before the old one dies. Without a grace
    window, rotation means choosing between an outage and leaving a compromised key
    live — and faced with that choice, people leave it live.

    `grace_hours=0` is the incident path: the old key stops working immediately.

    The replacement inherits the original's name and scopes, so a rotation cannot
    silently widen what the integration can do. The only thing that changes is the
    secret — and it is shown once, here.
    """
    _require_store()

    rotated = await store.rotate_api_key(
        account.id, key_id, grace_hours=payload.grace_hours
    )
    if rotated is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No such key, or it has already been revoked.",
        )
    key, public = rotated
    return ApiKeyCreated(**public.model_dump(), key=key)


@router.get("/api-keys/{key_id}/audit")
async def key_audit(
    key_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    account: Account = Depends(current_account),
) -> dict:
    """What this key did, and who changed it when.

    Scoped by the caller's account, so one aggregator cannot read another's trail —
    which would otherwise expose their integration's usage pattern.
    """
    _require_store()
    return {
        "key_id": key_id,
        "events": await store.key_audit_trail(account.id, key_id, limit=limit),
    }


# --------------------------------------------------------------------------- #
# The immutable audit log
# --------------------------------------------------------------------------- #


def _request_context(request: Request) -> mailer.RequestContext:
    """Device and location for a security email.

    Built here rather than inside `mailer` because the geo lookup lives in `iam.geo`, and
    the mail layer should not acquire a dependency on a 60MB optional database — it stays a
    pure function of its arguments and testable without one.

    A failed or absent lookup yields `None`, which the email renders as an em dash. The
    notice is still worth sending: the IP and browser alone let a recipient recognise their
    own request.
    """
    client_ip = request.client.host if request.client else None
    ua = useragent.parse(request.headers.get("user-agent"))
    located = geo.lookup(client_ip)

    return mailer.RequestContext(
        ip=client_ip,
        os_name=ua.os,
        browser=ua.browser,
        location=located.label if located else None,
    )


def _enrich_audit_entry(entry: dict) -> dict:
    """Add a readable device summary and an approximate location to one audit row.

    ## Why this is computed on read rather than stored on write

    Both derivations improve over time and neither is authoritative:

      * The user-agent parser gains browsers. A row stored in January with
        "Unknown browser" should read correctly once the parser learns that string —
        which only happens if the raw UA is the stored truth and the label is derived.
      * The GeoLite2 database is refreshed monthly. Storing "Warrington" would freeze an
        answer that the next database revision might correct.

    So the audit log keeps exactly what the client sent — the raw `user_agent` and `ip`,
    which is what makes it evidence — and the presentation layer interprets it. Storing the
    interpretation would also mean re-deriving nothing is possible after a parser bug.

    Cost is one dict-building pass and one mmap lookup per row, on a page of at most 200.
    """
    ua = useragent.parse(entry.get("user_agent"))
    location = geo.lookup(entry.get("ip"))

    return {
        **entry,
        # Nested rather than flattened so a client can render "Mobile · Android 14" on one
        # line and "Chrome 141" beneath it, which is what the portal does.
        "agent": {
            "device": ua.device,
            "os": ua.os,
            "browser": ua.browser,
            "summary": ua.summary,
            "is_bot": ua.is_bot,
        },
        # Null when the GeoLite2 database is not installed. The portal falls back to the
        # raw IP, so the feature is additive rather than required.
        "location": (
            {
                "label": location.label,
                "city": location.city,
                "country": location.country,
                "country_code": location.country_code,
                "confidence": location.confidence,
            }
            if location
            else None
        ),
    }


@router.get("/audit")
async def my_audit_log(
    cursor: str | None = Query(default=None, description="Opaque cursor from the previous page"),
    page_size: int = Query(default=50, ge=1, le=200),
    action: str | None = Query(default=None, description="Filter to one action type"),
    account: Account = Depends(current_account),
) -> dict:
    """**"What happened to my account?"** — including actions an aggregator took.

    Keyset-paginated, not `skip`/`limit`. `skip(n)` is O(n) in Mongo — the server walks
    and discards n documents per page — and it is also *incorrect* under concurrent
    writes: a new entry shifts every later page, silently duplicating or skipping rows.
    Verified against 500 live entries: 10 pages, zero duplicates, flat per-page cost.

    There is deliberately no `total`. `count_documents` on a growing collection is an
    unindexed scan that gets slower exactly as the log becomes more valuable, and
    "load more" needs no total.
    """
    _require_store()
    page = await store.audit_page(
        account_id=account.id, cursor=cursor, page_size=page_size, action=action
    )
    return {
        "entries": [_enrich_audit_entry(e) for e in page.entries],
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
        "page_size": page.page_size,
    }


@router.get("/audit/activity")
async def my_audit_activity(
    days: int = Query(default=30, ge=1, le=365),
    account: Account = Depends(current_account),
) -> dict:
    """Counts by action over a window, for the portal's activity widget."""
    _require_store()
    return await store.audit_summary(account.id, days=days)


@router.get("/audit/organisation")
async def organisation_audit_log(
    cursor: str | None = Query(default=None),
    page_size: int = Query(default=50, ge=1, le=200),
    account: Account = Depends(current_account),
) -> dict:
    """**"What did my organisation do?"** — an aggregator's own actions across customers.

    The mirror of `/audit`: that one is scoped by `account_id` (things done *to* you),
    this one by `actor_id` (things done *by* you). The two fields are separate precisely
    so both questions are answerable.
    """
    _require_store()
    if not account.can_use_api:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Organisation audit is for commercial accounts. Your own activity is at "
            "GET /iam/audit.",
        )
    page = await store.audit_page(
        account_id=account.id, cursor=cursor, page_size=page_size, as_actor=True
    )
    return {
        "entries": page.entries,
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
        "page_size": page.page_size,
    }


# --------------------------------------------------------------------------- #
# Multi-tenancy — the subscriber's own view and control
# --------------------------------------------------------------------------- #


@router.get("/me/aggregators")
async def my_aggregators(account: Account = Depends(current_account)) -> dict:
    """Which organisations can see my data.

    Takes no parameter — the account comes from the session — because this is
    deliberately **only** available to the subscriber. One aggregator learning that
    another also serves this farmer is commercially sensitive and none of their
    business.
    """
    _require_store()
    return {"aggregators": await store.list_account_aggregators(account.id)}


@router.delete(
    "/me/aggregators/{aggregator_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def revoke_aggregator(
    aggregator_id: str, account: Account = Depends(current_account)
) -> None:
    """Remove an organisation's access to my data.

    Recorded as `REVOKED_BY_SUBSCRIBER`, which the aggregator **cannot** undo —
    `attach_membership` refuses to reactivate that state. Otherwise an aggregator could
    simply re-attach itself after the person removed it, which would make this control
    decorative.

    Other aggregators' access is untouched, and the identity itself is unaffected.
    """
    _require_store()
    if not await store.detach_membership(account.id, aggregator_id, by_subscriber=True):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "That organisation does not have access."
        )
    await store.record_audit(
        account_id=account.id,
        action=AuditAction.MEMBERSHIP_REVOKED_BY_SUBSCRIBER,
        target_id=aggregator_id,
    )


@router.delete(
    "/customers/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def detach_customer(
    account_id: str,
    aggregator: Aggregator = Depends(require_scope(ApiKeyScope.WRITE)),
) -> None:
    """Stop serving a customer.

    **Detaches the membership; does not delete the person.** The identity, their plot
    and any other aggregator's access all survive — which is the whole point of
    modelling tenancy as an edge. A `DELETE` that removed the account would let one
    aggregator destroy another's customer relationship.
    """
    _require_store()
    await owned_account(account_id, aggregator)      # 404s if not this tenant's

    await store.detach_membership(account_id, aggregator.account.id)
    await store.record_audit(
        account_id=account_id,
        action=AuditAction.MEMBERSHIP_DETACHED,
        actor_id=aggregator.account.id,
        actor_kind="aggregator",
    )


# --------------------------------------------------------------------------- #
# Service accounts — what replaces the shared X-SHELTER-Key
# --------------------------------------------------------------------------- #


class ServiceAccountCreate(BaseModel):
    """Provision a machine principal.

    A service account has no password and no portal login: its only credential is a
    scoped key, so there is nothing to phish and no session to steal.
    """

    name: str = Field(
        min_length=1, max_length=120,
        description="What this principal is, e.g. 'netlify-frontend' or 'ci-smoke-tests'",
    )
    email: EmailStr = Field(
        description="Contact address for key-expiry notices. Not a login — service "
        "accounts cannot authenticate with a password."
    )
    scopes: list[ApiKeyScope] = Field(
        default_factory=lambda: list(FRONTEND_SCOPES),
        description="Least privilege. Defaults to the frontend's actual needs "
        "(register subscribers, read, assess) which deliberately EXCLUDES "
        "platform:broadcast — the portal has no reason to page a district.",
    )
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ServiceAccountCreated(BaseModel):
    account_id: str
    name: str
    scopes: list[ApiKeyScope]
    key: str = Field(
        description="Shown once. Only a SHA-256 hash is stored, so it cannot be "
        "recovered — rotate rather than recover."
    )
    warning: str = (
        "Store this now; it will not be shown again. Set it as SHELTER_API_KEY_V2 (or "
        "SHELTER_API_KEY once migrated) in the consumer's environment and send it as "
        "the X-SHELTER-API-Key header."
    )


@router.post(
    "/service-accounts",
    response_model=ServiceAccountCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
)
async def create_service_account(payload: ServiceAccountCreate) -> ServiceAccountCreated:
    """Create a machine principal and mint its first scoped key.

    **Guarded by the legacy shared key on purpose, and this is the one place that is
    correct.** Provisioning the *replacement* for the shared key cannot itself require
    a scoped key — that is circular, and a fresh deployment would have no way in. Once
    migrated, `make iam-service-account` (which runs in the container, off the network)
    is the safer path and the shared key can be removed entirely.
    """
    _require_store()

    invalid = [s for s in payload.scopes if s not in PLATFORM_SCOPES]
    if invalid:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"These are tenant scopes, not platform scopes: "
            f"{[s.value for s in invalid]}. A service account acts across the platform; "
            f"customer-scoped permissions belong on a commercial account's key.",
        )

    provisioned = await platform.provision_service_account(
        payload.name,
        payload.scopes,
        email=payload.email,
        expires_in_days=payload.expires_in_days,
    )
    if provisioned is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Could not provision the service account — that email may already be in "
            "use, or the IAM store is unreachable.",
        )

    account_id, key = provisioned
    log.info(
        "service account provisioned via API",
        extra={"account_id": account_id, "name": payload.name,
               "scopes": [s.value for s in payload.scopes]},
    )
    return ServiceAccountCreated(
        account_id=account_id, name=payload.name, scopes=payload.scopes, key=key
    )


@router.get("/service-accounts", dependencies=[Depends(require_api_key)])
async def list_service_accounts() -> dict:
    """Every machine principal and the state of its keys.

    The migration dashboard: it answers "which consumers still exist, and is anything
    stale or expiring?" — questions the single shared key made unanswerable.
    """
    _require_store()

    accounts = [
        a for a in await store.list_accounts_by_kind(AccountKind.SERVICE)
    ]
    out = []
    for account in accounts:
        out.append({
            "account_id": account.id,
            "name": account.organisation or account.first_name,
            "status": account.status.value,
            "keys": [k.model_dump(mode="json") for k in await store.list_api_keys(account.id)],
        })
    return {"service_accounts": out}


# --------------------------------------------------------------------------- #
# Passwordless sign-in, password reset, and TOTP
#
# Every endpoint in this section shares one property: **a request never reveals
# whether the account exists.** An unknown address gets the same response, and the
# same work is done, as a known one. Otherwise these are free account-enumeration
# oracles over a list of farmers in named districts — a privacy leak with physical
# consequences in a region where that list has value to people other than us.
# --------------------------------------------------------------------------- #


class MagicLinkRequest(BaseModel):
    email: EmailStr
    #: Where to land after sign-in. Sanitised server-side by `safe_next_path`; a
    #: crafted absolute URL here would otherwise produce a genuine SHELTER link that
    #: signs the user in and hands them to an attacker's page.
    next: str | None = Field(default=None, max_length=200)


class MagicLinkAccepted(BaseModel):
    """Deliberately says nothing about whether the address is registered."""

    sent: bool = True
    detail: str = (
        "If that address has a SHELTER account, a sign-in link is on its way. "
        "The link works once and expires in 15 minutes."
    )
    expires_in_minutes: int = passwordless.MAGIC_LINK_TTL_MINUTES


@router.post("/auth/magic-link", response_model=MagicLinkAccepted)
async def request_magic_link(
    payload: MagicLinkRequest, request: Request
) -> MagicLinkAccepted:
    """Email a single-use sign-in link.

    The primary path for individuals: no password to forget, mistype, or have written
    on a slip someone else can read. That matters more here than for a typical SaaS —
    the target user may share a handset and be on a metered connection, so a forgotten
    password is a support call rather than a self-service reset.

    **Always returns 200 with the same body**, whether or not the address exists.
    Throttled per address (not per IP) because this endpoint sends mail on demand:
    unthrottled it is a free email cannon pointed at anyone.
    """
    _require_store()

    if not settings.iam_magic_link_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Magic-link sign-in is not enabled on this deployment. Use your password.",
        )
    if not mailer.available():
        # Better an honest 503 than a button that silently does nothing and leaves the
        # user waiting for an email that was never sent.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Email delivery is unavailable, so a sign-in link cannot be sent right "
            "now. Please use your password, or try again shortly.",
        )

    purpose = passwordless.TokenPurpose.MAGIC_LINK
    recent = await store.count_recent_token_requests(payload.email, purpose)
    await store.record_token_request(payload.email, purpose)

    if recent >= passwordless.MAX_LINK_REQUESTS:
        # 429 is safe to distinguish here: it reveals a rate, not an account. Someone
        # probing addresses learns nothing, because the limit is reached by *their* own
        # requests for that address regardless of whether it exists.
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many sign-in links requested. Please wait "
            f"{passwordless.LINK_REQUEST_WINDOW_MINUTES} minutes.",
        )

    issued = await store.issue_single_use_token(
        payload.email, purpose, next_path=payload.next
    )
    if issued is not None:
        token, account = issued
        await mailer.send_magic_link(
            account.email,
            account.first_name,
            passwordless.magic_link_url(token, next_path=payload.next),
            _request_context(request),
        )
        # NOT `LOGIN_SUCCEEDED`. Requesting a link is not signing in — the link may never be
        # opened, and labelling it as a success put a device that never authenticated into the
        # trusted-device table, which is the opposite of what that table asserts.
        await store.record_audit(
            account_id=account.id, action=AuditAction.MAGIC_LINK_REQUESTED,
            detail="magic link requested",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )

    # Same response either way.
    return MagicLinkAccepted()


class MagicLinkRedeem(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class MagicLinkSession(SessionToken):
    """A session plus where the portal should navigate next."""

    next: str = "/dashboard"


@router.post("/auth/magic-link/verify", response_model=MagicLinkSession)
async def redeem_magic_link(
    payload: MagicLinkRedeem, request: Request
) -> MagicLinkSession:
    """Exchange a link token for a session.

    Single use, enforced atomically — the token is deleted in the same operation that
    validates it, so two concurrent clicks cannot both succeed.

    Redeeming also **confirms the email address**, because clicking a link sent to that
    mailbox is exactly the proof `verify-email` asks for. Password reset deliberately
    does not imply this.
    """
    _require_store()

    redeemed = await store.redeem_single_use_token(
        payload.token, passwordless.TokenPurpose.MAGIC_LINK
    )
    if redeemed is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That sign-in link is invalid, already used, or has expired. Request a "
            "new one.",
        )

    account, next_path = redeemed

    # A magic link proves mailbox control, which is a stronger signal than a password
    # — so it satisfies the second factor too. Requiring TOTP on top would mean an
    # emailed link is treated as weaker than the password it replaces.
    token, expires_in = security.issue_session(account.id, account.kind.value)
    await store.record_audit(
        account_id=account.id, action=AuditAction.LOGIN_SUCCEEDED,
        detail="magic link redeemed",
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return MagicLinkSession(
        access_token=token, expires_in=expires_in, account=account, next=next_path
    )


class PasswordResetRequest(BaseModel):
    email: EmailStr


@router.post("/auth/password-reset", response_model=MagicLinkAccepted)
async def request_password_reset(
    payload: PasswordResetRequest, request: Request
) -> MagicLinkAccepted:
    """Email a password-reset link. Same non-disclosure and throttling as magic link."""
    _require_store()

    if not mailer.available():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Email delivery is unavailable, so a reset link cannot be sent right now.",
        )

    purpose = passwordless.TokenPurpose.PASSWORD_RESET
    recent = await store.count_recent_token_requests(payload.email, purpose)
    await store.record_token_request(payload.email, purpose)

    if recent >= passwordless.MAX_LINK_REQUESTS:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many reset links requested. Please wait "
            f"{passwordless.LINK_REQUEST_WINDOW_MINUTES} minutes.",
        )

    issued = await store.issue_single_use_token(payload.email, purpose)
    if issued is not None:
        token, account = issued
        await mailer.send_password_reset(
            account.email,
            account.first_name,
            passwordless.password_reset_url(token),
            _request_context(request),
        )

    return MagicLinkAccepted(
        detail=(
            "If that address has a SHELTER account, a password-reset link is on its "
            "way. The link works once and expires in 1 hour."
        ),
        expires_in_minutes=passwordless.PASSWORD_RESET_TTL_MINUTES,
    )


class PasswordResetConfirm(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: Password

    _check_password = field_validator("password")(
        IndividualSignup._not_obvious.__func__
    )


@router.post("/auth/password-reset/confirm", response_model=SessionToken)
async def confirm_password_reset(payload: PasswordResetConfirm) -> SessionToken:
    """Set a new password and sign in.

    Signing in immediately is deliberate: a reset that ends at a login form makes the
    user type the password they just chose, which is where a typo in the confirmation
    field surfaces as "my new password doesn't work".

    Completing a reset invalidates every other outstanding reset token, so a second
    forwarded email cannot set the password again afterwards.
    """
    _require_store()
    # Screened BEFORE the token is redeemed. A reset token is single-use, so rejecting the
    # password after consuming it would leave the user holding a spent link and needing to
    # request another — punishing them twice for one weak password.
    await _reject_if_breached(payload.password)

    redeemed = await store.redeem_single_use_token(
        payload.token, passwordless.TokenPurpose.PASSWORD_RESET
    )
    if redeemed is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That reset link is invalid, already used, or has expired. Request a new "
            "one.",
        )

    account, _ = redeemed
    if not await store.set_password(account.id, payload.password):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not update your password."
        )

    await store.record_audit(
        account_id=account.id, action=AuditAction.PASSWORD_CHANGED,
        detail="via reset link",
    )
    token, expires_in = security.issue_session(account.id, account.kind.value)
    return SessionToken(access_token=token, expires_in=expires_in, account=account)


class TrustedDevice(BaseModel):
    """One device this account has signed in from."""

    #: The account this sign-in belongs to. Present on every row so a row identifies a PERSON as
    #: well as a machine — a household or a cooperative office may share a handset, and "Chrome on
    #: Android, Lagos" alone does not say whose sign-in it was.
    email: str
    device: str = Field(description='Device and OS, e.g. "Mobile · Android 14".')
    browser: str
    #: The raw user-agent, for an operator comparing two rows that summarise identically.
    user_agent: str | None = None
    ip: str | None = None
    #: Approximate, from an offline IP database. See the caveat the page renders beside it.
    location: str | None = None
    last_login: datetime | None = None
    first_seen: datetime | None = None
    sign_ins: int = 0
    #: True for the request reading this list, so the page can mark "this device" rather than
    #: leaving the reader to work out which row is them — the first thing anyone checks.
    is_current: bool = False


class TrustedDeviceList(BaseModel):
    devices: list[TrustedDevice]
    #: Rendered above the table verbatim, so the policy and the data cannot drift apart.
    notice: str = (
        "Your trusted devices are listed below. They will remain trusted devices unless "
        "there is a period of inactivity on your SHELTER account."
    )


@router.get("/security/devices", response_model=TrustedDeviceList)
async def my_trusted_devices(
    request: Request, account: Account = Depends(current_account)
) -> TrustedDeviceList:
    """Devices this account has signed in from, most recent first.

    Scoped to the caller with no id parameter: `trusted_devices(account.id)` reads one account's
    own sign-in history, so there is nothing to tamper with. That scoping IS the authorisation.

    Derived from the audit log rather than a devices table — see `store.trusted_devices` for why.
    The **location is resolved here**, at read time, from the stored IP: the geo database is
    optional and 60MB, so a deployment without it still gets a working table showing raw IPs
    rather than an empty one.
    """
    _require_store()

    rows = await store.trusted_devices(account.id)

    # What the CURRENT request looks like, so one row can be marked as "this device". Compared on
    # (user agent, IP) because that is what the rows are grouped by — matching on the user agent
    # alone would mark a row from another network as this one.
    here_ua = request.headers.get("user-agent")
    here_ip = request.client.host if request.client else None

    devices: list[TrustedDevice] = []
    for row in rows:
        parsed = useragent.parse(row.get("user_agent"))
        located = geo.lookup(row.get("ip"))
        devices.append(
            TrustedDevice(
                email=account.email,
                # Device and OS together, browser separately — the split `ParsedAgent` exists for.
                device=f"{parsed.device} · {parsed.os}",
                browser=parsed.browser,
                user_agent=row.get("user_agent"),
                ip=row.get("ip"),
                # Falls back to the raw IP rather than to null: "we could not name this place" and
                # "there was no address" are different, and only the second deserves a blank.
                location=located.label if located else row.get("ip"),
                last_login=row.get("last_seen"),
                first_seen=row.get("first_seen"),
                sign_ins=row.get("sign_ins", 0),
                is_current=(
                    row.get("user_agent") == here_ua and row.get("ip") == here_ip
                ),
            )
        )

    return TrustedDeviceList(devices=devices)


# --- In-session password change -------------------------------------------- #
#
# Distinct from the reset flow above, and deliberately not built on it. See
# `passwordless.PASSWORD_CODE_LENGTH` for why a six-character code is defensible here and would not
# be there: this runs behind a live session, is unreachable while signed out, and is attempt-bounded.
#
# The reset link stays exactly as it is for the forgot-password case. A subscriber who cannot sign in
# has no session to prove anything with, so a short code typed into an unauthenticated form would be
# the sole credential — which is the situation that needs 256 bits and a single-use link.


class PasswordChangeCodeSent(BaseModel):
    """Confirmation that a code is on its way, without repeating the address back."""

    detail: str
    expires_in_minutes: int
    #: Masked, so the reader can confirm WHICH mailbox to open without the full address being
    #: rendered into a page that may be over someone's shoulder or in a screenshot.
    sent_to: str


def _mask_email(email: str) -> str:
    """`a****@example.com`. Enough to recognise, not enough to harvest."""
    local, _, domain = email.partition("@")
    if not domain:
        return "your registered address"
    head = local[0] if local else ""
    return f"{head}{'*' * max(len(local) - 1, 3)}@{domain}"


@router.post("/password/change/request", response_model=PasswordChangeCodeSent)
async def request_password_change_code(
    request: Request, account: Account = Depends(current_account)
) -> PasswordChangeCodeSent:
    """Email a one-time code to confirm a password change. **Signed-in callers only.**

    Sent to the account's REGISTERED address, never to one supplied in the request — that is what
    makes the code a proof of mailbox control rather than a step the requester can redirect to
    themselves.

    Throttled on the address like every other mail-sending endpoint: without a limit this is a free
    email cannon, and being authenticated does not change that.
    """
    _require_store()

    if not mailer.available():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Email delivery is unavailable, so a confirmation code cannot be sent right "
            "now. Your current password is unchanged.",
        )

    purpose = passwordless.TokenPurpose.PASSWORD_CHANGE_CODE
    recent = await store.count_recent_token_requests(account.email, purpose)
    await store.record_token_request(account.email, purpose)
    if recent >= passwordless.MAX_LINK_REQUESTS:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many codes requested. Please wait "
            f"{passwordless.LINK_REQUEST_WINDOW_MINUTES} minutes.",
        )

    code = await store.issue_password_change_code(account.id)
    if code is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Could not start a password change. Your current password is unchanged.",
        )

    # The device block travels, because this doubles as a security notice: if the reader did not
    # just ask for this, the origin is what tells them somebody else has their session.
    await mailer.send_password_change_code(
        account.email, account.first_name, code, _request_context(request)
    )

    # Audited on REQUEST, not only on completion. An abandoned or failed attempt is exactly what an
    # incident review needs to see — recording only successes would hide the interesting case.
    await store.record_audit(
        account_id=account.id,
        action=AuditAction.PASSWORD_CHANGE_REQUESTED,
        detail="confirmation code sent",
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    return PasswordChangeCodeSent(
        detail=(
            "A 6-character code is on its way. Enter it with your new password to "
            "finish. Your current password keeps working until then."
        ),
        expires_in_minutes=passwordless.PASSWORD_CODE_TTL_MINUTES,
        sent_to=_mask_email(account.email),
    )


class PasswordChangeConfirm(BaseModel):
    code: str = Field(
        min_length=passwordless.PASSWORD_CODE_LENGTH,
        # A generous ceiling rather than an exact length: the client may send it spaced or
        # hyphenated, and `normalise_password_change_code` strips both. Rejecting on length before
        # normalising would fail a correctly-typed code.
        max_length=32,
        description="The 6-character code emailed to your registered address.",
    )
    password: Password

    _check_password = field_validator("password")(
        IndividualSignup._not_obvious.__func__
    )


@router.post("/password/change/confirm", response_model=SessionToken)
async def confirm_password_change(
    payload: PasswordChangeConfirm,
    request: Request,
    account: Account = Depends(current_account),
) -> SessionToken:
    """Verify the code and set the new password. **Signed-in callers only.**

    A fresh session token is returned so the caller is not left holding one minted before the
    credential changed. The response shape matches the reset flow for the same reason it does there:
    ending at a login form makes the user immediately retype the password they just chose, which is
    where a typo surfaces as "my new password doesn't work".
    """
    _require_store()

    # Screened BEFORE the code is consumed, exactly as in the reset flow. Rejecting the password
    # afterwards would spend the code and force a second email — punishing the user twice for one
    # weak choice.
    await _reject_if_breached(payload.password)

    if not await store.redeem_password_change_code(account.id, payload.code):
        # One message for wrong, expired and spent. Distinguishing them would tell someone
        # probing a session which state they are in, and the remedy is the same in all three.
        await store.record_audit(
            account_id=account.id,
            action=AuditAction.PASSWORD_CHANGE_FAILED,
            # A rejected code is a FAILURE outcome. Left as the default `success` the row would read
            # as "a password change happened", and the whole reason for logging these is that a run
            # of them is what someone guessing a code they never received looks like.
            outcome=AuditOutcome.FAILURE,
            detail="invalid or expired confirmation code",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"That code is not valid, has expired, or has already been used. Request a "
            f"new one. Codes expire after "
            f"{passwordless.PASSWORD_CODE_TTL_MINUTES} minutes and allow "
            f"{passwordless.PASSWORD_CODE_MAX_ATTEMPTS} attempts.",
        )

    if not await store.set_password(account.id, payload.password):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not update your password."
        )

    await store.record_audit(
        account_id=account.id,
        action=AuditAction.PASSWORD_CHANGED,
        detail="via emailed code, from the portal",
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    token, expires_in = security.issue_session(account.id, account.kind.value)
    return SessionToken(access_token=token, expires_in=expires_in, account=account)


# --- TOTP ------------------------------------------------------------------ #


class TotpEnrolment(BaseModel):
    secret: str = Field(description="Base32. Shown once, for manual entry.")
    provisioning_uri: str = Field(
        description="Encode as a QR code for the authenticator app to scan."
    )
    detail: str = (
        "Scan the QR code, then confirm with a generated code. Two-factor is not "
        "active until you confirm — so a mistyped scan cannot lock you out."
    )


@router.post("/auth/totp/enrol", response_model=TotpEnrolment)
async def begin_totp(account: Account = Depends(current_account)) -> TotpEnrolment:
    """Stage a TOTP secret. **Not active until confirmed.**

    Staged rather than activated immediately because a secret is only trustworthy once
    the user's app is proven to generate matching codes. Activating on issue would mean
    a mistyped QR scan locks the account out — and for an aggregator that means losing
    access to every customer they manage.
    """
    _require_store()

    if not settings.iam_totp_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Two-factor authentication is not enabled on this deployment.",
        )

    started = await store.begin_totp_enrolment(account.id)
    if started is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not start enrolment."
        )
    secret, uri = started
    return TotpEnrolment(secret=secret, provisioning_uri=uri)


class TotpConfirm(BaseModel):
    code: str = Field(min_length=6, max_length=8)


class TotpActivated(BaseModel):
    enabled: bool = True
    recovery_codes: list[str] = Field(
        description="Shown once. Store them somewhere other than the device running "
        "your authenticator — they are how you recover a lost or wiped phone."
    )
    warning: str = (
        "These codes are shown only now and are stored hashed. Each works once. "
        "Without them, a lost authenticator means losing access to this account."
    )


@router.post("/auth/totp/confirm", response_model=TotpActivated)
async def confirm_totp(
    payload: TotpConfirm, account: Account = Depends(current_account)
) -> TotpActivated:
    """Activate TOTP and issue recovery codes."""
    _require_store()

    codes = await store.confirm_totp_enrolment(account.id, payload.code)
    if codes is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That code did not match. Check your device's clock is correct and try "
            "the current code.",
        )
    return TotpActivated(recovery_codes=codes)


@router.get("/auth/totp")
async def totp_status(account: Account = Depends(current_account)) -> dict:
    """Whether TOTP is active, and how many recovery codes remain.

    The remaining count matters: a user down to one code should regenerate before they
    are locked out, and nothing else would tell them.
    """
    _require_store()
    return await store.totp_state(account.id)


@router.delete("/auth/totp", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def remove_totp(account: Account = Depends(current_account)) -> None:
    """Disable TOTP and discard the secret and recovery codes.

    Discarded rather than retained, so re-enabling cannot silently reuse material the
    user may have exported to a device they no longer control.
    """
    _require_store()
    if not await store.disable_totp(account.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Two-factor was not enabled.")


# --------------------------------------------------------------------------- #
# Team management
#
# Membership is per (person, workspace), so every route here names the workspaces it affects
# rather than acting on "the organisation" as a whole.
# --------------------------------------------------------------------------- #


@router.get("/team", response_model=list[TeamMember])
async def get_team(
    account: Account = Depends(require_permission(Permission.MANAGE_TEAM)),
) -> list[TeamMember]:
    """Colleagues in this organisation, with the workspaces and roles each holds."""
    _require_store()

    organisation = await store.organisation_for(account.id)
    return [TeamMember(**member) for member in await store.list_team(organisation)]


@router.get("/team/assignable-roles", response_model=list[RoleOption])
async def get_assignable_roles(
    account: Account = Depends(require_permission(Permission.MANAGE_TEAM)),
) -> list[RoleOption]:
    """The roles this member may assign.

    Narrower than `GET /iam/roles`, which lists every role that exists: only an owner may
    create another owner. Without that rule a member with `team:manage` could mint an
    Organization Owner and inherit billing authority through them — a one-hop escalation.
    """
    _require_store()

    granter = await store.member_permissions(account.id)
    out: list[RoleOption] = []
    for role in team_mod.grantable_roles(granter):
        label, description = ROLE_LABELS[role]
        permissions = roles_mod.permissions_for(role)
        out.append(
            RoleOption(
                value=role.value,
                label=label,
                description=description,
                permissions=sorted(p.value for p in permissions),
                scopes=sorted(sc.value for sc in roles_mod.scopes_for(permissions)),
            )
        )
    return out


@router.post("/team/invitations", status_code=status.HTTP_201_CREATED)
async def invite_member(
    payload: InviteWrite,
    request: Request,
    account: Account = Depends(require_permission(Permission.MANAGE_TEAM)),
) -> dict:
    """Invite a colleague to one or more workspaces, each with a role.

    ## Two refusals that are the whole point of this route

    **A workspace the inviter does not administer.** Checked per named workspace against
    `member_permissions_in`, so a member with `team:manage` on one project cannot add people to
    another one they merely belong to.

    **A role wider than the inviter's own.** Otherwise an Engineering member could invite
    themselves — or a colleague they control — back as an Organization Owner, and no amount of
    nav hiding would catch it.
    """
    _require_store()

    organisation = await store.organisation_for(account.id)
    known = {w["id"] for w in await store.list_workspaces(organisation)}
    assignable = {r.value for r in team_mod.grantable_roles(
        await store.member_permissions(account.id)
    )}

    for grant in payload.grants:
        if grant.workspace_id not in known:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such workspace.")

        if Permission.MANAGE_TEAM not in await store.member_permissions_in(
            account.id, grant.workspace_id
        ):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "You can only invite people to a workspace you manage. Your role on "
                f"{grant.workspace_id} does not include team management.",
            )

        if grant.role not in assignable:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"You cannot assign the {grant.role} role. A role can never be wider than "
                "the one granting it — ask an Organization Owner.",
            )

    created = await store.create_invitation(
        organisation,
        str(payload.email),
        [grant.model_dump() for grant in payload.grants],
        account.id,
        first_name=payload.first_name,
        last_name=payload.last_name,
        organisation_name=account.organisation or "",
    )
    if created is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not create the invitation."
        )

    document, plaintext = created
    sent = await mailer.send_team_invitation(
        str(payload.email),
        organisation_name=account.organisation or "your organisation",
        inviter=f"{account.first_name} {account.last_name}".strip(),
        token=plaintext,
        workspaces=[g.workspace_id for g in payload.grants],
        # The INVITER's device, not the recipient's — they have no session yet. It answers
        # "who sent this, and from where", the one check a person can make against an
        # unexpected invitation to a platform they have never heard of.
        context=_request_context(request),
    )

    await store.record_audit(
        account_id=account.id,
        action=AuditAction.MEMBER_INVITED,
        target_id=str(payload.email),
        detail=", ".join(f"{g.workspace_id}:{g.role}" for g in payload.grants),
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    # The token is NEVER returned in the response, even to an owner. An invitation is a
    # credential; the only place it should exist is the invited person's mailbox. Returning it
    # would put it in the portal's network log and in any error-reporting tool watching it.
    return {
        "invited": document["email"],
        "email_sent": sent,
        "expires_at": document["expires_at"].isoformat(),
    }


@router.get("/team/invitations")
async def get_invitations(
    account: Account = Depends(require_permission(Permission.MANAGE_TEAM)),
) -> list[dict]:
    """Invitations sent but not yet accepted."""
    _require_store()

    return await store.list_invitations(await store.organisation_for(account.id))


#: Lifetime of the scoped session an invitation redeems into.
#:
#: Not a config setting: `test_config.py` fails the build on any setting that is declared and
#: never read, and this has no deployment for which a different value would be right. A
#: constant beside the route it governs is easier to find than a `.env` key.
SETUP_SESSION_MINUTES = 30


class InvitationRedeemed(BaseModel):
    """The scoped session an invitation redeems into.

    Carries a real session token, so the invited person is signed in — but one that can do
    nothing except set a password. `must_set_password` tells the portal to render that form
    and nothing else; the backend refuses everything else regardless, so the flag is a UI
    hint rather than the control.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    must_set_password: bool = True
    email: str
    organisation: str | None = None
    workspaces: int = 0
    detail: str = (
        "Choose your own password to finish. This link has now been used and will not "
        "work again."
    )


class FirstPassword(BaseModel):
    password: str = Field(min_length=12, max_length=200)


@router.post("/team/invitations/redeem", response_model=InvitationRedeemed)
async def redeem_invitation(payload: dict, request: Request) -> InvitationRedeemed:
    """Redeem a team invitation. Creates the account, signs them in to set a password.

    ## Unauthenticated, deliberately

    The token *is* the proof — the same reasoning as email verification. Requiring a session
    would defeat the purpose: the invited colleague usually has no account at all, which is
    what made the previous accept-only flow require a signup detour and a second verification
    email before the invitation could even be used.

    ## No password is ever generated

    A one-time password was the obvious alternative. It would be valid at `POST /iam/login` —
    public and reachable by anyone, so an online guessing target for its whole 14-day life —
    would have to be short enough to type (perhaps 50-60 bits against this token's 256), and
    would survive being forwarded in a reply-all. Instead the account is created with **no
    password hash**, and this returns a session scoped to `SCOPE_SET_PASSWORD`.

    ## What a hijacked session from here can do

    Nothing but set the password on an account that has no data yet. It lasts 15 minutes, not
    the usual 12 hours, and `current_account` refuses it on every other route — so the window
    is narrow and its contents are empty. Setting the password issues a *new* session with a
    new `jti`, which is what actually retires the scoped one.
    """
    _require_store()

    token = str(payload.get("token", "")).strip()
    if not token:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Missing token.")

    redeemed = await store.redeem_team_invitation(token)
    if redeemed is None:
        # One message for every failure — expired, already used, or never valid. Telling a
        # holder of a random token which one it was is an oracle over invitation state.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This invitation is not valid. It may have expired or already been used. Ask "
            "your colleague to send a new one.",
        )

    account, invitation = redeemed

    # 30 minutes, and scoped. Long enough that reading the invitation, opening the link and
    # choosing a password on a poor connection does not lapse mid-flow; short enough that an
    # abandoned tab is not a standing credential. The token behind it stays valid for 14 days,
    # so a lapse costs one more click rather than a new invitation.
    session_token, expires_in = security.issue_session(
        account.id,
        account.kind.value,
        minutes=SETUP_SESSION_MINUTES,
        scope=security.SCOPE_SET_PASSWORD,
    )

    await store.record_audit(
        account_id=account.id,
        action=AuditAction.MEMBER_JOINED,
        target_id=invitation["organisation_id"],
        detail=", ".join(
            f"{g.get('workspace_id')}:{g.get('role')}" for g in invitation.get("grants", [])
        ),
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    return InvitationRedeemed(
        access_token=session_token,
        expires_in=expires_in,
        email=account.email,
        organisation=invitation.get("organisation_name") or account.organisation,
        workspaces=len(invitation.get("grants", [])),
    )


@router.post("/team/first-password", response_model=SessionToken)
async def set_first_password(
    payload: FirstPassword,
    request: Request,
    background: BackgroundTasks,
    account: Account = Depends(password_setup_session),
) -> SessionToken:
    """Set the password an invited member chose, and hand back a full session.

    `password_setup_session` is the **only** dependency that accepts a `SCOPE_SET_PASSWORD`
    token, so this is the single route reachable with one. A test asserts that.

    Returning a full session immediately rather than redirecting to the login form is the same
    reasoning `confirm_password_reset` uses: making someone type the password they chose ten
    seconds ago is where a typo in the confirmation field surfaces as "my new password doesn't
    work". The **new session has a new `jti`**, which is what retires the scoped one — a
    hijacked invite session cannot ride along past this point.
    """
    _require_store()

    # Screened before anything is written, so a rejected password does not leave the account
    # half-activated with a spent invitation.
    await _reject_if_breached(payload.password)

    if not await store.set_password(account.id, payload.password):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not set your password."
        )

    context = _request_context(request)
    organisation = account.organisation or "your organisation"

    await store.record_audit(
        account_id=account.id,
        action=AuditAction.PASSWORD_CHANGED,
        detail="first password, via team invitation",
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    # Both emails in the background: neither should delay handing back the session, and a
    # mail outage must not fail an activation the user already completed successfully.
    #
    # Two emails rather than one, and in this order. The security notice carries the device
    # block and needs reading in the first minute; the welcome is prose. Merged, "was this
    # activation me?" would sit under three paragraphs about satellites — which is how a real
    # compromise goes unnoticed.
    workspace_count = len(await store.member_edges(account.id))
    background.add_task(
        mailer.send_profile_activated,
        account.email,
        account.first_name,
        organisation_name=organisation,
        context=context,
    )
    background.add_task(
        mailer.send_team_welcome,
        account.email,
        account.first_name,
        organisation_name=organisation,
        workspace_count=workspace_count,
    )

    token, expires_in = security.issue_session(account.id, account.kind.value)
    return SessionToken(access_token=token, expires_in=expires_in, account=account)


@router.post("/team/invitations/resend", status_code=status.HTTP_201_CREATED)
async def resend_team_invitation(
    payload: dict,
    request: Request,
    account: Account = Depends(require_permission(Permission.MANAGE_TEAM)),
) -> dict:
    """Reissue an outstanding invitation, including one whose 14 days have elapsed.

    ## Who may do this

    Anyone holding `team:manage` on a workspace the invitation names — Organization Owner,
    Operations, or a Custom role granted it. Checked per named workspace rather than on the
    union, so an Operations member of one project cannot resend an invitation into another.

    ## A fresh token, not an extended expiry

    Extending the existing `expires_at` would leave the original link live after two weeks in a
    mailbox that may have been forwarded. Reissuing destroys the old token hash, so only the
    newly emailed link works.

    The grants are carried over unchanged — a resend is "send that again". Changing the role
    means revoking and inviting afresh, so an Operations member resending an Owner's invitation
    cannot alter what was offered.
    """
    _require_store()

    email = str(payload.get("email", "")).strip().lower()
    if not email:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Missing email address.")

    organisation = await store.organisation_for(account.id)
    existing = await store.find_invitation(organisation, email)
    if existing is None:
        # 404 covers both "never invited" and "already accepted". Distinguishing them would
        # report on the state of an address the caller may simply have mistyped.
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No outstanding invitation for that address. If they have already joined, they "
            "appear under Members; otherwise send a new invitation.",
        )

    # Authority is per workspace, matching every other workspace-scoped operation: holding
    # team management on one project must not let someone resend an invitation into another.
    for grant in existing.get("grants", []):
        workspace_id = grant.get("workspace_id")
        if not workspace_id:
            continue
        if Permission.MANAGE_TEAM not in await store.member_permissions_in(
            account.id, workspace_id
        ):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "This invitation covers a workspace you do not manage, so you cannot resend "
                "it. An Organization Owner of that workspace can.",
            )

    reissued = await store.resend_invitation(organisation, email, account.id)
    if reissued is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not reissue the invitation."
        )

    document, plaintext = reissued
    sent = await mailer.send_team_invitation(
        email,
        organisation_name=account.organisation or "your organisation",
        inviter=f"{account.first_name} {account.last_name}".strip(),
        token=plaintext,
        workspaces=[g.get("workspace_id", "") for g in document.get("grants", [])],
        context=_request_context(request),
    )

    await store.record_audit(
        account_id=account.id,
        action=AuditAction.MEMBER_INVITE_RESENT,
        target_id=email,
        detail=", ".join(
            f"{g.get('workspace_id')}:{g.get('role')}" for g in document.get("grants", [])
        ),
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    # The token is never returned, even to an owner — an invitation is a credential, and the
    # only place it should exist is the invited person's mailbox.
    return {
        "resent": email,
        "email_sent": sent,
        "expires_at": document["expires_at"].isoformat(),
        "detail": (
            "A new link is on its way. Any earlier invitation to this address no longer "
            "works."
        ),
    }


@router.put("/team/{member_account_id}/grants")
async def set_grants(
    member_account_id: str,
    grants: list[WorkspaceGrant],
    request: Request,
    account: Account = Depends(require_permission(Permission.MANAGE_TEAM)),
) -> dict:
    """Replace a colleague's workspace roles with exactly this set.

    A workspace omitted here has its access revoked, which is the intended reading of an edit
    form: the administrator is stating the full set, not adding to it.

    An owner cannot demote themselves to the point where nobody can administer the
    organisation — refused rather than allowed, because the recovery from it requires an
    operator with database access.
    """
    _require_store()

    organisation = await store.organisation_for(account.id)
    known = {w["id"] for w in await store.list_workspaces(organisation)}
    assignable = {r.value for r in team_mod.grantable_roles(
        await store.member_permissions(account.id)
    )}

    for grant in grants:
        if grant.workspace_id not in known:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such workspace.")
        if Permission.MANAGE_TEAM not in await store.member_permissions_in(
            account.id, grant.workspace_id
        ):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "You can only change access on a workspace you manage.",
            )
        if grant.role not in assignable:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"You cannot assign the {grant.role} role.",
            )

    if member_account_id == account.id and not any(
        grant.role == roles_mod.Role.OWNER.value for grant in grants
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "You cannot remove your own Organization Owner role. Grant it to a colleague "
            "first — an organisation with no owner cannot be administered by anyone.",
        )

    written = await store.set_member_grants(
        organisation, member_account_id, [g.model_dump() for g in grants]
    )
    if not written:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not update access."
        )

    await store.record_audit(
        account_id=member_account_id,
        actor_id=account.id,
        actor_kind="aggregator",
        action=AuditAction.MEMBER_ROLE_CHANGED,
        target_id=member_account_id,
        detail=", ".join(f"{g.workspace_id}:{g.role}" for g in grants),
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return {"updated": written}


@router.delete(
    "/team/{member_account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # Required alongside a 204: FastAPI infers a response model from the `-> None`
    # annotation and then asserts that a 204 has no body, which fails at import time rather
    # than at request time — the app does not start at all.
    response_model=None,
)
async def remove_member(
    member_account_id: str,
    request: Request,
    account: Account = Depends(require_permission(Permission.MANAGE_TEAM)),
) -> None:
    """Remove a colleague's access to every workspace in this organisation.

    Their ACCOUNT survives — this removes membership, not the person. Deleting the account
    would take their own areas and alert history with it, which is not what "remove from team"
    means and is not reversible.
    """
    _require_store()

    if member_account_id == account.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "You cannot remove your own access. Ask another Organization Owner.",
        )

    organisation = await store.organisation_for(account.id)
    if not await store.revoke_member(organisation, member_account_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such team member.")

    await store.record_audit(
        account_id=member_account_id,
        actor_id=account.id,
        actor_kind="aggregator",
        action=AuditAction.MEMBER_REMOVED,
        target_id=member_account_id,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


# --------------------------------------------------------------------------- #
# Usage — the billing surface
#
# Two audiences, two prices, one platform. See `app/iam/attribution.py` for the model.
# --------------------------------------------------------------------------- #


class UsageLine(BaseModel):
    """Assessments consumed for one monitored area over the period."""

    aoi_id: str
    assessments: int
    #: The aggregator's own reference for this farmer or plot, when they supplied one. Present so
    #: an invoice can be reconciled against their system without a lookup on our side.
    external_ref: str | None = None
    #: The aggregator project this area belongs to. Null for an individual, who has no projects,
    #: and null on an aggregator's area created before workspaces existed or repaired by
    #: reconciliation without a resolvable project — see `workspaces` below.
    workspace_id: str | None = None


class WorkspaceUsage(BaseModel):
    """Consumption for one of an aggregator's projects.

    The granularity a partner actually reconciles at: "what did the Kano rollout cost this
    quarter" is answerable per project and not from an aggregator-wide total.
    """

    #: Null groups every area not assigned to a project. Reported rather than dropped, so the
    #: breakdown always sums to the totals above — a short sum would read as missing revenue.
    workspace_id: str | None
    areas: int
    subjects: int
    assessments: int


class UsageReport(BaseModel):
    """What this account consumed, and therefore what it is billed for.

    ## Where each number comes from

    Counts are aggregated in **Postgres** by `aoi_id`, which holds no tenant column. Ownership is
    resolved in **Mongo**. That split is deliberate: the pipeline keeps producing assessments when
    the IAM store is unavailable, because Scout, Analyst and Oracle never ask who is paying.
    """

    owner_id: str
    #: `individual` (B2C, personal subscription) or `aggregator` (B2B, billed as one commercial
    #: customer for all their farmers). Never both — an individual has no aggregator.
    owner_kind: str | None
    period_start: str | None = None
    period_end: str | None = None
    #: Areas currently monitored and billable.
    areas: int
    #: Distinct farmers behind those areas. Equals `areas` for an individual; for an aggregator
    #: it is what makes "1,204 areas across 890 farmers" sayable on an invoice.
    subjects: int
    total_assessments: int
    #: Per-project breakdown. Empty for an individual. Sums to `areas` and `total_assessments`.
    workspaces: list[WorkspaceUsage] = []
    lines: list[UsageLine]


@router.get("/usage", response_model=UsageReport)
async def my_usage(
    since: str | None = Query(
        default=None,
        description="ISO-8601 start of the billing period. Omit for all time.",
    ),
    until: str | None = Query(
        default=None, description="ISO-8601 end, exclusive."
    ),
    workspace_id: str | None = Query(
        default=None,
        description=(
            "Narrow to one of your projects. Applied IN ADDITION to your own ownership, never "
            "instead of it — a workspace id is not a credential. Ignored for individuals, who "
            "have no projects."
        ),
    ),
    account: Account = Depends(verified_account),
) -> UsageReport:
    """What this account consumed. **An aggregator sees its own customers' usage, not theirs.**

    Scoped to the caller: `owned_aoi_ids` resolves only areas billed to this account, so an
    aggregator cannot read another's consumption and an individual sees only their own plots.
    That scoping is the authorisation — there is no id parameter to tamper with.

    `workspace_id` narrows the lines to one project. It is a **filter, not a scope**: the
    ownership predicate is unconditional, so passing another aggregator's workspace id returns
    nothing rather than their data.
    """
    _require_store()

    start = _parse_period(since, "since")
    end = _parse_period(until, "until")

    # Include ended areas when pricing a closed period: one removed mid-month was still
    # monitored for part of it and belongs on that invoice.
    aoi_ids = await store.owned_aoi_ids(
        account.id, include_ended=start is not None, workspace_id=workspace_id
    )
    counts = await repository.count_assessments_by_area(aoi_ids, since=start, until=end)
    summary = await store.attribution_summary(account.id)

    lines: list[UsageLine] = []
    # Assessment counts per project, accumulated from the lines we are already building rather
    # than from a second aggregation. The counts live in Postgres and the projects in Mongo, so
    # there is no single query that could produce this — and summing here guarantees the
    # breakdown and the line items agree, which two independent queries could not.
    assessments_by_workspace: dict[str | None, int] = {}

    for aoi_id in aoi_ids:
        record = await store.attribution_for(aoi_id) or {}
        assessments = counts.get(aoi_id, 0)
        project = record.get("workspace_id")
        assessments_by_workspace[project] = (
            assessments_by_workspace.get(project, 0) + assessments
        )
        lines.append(
            UsageLine(
                aoi_id=aoi_id,
                assessments=assessments,
                external_ref=record.get("external_ref"),
                workspace_id=project,
            )
        )

    # Area and farmer counts come from the Mongo aggregation (current, unfiltered by period);
    # assessment counts come from Postgres over the requested window. Joined on the project id.
    workspaces = [
        WorkspaceUsage(
            workspace_id=group["workspace_id"],
            areas=group["areas"],
            subjects=group["subjects"],
            assessments=assessments_by_workspace.get(group["workspace_id"], 0),
        )
        for group in summary.get("workspaces", [])
        # Honour the filter here too, so a caller asking for one project does not receive a
        # breakdown spanning all of them.
        if workspace_id is None or group["workspace_id"] == workspace_id
    ]

    return UsageReport(
        owner_id=account.id,
        owner_kind=summary["owner_kind"],
        period_start=start.isoformat() if start else None,
        period_end=end.isoformat() if end else None,
        areas=summary["areas"],
        subjects=summary["subjects"],
        total_assessments=sum(counts.values()),
        workspaces=workspaces,
        lines=lines,
    )


def _parse_period(value: str | None, field: str) -> datetime | None:
    """ISO-8601 or a 422. Naive input is read as UTC.

    Rejected loudly rather than silently ignored: a malformed date that fell through to "all
    time" would produce an invoice for the wrong period, and the caller would have no way to
    tell from the response.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"`{field}` must be an ISO-8601 timestamp, e.g. 2026-08-01T00:00:00Z.",
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Customer areas — the Partner API's area lifecycle
#
# `POST /subscribers/{id}/areas` and friends exist, but they are gated on
# `platform:subscribers:write` — a SERVICE-account scope that `create_api_key` refuses to mint
# for a commercial account. So an aggregator could not manage its own customers' areas at all:
# the routes existed and were unreachable by the audience that needs them.
#
# These are the same operations behind `require_scope(WRITE)` and `owned_account`, so the CBN
# Anchor Scheme can add a farmer's second plot or correct a name through the Partner API, and
# still cannot touch another aggregator's customers.
# --------------------------------------------------------------------------- #


class CustomerAreaPatch(BaseModel):
    """What may be changed on a customer's monitored area.

    **Deliberately excludes geometry.** Renaming and re-cropping are in-place edits that keep the
    `aoi_id`, so the assessment history stays attached and stays *meaningful*. Moving or resizing
    an area is different in kind: past assessments measured the old footprint, so afterwards one
    timeline mixes readings of two different pieces of ground under one heading.

    An aggregator that needs a different footprint should add a new area — each then keeps its own
    clean history — and remove the old one if it is no longer farmed. The recommendation is stated
    in the route description rather than being folklore.
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    crop: str | None = Field(default=None, max_length=60)


@router.get("/customers/{account_id}/areas", response_model=list[AreaOfInterest])
async def list_customer_areas(
    account_id: str,
    aggregator: Aggregator = Depends(require_scope(ApiKeyScope.READ)),
) -> list[AreaOfInterest]:
    """Every area monitored for one of your customers."""
    _require_store()

    account = await owned_account(account_id, aggregator)
    if not account.subscriber_id:
        return []

    subscriber = await repository.get_subscriber(account.subscriber_id)
    return subscriber.areas if subscriber else []


@router.post(
    "/customers/{account_id}/areas",
    response_model=AreaOfInterest,
    status_code=status.HTTP_201_CREATED,
)
async def add_customer_area(
    account_id: str,
    payload: AreaOfInterest,
    background: BackgroundTasks,
    aggregator: Aggregator = Depends(require_scope(ApiKeyScope.WRITE)),
) -> AreaOfInterest:
    """Add another plot for an existing customer, and scan it immediately.

    **There is no limit on areas per customer.** A farmer with four scattered plots is the normal
    case, not an edge one, and each is assessed independently on every satellite pass.

    Scanned on creation for the same reason onboarding is: a plot added during a developing flood
    should not wait up to six hours for the next scheduled cycle.

    Billing attribution is inherited from the customer's existing areas, so a new plot lands on
    the same invoice as the rest rather than being re-derived — see `app/iam/attribution.py`.
    """
    _require_store()

    account = await owned_account(account_id, aggregator)
    if not account.subscriber_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This customer has no subscription yet. Onboard them with an `area` first, then "
            "add further plots here.",
        )

    area = _normalise_area(payload)
    subscriber = await repository.get_subscriber(account.subscriber_id)
    if subscriber is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Subscription not found.")

    try:
        created = await repository.add_area(account.subscriber_id, area)
    except repository.DuplicateAreaError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if created is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Subscription not found.")

    # The aggregator remains the billable party for every plot of this farmer.
    await store.record_attribution(
        aoi_id=created.id,
        owner_kind=attribution.OwnerKind.AGGREGATOR,
        owner_id=aggregator.account.id,
        subscriber_id=account.subscriber_id,
        subject_account_id=account.id,
        # The presented key's project, as on the onboarding path.
        workspace_id=aggregator.workspace_id,
    )

    from app.agents import pipeline

    await pipeline.enqueue_scan(subscriber, created)

    await store.record_audit(
        account_id=account.id,
        actor_id=aggregator.account.id,
        actor_kind="aggregator",
        action=AuditAction.AREA_ADDED,
        target_id=created.id,
        detail=f"{created.name} · {created.hectares or '?'} ha",
    )

    # Tell the CUSTOMER, not the aggregator.
    #
    # Reported: an aggregator created a monitoring area for a customer and nothing was sent to
    # anyone. The area was queued and audited, so the only confirmation was the HTTP 201 the
    # integration received — which the farmer whose land it is never sees.
    #
    # `added_by` names the aggregator, because someone who did not press the button themselves
    # should be told who did. A silent change to what is watched on your land is not acceptable
    # even when it is legitimate.
    background.add_task(
        mailer.send_area_added,
        account.email,
        account.first_name,
        area_name=created.name,
        hectares=created.hectares,
        admin1=created.admin1,
        admin2=created.admin2,
        country=created.country,
        added_by=aggregator.account.organisation or None,
    )

    return created


class ScanQueued(BaseModel):
    """Acknowledgement that a scan was accepted onto the queue.

    Deliberately NOT an assessment. See the route for why the work is queued rather than awaited;
    the fields here are what a partner needs to correlate the eventual result, and nothing that
    would imply a reading already exists.
    """

    aoi_id: str = Field(description="The area queued.", examples=["aoi_091d52d4"])
    job_id: str = Field(
        description=(
            "The queued job. Identifies the first stream message, not the whole scan — it is "
            "regenerated per stage and again per retry, so quote `aoi_id` in a support request."
        ),
        examples=["job_3f8a1c2e"],
    )
    #: **No `run_id` here, deliberately.** It is the id that would actually be useful — one value
    #: spanning Scout → Analyst → Oracle → Herald — but `enqueue_scan` mints it internally, binds
    #: it for the duration of the enqueue and returns `job.id`. Reading `tracing.current_run_id()`
    #: after that call returns None, because `trace()` resets on exit. Publishing a field that is
    #: always null would be worse than omitting it, and widening `enqueue_scan`'s return type to
    #: expose it belongs in a change that needs it at more than one call site.
    queued_at: datetime
    detail: str = Field(
        description="What to expect, in plain language.",
        examples=[
            "Queued. The assessment lands in a few minutes and reaches you as a "
            "`shelter.alert` webhook if it crosses your threshold."
        ],
    )


@router.post(
    "/customers/{account_id}/areas/{aoi_id}/scan",
    response_model=ScanQueued,
    status_code=status.HTTP_202_ACCEPTED,
    # Declared rather than left to prose. FastAPI infers only 202 and 422 from the signature, so a
    # generated client would have no branch for the two outcomes a partner integration must
    # actually handle — a 429 needs a backoff, and a 409 means stop retrying and fix the
    # subscription. Undocumented, both arrive as "unexpected error" in generated code.
    responses={
        403: {"description": "The key lacks the `scan:trigger` scope."},
        404: {
            "description": (
                "No such area for this customer. Deliberately identical to the response for an "
                "area belonging to another tenant — area ids appear in webhook payloads, so a "
                "distinguishable refusal would confirm one exists."
            )
        },
        409: {
            "description": (
                "Nothing to scan: the customer has no subscription, or it is paused. Retrying "
                "will not help — reactivate the subscription first."
            )
        },
        429: {
            "description": (
                "This area's hourly scan limit is reached. Carries `Retry-After`. Scheduled "
                "monitoring is unaffected."
            )
        },
    },
)
async def trigger_customer_area_scan(
    account_id: str,
    aoi_id: str,
    aggregator: Aggregator = Depends(require_scope(ApiKeyScope.SCAN)),
) -> ScanQueued:
    """Request an immediate assessment for one of your customers' areas.

    The area is normally assessed on the ~6-hour watch loop. This puts it at the front of the
    queue, for the case a partner has information we do not: a member reports water in the field,
    a dam release is announced, a plot was onboarded during a developing flood.

    ## 202, and why the assessment is not in the response

    A full scan is 10-40 seconds of STAC search, windowed COG reads and two forward passes, and
    it is the *Analyst* that is slow. Holding an HTTP connection open for it would make a partner's
    integration inherit our upstream latency, and a client timeout at 30s would abandon a scan that
    was going to succeed — leaving the caller with no result and us with the quota spent.

    So the work is queued and the result arrives the way every other assessment does: as a
    `shelter.alert` webhook if it crosses the subscriber's threshold, and on
    `GET /risk/areas/{aoi_id}` either way. That also means a triggered scan and a scheduled one are
    the *same* code path — `app/agents/pipeline.enqueue_scan` — rather than a second implementation
    that could drift.

    `POST /risk/assess` is the synchronous alternative, and it is deliberately not this: it takes a
    geometry rather than an area id, so it cannot be scoped to a tenant's own customers.

    ## Rate-limited per area, not per key

    `SCAN_TRIGGER_RATE_LIMIT_PER_HOUR` (default 4). Sentinel-1 revisits West Africa about every 6
    days, so re-scanning one plot every minute re-reads one scene for an identical answer and
    spends quota on free upstreams every deployment shares. Per area because that is where the cost
    falls — a per-key cap would let a large aggregator starve the queue while a small one is
    throttled over a single plot.

    **429 carries `Retry-After`.** A partner integration retries on a schedule; a limit with no
    stated interval invites a tight loop, which is the behaviour being limited.

    ## Scope

    `scan:trigger`, which is **not** granted by default — this spends real catalogue quota, so it
    is asked for explicitly. 404 (never 403) for an area outside your customers: area ids appear in
    webhook payloads, so a 403 would turn any of them into a membership oracle.
    """
    _require_store()

    account = await owned_account(account_id, aggregator)
    if not account.subscriber_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This customer has no subscription yet, so there is nothing to scan. Onboard them "
            "with an `area` first.",
        )

    # `get_area` returns `(subscriber_id, area)` precisely so authorisation needs no second query.
    #
    # Both halves are load-bearing. Without the ownership comparison this route would scan any
    # area id in the platform on request — the caller would learn nothing directly, since the
    # response carries no reading, but it would let one tenant spend another's quota and fire
    # advisories to a stranger's plot at a chosen moment.
    owned = await repository.get_area(aoi_id)
    if owned is None or owned[0] != account.subscriber_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Area not found.")

    subscriber = await repository.get_subscriber(account.subscriber_id)
    if subscriber is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Subscription not found.")

    # Refuse an INACTIVE subscription rather than queueing work whose result is discarded.
    #
    # `scheduler` skips inactive subscribers, so an accepted scan here would be measured, persisted
    # and then never dispatched — a 202 promising a webhook that cannot arrive. Saying so is the
    # difference between a paused subscription and a broken integration.
    if not subscriber.active:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This customer's subscription is paused, so a scan would produce no alert. "
            "Reactivate it first.",
        )

    # Throttle AFTER authorisation, so a caller cannot consume another tenant's budget by
    # guessing ids, and BEFORE the enqueue, so a refusal costs no queue work.
    #
    # `cache.incr` fails OPEN (returns 0 when the cache is unreachable). That is the right default
    # here for the same reason it is for chat: this route's ceiling is a courtesy to shared free
    # upstreams, and refusing a legitimate flood-season scan over a missing Redis key is the worse
    # failure. The queue's own dedupe and the scheduler's poll-state backoff still apply.
    limit = settings.scan_trigger_rate_limit_per_hour
    used = await cache.incr(cache.key("scan-trigger", aoi_id), 3_600)
    if limit > 0 and used > limit:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"This area has already been scanned {limit} times this hour. Satellites revisit "
            f"every few days, so a further scan now would re-read the same imagery. The area "
            f"remains on the scheduled watch loop.",
            # Stated, not implied. Without it a retry loop is the natural implementation, and a
            # retry loop is what this limit exists to prevent.
            headers={"Retry-After": "3600"},
        )

    area = owned[1]

    from app.agents import pipeline

    job_id = await pipeline.enqueue_scan(subscriber, area)

    await store.record_audit(
        account_id=account.id,
        actor_id=aggregator.account.id,
        actor_kind="aggregator",
        action=AuditAction.CUSTOMER_SCAN_TRIGGERED,
        target_id=aoi_id,
        detail=f"{area.name} · job {job_id}",
    )

    log.info(
        "partner triggered a scan",
        extra={
            "aoi_id": aoi_id,
            "subscriber_id": account.subscriber_id,
            "aggregator_id": aggregator.account.id,
            "workspace_id": aggregator.workspace_id,
            "job_id": job_id,
        },
    )

    return ScanQueued(
        aoi_id=aoi_id,
        job_id=job_id,
        queued_at=datetime.now(timezone.utc),
        detail=(
            f"Queued for {area.name}. The assessment usually lands within a few minutes and "
            f"reaches you as a `shelter.alert` webhook if it crosses the subscriber's threshold. "
            f"GET /risk/areas/{aoi_id} returns it either way."
        ),
    )


@router.patch("/customers/{account_id}/areas/{aoi_id}", response_model=AreaOfInterest)
async def patch_customer_area(
    account_id: str,
    aoi_id: str,
    payload: CustomerAreaPatch,
    aggregator: Aggregator = Depends(require_scope(ApiKeyScope.WRITE)),
) -> AreaOfInterest:
    """Rename or re-crop a customer's area. **The `aoi_id` and its history survive.**

    Safe by construction: the update is in place, so every past assessment stays attached to the
    same plot and stays meaningful — the ground being described has not changed.

    **Geometry is not editable here, on purpose.** Moving or resizing an area would leave the
    timeline mixing measurements of two different footprints under one name: a "65% flooded"
    reading from last week would describe land the customer may no longer farm. Add a new area
    instead — each keeps its own clean history — and remove the old one if it is out of use.
    """
    _require_store()

    account = await owned_account(account_id, aggregator)
    owned = await repository.get_area(aoi_id)
    if owned is None or owned[0] != account.subscriber_id:
        # 404, not 403: a 403 would confirm the id exists under another aggregator.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Area not found.")

    updated = await repository.update_area(
        aoi_id, name=payload.name, crop=payload.crop
    )
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Area not found.")

    await store.record_audit(
        account_id=account.id,
        actor_id=aggregator.account.id,
        actor_kind="aggregator",
        action=AuditAction.AREA_UPDATED,
        target_id=aoi_id,
        detail=f"name={payload.name or '—'} crop={payload.crop or '—'}",
    )
    return updated


@router.delete(
    "/customers/{account_id}/areas/{aoi_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def remove_customer_area(
    account_id: str,
    aoi_id: str,
    aggregator: Aggregator = Depends(require_scope(ApiKeyScope.WRITE)),
) -> None:
    """Stop monitoring one of a customer's plots.

    Past assessments are **kept**: they record what a satellite measured on a date, and removing
    the plot does not make that untrue. Billing for it stops — the attribution period is closed,
    not deleted, so a past invoice stays explainable.

    Refuses the customer's last area (409): a subscription with none is active but watching
    nowhere. Detach the customer instead.
    """
    _require_store()

    account = await owned_account(account_id, aggregator)
    owned = await repository.get_area(aoi_id)
    if owned is None or owned[0] != account.subscriber_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Area not found.")

    try:
        removed = await repository.delete_area(aoi_id)
    except repository.LastAreaError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Area not found.")

    await store.end_attribution(aoi_id)

    await store.record_audit(
        account_id=account.id,
        actor_id=aggregator.account.id,
        actor_kind="aggregator",
        action=AuditAction.AREA_REMOVED,
        target_id=aoi_id,
    )


# --------------------------------------------------------------------------- #
# Workspace → Customers → Areas, for the aggregator PORTAL
#
# The routes above are key-gated, which is right for a machine integration and wrong for a human:
# an operator would have to paste their own API key into the UI they are signed in to, and a key
# pasted into a browser form is a key in browser history, in a screenshot, and in a support chat.
#
# These are the same operations behind a portal session plus `workspace:manage` on the named
# workspace, so an aggregator can onboard one customer by hand and watch it work before writing a
# line of Partner API code. Scoped by the workspace in the PATH rather than by a key, and checked
# per workspace — holding the permission on one project grants nothing on another.
# --------------------------------------------------------------------------- #


class WorkspaceCustomer(BaseModel):
    """One customer in a workspace, with enough detail to be useful in a list."""

    account_id: str
    email: str
    full_name: str
    external_ref: str | None = None
    subscriber_id: str | None = None
    areas: int = 0


@router.get(
    "/workspaces/{workspace_id}/customers", response_model=list[WorkspaceCustomer]
)
async def workspace_customers(
    workspace_id: str,
    account: Account = Depends(require_workspace_permission(Permission.VIEW_CUSTOMERS)),
) -> list[WorkspaceCustomer]:
    """Customers in this workspace.

    `require_workspace_permission` resolves the caller's role **on this workspace**, so a member
    who is View-Only here cannot list customers by holding a wider role on another project.
    """
    _require_store()

    organisation = await store.organisation_for(account.id)
    customers = await store.list_tenant_accounts(
        organisation, limit=500, workspace_id=workspace_id
    )

    out: list[WorkspaceCustomer] = []
    for customer in customers:
        edge = await store.get_membership(
            customer.id, organisation, workspace_id=workspace_id
        )
        area_count = 0
        if customer.subscriber_id:
            subscriber = await repository.get_subscriber(customer.subscriber_id)
            area_count = len(subscriber.areas) if subscriber else 0
        out.append(
            WorkspaceCustomer(
                account_id=customer.id,
                email=customer.email,
                full_name=f"{customer.first_name} {customer.last_name}".strip(),
                external_ref=edge.external_ref if edge else None,
                subscriber_id=customer.subscriber_id,
                areas=area_count,
            )
        )
    return out


class WorkspaceAreaRow(BaseModel):
    """One monitored area in a workspace, with the customer it belongs to and how they are reached.

    ## Why this shape exists

    An aggregator could see its customers and, separately, drill into one customer's areas. It could
    not see **the workspace's monitoring as a whole** — which plots are being watched, whose they
    are, and where each one's alerts go. Reported from a real test account whose workspace showed
    monitoring areas that appeared attached to nobody.

    The association the aggregator actually reasons about is
    `Workspace > Customer > Area > where the alert goes`, and answering it required three round
    trips plus a client-side join that no page performed. This row is that chain, resolved
    server-side.

    ## Absent fields mean unknown, never a default

    `customer_account_id is None` means the area carries no attribution — an area created before
    attribution existed, or one whose row was written wrong. It renders as "not linked to a
    customer" and is exactly the state that made this bug invisible, so it is surfaced rather than
    hidden behind a fallback that would make a broken link look like a valid one.
    """

    aoi_id: str
    name: str
    hectares: float | None = None
    crop: str | None = None
    #: Who contacts this subscriber about this plot — `direct` | `webhook` | `both`.
    delivery_mode: DeliveryMode = DeliveryMode.DIRECT

    #: The customer this plot belongs to. **None means unattributed** — see the class docstring.
    customer_account_id: str | None = None
    customer_name: str | None = None
    customer_email: str | None = None
    subscriber_id: str | None = None
    #: The aggregator's own reference for this customer, when they supplied one.
    external_ref: str | None = None
    #: True when the plot belongs to the aggregator itself rather than an onboarded customer.
    #: A real case, and one that used to be indistinguishable from a broken link.
    is_own_plot: bool = False

    #: Where alerts for THIS plot go, resolved through `Subscriber.channels_for` so a per-plot
    #: override is reflected rather than the subscriber's general setting.
    channels: list[ChannelBinding] = Field(default_factory=list)
    #: Alerts delivered for this plot, and when the most recent one was.
    alert_count: int = 0
    last_alert_at: datetime | None = None
    #: Latest severity, or None when no scan has completed yet — "queued", not "no risk".
    latest_severity: str | None = None


@router.get(
    "/workspaces/{workspace_id}/areas", response_model=list[WorkspaceAreaRow]
)
async def workspace_areas(
    workspace_id: str,
    account: Account = Depends(require_workspace_permission(Permission.VIEW_CUSTOMERS)),
) -> list[WorkspaceAreaRow]:
    """Every monitored area in this workspace, with its customer and its alert delivery.

    The one call that answers `Workspace > Customer > Area > Alerts`. Scoped by
    `require_workspace_permission`, so a member who is View-Only on this project cannot read it by
    holding a wider role on another.

    Includes the aggregator's **own** plots alongside its customers', flagged by `is_own_plot`. They
    are genuinely part of the workspace's monitoring and omitting them would reproduce the original
    complaint in reverse — a workspace showing fewer areas than the portal does.
    """
    _require_store()

    organisation = await store.organisation_for(account.id)

    # Every account whose areas belong to this workspace: the customers, plus the aggregator itself.
    accounts: list[Account] = list(
        await store.list_tenant_accounts(
            organisation, limit=500, workspace_id=workspace_id
        )
    )
    own = await store.get_account(organisation)
    if own is not None and own.subscriber_id:
        accounts.append(own)

    rows: list[WorkspaceAreaRow] = []
    for holder in accounts:
        if not holder.subscriber_id:
            continue
        subscriber = await repository.get_subscriber(holder.subscriber_id)
        if subscriber is None:
            continue

        edge = await store.get_membership(
            holder.id, organisation, workspace_id=workspace_id
        )

        for area in subscriber.areas:
            record = await store.attribution_for(area.id)
            # An area attributed to a DIFFERENT workspace is not this workspace's business. An
            # unattributed one is included, because hiding it is what let the broken links go
            # unnoticed — the aggregator needs to see that the link is missing.
            if record is not None and record.get("workspace_id") not in (
                None,
                workspace_id,
            ):
                continue

            history = await repository.assessment_history(area.id, limit=1)
            alerts = await repository.list_alerts(subscriber.id, limit=200)
            for_area = [a for a in alerts if a.assessment.aoi_id == area.id]

            rows.append(
                WorkspaceAreaRow(
                    aoi_id=area.id,
                    name=area.name,
                    hectares=area.hectares,
                    crop=area.crop,
                    delivery_mode=area.delivery_mode,
                    customer_account_id=holder.id,
                    customer_name=f"{holder.first_name} {holder.last_name}".strip(),
                    customer_email=holder.email,
                    subscriber_id=subscriber.id,
                    external_ref=edge.external_ref if edge else None,
                    is_own_plot=holder.id == organisation,
                    # Per-plot resolution: a binding naming this area REPLACES the general ones,
                    # so this shows what would actually be used rather than every row on file.
                    channels=subscriber.channels_for(Severity.INFO, aoi_id=area.id),
                    alert_count=len(for_area),
                    last_alert_at=for_area[0].created_at if for_area else None,
                    latest_severity=(
                        history[0].severity.value if history else None
                    ),
                )
            )

    # Most recently alerted first, then unalerted plots by name — the reading order an operator
    # wants: what needs attention, then what is merely being watched.
    rows.sort(key=lambda r: (r.last_alert_at is None, -(r.alert_count), r.name))
    return rows

@router.post(
    "/workspaces/{workspace_id}/customers",
    response_model=WorkspaceCustomer,
    status_code=status.HTTP_201_CREATED,
)
async def add_workspace_customer(
    workspace_id: str,
    payload: CustomerCreate,
    request: Request,
    account: Account = Depends(require_workspace_permission(Permission.MANAGE_CUSTOMERS)),
) -> WorkspaceCustomer:
    """Onboard one customer into this workspace by hand.

    The same operation as `POST /iam/customers` on the Partner API, and it produces an identical
    record — so an aggregator can prove the flow works in the portal, see the first assessment
    arrive, and only then automate it. That is the point of having this at all: a partner should
    not have to debug their first integration against an API they have never seen succeed.

    No password is created. The customer claims their account by email and chooses their own — an
    aggregator who could set it could impersonate the farmer with no audit trail.
    """
    _require_store()

    organisation = await store.organisation_for(account.id)

    reject_unavailable_channel(payload.preferred_channel)

    existing_account = await store.get_account_by_email(str(payload.email))
    is_new = existing_account is None

    if existing_account is None:
        created, _token = await store.create_account(
            kind=AccountKind.INDIVIDUAL,
            email=str(payload.email),
            first_name=payload.first_name,
            last_name=payload.last_name,
            password=None,
            phone=payload.phone,
            language=payload.language,
            preferred_channel=payload.preferred_channel.value,
        )
        if created is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "Could not create the customer."
            )
        customer = created
    else:
        if existing_account.kind is not AccountKind.INDIVIDUAL:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "That address belongs to a commercial account and cannot be onboarded as a "
                "customer.",
            )
        customer = existing_account

    await store.attach_membership(
        customer.id,
        organisation,
        external_ref=payload.external_ref,
        onboarded_by_this_tenant=is_new,
        workspace_id=workspace_id,
    )

    subscriber_id: str | None = customer.subscriber_id
    area_count = 0

    if payload.area is not None:
        area = _normalise_area(payload.area)
        subscriber = Subscriber(
            id=customer.subscriber_id or await store.mint_subscriber_id(),
            name=f"{customer.first_name} {customer.last_name}".strip(),
            kind=SubscriberKind.FARMER,
            language=payload.language,
            areas=[area],
            channels=[
                ChannelBinding(channel=payload.preferred_channel, address=customer.email)
            ],
        )
        try:
            await repository.save_subscriber(subscriber)
            await store.bind_subscriber(customer.id, subscriber.id)
            subscriber_id = subscriber.id
            area_count = 1

            await store.record_attribution(
                aoi_id=area.id,
                owner_kind=attribution.OwnerKind.AGGREGATOR,
                owner_id=organisation,
                subscriber_id=subscriber.id,
                subject_account_id=customer.id,
                external_ref=payload.external_ref,
                # The project this customer was onboarded into, so the invoice can break down per
                # customer base. Known here and nowhere later — see `iam/attribution.py`.
                workspace_id=workspace_id,
            )

            from app.agents import pipeline

            await pipeline.enqueue_scan(subscriber, area)
        except Exception:
            # The customer exists and is durable; the plot can be added afterwards. Degrades
            # rather than failing the onboarding, matching the Partner API path.
            log.warning(
                "workspace customer created but area binding failed",
                extra={"account_id": customer.id, "workspace_id": workspace_id},
            )

    await store.record_audit(
        account_id=customer.id,
        actor_id=account.id,
        actor_kind="aggregator",
        action=AuditAction.CUSTOMER_ONBOARDED,
        target_id=workspace_id,
        detail=f"{customer.email} · via portal",
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    return WorkspaceCustomer(
        account_id=customer.id,
        email=customer.email,
        full_name=f"{customer.first_name} {customer.last_name}".strip(),
        external_ref=payload.external_ref,
        subscriber_id=subscriber_id,
        areas=area_count,
    )


@router.get(
    "/workspaces/{workspace_id}/customers/{account_id}/areas",
    response_model=list[AreaOfInterest],
)
async def workspace_customer_areas(
    workspace_id: str,
    account_id: str,
    account: Account = Depends(require_workspace_permission(Permission.VIEW_CUSTOMERS)),
) -> list[AreaOfInterest]:
    """One customer's monitored plots, within this workspace."""
    _require_store()

    customer = await _workspace_customer(account, workspace_id, account_id)
    if not customer.subscriber_id:
        return []
    subscriber = await repository.get_subscriber(customer.subscriber_id)
    return subscriber.areas if subscriber else []


@router.post(
    "/workspaces/{workspace_id}/customers/{account_id}/areas",
    response_model=AreaOfInterest,
    status_code=status.HTTP_201_CREATED,
)
async def add_workspace_customer_area(
    workspace_id: str,
    account_id: str,
    payload: AreaOfInterest,
    background: BackgroundTasks,
    account: Account = Depends(require_workspace_permission(Permission.MANAGE_CUSTOMERS)),
) -> AreaOfInterest:
    """Add another plot for a customer. Scanned immediately; no limit on plots.

    **This is the route the portal uses**, and it was the third of three add-area paths that
    created an area, queued a scan, and told nobody. See `mailer.send_area_added`.
    """
    _require_store()

    customer = await _workspace_customer(account, workspace_id, account_id)
    if not customer.subscriber_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This customer has no subscription yet. Add their first area when onboarding, or "
            "onboard them again with an area.",
        )

    area = _normalise_area(payload)
    subscriber = await repository.get_subscriber(customer.subscriber_id)
    if subscriber is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Subscription not found.")

    try:
        created = await repository.add_area(customer.subscriber_id, area)
    except repository.DuplicateAreaError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if created is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Subscription not found.")

    organisation = await store.organisation_for(account.id)
    await store.record_attribution(
        aoi_id=created.id,
        owner_kind=attribution.OwnerKind.AGGREGATOR,
        owner_id=organisation,
        subscriber_id=customer.subscriber_id,
        subject_account_id=customer.id,
        # The workspace whose route this is. An area added to a customer belongs to the project
        # that customer sits in, and this route already proved the caller's authority over it.
        workspace_id=workspace_id,
    )

    from app.agents import pipeline

    await pipeline.enqueue_scan(subscriber, created)

    # The customer is told, and by whom — same reasoning as `add_customer_area`.
    background.add_task(
        mailer.send_area_added,
        customer.email,
        customer.first_name,
        area_name=created.name,
        hectares=created.hectares,
        admin1=created.admin1,
        admin2=created.admin2,
        country=created.country,
        # `organisation` here is an ACCOUNT ID (see `store.organisation_for`), not a name, so
        # the acting member's own organisation name is used — that is what a customer would
        # recognise anyway.
        added_by=account.organisation or None,
    )

    return created


@router.patch(
    "/workspaces/{workspace_id}/customers/{account_id}/areas/{aoi_id}",
    response_model=AreaOfInterest,
)
async def patch_workspace_customer_area(
    workspace_id: str,
    account_id: str,
    aoi_id: str,
    payload: CustomerAreaPatch,
    account: Account = Depends(require_workspace_permission(Permission.MANAGE_CUSTOMERS)),
) -> AreaOfInterest:
    """Rename or re-crop a customer's plot. The `aoi_id` and its history survive.

    Geometry is not editable, for the same reason as on the Partner API: moving a footprint would
    leave one timeline mixing measurements of two different pieces of ground. Add a plot instead.
    """
    _require_store()

    customer = await _workspace_customer(account, workspace_id, account_id)
    owned = await repository.get_area(aoi_id)
    if owned is None or owned[0] != customer.subscriber_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Area not found.")

    updated = await repository.update_area(aoi_id, name=payload.name, crop=payload.crop)
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Area not found.")
    return updated


@router.delete(
    "/workspaces/{workspace_id}/customers/{account_id}/areas/{aoi_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def remove_workspace_customer_area(
    workspace_id: str,
    account_id: str,
    aoi_id: str,
    account: Account = Depends(require_workspace_permission(Permission.MANAGE_CUSTOMERS)),
) -> None:
    """Stop monitoring one plot. Past assessments are kept; billing for it stops."""
    _require_store()

    customer = await _workspace_customer(account, workspace_id, account_id)
    owned = await repository.get_area(aoi_id)
    if owned is None or owned[0] != customer.subscriber_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Area not found.")

    try:
        removed = await repository.delete_area(aoi_id)
    except repository.LastAreaError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Area not found.")

    await store.end_attribution(aoi_id)


async def _workspace_customer(
    member: Account, workspace_id: str, account_id: str
) -> Account:
    """A customer of this workspace, or 404.

    The membership edge is checked **with the workspace in the query**, so a customer belonging to
    another project of the same organisation is never a candidate — knowing their id is not enough.
    404 rather than 403 so the id space cannot be probed.
    """
    organisation = await store.organisation_for(member.id)
    customer = await store.get_account(account_id)
    if customer is None or not await store.is_member(
        account_id, organisation, workspace_id=workspace_id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such customer in this workspace.")
    return customer
