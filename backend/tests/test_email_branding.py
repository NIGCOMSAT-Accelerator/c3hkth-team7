"""The email chrome — consortium attribution, and the constraints email HTML imposes.

Reported from an aggregator activation email: the footer read as "Powered by NIGCOMSAT" with no
FreePass logo visible. The logo *was* there — the order was `NIGCOMSAT | <FreePass svg>`, and since
NIGCOMSAT is text sitting directly under the "Powered by" label while the vector trailed it, the eye
read the partner as the provider and the FreePass mark as an afterthought.

SHELTER is a FreePass product; NIGCOMSAT is the satellite and broadcast partner. The order carries
that, so it is worth a test rather than a comment.

These assert against the rendered HTML of **every** sender, not the layout function alone: the bug
was reported on one email and the fix has to reach all nine, which is only true while they all go
through `email_layout.render`.
"""

from __future__ import annotations

import inspect
import pathlib

from app.email import layout
from app.iam import mailer

#: How the FreePass wordmark is identifiable in the output.
#:
#: A base64 PNG data URI, NOT inline SVG: Outlook's sanitiser strips `<svg>` from incoming mail,
#: so the logo rendered as nothing at all in New Outlook for Windows. Matched on the data-URI
#: prefix rather than on the payload, which changes on any re-export.
FREEPASS_IMG = "data:image/png;base64,"
NIGCOMSAT_TEXT = 'alt="NIGCOMSAT"'


def _rendered_footer() -> str:
    return layout._footer()


def _without_comments(html: str) -> str:
    """HTML with `<!-- ... -->` removed.

    The footer carries a comment explaining why inline `<svg>` was abandoned, so a naive
    substring check for "<svg" matches the explanation rather than any markup.
    """
    import re

    return re.sub(r"<!--.*?-->", "", html, flags=re.S)


def test_the_freepass_logo_comes_before_nigcomsat():
    """**The reported bug.** FreePass leads the attribution; NIGCOMSAT follows.

    Reversing these is a two-line edit and produces an email that reads as though NIGCOMSAT
    provides the service, so the order is pinned rather than left to review.
    """
    footer = _rendered_footer()
    svg_at = footer.find(FREEPASS_IMG)
    nigcomsat_at = footer.find(NIGCOMSAT_TEXT)

    assert svg_at >= 0, "the FreePass wordmark is missing from the footer entirely"
    assert nigcomsat_at >= 0, "the NIGCOMSAT attribution is missing from the footer"
    assert svg_at < nigcomsat_at, (
        "NIGCOMSAT appears before the FreePass logo, so the footer reads as "
        "'Powered by NIGCOMSAT' — the exact defect reported from an activation email"
    )


def test_both_partners_are_separated_by_a_divider():
    """`FreePass | NIGCOMSAT` — the pipe is what makes it read as a consortium.

    Without it the two run together as one wordmark. Encoded as `&#124;` rather than a literal `|`
    because a bare pipe in some clients' quoted-printable handling has been known to break the row.
    """
    footer = _rendered_footer()
    svg_at = footer.find(FREEPASS_IMG)
    nigcomsat_at = footer.find(NIGCOMSAT_TEXT)
    between = footer[svg_at:nigcomsat_at]

    assert "&#124;" in between, "no divider between the FreePass logo and NIGCOMSAT"


def test_the_freepass_mark_is_embedded_not_fetched():
    """A remote logo is invisible on first open, which is when trust is decided.

    Most clients block external images until the recipient clicks "show images". An `<img src="https://…">`
    here would render as a broken-image placeholder on exactly the email where the reader is asking
    "is this legitimate?".

    So the bytes travel *inside* the message as a `data:` URI. That was also true of the inline SVG
    it replaced — the reason for the change is that Outlook strips `<svg>`, not that SVG was fetched.
    """
    footer = _without_comments(_rendered_footer())

    assert "data:image/png;base64," in footer, "the wordmark is not embedded"
    assert "<svg" not in footer, (
        "the footer contains inline SVG; Outlook's sanitiser strips it and the logo vanishes"
    )
    assert 'src="http' not in footer, (
        "the footer fetches a remote image; it will be blocked on first open"
    )


def test_every_sender_goes_through_the_shared_layout():
    """The footer fix must reach all nine emails, not just the one that was reported.

    A sender that hand-rolls its own HTML would keep whatever footer it was written with — which is
    the drift `email_layout` exists to end, and is how the verification email once had a branded
    header while the welcome email had none.
    """
    senders = sorted(n for n in dir(mailer) if n.startswith("send_"))
    assert len(senders) >= 9, f"expected at least 9 senders, found {len(senders)}"

    hand_rolled = [
        name
        for name in senders
        if "layout.render(" not in inspect.getsource(getattr(mailer, name))
    ]
    assert not hand_rolled, (
        f"these senders build their own HTML and will not pick up chrome changes: {hand_rolled}"
    )


