"""Generic outbound webhook.

For subscribers who want the alert in their own system — an insurer's payout
engine, a state government dashboard, a cooperative's SMS gateway. Sends the
full structured alert as JSON so the receiver gets the evidence and the
forecast series, not just the prose.

Payloads are HMAC-SHA256 signed. The receiver must verify `X-SHELTER-Signature`
before acting: an unauthenticated flood alert is an obvious vector for
triggering unwarranted evacuations or insurance payouts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from app.config import settings
from app.dispatch.base import Dispatcher
from app.models.enums import Channel
from app.models.schemas import Advisory, ChannelBinding, DeliveryReceipt, RiskAssessment


class WebhookDispatcher(Dispatcher):
    channel = Channel.WEBHOOK

    @property
    def available(self) -> bool:
        # Always available — the endpoint comes from the subscriber, not config.
        return True

    async def send(
        self,
        binding: ChannelBinding,
        advisory: Advisory,
        assessment: RiskAssessment,
    ) -> DeliveryReceipt:
        if not binding.address.startswith("https://"):
            # Refusing plain HTTP is deliberate: the payload can trigger
            # evacuations and payouts downstream.
            return self._failed(binding.address, "webhook URL must be https")

        payload = {
            "event": "shelter.alert",
            "sent_at": int(time.time()),
            "severity": assessment.severity.value,
            "hazard": assessment.hazard.value,
            "advisory": advisory.model_dump(mode="json"),
            "assessment": assessment.model_dump(mode="json"),
        }
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "SHELTER/1.0",
            "X-SHELTER-Event": "alert",
        }
        if settings.webhook_signing_secret:
            headers["X-SHELTER-Signature"] = self._sign(body)

        try:
            async with self.client() as client:
                response = await client.post(
                    binding.address, content=body, headers=headers
                )
                response.raise_for_status()
                return self._ok(binding.address, str(response.status_code))
        except Exception as exc:
            return self._failed(binding.address, str(exc))

    @staticmethod
    def _sign(body: str) -> str:
        digest = hmac.new(
            settings.webhook_signing_secret.encode(),
            body.encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"sha256={digest}"
