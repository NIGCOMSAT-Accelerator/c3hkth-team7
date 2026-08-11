"""Dispatch contract.

Every channel implements the same two things: can it run at all (`available`),
and send one alert to one address. Failures return a receipt with the error
rather than raising, because one dead channel must not stop the other five.
"""

from __future__ import annotations

import abc

import httpx

from app.config import settings
from app.logging_config import get_logger
from app.models.enums import Channel, DeliveryStatus
from app.models.schemas import Advisory, ChannelBinding, DeliveryReceipt, RiskAssessment


class Dispatcher(abc.ABC):
    """One delivery surface."""

    channel: Channel

    def __init__(self) -> None:
        self.log = get_logger(f"dispatch.{self.channel.value}")

    @property
    @abc.abstractmethod
    def available(self) -> bool:
        """True when this channel's credentials are configured."""

    @abc.abstractmethod
    async def send(
        self,
        binding: ChannelBinding,
        advisory: Advisory,
        assessment: RiskAssessment,
    ) -> DeliveryReceipt:
        """Deliver one alert. Must not raise."""

    # -- helpers shared by the HTTP-based channels -------------------- #

    def _ok(self, address: str, provider_id: str | None = None) -> DeliveryReceipt:
        return DeliveryReceipt(
            channel=self.channel,
            address=address,
            status=DeliveryStatus.SENT,
            provider_message_id=provider_id,
        )

    def _failed(self, address: str, error: str) -> DeliveryReceipt:
        self.log.warning(
            "delivery failed", extra={"channel": self.channel.value, "error": error}
        )
        return DeliveryReceipt(
            channel=self.channel,
            address=address,
            status=DeliveryStatus.FAILED,
            error=error[:500],
        )

    def _skipped(self, address: str, reason: str) -> DeliveryReceipt:
        return DeliveryReceipt(
            channel=self.channel,
            address=address,
            status=DeliveryStatus.SKIPPED,
            error=reason,
        )

    @staticmethod
    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(settings.webhook_timeout_seconds, connect=10.0)
        )


def explanation_lines(advisory: Advisory) -> list[str]:
    """The three explanation surfaces as plain lines, or an empty list.

    ## Placed AFTER the actions, deliberately

    The advisory and its actions are what a farmer must read; the explanations are why. Someone
    skimming an SMS during a storm needs the instruction first, and burying "move stored produce"
    under three paragraphs of narration would make the message worse rather than richer.

    Empty when no explanation was produced, so a message never carries a bare heading with nothing
    under it — which reads as a broken template and undermines the rest of the alert.

    Shared by every channel through `render_plain`/`render_markdown` rather than added per
    dispatcher: five near-identical implementations is how one channel ends up omitting them, and
    a subscriber comparing their email against their WhatsApp should see the same words.
    """
    e = advisory.explanations
    out: list[str] = []

    if e.crop:
        out += ["", "Your crop:", f"  {e.crop}"]
    if e.drivers:
        out += ["", "Why this reading:", f"  {e.drivers}"]
    if e.irrigation:
        out += ["", "Watering:", f"  {e.irrigation}"]

    return out


