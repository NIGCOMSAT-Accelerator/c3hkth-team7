"""Transactional email for onboarding — verification and welcome messages.

Separate from `app/dispatch/email_channel.py`, and the distinction matters: that
module delivers *hazard advisories* as one of seven channels, and its failures are
absorbed by the dispatch router so one dead channel never breaks a fan-out. This one
sends *account* mail, where a failure has to be visible to the person clicking the
button — a signup that silently sends no verification link leaves an account that can
never be activated.

Both speak the same SMTP relay, so `SMTP_HOST=smtp-relay.brevo.com` configures alerts
and onboarding together. Brevo needs no code: it is a standards-compliant relay on
:587 with STARTTLS, which is exactly what `smtplib` already does.

**Every send is best-effort and non-blocking.** `smtplib` is synchronous, so it runs
on a worker thread; a failure returns False and is logged rather than raising, and the
caller decides. Signup deliberately still succeeds when mail fails — the account
exists and the link can be re-sent, whereas rolling back the registration would lose
the password the user just chose.
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

import httpx

from app.config import settings
from app.email import layout
from app.iam import passwordless as pwless
from app.iam import team
from app.logging_config import get_logger

log = get_logger(__name__)

_BRAND = "#6a0dad"
_INK = "#0b001b"


def _verification_url(token: str) -> str:
    """The link in the email.

    Points at the *portal*, not the API: the user needs a page that confirms and then
    offers the next step, and a raw JSON response from a POST endpoint is a dead end
    in a mail client. The portal calls the API on their behalf.

    `/auth/verify`, not `/verify` — the portal has no route at the bare path, so every
    confirmation link sent before this fix landed on a 404 and no account could be
    verified from its email. `passwordless.magic_link_url` already used the correct
    prefix, which is why sign-in links worked while confirmation links did not.

    The `purpose` parameter tells the page which redemption to attempt. Both token kinds
    arrive at the same route and are indistinguishable by shape, and trying the wrong one
    *consumes* a single-use token — so the kind is stated rather than guessed.
    """
    base = settings.public_site_url.rstrip("/")
    return f"{base}/auth/verify?purpose=email&token={token}"


async def send_verification(email: str, first_name: str, token: str) -> bool:
    """Email the confirmation link. False on any failure."""
    url = _verification_url(token)
    subject = "Confirm your email to activate SHELTER alerts"

    plain = (
        f"Hello {first_name},\n\n"
        "Confirm your email address to activate your SHELTER early-warning alerts:\n\n"
        f"{url}\n\n"
        f"The link is valid for {settings.iam_verification_ttl_hours} hours.\n\n"
        "Until you confirm, we will not send alerts to this address — that is "
        "deliberate, so nobody can subscribe someone else's inbox.\n\n"
        "If you did not create this account, ignore this message and nothing "
        "further will happen.\n\n"
        "SHELTER — satellite early warning for flood, crop and health risk.\n"
    )

    html = layout.render(
        kind="verify",
        eyebrow="Account activation",
        title="Confirm your email",
        body_html=(
            f'<p style="margin:0 0 20px;">Hello {first_name}, confirm your address '
            f"to activate your alerts.</p>"
            + layout.button(url, "Confirm my email")
            + '<p style="margin:0 0 6px;font-size:13px;color:#6a7282;">'
            "Or paste this into your browser:</p>"
            f'<p style="margin:0 0 22px;font-size:12px;color:#99a1af;'
            f'word-break:break-all;">{url}</p>'
            + layout.note(
                f"Valid for {settings.iam_verification_ttl_hours} hours. Until you "
                "confirm, we send nothing to this address — that is deliberate, so "
                "nobody can subscribe someone else's inbox."
            )
        ),
    )
    return await _send(email, subject, plain, html)


def _invitation_url(token: str) -> str:
    """The link in a team invitation.

    Points at `/auth/invite`, which is a page rather than an endpoint: the invited person may
    have no SHELTER account at all, so the page has to offer signup-then-accept. A link
    straight to the accept endpoint would 401 for exactly the people invitations are for.
    """
    base = settings.public_site_url.rstrip("/")
    return f"{base}/auth/invite?token={token}"


async def send_team_invitation(
    email: str,
    *,
    organisation_name: str,
    inviter: str,
    token: str,
    workspaces: list[str],
    context: RequestContext | None = None,
) -> bool:
    """Invite a colleague into an organisation's workspaces. False on any failure.

    Names the organisation and the inviter, because an unexplained invitation to a satellite
    warning platform reads as phishing — and the one defence a recipient has is recognising
    who sent it.

    The workspace COUNT is stated rather than the workspace names. Names are chosen by the
    aggregator ("Bayelsa flood pilot") and can carry commercial information; an invitation may
    reach a mistyped address, so the email says as little as it can while remaining useful.
    """
    url = _invitation_url(token)
    subject = f"{inviter} invited you to {organisation_name} on SHELTER"
    count = len(workspaces)
    where = "1 workspace" if count == 1 else f"{count} workspaces"

    plain = (
        "Hello,\n\n"
        f"{inviter} has invited you to join {organisation_name} on SHELTER, "
        f"with access to {where}.\n\n"
        f"{url}\n\n"
        f"The invitation is valid for {team.INVITE_TTL_HOURS // 24} days.\n\n"
        "You will need to sign in — or create an account with this email address — "
        "before the invitation can be accepted. It only works for this address, so "
        "forwarding it will not grant access to anyone else.\n\n"
        "If you were not expecting this, ignore the message. Nothing happens until "
        "you accept.\n\n"
        # The INVITER's device, not the recipient's — the recipient has no session yet. It
        # answers "who actually sent this, and from where", which is the one check a person
        # can make against an unexpected invitation to a platform they do not know.
        + (context.plain() if context else "")
        + "\nSHELTER — satellite early warning for flood, crop and health risk.\n"
    )

    html = layout.render(
        kind="invite",
        eyebrow="Team invitation",
        title=f"Join {organisation_name}",
        body_html=(
            f'<p style="margin:0 0 20px;">{inviter} has invited you to join '
            f"<strong>{organisation_name}</strong> on SHELTER, with access to {where}.</p>"
            + layout.button(url, "Accept invitation")
            + '<p style="margin:0 0 6px;font-size:13px;color:#6a7282;">'
            "Or paste this into your browser:</p>"
            f'<p style="margin:0 0 22px;font-size:12px;color:#99a1af;'
            f'word-break:break-all;">{url}</p>'
            + layout.note(
                f"Valid for {team.INVITE_TTL_HOURS // 24} days, and it works once. The link "
                "signs you in and asks you to choose your own password — there is no "
                "temporary password to type, and nothing in this email is a credential after "
                "you have used it."
            )
            + (context.block() if context else "")
        ),
    )
    return await _send(email, subject, plain, html)


async def send_profile_activated(
    email: str,
    first_name: str,
    *,
    organisation_name: str,
    context: RequestContext | None = None,
) -> bool:
    """Sent the moment an invited member sets their own password.

    ## Why this is a separate email from the welcome below

    They answer different questions and one of them is time-critical. This is a **security
    notice**: an account was just activated, from this device, in this place. If that was not
    them, the device block is what tells them so — and it needs to arrive on its own rather
    than as a footnote under three paragraphs of prose about the product.

    The welcome email explains what SHELTER is. Nobody reads a security alert for that, and
    burying "was this you?" under a story is how a real compromise goes unnoticed.
    """
    subject = "Your SHELTER profile is now active"

    plain = (
        f"Hello {first_name},\n\n"
        f"You have set your password and your SHELTER profile is now active as part of "
        f"{organisation_name}.\n\n"
        "From now on, sign in with your email address and the password you just chose. "
        "Your invitation link no longer works — it was single-use, which is deliberate: "
        "a forwarded invitation cannot let anyone else in.\n\n"
        "If this was NOT you, change your password immediately and tell your "
        "organisation's administrator. The details below are the device and location the "
        "activation came from.\n\n"
        + (context.plain() if context else "")
        + "\nSHELTER — satellite-enabled & AI-powered early warning for flood, crop and "
        "health risk.\n"
    )

    html = layout.render(
        kind="security",
        eyebrow="Account security",
        title="Your profile is active",
        body_html=(
            f'<p style="margin:0 0 16px;">Hello {first_name}, you have set your password '
            f"and your SHELTER profile is now active as part of "
            f"<strong>{organisation_name}</strong>.</p>"
            '<p style="margin:0 0 16px;">Sign in from now on with your email address and the '
            "password you just chose. Your invitation link no longer works — it was "
            "single-use, so a forwarded invitation cannot let anyone else in.</p>"
            + layout.note(
                "If this was not you, change your password immediately and tell your "
                "organisation's administrator. The activation came from the device and "
                "location below."
            )
            + (context.block() if context else "")
        ),
    )
    return await _send(email, subject, plain, html)


async def send_team_welcome(
    email: str,
    first_name: str,
    *,
    organisation_name: str | None = None,
    workspace_count: int = 0,
) -> bool:
    """The story email, sent once for every account that becomes active.

    ## One welcome for every account type

    This was the *team member* welcome only, so an aggregator owner who signed up directly
    never received it and neither did an individual — the two account types most likely to be
    reading an advisory unaided. It is now sent from `verify_email`, the single point at which
    any account transitions to active, so every active account gets it exactly once.

    `organisation_name` is therefore **optional**: an individual farmer has no organisation,
    and the opening line adapts rather than printing "as part of None".

    ## Why the story is worth an email

    A colleague added to a workspace usually knows they were invited and not much else. They
    will be reading flood and crop advisories that someone acts on — and understanding *why*
    the system is cautious is what makes those advisories legible. "Severity is capped when
    confidence is low" reads as a bug until you know the service would rather under-claim than
    send a farmer to move grain that did not need moving.

    ## Deliberately separate from `send_profile_activated`

    That one is a security notice with a device block and needs to be read in the first minute.
    This one is prose. Merged, the security question — "was this activation me?" — would sit
    under three paragraphs about satellites, which is exactly how a real compromise goes
    unnoticed.

    **No device block here**, for the same reason: it carries no security claim, so a device
    table would train the reader to skim past the ones that matter.

    Every figure below is a property of the system as built — the four agents, Fahis's
    verdicts, the radar-through-cloud argument, the confidence floor. Nothing here is a
    forward-looking promise, because a welcome email is the worst place to create an
    expectation the pipeline cannot meet.
    """
    subject = "Welcome to SHELTER"
    where = (
        f" across {workspace_count} workspaces" if workspace_count > 1 else ""
    )

    # The opening adapts to the account type. An individual has no organisation, and
    # "as part of None" is the kind of detail that makes a welcome email read as automated.
    opening_plain = (
        f"Welcome to SHELTER, as part of {organisation_name}{where}."
        if organisation_name
        else "Welcome to SHELTER."
    )
    opening_html = (
        f"Welcome to SHELTER, as part of <strong>{organisation_name}</strong>{where}."
        if organisation_name
        else "Welcome to SHELTER."
    )

    plain = (
        f"Hello {first_name},\n\n"
        f"{opening_plain}\n\n"
        "WHY THIS EXISTS\n"
        "When a flood reaches a farm in the Niger Delta, or a dry spell starts to "
        "cost a rice season in Kebbi, the information that would have helped almost "
        "always existed. It sat in satellite archives nobody had turned into a "
        "sentence a farmer could act on. SHELTER closes that gap.\n\n"
        "HOW IT WORKS\n"
        "Four agents work in a line, without being asked:\n"
        "  * Scout watches for new satellite passes over the areas you monitor.\n"
        "  * Analyst measures what changed — standing water, crop stress, rainfall.\n"
        "  * Oracle decides whether that is worth warning about, and how urgently.\n"
        "  * Herald delivers the advisory on a channel the person already has.\n\n"
        "It uses radar as well as optical imagery, which matters more than it sounds: "
        "radar sees through cloud, so monitoring continues during the storm that "
        "blinds optical-only services — the exact hours when a flood warning is worth "
        "most.\n\n"
        "AND A FIFTH AGENT THAT ASKS WHETHER WE WERE RIGHT\n"
        "Days after an advisory goes out, Fahis searches independent reporting for the "
        "hazard we warned about and records a verdict. When nothing can be found — "
        "common for a remote local government area — that is recorded as unverified "
        "rather than counted as a false alarm. Accuracy figures nobody audits are not "
        "figures at all.\n\n"
        "WHAT WE WILL NOT DO\n"
        "We do not invent numbers. When rainfall data is unavailable the advisory says "
        "so instead of guessing, and when confidence is low the severity is capped "
        "rather than escalated. Sending someone to move grain that did not need moving "
        "costs them a day's work and costs us the next warning they would have "
        "believed.\n\n"
        "For 33 million people facing food insecurity in Nigeria, and 282 million "
        "undernourished across Africa, a warning that arrives in time is the "
        "difference between a hard season and a lost one.\n\n"
        "SHELTER — satellite-enabled & AI-powered early warning for flood, crop and "
        "health risk.\n"
        "A NIGCOMSAT x FreePass ZeroRate consortium initiative.\n"
    )

    def para(text: str) -> str:
        return f'<p style="margin:0 0 15px;line-height:1.62;">{text}</p>'

    def salutation(text: str) -> str:
        """The greeting, on its own line with tighter spacing beneath it.

        `margin-bottom` is smaller than `para`'s: a salutation belongs *with* the paragraph
        that follows it, and a full paragraph gap makes it read as an orphaned line rather
        than the opening of a letter.
        """
        return f'<p style="margin:0 0 9px;line-height:1.62;">{text}</p>'

    def heading(text: str) -> str:
        return (
            f'<p style="margin:22px 0 8px;font-size:11.5px;font-weight:700;'
            f'letter-spacing:0.07em;text-transform:uppercase;color:{_BRAND};">'
            f"{text}</p>"
        )

    html = layout.render(
        kind="welcome",
        eyebrow="Welcome",
        title="Welcome to SHELTER",
        body_html=(
            # Salutation on its own line, then the welcome.
            #
            # These were one run-on sentence — "Hello Lionel, welcome to SHELTER as part of
            # CreditChek Africa" — which reads as a form letter and buries the greeting. A
            # letter opens by addressing the reader and then says its piece; that is the
            # convention every other email in the inbox follows.
            salutation(f"Hello {first_name},")
            + para(opening_html)
            + heading("Why this exists")
            + para(
                "When a flood reaches a farm in the Niger Delta, or a dry spell starts to "
                "cost a rice season in Kebbi, the information that would have helped almost "
                "always existed — sitting in satellite archives nobody had turned into a "
                "sentence a farmer could act on. SHELTER closes that gap."
            )
            + heading("How it works")
            + para(
                "Four agents work in a line, without being asked: <strong>Scout</strong> "
                "watches for new satellite passes over your areas, <strong>Analyst</strong> "
                "measures what changed, <strong>Oracle</strong> decides whether it is worth "
                "warning about, and <strong>Herald</strong> delivers the advisory on a "
                "channel the person already has."
            )
            + para(
                "It uses radar as well as optical imagery, which matters more than it "
                "sounds: radar sees through cloud, so monitoring continues during the storm "
                "that blinds optical-only services — the exact hours when a flood warning is "
                "worth most."
            )
            + heading("And a fifth agent that asks whether we were right")
            + para(
                "Days later, <strong>Fahis</strong> searches independent reporting for the "
                "hazard we warned about and records a verdict. When nothing can be found — "
                "common for a remote local government area — that is recorded as "
                "<em>unverified</em> rather than counted as a false alarm. Accuracy figures "
                "nobody audits are not figures at all."
            )
            + heading("What we will not do")
            + para(
                "We do not invent numbers. When rainfall data is unavailable the advisory "
                "says so instead of guessing, and when confidence is low the severity is "
                "capped rather than escalated. Sending someone to move grain that did not "
                "need moving costs them a day's work — and costs us the next warning they "
                "would have believed."
            )
            + layout.note(
                "For 33 million people facing food insecurity in Nigeria, and 282 million "
                "undernourished across Africa, a warning that arrives in time is the "
                "difference between a hard season and a lost one."
            )
        ),
    )
    return await _send(email, subject, plain, html)


async def send_welcome(email: str, first_name: str, *, area_name: str | None = None) -> bool:
    """Sent once the plot is bound and autonomous monitoring is live.

    Its job is to set expectations: the system now watches without being asked, which
    is unusual enough that saying so prevents "is it working?" support traffic.
    """
    subject = "Your SHELTER monitoring is live"
    where = f" for {area_name}" if area_name else ""

    plain = (
        f"Hello {first_name},\n\n"
        f"Your SHELTER early-warning monitoring is now active{where}.\n\n"
        "What happens next, without you doing anything:\n"
        "  * Sentinel-1 radar checks your plot every few days — it sees through "
        "cloud and rain, so monitoring continues during the storm that blinds "
        "optical satellites.\n"
        "  * You get a 7-day outlook, and an alert if flood, waterlogging or crop "
        "stress risk rises.\n"
        "  * Every alert says what was measured and why, so you can judge it.\n\n"
        "You do not need to open an app or check a website. We contact you.\n\n"
        "Change your language, channels or plot any time in the portal:\n"
        f"{settings.public_site_url}\n\n"
        "SHELTER — satellite early warning for flood, crop and health risk.\n"
    )
    html = layout.render(
        kind="welcome",
        eyebrow="Monitoring active",
        title="Your monitoring is live",
        body_html=(
            f'<p style="margin:0 0 18px;">Hello {first_name}, SHELTER is now '
            f"watching{where}.</p>"
            '<p style="margin:0 0 10px;font-weight:600;color:#0b001b;">'
            "What happens next, without you doing anything</p>"
            '<ul style="margin:0 0 20px;padding-left:20px;color:#364153;">'
            "<li style=\"margin-bottom:8px;\">Sentinel-1 radar checks your plot every "
            "few days. It sees through cloud and rain, so monitoring continues during "
            "the storm that blinds optical satellites.</li>"
            "<li style=\"margin-bottom:8px;\">You get a 7-day outlook, and an alert if "
            "flood, waterlogging or crop-stress risk rises.</li>"
            "<li>Every alert says what was measured and why, so you can judge it.</li>"
            "</ul>"
            '<p style="margin:0 0 20px;">You do not need to open an app or check a '
            "website. We contact you.</p>"
            + layout.button(settings.public_site_url, "Open my dashboard")
            + layout.note(
                "Change your language, channels or plot any time in the portal."
            )
        ),
    )
    return await _send(email, subject, plain, html)



async def send_area_added(
    email: str,
    first_name: str,
    *,
    area_name: str,
    hectares: float | None = None,
    admin1: str | None = None,
    admin2: str | None = None,
    country: str | None = None,
    added_by: str | None = None,
) -> bool:
    """Confirm one plot is under monitoring, and say where we think it is.

    ## Why this exists

    Reported by an aggregator: a monitoring area was created for a customer and **nothing was
    sent to anyone**. The area was queued for scanning and audited, but no channel was contacted,
    so the only confirmation was the HTTP 201 the API returned to the integration — which the
    farmer never sees. The individual portal path had the same gap.

    ## Why it restates the location

    The body names the district and country, not just the plot name. That is the same reasoning as
    the picker's confirmation card, and the same incident: a farm described as being in Kobape,
    Ogun State was once activated at Warrington, England. An email that says "we are watching
    Alspecs Farms in Obafemi Owode, Ogun, NG" is checkable by the person who owns the land, and is
    the last chance to catch a wrong location before advisories start arriving about the wrong
    field.

    `added_by` names the aggregator when a partner created the plot on someone's behalf. A farmer
    who did not press the button themselves should be told who did — silent changes to what is
    being monitored on your land are not acceptable even when they are legitimate.

    Never raises: `_send` swallows provider failures. A missing confirmation must not fail an area
    that is already durable and already queued.
    """
    subject = f"Now monitoring {area_name}"

    where = ", ".join(part for part in (admin2, admin1, country) if part)
    size = f"{hectares:,.0f} hectares" if hectares else None
    on_behalf = f" at the request of {added_by}" if added_by else ""

    details_plain = "\n".join(
        f"  {label}: {value}"
        for label, value in (
            ("Plot", area_name),
            ("Where", where or "not identified"),
            ("Size", size or "not stated"),
        )
    )

    plain = (
        f"Hello {first_name},\n\n"
        f"{area_name} is now under satellite monitoring{on_behalf}.\n\n"
        f"{details_plain}\n\n"
        "WHAT HAPPENS NOW\n"
        "The first scan is already queued — you do not need to wait for a cycle to "
        "start. After that, Sentinel-1 radar checks this plot every few days. Radar "
        "sees through cloud and rain, so monitoring continues during the storm that "
        "blinds optical satellites.\n\n"
        "You will hear from us when flood, waterlogging or crop-stress risk rises — "
        "not otherwise. Every alert says what was measured and why.\n\n"
        "IF THE LOCATION ABOVE IS WRONG\n"
        "Tell us before the first alert arrives. An advisory about the wrong field is "
        "worse than no advisory, and it is much easier to correct now.\n"
    )

    def row(label: str, value: str) -> str:
        return (
            f'<tr><td style="padding:3px 14px 3px 0;font-size:12.5px;color:#6a7282;">'
            f"{label}</td>"
            f'<td style="padding:3px 0;font-size:13.5px;color:#0b001b;">{value}</td></tr>'
        )

    html = layout.render(
        kind="welcome",
        eyebrow="Monitoring active",
        title=f"Now monitoring {area_name}",
        body_html=(
            f'<p style="margin:0 0 9px;">Hello {first_name},</p>'
            f'<p style="margin:0 0 16px;"><strong>{area_name}</strong> is now under '
            f"satellite monitoring{on_behalf}.</p>"
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            'style="margin:0 0 20px;">'
            + row("Plot", area_name)
            + row("Where", where or "not identified")
            + row("Size", size or "not stated")
            + "</table>"
            '<p style="margin:0 0 10px;font-weight:600;color:#0b001b;">'
            "What happens now</p>"
            '<ul style="margin:0 0 20px;padding-left:20px;color:#364153;">'
            '<li style="margin-bottom:8px;">The first scan is already queued — no need '
            "to wait for a cycle to start.</li>"
            '<li style="margin-bottom:8px;">After that, Sentinel-1 radar checks this '
            "plot every few days, through cloud and rain.</li>"
            "<li>You hear from us when risk rises, and not otherwise.</li>"
            "</ul>"
            + layout.button(settings.public_site_url, "See this plot")
            + layout.note(
                "If the location above is not your land, tell us before the first alert "
                "arrives — an advisory about the wrong field is worse than none, and it is "
                "far easier to correct now."
            )
        ),
    )
    return await _send(email, subject, plain, html)


async def send_channels_changed(
    email: str,
    first_name: str,
    *,
    channels: list[dict],
    changed_by: str | None = None,
) -> bool:
    """Confirm where alerts will now be delivered.

    ## Why this is a security notice, not a preferences receipt

    Alert channels decide who learns that a farm is flooding. Changing them silently — especially
    when an aggregator does it on a customer's behalf — is the kind of change that must always be
    announced, for the same reason a password change is. `changed_by` names the party when it was
    not the subscriber themselves.

    Lists the CURRENT configuration rather than a diff. A diff is shorter and much harder to act
    on: what a subscriber needs to check is "will my alerts reach me", and that is answered by
    seeing the whole list, not by knowing what moved.
    """
    subject = "Where your SHELTER alerts will arrive"
    by = f" by {changed_by}" if changed_by else ""

    def describe(entry: dict) -> str:
        where = f" for {entry['area']}" if entry.get("area") else " for all your plots"
        # The score dial is stated when set. It changes what reaches you, so omitting it would
        # under-report a change the subscriber may not have made — the one thing this notice
        # exists to catch. Absent means the severity ladder governs alone, which needs no words.
        dial = (
            f", and only above risk {entry['min_score']:.2f}"
            if entry.get("min_score") is not None
            else ""
        )
        return (
            f"{entry['channel'].replace('_', ' ')} to {entry['address']}{where}, "
            f"from {entry['min_severity']} upwards{dial}"
        )

    lines = [f"  - {describe(c)}" for c in channels] or [
        "  - nothing configured, which means no alerts can reach you"
    ]

    plain = (
        f"Hello {first_name},\n\n"
        f"Your SHELTER alert delivery was changed{by}. Alerts will now reach you:\n\n"
        + "\n".join(lines)
        + "\n\n"
        "IF YOU DID NOT EXPECT THIS\n"
        "Sign in and change it back, or reply to this message. Alert channels decide who "
        "learns that your farm is at risk, so a change you did not make is worth "
        "checking immediately.\n"
    )

    html = layout.render(
        kind="security",
        eyebrow="Alert delivery",
        title="Where your alerts will arrive",
        body_html=(
            f'<p style="margin:0 0 9px;">Hello {first_name},</p>'
            f'<p style="margin:0 0 16px;">Your alert delivery was changed{by}. '
            f"Alerts will now reach you:</p>"
            '<ul style="margin:0 0 20px;padding-left:20px;color:#364153;">'
            + "".join(
                f'<li style="margin-bottom:8px;">{describe(c)}</li>' for c in channels
            )
            + "</ul>"
            + layout.button(settings.public_site_url, "Review my settings")
            + layout.note(
                "If you did not expect this, change it back or reply to this message. Alert "
                "channels decide who learns that your farm is at risk."
            )
        ),
    )
    return await _send(email, subject, plain, html)


class RequestContext:
    """Where a security-sensitive request came from, for the email that reports it.

    Passed rather than derived here because `mailer` has no access to the HTTP request —
    keeping it that way means the mail layer stays a pure function of its arguments and can
    be unit-tested without a request factory.

    Every field is optional. A missing one renders as an em dash in the email rather than
    omitting the row: "we do not know your browser" is information, an absent row looks like
    a broken template.
    """

    __slots__ = ("ip", "os_name", "browser", "location")

    def __init__(
        self,
        *,
        ip: str | None = None,
        os_name: str | None = None,
        browser: str | None = None,
        location: str | None = None,
    ) -> None:
        self.ip = ip
        self.os_name = os_name
        self.browser = browser
        self.location = location

    @classmethod
    def from_request(cls, request, location: str | None = None) -> RequestContext:
        """Build from a FastAPI `Request`, parsing the user-agent.

        `location` is passed in rather than looked up here so `mailer` does not import
        `geo` — the mail layer should not acquire a dependency on a 60MB optional database.
        """
        from app.iam import useragent

        ua = useragent.parse(request.headers.get("user-agent"))
        return cls(
            ip=request.client.host if request.client else None,
            os_name=ua.os,
            browser=ua.browser,
            location=location,
        )

    def block(self) -> str:
        """The rendered HTML table."""
        return layout.request_details(
            ip=self.ip,
            os_name=self.os_name,
            browser=self.browser,
            location=self.location,
        )

    def plain(self) -> str:
        """Plain-text equivalent, for the text/plain alternative.

        Not skipped: a recipient reading the text part is exactly the security-conscious
        user most likely to want these details, and many mail clients on feature phones show
        only the text alternative.
        """
        return (
            "\nWhere this request came from:\n"
            f"  IP address:         {self.ip or 'unknown'}\n"
            f"  Operating system:   {self.os_name or 'unknown'}\n"
            f"  Browser:            {self.browser or 'unknown'}\n"
            f"  Estimated location: {self.location or 'unknown'}\n"
            "\nLocation is estimated from the IP address and is often the mobile "
            "network's gateway rather than your exact town. If none of this looks like "
            "you, do not use the link above.\n"
        )


async def send_api_key_notice(
    email: str,
    first_name: str,
    key_name: str,
    hint: str,
    *,
    scopes: list[str] | None = None,
) -> bool:
    """Tell an aggregator a key was minted, and how to integrate with it.

    Two jobs in one message, and the ordering matters:

    **A security notification.** The key itself is *not* in this email — it was shown
    once, in the HTTP response — because email is stored, forwarded, backed up and
    searchable. Sending only the last-4 hint lets the recipient recognise a key they
    did *not* create, which is the whole point of the notice.

    **An onboarding aid.** The links below are the integration contract: the OpenAPI
    document and the Swagger UI, both ungated, so a partner can generate a client and
    read every endpoint without a further support round trip. Naming the granted scopes
    here is what makes a 403 later self-diagnosable — "my key lacks that scope" instead
    of "the API is broken".
    """
    base = settings.public_site_url.rstrip("/")
    api_base = settings.api_base_url.rstrip("/") if settings.api_base_url else base
    docs_url = f"{api_base}{settings.api_prefix}/docs"
    spec_url = f"{api_base}{settings.api_prefix}/openapi.json"

    granted = ", ".join(scopes) if scopes else "(see the portal)"

    subject = "Your SHELTER API key is ready — integration details"
    plain = (
        f"Hello {first_name},\n\n"
        f'An API key named "{key_name}" (ending {hint}) was created on your '
        "SHELTER account.\n\n"
        f"Granted scopes: {granted}\n\n"
        "The key itself is NOT in this email. It was shown once, at creation, and is "
        "stored only as a hash — it cannot be recovered by you, by us, or by anyone "
        "with our database. If you have lost it, rotate the key in the portal.\n\n"
        "--- Integrating ---\n\n"
        f"  Interactive API docs : {docs_url}\n"
        f"  OpenAPI 3 spec       : {spec_url}\n\n"
        "Both are public — no credential needed to read them, so you can generate a "
        "client before writing any code:\n\n"
        f"  openapi-generator generate -i {spec_url} -g python -o ./shelter-client\n\n"
        "Send the key as a header on every request:\n\n"
        "  X-SHELTER-API-Key: shltky…\n\n"
        "Your key is scoped to your own customers only. Requests for another "
        "organisation's subscribers return 404, not 403 — we do not confirm that "
        "another tenant's records exist.\n\n"
        "--- Security ---\n\n"
        "If you did not create this key, revoke it immediately in the portal and "
        "change your password. Keys can be rotated with a grace window, so you can "
        "deploy a replacement before the old one stops working.\n\n"
        "SHELTER — satellite early warning for flood, crop and health risk.\n"
    )
    html = layout.render(
        kind="api_key",
        eyebrow="Security notice",
        title="API key created",
        body_html=(
            f'<p style="margin:0 0 18px;">Hello {first_name}, an API key named '
            f"<strong>{key_name}</strong> (ending {hint}) was created on your SHELTER "
            f"account.</p>"
            f'<p style="margin:0 0 18px;font-size:13px;color:#6a7282;">'
            f"Granted scopes: {granted}</p>"
            '<p style="margin:0 0 20px;">The key itself is <strong>not</strong> in this '
            "email. It was shown once, at creation, and is stored only as a hash — it "
            "cannot be recovered by you, by us, or by anyone with our database. If you "
            "have lost it, rotate the key in the portal.</p>"
            + layout.button(docs_url, "Read the API reference")
            + layout.note(
                "If you did not create this key, revoke it immediately in the portal "
                "and change your password."
            )
        ),
    )
    return await _send(email, subject, plain, html)


# --------------------------------------------------------------------------- #
# Transport selection
#
# Two ways to reach the same Brevo account. They fail differently, which is the
# whole reason both exist:
#
#   brevo_api  Authenticates with the API key alone. Unaffected by Brevo's
#              sender-IP allow-list, and returns a messageId we can audit against.
#   smtp       Needs a separate SMTP key AND this host's egress IP on Brevo's
#              permit list. Fragile on a laptop, a CI runner, or a VPS whose
#              address changes — verified: the relay returned
#              `535 Authentication failed` where the API returned 201.
#
# Resolution mirrors `ADVISORY_PROVIDER` in the LLM layer, including the rule that
# a *forced* provider which is not configured degrades to noop rather than silently
# routing elsewhere. An explicit choice quietly ignored is worse than mail not
# being sent, because the operator believes it went.
# --------------------------------------------------------------------------- #


def resolve_provider() -> str:
    """Which transport will actually be used: "brevo_api" | "smtp" | "noop".

    Reported by `/health`, so a deployment that thinks it configured email but
    resolves to noop can see that on a calm day rather than discovering it when a
    subscriber never receives a verification link.
    """
    configured = (settings.notification_provider or "auto").strip().lower()

    if configured == "brevo_api":
        return "brevo_api" if _brevo_ready() else "noop"
    if configured == "smtp":
        return "smtp" if settings.smtp_host else "noop"
    if configured == "noop":
        return "noop"

    # auto: prefer the API. It needs one credential instead of two, and no IP
    # allow-listing, so it is the path most likely to work on an unknown host.
    if _brevo_ready():
        return "brevo_api"
    if settings.smtp_host:
        return "smtp"
    return "noop"


def _brevo_ready() -> bool:
    """The API path needs a key and a verified sender.

    The sender falls back to `SMTP_FROM` so a deployment that only set the SMTP
    identity still works — but a missing key is fatal to this path, because Brevo
    rejects an unauthenticated POST outright.
    """
    return bool(settings.brevo_api_key and (settings.brevo_sender_email or settings.smtp_from))


def available() -> bool:
    """Whether onboarding email can be sent at all."""
    return resolve_provider() != "noop"


async def _send(to: str, subject: str, plain: str, html: str | None) -> bool:
    """Send one message over the resolved transport. Never raises.

    Returns False on any failure. Callers treat that as "the link was not sent" and
    continue — a signup must not be lost because mail was briefly unavailable, since
    the account is already durable and the link can be re-sent.
    """
    provider = resolve_provider()

    if provider == "noop":
        log.warning(
            "no email transport configured; onboarding email not sent",
            extra={"to_domain": to.split("@")[-1], "subject": subject,
                   "configured": settings.notification_provider},
        )
        return False

    if provider == "brevo_api":
        sent = await _send_brevo_api(to, subject, plain, html)
        if sent:
            return True
        # Fall through to SMTP only under `auto`. A *forced* brevo_api must not
        # silently use a different transport — the operator chose one deliberately,
        # and a quiet substitution hides that their choice is broken.
        if (settings.notification_provider or "auto").lower() == "auto" and settings.smtp_host:
            log.warning("Brevo API send failed; falling back to SMTP")
            return await _send_smtp(to, subject, plain, html)
        return False

    return await _send_smtp(to, subject, plain, html)


async def _send_brevo_api(to: str, subject: str, plain: str, html: str | None) -> bool:
    """POST to Brevo's transactional endpoint.

    Uses `httpx`, which the project already depends on — no vendor SDK, for the same
    reason `app/llm/` speaks plain HTTP: one less dependency whose release cadence we
    do not control, and a payload we can read.
    """
    sender_email = settings.brevo_sender_email or settings.smtp_from
    payload: dict = {
        "sender": {"name": settings.brevo_sender_name, "email": sender_email},
        "to": [{"email": to}],
        "subject": subject,
        "textContent": plain,
        # Tagged so transactional onboarding mail is filterable from alert traffic in
        # Brevo's dashboard, which matters when diagnosing a delivery complaint.
        "tags": [settings.brevo_tag],
    }
    if html:
        payload["htmlContent"] = html
    if settings.brevo_reply_to_email or settings.smtp_reply_to:
        payload["replyTo"] = {"email": settings.brevo_reply_to_email or settings.smtp_reply_to}

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
        log.warning(
            "Brevo API request failed",
            extra={"to_domain": to.split("@")[-1], "error": f"{type(exc).__name__}: {exc}"},
        )
        return False

    if response.status_code in (200, 201, 202):
        message_id = None
        try:
            message_id = response.json().get("messageId")
        except Exception:
            pass    # a 2xx with an unparseable body is still a successful send
        log.info(
            "onboarding email sent",
            extra={"to_domain": to.split("@")[-1], "subject": subject,
                   "provider": "brevo_api", "message_id": message_id},
        )
        return True

    # Brevo returns a JSON body naming the problem — an unverified sender, an
    # exhausted credit balance, a malformed address. Surfacing it turns "email does
    # not work" into one actionable line. Truncated so an HTML error page cannot
    # flood the log.
    detail = (response.text or "")[:300].replace("\n", " ")
    log.warning(
        "Brevo API rejected the message",
        extra={"to_domain": to.split("@")[-1], "status": response.status_code,
               "detail": detail},
    )
    return False


async def _send_smtp(to: str, subject: str, plain: str, html: str | None) -> bool:
    """Send over the SMTP relay.

    Requires the sending host's egress IP to be on Brevo's permit list as well as a
    valid SMTP key — which is why the API path is preferred under `auto`.
    """
    if not settings.smtp_host:
        return False

    message = EmailMessage()
    message["Subject"] = subject
    # A named sender. Brevo shows it beside the address, and an unnamed sender is
    # markedly more likely to be filed as spam.
    message["From"] = formataddr((settings.smtp_from_name, settings.smtp_from))
    message["To"] = to
    if settings.smtp_reply_to:
        # Brevo's relay address is not a mailbox, so without this a reply bounces.
        message["Reply-To"] = settings.smtp_reply_to
    message["Message-ID"] = make_msgid(domain=settings.smtp_from.split("@")[-1])
    # Transactional, not marketing. Some providers deprioritise bulk-looking mail.
    message["X-Entity-Ref-ID"] = message["Message-ID"]

    message.set_content(plain)
    if html:
        message.add_alternative(html, subtype="html")

    try:
        await asyncio.to_thread(_send_sync, message)
        log.info(
            "onboarding email sent",
            extra={"to_domain": to.split("@")[-1], "subject": subject, "provider": "smtp"},
        )
        return True
    except Exception as exc:
        # The address is never logged in full — it is subscriber PII, and a log
        # aggregator is a wider audience than the mail server.
        log.warning(
            "SMTP send failed",
            extra={"to_domain": to.split("@")[-1], "error": f"{type(exc).__name__}: {exc}"},
        )
        return False


def _send_sync(message: EmailMessage) -> None:
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
        if settings.smtp_use_tls:
            server.starttls()
        if settings.smtp_username and settings.smtp_password:
            server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(message)


async def send_magic_link(
    email: str, first_name: str, url: str, context: RequestContext | None = None
) -> bool:
    """The passwordless sign-in link.

    Deliberately spare. **This email is a credential**, so the only thing it should
    encourage is opening the link — no newsletter framing, no unrelated navigation, and an
    explicit "ignore it if you did not ask" so an unexpected link is a non-event rather
    than an alarm.

    The expiry and single-use property are stated because they change how a recipient
    treats the message: a link they know dies in 15 minutes is one they will not forward.
    """
    subject = "Your SHELTER sign-in link"
    plain = (
        f"Hello {first_name},\n\n"
        "Use this link to sign in to SHELTER:\n\n"
        f"{url}\n\n"
        "It works once and expires in 15 minutes.\n\n"
        "If you did not request it, ignore this message — nobody can sign in without "
        "opening the link, and it expires on its own.\n"
        + (context.plain() if context else "")
        + "\nSHELTER — satellite-enabled & AI-powered early warning for flood, crop and "
        "health risk.\n"
    )
    html = layout.render(
        kind="sign_in",
        eyebrow="Sign in",
        title="Your sign-in link",
        body_html=(
            f'<p style="margin:0 0 20px;">Hello {first_name}, tap below to sign in.</p>'
            + layout.button(url, "Sign in to SHELTER")
            # The device block sits immediately after the button, before the small print:
            # a recipient deciding whether to trust the link needs it before they act, not
            # after.
            + (context.block() if context else "")
            + '<p style="margin:0 0 6px;font-size:13px;color:#6a7282;">'
            "Or paste this into your browser:</p>"
            f'<p style="margin:0 0 22px;font-size:12px;color:#99a1af;'
            f'word-break:break-all;">{url}</p>'
            + layout.note(
                "Works once · expires in 15 minutes. If you did not request it, ignore "
                "this email — nobody can sign in without opening the link."
            )
        ),
    )
    return await _send(email, subject, plain, html)


async def send_password_reset(
    email: str, first_name: str, url: str, context: RequestContext | None = None
) -> bool:
    """Password-reset link.

    States that the existing password keeps working until the reset completes. Without
    that line, someone who requested this by mistake assumes they are locked out and
    requests another — which is the most common cause of duplicate reset traffic.
    """
    subject = "Reset your SHELTER password"
    plain = (
        f"Hello {first_name},\n\n"
        "Use this link to choose a new SHELTER password:\n\n"
        f"{url}\n\n"
        "It works once and expires in 1 hour. Your current password keeps working "
        "until you complete the reset.\n\n"
        "If you did not request this, ignore the message — nothing changes unless the "
        "link is opened.\n\n"
        + (context.plain() if context else "")
        + "\nSHELTER — satellite-enabled & AI-powered early warning for flood, crop and "
        "health risk.\n"
    )
    html = layout.render(
        kind="reset",
        eyebrow="Account security",
        title="Reset your password",
        body_html=(
            f'<p style="margin:0 0 20px;">Hello {first_name}, choose a new password '
            f"below.</p>"
            + layout.button(url, "Set a new password")
            # Before the small print: someone who did not request a reset needs to see
            # the origin at the moment they are deciding whether to worry.
            + (context.block() if context else "")
            + '<p style="margin:0 0 6px;font-size:13px;color:#6a7282;">'
            "Or paste this into your browser:</p>"
            f'<p style="margin:0 0 22px;font-size:12px;color:#99a1af;'
            f'word-break:break-all;">{url}</p>'
            + layout.note(
                "Works once · expires in 1 hour. Your current password keeps working "
                "until you finish, so if you requested this by mistake you are not "
                "locked out — just ignore it."
            )
        ),
    )
    return await _send(email, subject, plain, html)


async def send_password_change_code(
    email: str,
    first_name: str,
    code: str,
    context: RequestContext | None = None,
) -> bool:
    """The 6-character code confirming a password change requested from inside a session.

    ## Why the code is displayed rather than linked

    A link would sign the reader in, which is the wrong shape here: they are *already* signed in,
    and the thing being proved is control of the mailbox, not identity. A code they carry back to
    the open tab proves exactly that and nothing more — and it cannot be forwarded into a session
    somebody else controls.

    ## Why the device block is not optional on this message

    This is a **security notice** as much as a code delivery. If the reader did not just ask to
    change their password, the device and location are what tell them somebody with their session is
    trying to — and that is the moment the information is worth anything. The same reasoning as
    `send_profile_activated`: a security claim without its origin is unactionable.

    States plainly that the current password still works, because someone who requested this by
    mistake otherwise assumes they are mid-change and locked out.
    """
    subject = "Your SHELTER password change code"
    plain = (
        f"Hello {first_name},\n\n"
        "Use this code to confirm your new SHELTER password:\n\n"
        f"    {code}\n\n"
        f"It expires in {pwless.PASSWORD_CODE_TTL_MINUTES} minutes and works once. Your "
        "current password keeps working until you finish, so you are not locked out.\n\n"
        "If you did NOT request this, do not enter the code. Someone may have access to "
        "your signed-in session — change your password from a device you trust and review "
        "your activity log.\n\n"
        + (context.plain() if context else "")
        + "\nSHELTER — satellite-enabled & AI-powered early warning for flood, crop and "
        "health risk.\n"
    )
    html = layout.render(
        kind="reset",
        eyebrow="Account security",
        title="Confirm your password change",
        body_html=(
            f'<p style="margin:0 0 18px;">Hello {first_name}, enter this code in the tab '
            f"you left open to finish setting your new password.</p>"
            # Monospaced, widely letter-spaced and large: this is read off one screen and typed
            # into another, often on a phone, and the whole job of the layout here is to make
            # each character unambiguous at a glance.
            '<p style="margin:0 0 20px;padding:16px 12px;border-radius:10px;'
            'background:#f5f3ff;border:1px solid #ddd6fe;text-align:center;'
            'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:30px;'
            'font-weight:700;letter-spacing:0.34em;color:#4c1d95;">'
            f"{code}</p>"
            # Before the small print: someone who did not request this needs the origin at the
            # moment they are deciding whether to worry.
            + (context.block() if context else "")
            + layout.note(
                f"Works once · expires in {pwless.PASSWORD_CODE_TTL_MINUTES} minutes. Your "
                "current password keeps working until you finish. If you did not request "
                "this, do not enter the code — someone may be using your signed-in session."
            )
        ),
    )
    return await _send(email, subject, plain, html)
