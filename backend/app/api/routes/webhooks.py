"""Webhook subscription management — `/shelter/v1/api/webhook*`.

The integration surface for businesses running their own intelligence on SHELTER
output: an insurer's payout engine, a state dashboard, a cooperative's own AI
assistant. They subscribe here rather than becoming alert recipients, because their
requirements differ in kind — event filtering, per-endpoint secrets, at-least-once
delivery with backoff, and a queryable history. See `app/webhooks/engine.py`.

**Every endpoint here is behind the API key.** Creating a webhook subscription means
choosing a URL that SHELTER will then sign payloads to; unauthenticated, anyone could
register an endpoint and receive every alert in the system, which is a bulk
exfiltration path for subscriber-adjacent data.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Security,
    status,
)
from pydantic import BaseModel, Field, field_validator

from app.api.security_schemes import (
    aggregator_api_key,
    platform_api_key,
    portal_session,
)
from app.config import settings
from app.iam import store as iam_store
from app.iam.audit import AuditAction
from app.iam.models import AccountKind, ApiKeyScope
from app.iam.platform import require_platform_scope
from app.iam.roles import Permission
from app.iam.security import read_session
from app.logging_config import get_logger
from app.models import intelligence
from app.models.enums import HazardType, Severity
from app.webhooks import engine, publisher, store
from app.webhooks.schemas import AlertEventData, AlertWebhookEvent

log = get_logger(__name__)

# `/webhook` singular, matching the requested path shape. Collection operations
# live under `/webhook/subscriptions` rather than at `/webhooks`, so a reverse
# proxy needs one location block for the whole feature.
router = APIRouter(prefix="/webhook", tags=["webhook"])


class WebhookCreate(BaseModel):
    """A new endpoint registration."""

    name: str = Field(min_length=1, max_length=120, description="Who owns this endpoint")
    url: str = Field(description="HTTPS endpoint. Plain HTTP is rejected.")
    events: list[str] = Field(
        default_factory=list,
        description="Event names to receive. Empty means all — a new integration "
        "should get everything until it narrows, not nothing.",
    )
    min_severity: Severity | None = Field(
        default=None,
        description="Suppress anything below this severity. Null means no floor.",
    )
    aoi_ids: list[str] = Field(
        default_factory=list,
        description="Restrict to specific areas. Empty means every area.",
    )
    workspace_id: str | None = Field(
        default=None,
        description=(
            "Which workspace this endpoint serves. **Null means every workspace this account "
            "owns** — which is the right default for an aggregator running one programme, and "
            "the wrong one for an aggregator whose Bayelsa pilot and Kebbi season must not see "
            "each other's alerts."
        ),
    )

    @field_validator("url")
    @classmethod
    def _https_only(cls, value: str) -> str:
        """Reject plain HTTP outright.

        The payload can trigger an insurance payout or an evacuation downstream. Over
        plain HTTP both the content and the signing header are readable and
        modifiable in transit, so accepting it would make the signature theatre.
        """
        if not value.startswith("https://"):
            raise ValueError("webhook URL must use https://")
        if len(value) > 2000:
            raise ValueError("webhook URL is unreasonably long")
        return value


class WebhookCreated(BaseModel):
    """Registration response — **the only time the secret is ever returned.**"""

    id: str
    name: str
    url: str
    secret: str = Field(
        description="HMAC-SHA256 signing secret. Store it now: it is never "
        "returned again. Rotate via POST /webhook/subscriptions/{id}/rotate-secret."
    )
    events: list[str]
    min_severity: Severity | None
    aoi_ids: list[str]
    active: bool
    #: Which workspace this endpoint serves. Null means every workspace this account owns.
    #:
    #: Echoed back deliberately. Without it the caller cannot confirm the scope was applied — and
    #: it was not, visibly: the row stored `VDK6N8GGPY` correctly while the response reported
    #: `null`, so a correct write looked like a silently ignored parameter. A create response that
    #: omits a field it accepted is indistinguishable from one that discarded it.
    workspace_id: str | None = None
    signature_header: str = "X-SHELTER-Signature"
    timestamp_header: str = "X-SHELTER-Timestamp"
    delivery_header: str = "X-SHELTER-Delivery"
    verification_note: str = (
        "Sign f'{timestamp}.{raw_body}' with HMAC-SHA256 and compare against the "
        "hex digest after 'v1='. Use a constant-time comparison. Reject a timestamp "
        "older than 5 minutes — the timestamp is what makes a captured payload "
        "non-replayable. Deduplicate on X-SHELTER-Delivery: delivery is "
        "at-least-once, so retries repeat that id."
    )


class WebhookPublic(BaseModel):
    """A subscription as read back. Never includes the secret."""

    id: str
    name: str
    url: str
    events: list[str]
    min_severity: Severity | None
    aoi_ids: list[str]
    active: bool
    failure_streak: int
    last_error: str | None
    #: Which workspace this endpoint serves. **Null means every workspace this account owns.**
    #:
    #: Read back so the portal can render it: without it an aggregator with two projects sees two
    #: identically-described endpoints and cannot tell which programme each one belongs to.
    workspace_id: str | None = None


def _public(row: dict) -> WebhookPublic:
    return WebhookPublic(
        id=row["id"],
        name=row["name"],
        url=row["url"],
        events=list(row.get("events") or []),
        min_severity=row.get("min_severity"),
        aoi_ids=list(row.get("aoi_ids") or []),
        active=bool(row["active"]),
        failure_streak=int(row.get("failure_streak") or 0),
        last_error=row.get("last_error"),
        # `.get`, not `row[...]`: a row written before migration 016 has no such column.
        workspace_id=row.get("owner_workspace_id"),
    )


def _require_engine() -> None:
    if not settings.webhook_engine_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The webhook engine is disabled (WEBHOOK_ENGINE_ENABLED=false)",
        )


# --------------------------------------------------------------------------- #
# Discovery — the one unauthenticated route
# --------------------------------------------------------------------------- #


#: Per-category sample content.
#:
#: Each category gets its OWN advisory and explanations, not one shared set. A shared body would
#: put "prepare drainage" under an `emergency` sample and "act immediately" under `info`, which
#: teaches an integrator the wrong shape for the category they most need to handle correctly.
#:
#: The scenarios differ too, because the categories describe genuinely different situations: `info`
#: is a routine reading on a quiet plot, `emergency` is water across most of a field.
_SAMPLES: dict[str, dict] = {
    "info": {
        "hazard": HazardType.CROP_VEGETATION_ANOMALY,
        "confidence": 0.412,
        "evidence": [
            "Plant growth is 4% below the seasonal average for this area",
            "Rainfall data was unavailable for this cycle",
            "Optical imagery was blocked by cloud; this reading is radar-only",
        ],
        "advisory": {
            "headline": "Routine reading for Musa maize plot",
            "body": (
                "Plant growth is slightly below the seasonal average. Nothing needs attention. "
                "Rainfall data was not available this cycle, so confidence is lower than usual."
            ),
            "actions": [],
            "broadcast_text": "",
        },
        "explanations": {
            "crop": (
                "Your crop is growing a little more slowly than usual for this time of year. "
                "This is a small difference and not yet a problem."
            ),
            "drivers": (
                "This reading rests on the satellite imagery alone. Rainfall data was not "
                "available this cycle, which is why confidence is limited."
            ),
            "irrigation": (
                "We cannot advise on irrigation this cycle: no soil-moisture measurement was "
                "available. Check the soil by hand at root depth before deciding."
            ),
        },
    },
    "advisory": {
        "hazard": HazardType.CROP_DROUGHT_STRESS,
        "confidence": 0.71,
        "evidence": [
            "18% of the cropland shows drought stress",
            "No rain forecast in the next 7 days",
            "Soil drains freely, so moisture is lost quickly",
        ],
        "advisory": {
            "headline": "Dry conditions building on Musa maize plot",
            "body": (
                "Just under a fifth of the plot shows drought stress, and no rain is forecast for "
                "the next seven days. The crop is not yet at risk, but conditions are drying."
            ),
            "actions": [
                "Plan irrigation for the next few days if water is available.",
                "Check the driest corners of the plot first.",
            ],
            "broadcast_text": "SHELTER: dry conditions building on your plot.",
        },
        "explanations": {
            "crop": (
                "Almost a fifth of your plot is showing drought stress. The plants are growing "
                "more slowly than healthy ones would at this stage."
            ),
            "drivers": (
                "No rain is expected for seven days, and this soil drains quickly, so what falls "
                "does not stay in reach of the roots."
            ),
            "irrigation": (
                "Irrigate within the next few days if you can. Check the soil at root depth "
                "first — the satellite sees the surface, which dries before the roots do."
            ),
        },
    },
    "watch": {
        "hazard": HazardType.FLOOD_INUNDATION,
        "confidence": 0.55,
        "evidence": [
            "12% of the area is under standing water in the latest radar pass",
            "Rainfall data was unavailable for this cycle",
            "Optical imagery was blocked by cloud; this reading is radar-only",
        ],
        "advisory": {
            "headline": "Standing water detected on Musa maize plot",
            "body": (
                "Radar shows standing water across 12% of the plot. Cloud blocked the optical "
                "view and rainfall data was unavailable, so this reading is radar-only."
            ),
            "actions": [
                "Check low-lying parts of the plot for standing water.",
                "Clear drainage channels while access is still easy.",
                "Move stored produce to higher ground.",
            ],
            "broadcast_text": "SHELTER: standing water on your plot. Check drainage.",
        },
        "explanations": {
            "crop": (
                "Radar shows water sitting on about an eighth of your plot. Radar sees through "
                "cloud, so this reading holds even though the sky was covered. Waterlogged roots "
                "cannot take up nutrients, so growth slows even after the surface dries."
            ),
            "drivers": (
                "The watch level is because water is already standing on the plot. Rainfall could "
                "not be measured this cycle, so we cannot say whether more is coming."
            ),
            "irrigation": (
                "Hold — do not irrigate. Water is already sitting on the plot, and adding more "
                "would keep the roots starved of air."
            ),
        },
    },
    "warning": {
        "hazard": HazardType.FLOOD_FORECAST,
        "confidence": 0.88,
        "evidence": [
            "34% of the area is under standing water in the latest radar pass",
            "62 mm of rain forecast over the next 3 days",
            "Soil drains poorly, so water will stand rather than soak away",
            "About 1,400 people live within the affected footprint",
        ],
        "advisory": {
            "headline": "Flooding expected on Musa maize plot within 3 days",
            "body": (
                "A third of the plot is already under water and 62 mm of rain is forecast over "
                "the next three days. This soil drains poorly, so the water will stand."
            ),
            "actions": [
                "Move stored produce and equipment to higher ground today.",
                "Clear every drainage channel now, before the rain arrives.",
                "Warn neighbouring farmers in the same low-lying area.",
            ],
            "broadcast_text": "SHELTER WARNING: flooding expected within 3 days. Move produce.",
        },
        "explanations": {
            "crop": (
                "A third of your plot is already under water, and more rain is coming. At this "
                "level the crop is at risk, not just slowed."
            ),
            "drivers": (
                "Two things agree: water is already standing, and 62 mm of rain is forecast over "
                "three days. This soil drains poorly, so that rain will sit rather than soak "
                "away."
            ),
            "irrigation": (
                "Hold — do not irrigate. The plot is already flooding and more rain is expected."
            ),
        },
    },
    "emergency": {
        "hazard": HazardType.FLOOD_INUNDATION,
        "confidence": 0.93,
        "evidence": [
            "71% of the area is under standing water in the latest radar pass",
            "118 mm of rain fell in the last 48 hours",
            "Water depth is increasing between consecutive radar passes",
            "About 1,400 people live within the affected footprint",
        ],
        "advisory": {
            "headline": "Severe flooding on Musa maize plot — act now",
            "body": (
                "Radar shows water across 71% of the plot and rising between passes, after "
                "118 mm of rain in 48 hours. Follow official emergency guidance."
            ),
            "actions": [
                "Move people and livestock to higher ground immediately.",
                "Do not attempt to cross moving water.",
                "Follow instructions from local emergency services.",
            ],
            "broadcast_text": "SHELTER EMERGENCY: severe flooding. Move to higher ground.",
        },
        "explanations": {
            "crop": (
                "Almost three quarters of your plot is under water, and the water is still "
                "rising. This crop is very likely lost; the priority now is people and "
                "livestock."
            ),
            "drivers": (
                "118 mm of rain fell in two days, and radar shows the water deepening between "
                "passes rather than draining. Both measurements point the same way."
            ),
            "irrigation": (
                "Hold — do not irrigate. The plot is flooded."
            ),
        },
    },
}


def _sample_event(severity: str) -> dict:
    """One schema-valid example payload, with content that fits its category.

    Built through `AlertEventData` and `intelligence.describe` — the same model and table the
    Herald uses on a live dispatch — so a field renamed there changes the published sample too and
    the documentation cannot drift from the wire.
    """
    sample = _SAMPLES[severity]
    hazard = sample["hazard"]
    confidence = sample["confidence"]

    data = AlertEventData(
        alert_id="alert_8ebc462217904d78",
        severity=severity,
        hazard=hazard.value,
        intelligence=intelligence.describe(Severity(severity), confidence, hazard),
        explanations=sample["explanations"],
        advisory={
            **sample["advisory"],
            "language": "en",
            "generated_by": "gemini-2.5-flash",
        },
        assessment={
            "id": "risk_14950a405aab4c1f",
            "aoi_id": "aoi_091d52d4eb874c6d",
            "aoi_name": "Musa maize plot",
            "hazard": hazard.value,
            "severity": severity,
            "score": round(confidence * 0.9, 2),
            "confidence": confidence,
            "lead_time_days": 7,
            "evidence": sample["evidence"],
            "data_sources": ["sentinel-1", "sentinel-2", "worldcover", "openstreetmap"],
            "assessed_at": "2026-08-10T06:45:07Z",
        },
    )

    payload = engine.event_payload(
        "shelter.alert", data.model_dump(mode="json"), delivery_id="whd_8f2c1a94"
    )

    # A FIXED timestamp, overriding the live one `event_payload` stamps.
    #
    # Without this the sample carries `datetime.now()`, so the exported OpenAPI document differs on
    # every generation and `openapi-check` fails on a spec nobody changed. The date is arbitrary and
    # illustrative — an integrator reading a sample needs the field's shape, not the moment it was
    # rendered.
    payload["sent_at"] = "2026-08-10T06:45:07Z"
    return payload


#: Ordered so ReDoc lists the examples from least to most urgent, which is how the ladder reads.
_SAMPLE_CATEGORIES = [
    (severity.value, intelligence.CATEGORY[severity]) for severity in Severity
]


@router.get("")
async def webhook_info() -> dict:
    """What this engine sends and how to verify it.

    Unauthenticated on purpose: it is documentation, contains no data about any
    subscriber or endpoint, and a developer evaluating the integration should be able
    to read the contract before asking for a key.
    """
    return {
        "engine": "shelter-webhooks",
        "enabled": settings.webhook_engine_enabled,
        "events": [
            {
                "name": "shelter.alert",
                "when": "An advisory has been generated and dispatched for an area",
                "data": "severity, hazard, advisory text and actions, full assessment "
                "with evidence and the 7-day forecast series",
            },
            {
                "name": "shelter.verification",
                "when": "Fahis has adjudicated a past warning against outside reporting",
                "data": "assessment_id, verdict, sources consulted",
            },
            {
                "name": "shelter.test",
                "when": "You called POST /webhook/subscriptions/{id}/test",
                "data": "A fixed sample payload. Use it to validate your signature "
                "check before going live.",
            },
        ],
        "delivery": {
            "guarantee": "at-least-once",
            "deduplicate_on": "X-SHELTER-Delivery",
            "retry_schedule_seconds": list(engine.RETRY_SCHEDULE_SECONDS),
            "retried_on": "network errors, 5xx, 408, 429",
            "not_retried_on": "other 4xx — those mean the payload is being rejected, "
            "so retrying would only hammer your endpoint",
            "auto_disabled_after": settings.webhook_max_consecutive_failures,
        },
        "signature": {
            "header": "X-SHELTER-Signature",
            "format": f"{engine.SIGNATURE_VERSION}=<hex hmac-sha256>",
            "signed_string": "{X-SHELTER-Timestamp}.{raw_request_body}",
            "why_the_timestamp": "A body-only signature stays valid forever, so a "
            "captured payload could be replayed indefinitely. Reject timestamps "
            "older than your tolerance (300s is typical).",
        },
        "requirements": ["https:// only", "respond 2xx within "
                         f"{settings.webhook_timeout_seconds:.0f}s", "be idempotent"],
    }


# --------------------------------------------------------------------------- #
# Subscription management
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WebhookCaller:
    """Who is managing webhooks, and which subscriptions they may touch.

    ## Why this replaced `require_platform_scope` on the subscription routes

    Every webhook route required `platform:operate`, a scope only the operations team's key holds. So
    `ApiKeyScope.WEBHOOKS` ("webhooks:manage") was grantable, documented as "manage webhook
    subscriptions belonging to this aggregator", and completely inert — an aggregator got a 403
    advising them to mint a new key, which would not have helped.

    That is not merely an unusable endpoint. An aggregator relaying alerts itself
    (`delivery_mode='webhook'`) has SHELTER contact nobody directly, so with no registered endpoint
    the alert has nowhere to go at all: the farmer is watched and nobody is told.

    ## `owner_account_id is None` means unrestricted

    Same convention as `Audience.permitted_subscriber_ids`, and the same trap: an empty-ish value must
    never be read as "no filter" for a tenant caller. The platform caller gets None; an aggregator
    always carries its own id. `resolve_audience` documents why this distinction is load-bearing.
    """

    #: None for the platform caller — unrestricted. Otherwise the aggregator's organisation id.
    owner_account_id: str | None
    owner_workspace_id: str | None
    label: str


async def webhook_caller(
    session: object | None = Security(portal_session),
    platform_key: str | None = Security(platform_api_key),
    aggregator_key: str | None = Security(aggregator_api_key),
) -> WebhookCaller:
    """Resolve a webhook manager: a signed-in aggregator, the platform, or an aggregator key.

    ## The portal session is checked FIRST, and this is why it exists at all

    Without it, these routes accepted only API keys — so the portal's Webhooks page could not create
    a subscription, its "+ Create" button pointed at a route nobody had built, and the page reported
    a **404**. The missing route was a symptom: a form there would have returned 403 anyway.

    That is a circular dead end for a new aggregator. `webhooks:manage` is grantable only on an API
    key, an API key is minted in the portal, and the portal is where someone goes to set up the
    webhook they need *before* they have written any integration code. Requiring a key to register
    an endpoint means the only path to asynchronous delivery ran through the programmatic channel
    the endpoint exists to serve.

    Session first for the same reason as `resolve_audience`: the frontend attaches its service key
    to every request, so checking a key first would resolve a browser request as the *platform* and
    silently give a signed-in aggregator unrestricted scope over every subscription on the
    deployment.

    A session must carry `integration:manage` on the account, which is the same permission that
    gates the page.
    """
    session_token = getattr(session, "credentials", None)
    if session_token:
        claims = read_session(session_token)
        if claims is not None:
            account = await iam_store.get_account(claims.get("sub", ""))
            if account is not None and account.kind is AccountKind.COMMERCIAL:
                organisation = await iam_store.organisation_for(account.id)
                permitted = await iam_store.member_permissions(account.id)
                if Permission.MANAGE_INTEGRATION in permitted:
                    return WebhookCaller(
                        owner_account_id=organisation,
                        owner_workspace_id=None,
                        label=f"portal:{organisation}",
                    )

    if platform_key:
        resolved = await iam_store.resolve_api_key(platform_key.strip())
        if resolved is not None:
            _account, scopes, _workspace = resolved
            if ApiKeyScope.PLATFORM_OPERATE in scopes:
                return WebhookCaller(
                    owner_account_id=None, owner_workspace_id=None, label="platform"
                )

    if aggregator_key:
        resolved = await iam_store.resolve_api_key(aggregator_key.strip())
        if resolved is not None:
            account, scopes, workspace_id = resolved
            if (
                account.kind is AccountKind.COMMERCIAL
                and ApiKeyScope.WEBHOOKS in scopes
            ):
                organisation = await iam_store.organisation_for(account.id)
                return WebhookCaller(
                    owner_account_id=organisation,
                    owner_workspace_id=workspace_id,
                    label=f"aggregator:{organisation}",
                )

    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        "Managing webhooks needs a signed-in aggregator with `integration:manage`, a platform "
        "key with `platform:operate`, or an aggregator key with `webhooks:manage`. Scopes cannot "
        "be added to an existing key — mint a new one from the portal, so widening authority is "
        "always an explicit act.",
    )


async def _resolve_workspace(
    requested: str | None, caller: WebhookCaller
) -> str | None:
    """Which workspace a new subscription belongs to.

    ## Why a request cannot simply be trusted

    A supplied `workspace_id` is a *filter*, not an authorisation — the same rule
    `Audience.narrow` follows. Without a check, a signed-in aggregator could register an endpoint
    against another tenant's workspace id and start receiving that tenant's alert events: plot
    locations, contact addresses, severity. A cross-tenant read achieved entirely through a write.

    So membership is verified against the caller's own organisation before the value is used, and an
    id the caller does not own is **refused with a 404** rather than silently ignored. Ignoring it
    would be worse than refusing: the endpoint would be created against every workspace, which is
    wider than what was asked for and looks like it succeeded.

    ## The three cases

      * **A key caller** already carries its own workspace and it is authoritative — a key is minted
        against exactly one. A payload naming a different one is refused, because a key must not be
        able to widen or move its own scope.
      * **A session caller** spans every workspace the account owns, so `caller.owner_workspace_id`
        is None and the payload is the only place the choice can come from.
      * **The platform caller** is unrestricted; its subscriptions are platform-owned and carry no
        workspace at all.
    """
    if caller.owner_account_id is None:
        # Platform. Its rows are NULL-owned by design — see migration 016.
        return None

    if caller.owner_workspace_id is not None:
        # A key. Its own workspace wins, and a payload disagreeing with it is an attempt to move
        # the key's scope.
        if requested is not None and requested != caller.owner_workspace_id:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "No such workspace for this key.",
            )
        return caller.owner_workspace_id

    if requested is None:
        # Every workspace this account owns. The honest default for a single-programme aggregator.
        return None

    workspaces = await iam_store.list_workspaces(caller.owner_account_id)
    if requested not in {w["id"] for w in workspaces}:
        log.warning(
            "webhook registration named a workspace the caller does not own",
            extra={"caller": caller.label, "workspace_id": requested},
        )
        # 404, not 403 — a 403 confirms the id exists and turns this into a workspace-enumeration
        # oracle across tenants. Same reasoning as `_owned_or_404`.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such workspace.")

    return requested


def _owned_or_404(row: dict | None, caller: WebhookCaller) -> dict:
    """One subscription, if this caller owns it.

    **404 rather than 403 for someone else's**, matching `get_alert` and `get_customer`: a 403
    confirms the id exists and turns this into an enumeration oracle over other aggregators'
    integrations. Subscription ids appear in delivery logs and support threads.
    """
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such subscription")
    if caller.owner_account_id is None:
        return row
    if row.get("owner_account_id") != caller.owner_account_id:
        log.warning(
            "cross-tenant webhook access refused",
            extra={"subscription_id": row.get("id"), "caller": caller.label},
        )
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such subscription")
    return row

async def _notify_webhook_change(
    caller: WebhookCaller,
    *,
    endpoint_url: str,
    created: bool,
    request: Request,
) -> None:
    """Email the owning account that a webhook was registered or removed. **Never raises.**

    ## Why this is worth an email at all

    A webhook subscription is a standing instruction to forward every matching advisory — which
    names a subscriber, their plot and its coordinates — to a URL. Registering one is the quietest
    exfiltration the platform allows: no password change, no unusual login, and the alerts simply
    start arriving somewhere else as well. Removing one is the mirror image, and its first symptom
    is a customer asking why nobody warned them.
    Neither was emailed, and neither was even audited, before this.

    ## The platform caller gets no email, deliberately

    `owner_account_id is None` means the operations team's own key, which belongs to no single
    person — there is no inbox that "you did this" would be true of. The audit entry still records
    it. Same convention as `Audience.permitted_subscriber_ids`, and the same trap: None means
    unrestricted, not "nobody".

    Swallows everything. A subscription that is already stored must not fail its response because
    a mail provider was slow, and the caller has no way to act on a mail error anyway.
    """
    if not caller.owner_account_id:
        return

    try:
        from app.iam import mailer

        account = await iam_store.get_account(caller.owner_account_id)
        if account is None:
            return

        await mailer.send_webhook_notice(
            account.email,
            account.first_name,
            endpoint_url=endpoint_url,
            created=created,
            context=mailer.request_context(request),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "webhook change notice could not be sent",
            extra={"owner": caller.owner_account_id, "error": str(exc)},
        )


@router.post(
    "/subscriptions",
    response_model=WebhookCreated,
    status_code=status.HTTP_201_CREATED,
)

async def create_subscription(
    payload: WebhookCreate,
    request: Request,
    background: BackgroundTasks,
    caller: WebhookCaller = Depends(webhook_caller),
) -> WebhookCreated:
    """Register an endpoint. Returns the signing secret **once**.

    Stamped with the caller's owner so an aggregator's subscription is theirs alone. A platform
    caller leaves it NULL, which is what every pre-existing row carries — see migration 016.
    """
    _require_engine()

    # Resolved BEFORE the try, deliberately.
    #
    # `_resolve_workspace` raises a 404 for a workspace the caller does not own. Inside the try, the
    # bare `except Exception` below catches it and re-raises a **503** — so a deliberate
    # cross-tenant refusal was being reported as "the service is unavailable". Observed live:
    # `HTTP 503  Could not create the subscription: 404: No such workspace.` An authorisation
    # decision must not be laundered into an infrastructure error, both because it misleads the
    # caller and because a 503 invites a retry.
    #
    # An explicitly chosen workspace wins over the caller's own. The two differ by caller: an API
    # KEY is minted against one workspace, so `caller.owner_workspace_id` is authoritative and a
    # payload naming a different one must not widen it. A PORTAL SESSION spans every workspace the
    # account owns, so the caller carries None and the payload is the only place the choice can come
    # from — which is why the field exists at all.
    workspace_id = await _resolve_workspace(payload.workspace_id, caller)

    try:
        row = await store.create_subscription(
            payload.name,
            payload.url,
            events=payload.events,
            min_severity=payload.min_severity.value if payload.min_severity else None,
            aoi_ids=payload.aoi_ids,
            owner_account_id=caller.owner_account_id,
            owner_workspace_id=workspace_id,
        )
    except Exception as exc:
        log.exception("webhook subscription create failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not create the subscription: {exc}",
        ) from exc

    log.info(
        "webhook subscription created",
        extra={"subscription_id": row["id"], "url": row["url"]},
    )

    if caller.owner_account_id:
        await iam_store.record_audit(
            account_id=caller.owner_account_id,
            action=AuditAction.WEBHOOK_CREATED,
            target_id=row["id"],
            detail=f"{row['name']} -> {row['url']}",
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )

    background.add_task(
        _notify_webhook_change,
        caller,
        endpoint_url=row["url"],
        created=True,
        request=request,
    )
    return WebhookCreated(
        id=row["id"],
        name=row["name"],
        url=row["url"],
        secret=row["secret"],
        events=list(row.get("events") or []),
        min_severity=row.get("min_severity"),
        aoi_ids=list(row.get("aoi_ids") or []),
        active=bool(row["active"]),
        # Read back from the ROW, not from the request. The row is what was actually stored, so a
        # caller comparing the two can tell an applied scope from an accepted-and-discarded one.
        workspace_id=row.get("owner_workspace_id"),
    )


@router.get(
    "/subscriptions",
    response_model=list[WebhookPublic],
)
async def list_subscriptions(
    include_inactive: bool = Query(default=False),
    caller: WebhookCaller = Depends(webhook_caller),
) -> list[WebhookPublic]:
    """This caller's subscriptions. The platform sees all; an aggregator sees its own.

    The owner filter is **inside** the query rather than applied afterwards, so another
    aggregator's row is never a candidate — same reasoning as the chat-history session filter.
    """
    return [
        _public(r)
        for r in await store.list_subscriptions(
            include_inactive=include_inactive,
            owner_account_id=caller.owner_account_id,
        )
    ]


@router.get(
    "/subscriptions/{subscription_id}",
    response_model=WebhookPublic,
)
async def get_subscription(
    subscription_id: str, caller: WebhookCaller = Depends(webhook_caller)
) -> WebhookPublic:
    return _public(_owned_or_404(await store.get_subscription(subscription_id), caller))


@router.delete(
    "/subscriptions/{subscription_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # See the note on subscribers.delete_subscriber: without this, FastAPI infers a
    # response model from `-> None` and asserts at import time that a 204 must not
    # have a body, so the app never starts.
    response_model=None,
)
async def delete_subscription(
    subscription_id: str,
    request: Request,
    background: BackgroundTasks,
    caller: WebhookCaller = Depends(webhook_caller),
) -> None:
    # Ownership proved BEFORE the delete, not after: a cross-tenant id must not be destroyed
    # and then reported as missing.
    #
    # The row is also kept, not discarded: the notice has to name the URL that stopped
    # receiving alerts, and after the delete there is nothing left to read it from. An id is
    # not something a recipient can recognise.
    doomed = _owned_or_404(await store.get_subscription(subscription_id), caller)
    if not await store.delete_subscription(subscription_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such subscription")

    if caller.owner_account_id:
        await iam_store.record_audit(
            account_id=caller.owner_account_id,
            action=AuditAction.WEBHOOK_DELETED,
            target_id=subscription_id,
            detail=(doomed or {}).get("url"),
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )

    background.add_task(
        _notify_webhook_change,
        caller,
        endpoint_url=(doomed or {}).get("url") or subscription_id,
        created=False,
        request=request,
    )


@router.post(
    "/subscriptions/{subscription_id}/rotate-secret",
)
async def rotate_secret(
    subscription_id: str, caller: WebhookCaller = Depends(webhook_caller)
) -> dict:
    """Mint a new signing secret. The old one stops working immediately.

    A hard cutover rather than a grace period with two valid secrets: a leaked
    secret can forge flood alerts into a payout engine, so it must stop working the
    moment it is rotated. Deploy the new secret before rotating.
    """
    # Ownership first. Every one of these mutates or reveals an integration, so a
    # cross-tenant id must be refused BEFORE the action, not reported afterwards.
    _owned_or_404(await store.get_subscription(subscription_id), caller)

    secret = await store.rotate_secret(subscription_id)
    if secret is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such subscription")

    log.info("webhook secret rotated", extra={"subscription_id": subscription_id})
    return {
        "id": subscription_id,
        "secret": secret,
        "warning": "The previous secret is now invalid. Deliveries signed with it "
        "will fail your verification until you deploy this one.",
    }


@router.post(
    "/subscriptions/{subscription_id}/activate",
    response_model=WebhookPublic,
)
async def activate(
    subscription_id: str, caller: WebhookCaller = Depends(webhook_caller)
) -> WebhookPublic:
    """Re-enable an endpoint, resetting its failure streak.

    Needed because the engine auto-disables after
    `WEBHOOK_MAX_CONSECUTIVE_FAILURES`, and a business that has fixed their endpoint
    needs a way back in without re-registering and re-deploying a new secret.
    """
    # Ownership first. Every one of these mutates or reveals an integration, so a
    # cross-tenant id must be refused BEFORE the action, not reported afterwards.
    _owned_or_404(await store.get_subscription(subscription_id), caller)

    if not await store.set_active(subscription_id, True):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such subscription")
    row = await store.get_subscription(subscription_id)
    return _public(row or {})


@router.post(
    "/subscriptions/{subscription_id}/deactivate",
    response_model=WebhookPublic,
)
async def deactivate(
    subscription_id: str, caller: WebhookCaller = Depends(webhook_caller)
) -> WebhookPublic:
    # Ownership first. Every one of these mutates or reveals an integration, so a
    # cross-tenant id must be refused BEFORE the action, not reported afterwards.
    _owned_or_404(await store.get_subscription(subscription_id), caller)

    if not await store.set_active(subscription_id, False, reason="deactivated by operator"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such subscription")
    row = await store.get_subscription(subscription_id)
    return _public(row or {})


# --------------------------------------------------------------------------- #
# Observability — the integration support surface
# --------------------------------------------------------------------------- #


@router.get(
    "/subscriptions/{subscription_id}/deliveries",
)
async def deliveries(
    subscription_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    caller: WebhookCaller = Depends(webhook_caller),
) -> dict:
    """Delivery history and health for one endpoint.

    This exists because "did you actually send it?" is the first question in every
    integration support thread, and answering it from application logs is slow and
    unreliable.
    """
    # Replaces a bare existence check: same 404, but scoped to this caller's own endpoint.
    _owned_or_404(await store.get_subscription(subscription_id), caller)

    return {
        "subscription_id": subscription_id,
        "stats": await store.delivery_stats(subscription_id),
        "deliveries": await store.delivery_history(subscription_id, limit=limit),
    }


@router.post(
    "/subscriptions/{subscription_id}/test",
)
async def send_test(
    subscription_id: str, caller: WebhookCaller = Depends(webhook_caller)
) -> dict:
    """Send a `shelter.test` payload now.

    The point is that a business can validate their signature verification against
    real bytes from the real engine *before* a live flood alert depends on it. The
    payload is fixed and obviously synthetic so it cannot be mistaken for a warning.
    """
    _require_engine()

    # Ownership first. Every one of these mutates or reveals an integration, so a
    # cross-tenant id must be refused BEFORE the action, not reported afterwards.
    _owned_or_404(await store.get_subscription(subscription_id), caller)

    subscription = await store.get_subscription(subscription_id)
    if subscription is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such subscription")

    delivery_id = engine.new_delivery_id()
    payload = engine.event_payload(
        "shelter.test",
        {
            "message": "If you can verify this signature, your integration is ready.",
            "synthetic": True,
        },
        delivery_id=delivery_id,
    )
    body = engine.canonical_body(payload)

    delivered, status_code, error = await engine.deliver(
        subscription["url"], body, subscription["secret"],
        event="shelter.test", delivery_id=delivery_id,
    )

    return {
        "delivered": delivered,
        "response_status": status_code,
        "error": error,
        "delivery_id": delivery_id,
        # Echoed so a developer can diff their own computed signature against ours
        # while debugging, which turns "it doesn't work" into a one-line comparison.
        "sent_body": body,
    }


@router.post("/sweep", dependencies=[Depends(require_platform_scope(ApiKeyScope.PLATFORM_OPERATE))])
async def trigger_sweep() -> dict:
    """Run the retry sweep now instead of waiting for the scheduler.

    Operational escape hatch: after fixing an endpoint, an operator should not have
    to wait up to a full scheduler cycle to drain the backlog.
    """
    _require_engine()
    return await publisher.sweep()


@router.get(
    "/event-schema",
    response_model=AlertWebhookEvent,
    responses={
        200: {
            "description": (
                "One example per severity category. Every field is the same in each — only "
                "`intelligence` and `severity` differ."
            ),
            "content": {
                "application/json": {
                    "examples": {
                        severity: {
                            "summary": f"{meta['label']} — {meta['urgency'].lower()}",
                            "description": meta["meaning"],
                            "value": _sample_event(severity),
                        }
                        for severity, meta in _SAMPLE_CATEGORIES
                    }
                }
            },
        }
    },
)
async def event_schema() -> AlertWebhookEvent:
    """The `shelter.alert` payload, as a typed schema with one example per category.

    ## Why this endpoint exists

    A partner integrating asynchronously receives this payload and nothing else. Describing it in
    prose means they hand-write a parser and discover a field they mis-read in production; a
    declared schema means they generate a client from our spec.

    So this returns a real, schema-valid example. It is the same model the Herald builds on a live
    dispatch (`webhooks.schemas.AlertEventData`), so a field renamed there changes here too and
    the documentation cannot drift from the wire.

    Unauthenticated, like `GET /webhook`: it is documentation, carries no subscriber data, and a
    developer evaluating the integration should be able to read the contract before asking for a
    key.

    **Returns `watch`** as the default body, because it is the highest category currently
    reachable — until trained weights are deployed, inference falls back to physical thresholds at
    confidence 0.55 and severity is capped there. The other four are in `examples`, so a handler
    can be built for all five before any occurs.
    """
    return AlertWebhookEvent.model_validate(_sample_event("watch"))
