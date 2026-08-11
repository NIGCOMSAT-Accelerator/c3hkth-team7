"""WhatsApp Cloud API.

The highest-reach channel in Nigeria, and the one with the most rules: outside
a 24-hour customer-service window Meta only permits pre-approved *template*
messages. Since an early warning is by definition unsolicited, production
traffic must use an approved template — free-form text will be silently
dropped by Meta even though the API returns 200.

`WHATSAPP_TEMPLATE_NAME` selects the approved template; when it is unset we
send free-form, which works for testing inside the 24-hour window.
"""

from __future__ import annotations

from app.config import settings
from app.dispatch.base import Dispatcher, render_plain
from app.models.enums import Channel
from app.models.schemas import Advisory, ChannelBinding, DeliveryReceipt, RiskAssessment


class WhatsAppDispatcher(Dispatcher):
    channel = Channel.WHATSAPP

    @property
    def available(self) -> bool:
        return bool(
            settings.whatsapp_access_token and settings.whatsapp_phone_number_id
        )

    async def send(
        self,
        binding: ChannelBinding,
        advisory: Advisory,
        assessment: RiskAssessment,
    ) -> DeliveryReceipt:
        if not self.available:
            return self._skipped(binding.address, "WhatsApp credentials not configured")

        url = (
            f"https://graph.facebook.com/{settings.whatsapp_api_version}"
            f"/{settings.whatsapp_phone_number_id}/messages"
        )
        headers = {
            "Authorization": f"Bearer {settings.whatsapp_access_token}",
            "Content-Type": "application/json",
        }

        if settings.whatsapp_template_name:
            payload = {
                "messaging_product": "whatsapp",
                "to": binding.address,
                "type": "template",
                "template": {
                    "name": settings.whatsapp_template_name,
                    "language": {"code": settings.whatsapp_template_lang},
                    "components": [
                        {
                            "type": "body",
                            "parameters": [
                                {"type": "text", "text": assessment.aoi_name},
                                {"type": "text", "text": advisory.headline},
                                {
                                    "type": "text",
                                    "text": (advisory.actions or ["Monitor conditions."])[0],
                                },
                            ],
                        }
                    ],
                },
            }
        else:
            payload = {
                "messaging_product": "whatsapp",
                "to": binding.address,
                "type": "text",
                "text": {"body": render_plain(advisory, assessment)},
            }

        try:
            async with self.client() as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
                message_id = (body.get("messages") or [{}])[0].get("id")
                return self._ok(binding.address, message_id)
        except Exception as exc:
            return self._failed(binding.address, str(exc))
