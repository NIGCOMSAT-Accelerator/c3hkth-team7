"""Telegram Bot API.

No template approval, no per-message cost, and it works over very poor
connections — which is why it is the default fallback when WhatsApp delivery
is refused.
"""

from __future__ import annotations

from app.config import settings
from app.dispatch.base import Dispatcher, render_markdown
from app.models.enums import Channel
from app.models.schemas import Advisory, ChannelBinding, DeliveryReceipt, RiskAssessment


class TelegramDispatcher(Dispatcher):
    channel = Channel.TELEGRAM

    @property
    def available(self) -> bool:
        return bool(settings.telegram_bot_token)

    async def send(
        self,
        binding: ChannelBinding,
        advisory: Advisory,
        assessment: RiskAssessment,
    ) -> DeliveryReceipt:
        if not self.available:
            return self._skipped(binding.address, "Telegram bot token not configured")

        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": binding.address,
            "text": render_markdown(advisory, assessment),
            "parse_mode": "Markdown",
            # The advisory is self-contained; a link preview would just push
            # the actions off the first screen on a small handset.
            "disable_web_page_preview": True,
        }

        try:
            async with self.client() as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                body = response.json()
                if not body.get("ok"):
                    return self._failed(
                        binding.address, body.get("description", "unknown error")
                    )
                return self._ok(
                    binding.address, str(body.get("result", {}).get("message_id"))
                )
        except Exception as exc:
            return self._failed(binding.address, str(exc))