def test_no_module_in_the_platform_builds_its_own_email_document():
    """**The sweep that caught the advisory email**, and the one that catches the next sender.

    The test above only looks at `iam/mailer.py`, which is where every *account* email lives. That is
    where the drift was fixed, and it is not where the drift remained: `dispatch/email_channel.py`
    sent the hazard advisories — the actual product — and built its own `<!doctype html>`. Eleven
    kinds of account mail shared a header, a footer and one set of tokens; the flood warning had
    none of them.

    That is worse than ordinary inconsistency. The one email a subscriber receives *because the
    service is working* was the only one that did not look like the service, and a farmer deciding
    whether to act on a warning starts by deciding whether it is genuine.

    So this sweeps the whole tree for a hand-built document rather than one module's senders, on the
    same reasoning as `test_tenancy.py`: fixing these one at a time is how the second one survives
    while the first is being patched.
    """
    root = pathlib.Path(inspect.getfile(mailer)).resolve().parents[1]

    #: Allowed to contain a document declaration, each for a stated reason.
    permitted = {
        # The layout IS the document.
        "email/layout.py",
        # Its module docstring quotes the markup it replaced.
        "email/__init__.py",
        # A browsable HTML reference page served over HTTP, not an email.
        "api/routes/devdocs.py",
    }

    offenders = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative in permitted:
            continue
        if "<!doctype" in path.read_text().lower():
            offenders.append(relative)

    assert not offenders, (
        f"these modules build their own email document and will not pick up chrome changes: "
        f"{offenders}. Supply a body to `app.email.layout.render` instead — it owns the SHELTER "
        f"mark, the FreePass/NIGCOMSAT footer, the preheader and the dark-mode rule."
    )


def test_the_shared_layout_lives_outside_the_identity_subsystem():
    """`app/email/`, not `app/iam/`.

    It was under `app/iam/`, which was the wrong home the moment advisory delivery needed it:
    importing the chrome from the identity package would make hazard delivery structurally depend on
    IAM, and would invite the next reader to conclude that `app/dispatch` may reach into `app.iam`
    generally.

    Asserted in both directions — the layout must also not reach back into either consumer, or it
    stops being a pure string builder and starts being a second place templates can live.
    """
    root = pathlib.Path(inspect.getfile(mailer)).resolve().parents[1]

    for name in ("layout.py", "assets.py", "__init__.py"):
        source = (root / "email" / name).read_text()
        for forbidden in ("from app.iam", "import app.iam", "from app.dispatch", "import app.dispatch"):
            assert forbidden not in source, (
                f"app/email/{name} imports {forbidden!r}; the chrome must not depend on the "
                f"subsystems that send through it"
            )


def test_the_footer_carries_the_freepass_copyright_line():
    """Attribution appears twice on purpose: the logo row, and the legal line beneath it.

    The logo row is brand; the copyright line is the operating entity. Losing the second would make
    the email read as co-branded with no owner.
    """
    footer = _rendered_footer()

    assert "FreePass Holding Co" in footer
    assert "&copy;" in footer


