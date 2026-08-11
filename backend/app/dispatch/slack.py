"""Slack, for agency and operations-room subscribers.

Uses Block Kit so an emergency is visually distinct in a busy channel — the
reader is scanning, not reading.
"""

from __future__ import annotations

from app.config import settings
from app.dispatch.base import Dispatcher
from app.models.enums import Channel, Severity
from app.models.schemas import Advisory, ChannelBinding, DeliveryReceipt, RiskAssessment

_SEVERITY_EMOJI: dict[Severity, str] = {
    Severity.EMERGENCY: "🚨",
    Severity.WARNING: "⚠️",
    Severity.WATCH: "👀",
    Severity.ADVISORY: "ℹ️",
    Severity.INFO: "📋",
}


class SlackDispatcher(Dispatcher):
    channel = Channel.SLACK

    @property
    def available(self) -> bool:
        return bool(settings.slack_bot_token)

    async def send(
        self,
        binding: ChannelBinding,
        advisory: Advisory,
        assessment: RiskAssessment,
    ) -> DeliveryReceipt:
        if not self.available:
            return self._skipped(binding.address, "Slack bot token not configured")

        emoji = _SEVERITY_EMOJI.get(assessment.severity, "📡")
        blocks: list[dict] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{emoji} {advisory.headline}"[:150],
                },
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": advisory.body},
            },
        ]

        if advisory.actions:
            actions = "\n".join(f"• {a}" for a in advisory.actions)
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*What to do*\n{actions}"},
                }
            )

        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"*{assessment.aoi_name}* · "
                            f"severity `{assessment.severity.value}` · "
                            f"confidence `{assessment.confidence:.0%}` · "
                            f"{assessment.lead_time_days}-day outlook"
                        ),
                    }
                ],
            }
        )

        payload = {
            "channel": binding.address or settings.slack_default_channel,
            "text": advisory.headline,  # notification fallback text
            "blocks": blocks,
        }
        headers = {
            "Authorization": f"Bearer {settings.slack_bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        try:
            async with self.client() as client:
                response = await client.post(
                    "https://slack.com/api/chat.postMessage",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                body = response.json()
                # Slack returns HTTP 200 on logical failures, so ok must be read.
                if not body.get("ok"):
                    return self._failed(
                        binding.address, body.get("error", "unknown slack error")
                    )
                return self._ok(binding.address, body.get("ts"))
        except Exception as exc:
            return self._failed(binding.address, str(exc))
