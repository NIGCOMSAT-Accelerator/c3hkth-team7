"""Agent 4 — Herald.

The last mile. Takes an assessment, writes the advisory in the subscriber's
language, sends it everywhere they asked for, and records what actually landed.

Two suppression rules keep the service trustworthy rather than noisy:

- **Floor.** INFO-grade findings are stored but not sent. A system that pings
  every week gets muted, and then the one that mattered is muted too.
- **Deduplication.** The same hazard at the same severity for the same area is
  not re-sent within `RESEND_WINDOW_HOURS` unless it has escalated.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.advisory import generator
from app.agents import fahis
from app.agents.base import Agent
from app.dispatch import router
from app.explain import explain_all
from app.models import intelligence
from app.models.enums import SEVERITY_ORDER, JobStage, Severity
from app.models.schemas import Alert, RiskAssessment, Subscriber
from app.queue.redis_client import get_redis
from app.store import repository
from app.webhooks import publisher as webhook_publisher
from app.webhooks import schemas as webhook_schemas

#: Findings below this are recorded but never dispatched.
#:
#: **INFO, not ADVISORY.** A subscriber who hears nothing for three weeks cannot tell working
#: monitoring from a dead pipeline, and the silence is indistinguishable from failure at exactly
#: the moment trust matters. So a low-risk reading is delivered too: it says "we looked, here is
#: what we saw, nothing needs doing" — which is the 24/7 confirmation the service promises.
#:
#: This is affordable because of the dedupe window, not in spite of it. The watch loop runs every
#: 6 hours, so a naive floor of INFO would page four times a day per plot. `_is_duplicate`
#: suppresses an equal-or-lower severity inside `RESEND_WINDOW_HOURS`, so in practice a quiet plot
#: produces roughly ONE heartbeat a day and an escalation still gets through immediately.
#:
#: **Opting out is per plot and per channel, and already existed**: `ChannelBinding.min_severity`
#: filters in `Subscriber.channels_for`. Someone who wants only real warnings sets that to
#: `advisory` on the plot in question — see `SubscriberCreate` for why the DEFAULT moved with this.
DISPATCH_FLOOR = Severity.INFO

#: Don't repeat an unchanged alert inside this window.
RESEND_WINDOW_HOURS = 18

_DEDUPE_KEY = "shelter:sent:{subscriber_id}:{aoi_id}:{hazard}"


class HeraldAgent(Agent[tuple[Subscriber, RiskAssessment], Alert | None]):
    stage = JobStage.HERALD
    next_stage = None

    async def run(
        self, payload: tuple[Subscriber, RiskAssessment]
    ) -> Alert | None:
        subscriber, assessment = payload

        # Persist first. The dashboard should show the finding even when we
        # deliberately choose not to page anyone about it.
        await repository.save_assessment(assessment)

        # Mark when Fahis may check this against reality. Scheduling only — the
        # Herald never invokes verification, and nothing about the verdict can
        # flow back into an advisory. Below `VERIFY_FLOOR` this is a no-op.
        await repository.schedule_verification(
            assessment.id,
            assessment.assessed_at,
            fahis.verify_after_for(assessment),
        )

        if SEVERITY_ORDER[assessment.severity] < SEVERITY_ORDER[DISPATCH_FLOOR]:
            self.log.info(
                "below dispatch floor; recorded only",
                extra={
                    "aoi_id": assessment.aoi_id,
                    "severity": assessment.severity.value,
                },
            )
            return None

        if await self._is_duplicate(subscriber, assessment):
            self.log.info(
                "duplicate alert suppressed",
                extra={
                    "subscriber_id": subscriber.id,
                    "aoi_id": assessment.aoi_id,
                    "severity": assessment.severity.value,
                },
            )
            return None

        advisory = await generator.generate(assessment, subscriber)

        # The three explanation surfaces, generated HERE and nowhere earlier.
        #
        # After both suppression gates on purpose: an info-level reading below `DISPATCH_FLOOR`,
        # and a duplicate inside the 18-hour window, are both returned above without reaching
        # this line. So the token cost scales with alerts actually delivered rather than with
        # scans — and most assessments are info-level and reach nobody.
        #
        # Attached to the advisory so they are stored with the alert and rendered identically in
        # the email and the portal. Regenerating them on read would show a farmer disputing
        # "you never warned me" text produced today from an assessment measured weeks ago.
        advisory = advisory.model_copy(
            update={"explanations": await explain_all(assessment)}
        )

        receipts = await router.deliver(subscriber, advisory, assessment)

        alert = Alert(
            subscriber_id=subscriber.id,
            assessment=assessment,
            advisory=advisory,
            receipts=receipts,
        )
        await repository.save_alert(alert)
        await self._mark_sent(subscriber, assessment)

        delivered = sum(1 for r in receipts if r.status.value == "sent")
        self.log.info(
            "alert dispatched",
            extra={
                "alert_id": alert.id,
                "subscriber_id": subscriber.id,
                "severity": assessment.severity.value,
                "channels_attempted": len(receipts),
                "channels_delivered": delivered,
                "generated_by": advisory.generated_by,
            },
        )

        if receipts and delivered == 0:
            # Worth its own line: every channel failed, including any broadcast
            # escalation. This is the state that needs a human.
            self.log.error(
                "alert reached nobody",
                extra={"alert_id": alert.id, "subscriber_id": subscriber.id},
            )

        # Business-integration fan-out. Deliberately *after* every subscriber
        # channel has been attempted and after the alert is logged: a partner's
        # endpoint must never delay or fail a farmer's warning. `publish` never
        # raises and only enqueues — retries are the scheduler's sweep.
        # Built through the DECLARED model, not a dict literal.
        #
        # An aggregator writes their handler once and runs it for years, so the field names are the
        # contract. Assembling this as a literal meant nothing declared the shape, it could not
        # appear in OpenAPI, and renaming a key would break every integration with no test failing.
        # `AlertEventData` is that declaration — see `app/webhooks/schemas.py`.
        event_data = webhook_schemas.AlertEventData(
            alert_id=alert.id,
            severity=assessment.severity.value,
            hazard=assessment.hazard.value,
                # The intelligence block: what this category MEANS and what it warrants.
                #
                # Sent so an aggregator's system acts on our interpretation of Watch rather than
                # inventing its own — the moment two partners disagree about what a category
                # warrants, the platform is delivering numbers again rather than intelligence.
                #
            # From the same table the Web UI reads, so a subscriber's portal and their
            # aggregator's dashboard cannot describe one alert differently.
            intelligence=intelligence.describe(
                assessment.severity, assessment.confidence, assessment.hazard
            ),
            # The three plain-language surfaces, already stored on the alert. Included here so an
            # async integration receives exactly what the email and portal show.
            explanations=advisory.explanations.model_dump(mode="json"),
            advisory=advisory.model_dump(mode="json"),
            assessment=assessment.model_dump(mode="json"),
        )

        await webhook_publisher.publish(
            "shelter.alert",
            event_data.model_dump(mode="json"),
            severity=assessment.severity.value,
            aoi_id=assessment.aoi_id,
        )

        return alert

    # ------------------------------------------------------------------ #

    async def _is_duplicate(
        self, subscriber: Subscriber, assessment: RiskAssessment
    ) -> bool:
        """True when an equal-or-lower severity alert went out recently.

        Escalation always gets through: a WATCH yesterday must not suppress
        today's EMERGENCY for the same hazard.
        """
        key = _DEDUPE_KEY.format(
            subscriber_id=subscriber.id,
            aoi_id=assessment.aoi_id,
            hazard=assessment.hazard.value,
        )
        previous = await get_redis().get(key)
        if not previous:
            return False

        try:
            last_severity = Severity(previous)
        except ValueError:
            return False

        return SEVERITY_ORDER[assessment.severity] <= SEVERITY_ORDER[last_severity]

    async def _mark_sent(
        self, subscriber: Subscriber, assessment: RiskAssessment
    ) -> None:
        key = _DEDUPE_KEY.format(
            subscriber_id=subscriber.id,
            aoi_id=assessment.aoi_id,
            hazard=assessment.hazard.value,
        )
        await get_redis().set(
            key,
            assessment.severity.value,
            ex=int(timedelta(hours=RESEND_WINDOW_HOURS).total_seconds()),
        )


def next_run_after(last: datetime | None, interval_seconds: int) -> bool:
    """Whether enough time has passed to re-evaluate an AOI."""
    if last is None:
        return True
    return datetime.now(timezone.utc) - last >= timedelta(seconds=interval_seconds)