def test_the_logo_fill_is_legible_in_both_light_and_dark():
    """**Reported: "the FreePass logo isn't clear".**

    It was drawn in `MUTED` (`#6a7282`), a mid-slate that measures 4.41:1 on the light canvas and
    3.60:1 against a dark client background — under the 4.5:1 readable threshold in BOTH
    directions, so washed out either way.

    There is no single grey that fixes it: `#4a5565` measures 6.89:1 on canvas but 2.30:1 on
    `#1a1a1a`. So there are two values, each correct for its own background, and this asserts
    both clear 4.5:1 rather than pinning the hex — a future re-tone is fine as long as it stays
    legible.
    """

    def luminance(hex_colour: str) -> float:
        hex_colour = hex_colour.lstrip("#")
        channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    def contrast(a: str, b: str) -> float:
        la, lb = luminance(a), luminance(b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

    assert contrast(layout.LOGO_INK, "#ffffff") >= 4.5, "logo unreadable on a white card"
    assert contrast(layout.LOGO_INK, layout.CANVAS) >= 4.5, "logo unreadable on the canvas"
    assert contrast(layout.LOGO_INK_DARK, "#1a1a1a") >= 4.5, "logo unreadable on dark grey"
    assert contrast(layout.LOGO_INK_DARK, "#000000") >= 4.5, "logo unreadable on black"

    # And the old single-value approach must not creep back.
    assert layout.LOGO_INK != layout.MUTED, (
        "the logo is back on MUTED, which measures under 4.5:1 on every background"
    )


def test_the_dark_mode_rule_swaps_the_logo_and_nothing_else():
    """The one permitted `<style>` block, and it must stay minimal.

    A media query cannot be inlined, so this is the only way to adapt at all. But Gmail strips
    `<style>` in several contexts, so it must be an *enhancement*: the DARK-INK asset is shown
    unconditionally by inline style, and the rule only swaps in the light-ink one on a dark
    background. A client that drops the block still shows a correctly-coloured logo.
    """
    rendered = layout.render(kind="welcome", eyebrow="E", title="T", body_html="<p>b</p>")

    assert "prefers-color-scheme: dark" in rendered
    assert ".fp-logo-light" in rendered and ".fp-logo-dark" in rendered, (
        "the dark-mode rule does not target both logo variants"
    )
    # Both assets must be present, since the swap is display-only.
    assert rendered.count("data:image/png;base64,") == 3, (
        "expected both FreePass inks plus the NIGCOMSAT emblem to be embedded"
    )
    # The default-visible one must be the dark ink, for the white card.
    light_at = rendered.find('class="fp-logo-light"')
    dark_at = rendered.find('class="fp-logo-dark"')
    assert 0 <= light_at < dark_at, (
        "the dark-ink asset must come first and be the unconditional one, so a client that "
        "strips <style> still renders a legible logo"
    )


def test_the_layout_uses_tables_and_inline_styles_only():
    """Outlook's Word engine ignores flex and most div layout; Gmail strips `<style>` blocks.

    Asserted because "tidying" this into semantic HTML is a natural instinct that silently breaks
    the two clients most Nigerian institutional recipients actually use.
    """
    # The RENDERED output, not the source: the module docstring legitimately names `display: flex`
    # and `<style>` while explaining why neither may be used.
    rendered = layout.render(
        kind="verify",
        eyebrow="Test",
        title="Test",
        body_html="<p>body</p>",
    )

    assert 'role="presentation"' in rendered, (
        "layout tables must be marked presentational, or screen readers announce them as data"
    )
    assert "display: flex" not in rendered, "flex layout does not render in Outlook"

    # Exactly ONE `<style>` block is permitted, and only for the dark-mode logo rule — a media
    # query has no inline form, so it is that or no dark-mode handling at all. Everything else
    # must stay inline because Gmail strips style blocks in several contexts.
    assert _without_comments(rendered).count("<style>") == 1, (
        "more than one style block, or none: the single permitted block is the dark-mode logo "
        "rule (see _DARK_MODE_CSS); every other rule must be inline for Gmail"
    )
    body_after_head = _without_comments(rendered).split("</head>")[-1]
    assert "<style" not in body_after_head, "style block in the body; Gmail will strip it"


# --------------------------------------------------------------------------- #
# The welcome email reaches every active account
#
# It was the TEAM-MEMBER welcome only, so an aggregator owner who signed up directly never
# received it and neither did an individual farmer — the two readers most likely to be acting on
# an advisory unaided, and therefore most in need of knowing why severity is capped when
# confidence is low.
# --------------------------------------------------------------------------- #


def test_the_welcome_is_sent_from_the_single_activation_point():
    """`verify_email` is the one transition to active for every account type.

    Sending from there rather than from each signup path is what makes "every active account gets
    this" true by construction instead of by remembering to add a call to the next path.
    """
    from app.api.routes import iam as iam_routes

    source = inspect.getsource(iam_routes.verify_email)

    assert "send_team_welcome" in source, (
        "verify_email does not send the welcome, so accounts that activate by link never get it"
    )
    assert "background.add_task" in source, (
        "the welcome must be backgrounded — a slow mail provider must not fail a verification "
        "that is already committed"
    )


def test_the_welcome_survives_an_account_with_no_organisation():
    """An individual has no organisation, and the email must not print "as part of None"."""
    signature = inspect.signature(mailer.send_team_welcome)
    organisation = signature.parameters["organisation_name"]

    assert organisation.default is None, (
        "organisation_name is still required, so an individual activation cannot send this email"
    )


def test_the_salutation_is_its_own_paragraph():
    """**Reported:** the greeting and the welcome sentence ran together on one line.

    "Hello Lionel, welcome to SHELTER as part of CreditChek Africa" reads as a form letter. A
    letter addresses the reader, then says its piece.
    """
    source = inspect.getsource(mailer.send_team_welcome)

    assert "salutation(" in source, "no separate salutation element"
    assert "Hello {first_name}, welcome" not in source, (
        "the greeting and the welcome sentence are back on one line"
    )
