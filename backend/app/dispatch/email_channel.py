"""Email for advisories. Brevo's HTTP API where available, SMTP otherwise.

The channel of record for government and cooperative subscribers — it is the
one that produces something filable. Sends multipart plain+HTML so it reads
correctly on a feature-phone mail client and in a browser.

## Why two transports, and why the API is preferred

This channel was SMTP-only, and on this deployment SMTP **fails**: Brevo's relay
returned `535 Authentication failed` because the SMTP path additionally requires the
sending host's egress IP to be on Brevo's permit list — fragile on a laptop, a CI
runner, or a VPS whose address changes. The consequence was specific and bad: a real
`watch` assessment with 65% standing water produced `email: failed`, the router logged
"alert reached nobody", and the subscriber's dashboard showed no active monitoring
because the portal reads *alerts*, not assessments.

`app/iam/mailer.py` had already hit and documented this, and solved it by preferring
Brevo's HTTP API, which authenticates on the key alone with no IP allow-list. That
transport was working for verification and invitation mail on the very same deployment
where advisories were silently failing — the same account, two different results,
because only one path had the workaround.

So this channel now resolves the transport the same way. Keeping the two in step matters
more than the code saved: an operator who configures email once should not find that
onboarding mail arrives and hazard warnings do not.

`smtplib` is blocking, so the SMTP send runs on a worker thread. The API path is
already async.
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

import httpx

from app.config import settings
from app.dispatch.base import Dispatcher, card_fields, render_plain
from app.dispatch.tracks import tracks
from app.email import layout
from app.models.enums import Channel, Severity
from app.models.schemas import Advisory, ChannelBinding, DeliveryReceipt, RiskAssessment

# Brand palette, matched to the FreePass / ZeroRate design system.
_BRAND = "#6a0dad"
_INK = "#0b001b"

def _brevo_ready() -> bool:
    """Whether the API transport can be used.

    Mirrors `iam.mailer._brevo_ready` deliberately — if the two ever disagree, onboarding
    mail and hazard advisories would take different paths on the same deployment, which is
    exactly the split that hid this bug.
    """
    return bool(
        settings.brevo_api_key and (settings.brevo_sender_email or settings.smtp_from)
    )


_SEVERITY_COLOR: dict[Severity, str] = {
    Severity.EMERGENCY: "#b42318",
    Severity.WARNING: "#dd7400",
    Severity.WATCH: "#9a2ce9",
    Severity.ADVISORY: "#6a0dad",
    Severity.INFO: "#4a5565",
}


class EmailDispatcher(Dispatcher):
    channel = Channel.EMAIL

    @property
    def available(self) -> bool:
        """Either transport counts. SMTP alone is still enough on a permitted host."""
        return bool(settings.smtp_host) or _brevo_ready()

    async def send(
        self,
        binding: ChannelBinding,
        advisory: Advisory,
        assessment: RiskAssessment,
    ) -> DeliveryReceipt:
        if not self.available:
            return self._skipped(binding.address, "SMTP not configured")

        message = EmailMessage()
        message["Subject"] = (
            f"[SHELTER {assessment.severity.value.upper()}] {advisory.headline}"
        )
        message["From"] = settings.smtp_from
        message["To"] = binding.address
        # Set explicitly so the receipt carries a real, traceable id. Left to
        # the MTA, this header is absent at send time and the receipt records
        # `provider_message_id: null`, making delivery unauditable.
        message["Message-ID"] = make_msgid(domain=settings.smtp_from.split("@")[-1])
        message.set_content(render_plain(advisory, assessment))
        message.add_alternative(
            self._html(advisory, assessment), subtype="html"
        )

        # Brevo's API first when configured: no IP allow-list, and it returns a messageId
        # that makes delivery auditable against the receipt.
        if _brevo_ready():
            message_id = await self._send_brevo_api(
                binding.address,
                message["Subject"],
                render_plain(advisory, assessment),
                self._html(advisory, assessment),
            )
            if message_id is not None:
                return self._ok(binding.address, message_id)
            # Fall through to SMTP rather than giving up — a transient API failure should
            # not cost a hazard warning when a working relay is also configured.
            if not settings.smtp_host:
                return self._failed(
                    binding.address, "Brevo API send failed and no SMTP relay configured"
                )
            self.log.warning("Brevo API send failed; falling back to SMTP")

        if not settings.smtp_host:
            return self._skipped(binding.address, "no email transport configured")

        try:
            await asyncio.to_thread(self._send_sync, message)
            return self._ok(binding.address, message["Message-ID"])
        except Exception as exc:
            return self._failed(binding.address, str(exc))

    async def _send_brevo_api(
        self, to: str, subject: str, plain: str, html: str
    ) -> str | None:
        """POST to Brevo's transactional endpoint. Returns the messageId, or None.

        `httpx` rather than a vendor SDK, matching `app/iam/mailer.py` and `app/llm/` — one
        less dependency whose release cadence we do not control, and a payload we can read.

        Tagged distinctly from onboarding mail so alert traffic is filterable in Brevo's
        dashboard, which is what a delivery complaint about a specific warning needs.
        """
        sender_email = settings.brevo_sender_email or settings.smtp_from
        payload: dict = {
            "sender": {"name": settings.brevo_sender_name, "email": sender_email},
            "to": [{"email": to}],
            "subject": subject,
            "textContent": plain,
            "htmlContent": html,
            "tags": [f"{settings.brevo_tag}-advisory"],
        }
        if settings.brevo_reply_to_email or settings.smtp_reply_to:
            payload["replyTo"] = {
                "email": settings.brevo_reply_to_email or settings.smtp_reply_to
            }

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
                response = await client.post(
                    settings.brevo_api_url,
                    json=payload,
                    headers={
                        "api-key": settings.brevo_api_key or "",
                        "content-type": "application/json",
                        "accept": "application/json",
                    },
                )
        except Exception as exc:
            # Never raises: `Dispatcher.send` must return a receipt, because one channel
            # failing must not break the fan-out to the others.
            self.log.warning(
                "Brevo API request failed",
                extra={"error": f"{type(exc).__name__}: {exc}"},
            )
            return None

        if response.status_code in (200, 201, 202):
            try:
                return str(response.json().get("messageId") or "brevo-accepted")
            except Exception:
                # A 2xx with an unparseable body is still a successful send.
                return "brevo-accepted"

        self.log.warning(
            "Brevo API rejected the advisory",
            extra={"status": response.status_code, "body": response.text[:200]},
        )
        return None

    @staticmethod
    def _send_sync(message: EmailMessage) -> None:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            if settings.smtp_use_tls:
                server.starttls()
            if settings.smtp_username and settings.smtp_password:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(message)

    @staticmethod
    def _html(advisory: Advisory, assessment: RiskAssessment) -> str:
        """The advisory body, wrapped in the platform's shared email chrome.

        ## Why this no longer builds its own document

        It used to, and the result was that the one email a subscriber receives *because the service
        is working* was the only one that did not look like the service: no SHELTER mark, no footer,
        no FreePass/NIGCOMSAT attribution, a different hairline colour (`#3407561a` against the
        layout's `#e6ddf5`), no preheader and no dark-mode rule. Eleven kinds of account mail went
        through `email.layout`; the hazard warning did not.

        That asymmetry is worse than ordinary inconsistency. A farmer decides whether to act on a
        flood warning partly by deciding whether it is genuine, and an email that shares no chrome
        with the welcome mail they already trust is the one that looks like phishing.

        Only the **accent** varies now — the header band is coloured by severity, so an EMERGENCY
        reads red before a word is parsed. Everything else is `layout.render`'s.
        """
        accent = _SEVERITY_COLOR.get(assessment.severity, _BRAND)
        actions = "".join(
            f'<li style="margin:0 0 8px;">{a}</li>' for a in advisory.actions
        )
        actions_block = (
            f'<p style="margin:24px 0 8px;font-weight:600;color:{_INK};">What to do</p>'
            f'<ol style="margin:0;padding-left:20px;color:#364153;">{actions}</ol>'
            if actions
            else ""
        )

        # The three explanation surfaces, after the actions.
        #
        # Order matters: the instruction comes first because that is what a farmer must act on,
        # and the narration follows because that is what makes the instruction believable. A
        # reader who stops after "what to do" has still got the important part.
        #
        # Each row is omitted individually when empty, so a partial failure produces a shorter
        # email rather than a heading with nothing under it.
        e = advisory.explanations
        rows = "".join(
            f'<tr>'
            f'<td style="padding:8px 0;vertical-align:top;width:96px;'
            f'color:#6a7282;font-size:12px;font-weight:600;">{label}</td>'
            f'<td style="padding:8px 0;color:#364153;font-size:13.5px;'
            f'line-height:1.55;">{text}</td>'
            f"</tr>"
            for label, text in (
                ("Your crop", e.crop),
                ("Why", e.drivers),
                ("Watering", e.irrigation),
            )
            if text
        )
        # ## The report card, above the prose
        #
        # Same fields as `situation_lines`, and derived from the same objects — but rendered as a
        # table rather than reusing that function's strings, because email HTML needs each row in
        # its own cell to survive Outlook. The wording is kept identical so a subscriber reading
        # the SMS and the email does not see two different summaries of one alert.
        #
        # A definition-list shape rather than prose: the point is that "Since last check" and
        # "Confidence" sit where the eye already expects them, on every alert.
        # Rendered by the layout, from the same `card_fields` the text channels use. Previously this
        # hand-built the table with its own colour literals, which is how it came to use a hairline
        # the rest of the platform does not.
        card_block = layout.card(card_fields(assessment))

        # The per-track modules: one block per measured dimension, most relevant first. `tracks`
        # omits a dimension entirely when nothing was measured, so an assessment with only a radar
        # pass produces one module rather than five reading zero.
        tracks_block = layout.track_modules(tracks(assessment))

        explanations_block = (
            f'<p style="margin:24px 0 4px;font-weight:600;color:{_INK};">'
            f"What this means for you</p>"
            f'<table role="presentation" cellpadding="0" cellspacing="0" '
            f'style="width:100%;border-collapse:collapse;">{rows}</table>'
            if rows
            else ""
        )
        evidence = "".join(
            f'<li style="margin:0 0 6px;">{e}</li>' for e in assessment.evidence[:5]
        )

        body_html = f"""\
{card_block}
      <p style="margin:0;color:#364153;font-size:15px;line-height:1.6;">
        {advisory.body}
      </p>
      {actions_block}
      {tracks_block}
      {explanations_block}
      <p style="margin:24px 0 8px;font-weight:600;color:{_INK};">Why we sent this</p>
      <ul style="margin:0;padding-left:20px;color:#6a7282;font-size:13px;">
        {evidence}
      </ul>
      <p style="margin:24px 0 0;font-size:12px;color:#99a1af;">
        {assessment.aoi_name} &middot; generated from Sentinel-1 and Sentinel-2 open data.
        This is a forecast, not a guarantee.
      </p>"""

        return layout.render(
            kind="alert",
            eyebrow=(
                f"{assessment.severity.value.upper()} &middot; "
                f"{assessment.lead_time_days}-day outlook"
            ),
            title=advisory.headline,
            body_html=body_html,
            accent=accent,
        )
