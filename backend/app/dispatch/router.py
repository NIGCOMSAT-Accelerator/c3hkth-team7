"""Delivery routing.

Fans one alert out across a subscriber's chosen channels concurrently, then
decides whether to escalate to satellite broadcast.

The escalation rule is the interesting part. Broadcast fires when either:

  - every terrestrial channel failed (the internet-is-down case SHELTER exists
    for), or
  - the alert is at or above `NIGCOMSAT_ALWAYS_BROADCAST_AT`, regardless of
    terrestrial success — at WARNING and above, redundant delivery is cheaper
    than a missed warning.
"""

from __future__ import annotations

import asyncio

from app.config import settings
from app.dispatch.base import Dispatcher
from app.dispatch.email_channel import EmailDispatcher
from app.dispatch.nigcomsat import NigcomsatDispatcher
from app.dispatch.signal_channel import SignalDispatcher
from app.dispatch.slack import SlackDispatcher
from app.dispatch.telegram import TelegramDispatcher
from app.dispatch.webhook import WebhookDispatcher
from app.dispatch.whatsapp import WhatsAppDispatcher
from app.logging_config import get_logger
from app.models.enums import (
    SEVERITY_ORDER,
    TERRESTRIAL_CHANNELS,
    Channel,
    DeliveryMode,
    DeliveryStatus,
    Severity,
)
from app.models.schemas import (
    Advisory,
    ChannelBinding,
    DeliveryReceipt,
    RiskAssessment,
    Subscriber,
)

log = get_logger(__name__)

_DISPATCHERS: dict[Channel, Dispatcher] = {
    d.channel: d
    for d in (
        WhatsAppDispatcher(),
        TelegramDispatcher(),
        SignalDispatcher(),
        EmailDispatcher(),
        SlackDispatcher(),
        WebhookDispatcher(),
        NigcomsatDispatcher(),
    )
}


def available_channels() -> list[Channel]:
    """Channels whose credentials are actually configured. Surfaced on /health
    so a misconfigured deployment is visible before an emergency, not during."""
    return [c for c, d in _DISPATCHERS.items() if d.available]


