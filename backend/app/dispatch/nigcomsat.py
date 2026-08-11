"""NIGCOMSAT-1R satellite broadcast — the offline last mile.

Every other channel in this package assumes the recipient has working internet.
During the flood that SHELTER exists to warn about, that assumption is exactly
what fails: towers lose power, backhaul floods, and the alert that mattered
never lands.

Broadcast inverts the dependency. It is one-way, needs no ground network at the
receiving end, and reaches the whole Ku-band footprint at once. The costs are
real and shape the design here:

- **No delivery confirmation.** Nothing comes back. A `SENT` receipt means the
  gateway accepted the burst, not that anyone received it.
- **Hard payload ceiling.** Broadcast bandwidth is shared and expensive, so the
  message is truncated at `nigcomsat_max_payload_bytes` and must stand alone —
  no links, no reply path, no "see the dashboard".
- **No addressing.** The footprint is the audience. Per-subscriber targeting
  happens at the receiving terminal, not here.
"""

from __future__ import annotations

from app.config import settings
from app.dispatch.base import Dispatcher
from app.models.enums import SEVERITY_ORDER, Channel, Severity
from app.models.schemas import Advisory, ChannelBinding, DeliveryReceipt, RiskAssessment


class NigcomsatDispatcher(Dispatcher):
    channel = Channel.NIGCOMSAT_BROADCAST

    @property
    def available(self) -> bool:
        return bool(settings.nigcomsat_gateway_url and settings.nigcomsat_api_key)

    async def send(
        self,
        binding: ChannelBinding,
        advisory: Advisory,
        assessment: RiskAssessment,
    ) -> DeliveryReceipt:
        if not self.available:
            return self._skipped(
                binding.address, "NIGCOMSAT gateway not configured"
            )

        text = self._payload_text(advisory, assessment)
        if not text:
            return self._failed(binding.address, "empty broadcast payload")

        payload = {
            "beam_id": settings.nigcomsat_beam_id,
            # The terminal group that should surface this burst. Falls back to
            # the whole beam when the subscriber has no specific terminal.
            "terminal_group": binding.address or "ALL",
            "priority": self._priority(assessment.severity),
            "severity": assessment.severity.value,
            "text": text,
            # Bounding box lets a terminal suppress bursts for areas it does
            # not serve, which is the only filtering possible on a one-way link.
            "bbox": None,
        }
        headers = {
            "Authorization": f"Bearer {settings.nigcomsat_api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with self.client() as client:
                response = await client.post(
                    f"{settings.nigcomsat_gateway_url.rstrip('/')}/v1/broadcast",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                body = response.json() if response.content else {}
                self.log.info(
                    "broadcast accepted by gateway",
                    extra={
                        "beam": settings.nigcomsat_beam_id,
                        "bytes": len(text.encode()),
                        "severity": assessment.severity.value,
                    },
                )
                return self._ok(binding.address, body.get("burst_id"))
        except Exception as exc:
            return self._failed(binding.address, str(exc))

    def _payload_text(self, advisory: Advisory, assessment: RiskAssessment) -> str:
        """Build the burst text, hard-capped to the byte budget.

        Truncation is on encoded bytes, not characters — the advisory may be in
        a language whose characters are multi-byte, and a burst that overruns
        the budget is rejected by the gateway rather than trimmed.
        """
        text = advisory.broadcast_text.strip()
        if not text:
            text = (
                f"SHELTER {assessment.severity.value.upper()}: "
                f"{advisory.headline}"
            )

        limit = settings.nigcomsat_max_payload_bytes
        encoded = text.encode("utf-8")
        if len(encoded) <= limit:
            return text

        # Cut on a byte boundary, then discard any partial trailing character.
        return encoded[:limit].decode("utf-8", errors="ignore").rstrip()

    @staticmethod
    def _priority(severity: Severity) -> int:
        """Gateway queue priority, 0 highest.

        Broadcast slots are contended, so an EMERGENCY must be able to jump
        ahead of a batch of routine advisories.
        """
        return max(0, 4 - SEVERITY_ORDER[severity])
