"""Signal, via a self-hosted signal-cli-rest-api instance.

Signal has no commercial API. `bbernhard/signal-cli-rest-api` wraps signal-cli
in HTTP and is what the docker-compose profile runs. Self-hosting is the point
for a sovereign system: no third party sits between the warning and the
recipient.
"""

from __future__ import annotations

from app.config import settings
from app.dispatch.base import Dispatcher, render_plain
from app.models.enums import Channel
from app.models.schemas import Advisory, ChannelBinding, DeliveryReceipt, RiskAssessment


class SignalDispatcher(Dispatcher):
    channel = Channel.SIGNAL

    @property
    def available(self) -> bool:
        return bool(settings.signal_api_url and settings.signal_sender_number)

    async def send(
        self,
        binding: ChannelBinding,
        advisory: Advisory,
        assessment: RiskAssessment,
    ) -> DeliveryReceipt:
        if not self.available:
            return self._skipped(binding.address, "Signal gateway not configured")

        url = f"{settings.signal_api_url.rstrip('/')}/v2/send"
        payload = {
            "message": render_plain(advisory, assessment),
            "number": settings.signal_sender_number,
            "recipients": [binding.address],
        }

        try:
            async with self.client() as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                body = response.json() if response.content else {}
                return self._ok(binding.address, body.get("timestamp"))
        except Exception as exc:
            return self._failed(binding.address, str(exc))