def card_fields(assessment: RiskAssessment) -> list[tuple[str, str]]:
    """`(label, value)` for the report card, in reading order.

    **The single source of the card's content.** `situation_lines` formats these as text for SMS,
    WhatsApp and plain email; `email_channel` renders them as table rows for HTML. Deriving both
    from one function is what stops a subscriber reading their SMS and their email and seeing two
    different summaries of the same alert — which is exactly the drift `email_layout` was created
    to end for account mail.

    A field is **omitted when its input is unknown**, never defaulted. A first assessment has no
    previous run; a plot with no fitted baseline has no seasonal norm; a cloudy cycle measured no
    soil water. Printing "no change" or "normal" there would assert something false.
    """
    change = assessment.change
    freshness = assessment.freshness
    fields: list[tuple[str, str]] = [
        (
            "Status",
            f"{assessment.severity.value.upper()} · "
            f"{assessment.hazard.value.replace('_', ' ')}",
        )
    ]

    if change.direction and change.previous_severity:
        moved = {"up": "Rising", "down": "Easing", "steady": "Unchanged"}.get(
            change.direction, change.direction
        )
        fields.append(
            ("Since last check", f"{moved} — was {change.previous_severity.upper()}")
        )

    if change.vs_seasonal:
        fields.append(
            (
                "Compared with normal",
                "About usual for this field at this time of year"
                if change.vs_seasonal == "normal"
                else f"{change.vs_seasonal.capitalize()} than this field usually is now",
            )
        )

    # A word, not a percentage. "62%" invites arithmetic nobody should perform on a confidence.
    band = (
        "High" if assessment.confidence >= 0.8
        else "Medium" if assessment.confidence >= 0.65
        else "Low"
    )
    fields.append(("Confidence", band))

    advice = assessment.soil_moisture.irrigation_advice
    if advice:
        verb = {
            "irrigate": "Irrigate",
            "hold": "No irrigation needed",
            "drain": "Do not irrigate — drain if you can",
        }
        fields.append(
            (
                "Soil water",
                f"{verb.get(advice, advice)} "
                f"({assessment.soil_moisture.volumetric:.2f} m3/m3)",
            )
        )

    if freshness.observed_at:
        seen = f"{freshness.observed_at:%d %b %H:%M} UTC"
        if freshness.platform:
            seen += f" · {freshness.platform}"
        fields.append(("Last look", seen))
    if freshness.next_expected:
        fields.append(("Next expected", f"Around {freshness.next_expected:%d %b}"))
    if freshness.caveat:
        fields.append(("Note", freshness.caveat))

    return fields


def situation_lines(assessment: RiskAssessment) -> list[str]:
    """The report card as plain-text lines, for every channel that is not HTML.

    Formatting only — the CONTENT comes from `card_fields`, so text and HTML cannot disagree about
    what an alert says. See that function for why each field may be absent.

    ## Why this goes above the advisory prose

    The advisory is the reasoning: grounded, checkable, and the thing that makes an alert judgeable
    rather than an instruction to obey. But it runs to several paragraphs, and a farmer reading on a
    phone decides whether to act in the first few seconds. So the card answers first and the prose
    stays underneath — nothing is removed.
    """
    lines = ["", "AT A GLANCE"]
    lines += [f"  {label}: {value}" for label, value in card_fields(assessment)]
    return lines


def render_plain(advisory: Advisory, assessment: RiskAssessment) -> str:
    """Full-length message for channels with no meaningful size limit."""
    lines = [f"⚠️  {advisory.headline}"]
    # The card first, then the reasoning — see `situation_lines`.
    lines += situation_lines(assessment)
    lines += ["", advisory.body]
    if advisory.actions:
        lines += ["", "What to do:"]
        lines += [f"  {i}. {a}" for i, a in enumerate(advisory.actions, 1)]

    lines += explanation_lines(advisory)

    lines += [
        "",
        f"Area: {assessment.aoi_name}",
        f"Severity: {assessment.severity.value.upper()}",
        f"Outlook: {assessment.lead_time_days} days",
        "",
        f"SHELTER · {settings.public_site_url}",
    ]
    return "\n".join(lines)


def render_markdown(advisory: Advisory, assessment: RiskAssessment) -> str:
    """For Slack and Telegram, which render a subset of Markdown."""
    lines = [f"*{advisory.headline}*"]
    lines += situation_lines(assessment)
    lines += ["", advisory.body]
    if advisory.actions:
        lines += ["", "*What to do:*"]
        lines += [f"• {a}" for a in advisory.actions]

    lines += explanation_lines(advisory)

    lines += [
        "",
        f"_{assessment.aoi_name} · {assessment.severity.value.upper()} · "
        f"{assessment.lead_time_days}-day outlook_",
    ]
    return "\n".join(lines)