async def deliver(
    subscriber: Subscriber, advisory: Advisory, assessment: RiskAssessment
) -> list[DeliveryReceipt]:
    """Send one alert to one subscriber across all eligible channels.

    Channels are resolved **for the area this assessment is about**, so a subscriber can route
    flood alerts for one plot differently from crop alerts for another. `assessment.aoi_id` was
    already on hand here; it simply was not passed, so every plot shared one delivery
    configuration — see `Subscriber.channels_for` for how specific overrides general.

    ## One limit of `min_score`, stated because it would otherwise surprise

    The satellite broadcast escalation is **not** subject to the subscriber's score dial. At or
    above `NIGCOMSAT_ALWAYS_BROADCAST_AT` (default WARNING) `_should_escalate` returns True on
    severity alone, so a raised dial silences a subscriber's own channels and a district-wide burst
    may still reach them.

    That is deliberate: a one-way broadcast addresses a beam, not a person, and there is no
    per-subscriber addressing in it to filter on — the same burst reaches everyone in the footprint
    whether or not they subscribe at all. A dial is a preference about *being messaged*; it is not a
    mechanism for opting out of a public emergency signal. The dial's documented promise is
    therefore narrower than "I will not hear about anything below 0.6", and the UI says so.
    """
    # ## Does SHELTER contact this subscriber at all?
    #
    # An aggregator that onboarded a farmer often needs to be the only voice reaching them —
    # otherwise our SMS and the partner's own both arrive about one flood, in two voices, and the
    # partner cannot switch ours off. `DeliveryMode.WEBHOOK` on the area says "relay it yourself":
    # SHELTER dispatches nothing directly and the aggregator's webhook is the delivery.
    #
    # The webhook fires regardless, in every mode — Herald publishes it after this returns, and
    # suppressing it would break integrations that use it for reporting. What the mode governs is
    # only *direct* dispatch.
    #
    # Resolved from the subscriber's own copy of the area rather than re-read: `Subscriber.areas`
    # is already loaded and is the same row. An area id that is not in the list (a manual dispatch
    # for an ad-hoc assessment) falls through to DIRECT, which is the safe default — never
    # silencing an alert because a lookup missed.
    area = next((a for a in subscriber.areas if a.id == assessment.aoi_id), None)
    if area is not None and area.delivery_mode is DeliveryMode.WEBHOOK:
        log.info(
            "direct dispatch suppressed; the aggregator relays this area",
            extra={
                "subscriber_id": subscriber.id,
                "aoi_id": assessment.aoi_id,
                "severity": assessment.severity.value,
            },
        )
        # No receipts, deliberately. An empty list is honest — nothing was attempted — and it also
        # keeps `_should_escalate` from firing a NIGCOMSAT broadcast on the reasoning that every
        # terrestrial channel failed. Nothing failed; nothing was tried.
        return []

    # The score is passed as well as the severity, so a subscriber's own sensitivity dial
    # (`ChannelBinding.min_score`) applies. Delivery-side only: the assessment has already been
    # computed and persisted by the time this runs, so a raised dial suppresses a MESSAGE and never
    # a measurement — the reading is still in Postgres and still on the dashboard.
    bindings = subscriber.channels_for(
        assessment.severity, assessment.aoi_id, assessment.score
    )

    terrestrial = [b for b in bindings if b.channel in TERRESTRIAL_CHANNELS]
    explicit_broadcast = [
        b for b in bindings if b.channel == Channel.NIGCOMSAT_BROADCAST
    ]

    receipts = await _send_all(terrestrial, advisory, assessment)

    broadcast_bindings = explicit_broadcast
    if not explicit_broadcast and _should_escalate(receipts, assessment.severity):
        # No explicit broadcast binding, but the situation warrants one. Send
        # to the whole beam — better a redundant burst than a silent failure.
        broadcast_bindings = [
            ChannelBinding(
                channel=Channel.NIGCOMSAT_BROADCAST,
                address="ALL",
                min_severity=Severity.INFO,
            )
        ]
        log.info(
            "escalating to satellite broadcast",
            extra={
                "subscriber_id": subscriber.id,
                "severity": assessment.severity.value,
                "terrestrial_delivered": any(
                    r.status == DeliveryStatus.SENT for r in receipts
                ),
            },
        )

    if broadcast_bindings:
        receipts += await _send_all(broadcast_bindings, advisory, assessment)

    if not receipts:
        log.warning(
            "no eligible channels for alert",
            extra={
                "subscriber_id": subscriber.id,
                "severity": assessment.severity.value,
            },
        )

    return receipts


async def _send_all(
    bindings: list[ChannelBinding], advisory: Advisory, assessment: RiskAssessment
) -> list[DeliveryReceipt]:
    """Fan out concurrently. One channel's failure never blocks another."""
    if not bindings:
        return []

    async def send_one(binding: ChannelBinding) -> DeliveryReceipt:
        dispatcher = _DISPATCHERS.get(binding.channel)
        if dispatcher is None:
            return DeliveryReceipt(
                channel=binding.channel,
                address=binding.address,
                status=DeliveryStatus.SKIPPED,
                error="no dispatcher registered for channel",
            )
        return await dispatcher.send(binding, advisory, assessment)

    results = await asyncio.gather(
        *(send_one(b) for b in bindings), return_exceptions=True
    )

    receipts: list[DeliveryReceipt] = []
    for binding, result in zip(bindings, results, strict=True):
        if isinstance(result, Exception):
            # A dispatcher is contractually not allowed to raise; if one does,
            # record it rather than losing the whole fan-out.
            log.exception(
                "dispatcher raised",
                extra={"channel": binding.channel.value, "error": str(result)},
            )
            receipts.append(
                DeliveryReceipt(
                    channel=binding.channel,
                    address=binding.address,
                    status=DeliveryStatus.FAILED,
                    error=str(result)[:500],
                )
            )
        else:
            receipts.append(result)
    return receipts


def _should_escalate(
    receipts: list[DeliveryReceipt], severity: Severity
) -> bool:
    """Whether to fire a satellite burst on top of terrestrial delivery."""
    try:
        threshold = Severity(settings.nigcomsat_always_broadcast_at)
    except ValueError:
        threshold = Severity.WARNING

    if SEVERITY_ORDER[severity] >= SEVERITY_ORDER[threshold]:
        return True

    # Nothing got through terrestrially — this is the case broadcast exists for.
    attempted = [r for r in receipts if r.status != DeliveryStatus.SKIPPED]
    return bool(attempted) and all(
        r.status == DeliveryStatus.FAILED for r in attempted
    )
