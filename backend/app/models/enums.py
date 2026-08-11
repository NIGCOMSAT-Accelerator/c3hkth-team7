"""Shared vocabulary for the pipeline.

These names travel across the queue and out to the frontend, so treat them as
part of the wire contract: rename with a migration, not in place.
"""

from __future__ import annotations

from enum import Enum


class Severity(str, Enum):
    """Alert severity. Ordered — comparison uses `SEVERITY_ORDER` below."""

    INFO = "info"
    ADVISORY = "advisory"
    WATCH = "watch"
    WARNING = "warning"
    EMERGENCY = "emergency"


SEVERITY_ORDER: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.ADVISORY: 1,
    Severity.WATCH: 2,
    Severity.WARNING: 3,
    Severity.EMERGENCY: 4,
}


class HazardType(str, Enum):
    """What the pipeline detected.

    Track A (agricultural intelligence) is the MVP surface; the flood and
    malaria hazards come from the Track B SAR engine running underneath it,
    which is what makes the cascade — flood today, crop loss next month,
    malaria six weeks out — visible in a single alert.
    """

    CROP_WATERLOGGING = "crop_waterlogging"
    CROP_DROUGHT_STRESS = "crop_drought_stress"
    CROP_VEGETATION_ANOMALY = "crop_vegetation_anomaly"
    FLOOD_INUNDATION = "flood_inundation"
    FLOOD_FORECAST = "flood_forecast"
    MALARIA_RISK = "malaria_risk"


class DeliveryMode(str, Enum):
    """Who contacts the subscriber about one monitored area.

    An aggregator that owns the customer relationship often needs to be the only voice reaching
    their farmer — otherwise SHELTER's SMS and the partner's own arrive about one flood, in two
    voices, and the partner cannot switch ours off.

    The aggregator webhook fires in **every** mode; it always has, and suppressing it would break
    integrations that use it for reporting. What this governs is *direct* dispatch to the
    subscriber's own channels.
    """

    #: SHELTER contacts the subscriber. The default, and what every pre-existing area does.
    DIRECT = "direct"
    #: SHELTER contacts nobody directly — the aggregator relays via their webhook.
    WEBHOOK = "webhook"
    #: Both, for a partner who wants their own record while SHELTER still reaches the farmer.
    BOTH = "both"


class Channel(str, Enum):
    """Delivery surfaces. Every subscriber picks one or more.

    NIGCOMSAT_BROADCAST is the sovereign fallback and the reason SHELTER works
    where internet alerts don't: it is one-way, needs no ground network at the
    receiving end, and reaches a whole footprint at once. Every other channel
    here fails in exactly the conditions a flood creates.
    """

    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"
    SIGNAL = "signal"
    EMAIL = "email"
    SLACK = "slack"
    WEBHOOK = "webhook"
    NIGCOMSAT_BROADCAST = "nigcomsat_broadcast"


#: Channels a subscriber may actually select at this stage of the product.
#:
#: **Email and webhook only.** Both are exercised end to end: email through Brevo's API, and
#: webhook through the signed publisher with its retry ledger. The other five are implemented and
#: registered in `dispatch/router._DISPATCHERS`, but no real delivery has ever been made on them —
#: they carry no credentials, so `Dispatcher.available` is False and a dispatch returns a SKIPPED
#: receipt reading "credentials not configured".
#:
#: The gap this closes is on the WRITE side. A subscriber could bind WhatsApp, see the save succeed,
#: and then silently never receive anything — the alert was generated, skipped at dispatch, and
#: nothing told them. For a service whose whole promise is "we contact you", a channel that accepts
#: an address and delivers nothing is worse than one that is absent.
#:
#: Widening this is a deliberate act: add the member here, and the channel becomes selectable
#: everywhere at once because every write path validates against this set. Do it when a real
#: message has been delivered on that channel, not when the code is written.
MVP_CHANNELS: frozenset[Channel] = frozenset(
    {
        Channel.EMAIL,
        Channel.WEBHOOK,
    }
)


#: Channels that need a working consumer internet connection at delivery time.
#: Used by the dispatcher to decide when to escalate to satellite broadcast.
TERRESTRIAL_CHANNELS: frozenset[Channel] = frozenset(
    {
        Channel.WHATSAPP,
        Channel.TELEGRAM,
        Channel.SIGNAL,
        Channel.EMAIL,
        Channel.SLACK,
        Channel.WEBHOOK,
    }
)


class SubscriberKind(str, Enum):
    FARMER = "farmer"
    COOPERATIVE = "cooperative"
    GOVERNMENT = "government"
    EMERGENCY_RESPONDER = "emergency_responder"
    PUBLIC_HEALTH = "public_health"
    INSURER = "insurer"


class JobStage(str, Enum):
    """Pipeline stages, in execution order. Each is a Redis stream.

    FAHIS is off the main line. Scout → Analyst → Oracle → Herald runs forward to
    delivery; Fahis runs *afterwards*, once enough time has passed for the world
    to have reported whatever we predicted. It consumes its own stream and
    enqueues nothing.
    """

    SCOUT = "scout"
    ANALYST = "analyst"
    ORACLE = "oracle"
    HERALD = "herald"
    FAHIS = "fahis"


class Verdict(str, Enum):
    """Fahis's finding on whether an alert matched reality.

    **UNVERIFIED is the default and is NOT a soft REFUTED.** A flood in a remote
    LGA may simply never be reported by anyone indexable — absence of evidence is
    not evidence of absence. Collapsing the two would train the system on noise
    and, worse, would let a correct warning be recorded as a false alarm because
    no journalist was nearby.

    Only CONFIRMED and REFUTED are usable as training signal. UNVERIFIED and
    PARTIAL are recorded and excluded from it.
    """

    #: Independent sources describe the hazard we warned about, in place and time.
    CONFIRMED = "confirmed"
    #: Sources describe something related but materially different — right area,
    #: wrong hazard, or right hazard at the wrong severity.
    PARTIAL = "partial"
    #: Sources affirmatively indicate the hazard did NOT occur. Rare and it should
    #: be: it requires a positive statement, not silence.
    REFUTED = "refuted"
    #: Nothing found either way. The expected outcome for most rural areas.
    UNVERIFIED = "unverified"
    #: Search itself was unavailable, so no conclusion was even attempted. Kept
    #: distinct from UNVERIFIED so an outage is never mistaken for a non-finding.
    NOT_ATTEMPTED = "not_attempted"


#: Verdicts that carry usable signal for model evaluation or retraining.
#: Everything else is recorded for the audit trail and excluded from metrics —
#: computing precision over UNVERIFIED rows would silently count unreported real
#: floods as false positives.
TRAINABLE_VERDICTS: frozenset[Verdict] = frozenset(
    {Verdict.CONFIRMED, Verdict.REFUTED}
)


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYING = "retrying"


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"
