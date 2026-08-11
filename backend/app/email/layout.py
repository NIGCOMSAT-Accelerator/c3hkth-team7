"""Shared email chrome — one header, one footer, per-type iconography.

## Why a layout module rather than HTML in each sender

Every template previously carried its own `<html>`, its own header colours and its own
footer. Three consequences, all of which had already started:

  * **Drift.** The verification email had a branded header; the welcome and API-key
    emails were plain text with no header at all. A subscriber seeing both would not read
    them as the same product.
  * **The footer said different things.** Or nothing.
  * **Fixing one thing meant editing five places**, so nobody did.

`render(...)` now owns the chrome and each sender supplies only its body. The design
tokens match `frontend/app/globals.css` (`brand-600 #6a0dad`, `ink #0b001b`,
`hairline #3407561a`), so an email and the portal read as one system.

## Constraints email HTML imposes that web HTML does not

These are not stylistic choices — Outlook, Gmail and Apple Mail each strip different
things:

  * **Tables for layout.** Outlook's Word rendering engine ignores `display: flex` and
    most of `div`-based layout. Nested tables are the only reliable structure.
  * **Inline styles only.** Gmail strips `<style>` blocks in many contexts, so a class
    is not dependable. Everything below is inlined.
  * **No external CSS, no webfonts.** A system font stack, always.
  * **Embedded images, never remote ones.** Most clients block remote images until the
    recipient clicks "show images", so a logo referenced by URL is invisible on first
    read — exactly when the recipient is deciding whether the mail is legitimate.
  * **Inline SVG is not safe either.** The header mark and the per-type icons are inline
    `<svg>` and render in Apple Mail and Gmail — but **Outlook's sanitiser strips `<svg>`
    from incoming mail entirely**, so they are decoration only and nothing depends on
    them. The footer's FreePass wordmark learned this the hard way: it was inline SVG and
    showed as *nothing at all* in New Outlook. It is now a base64 PNG (`email_assets.py`),
    which keeps the no-fetch property and survives the sanitiser.
"""

from __future__ import annotations

from app.email.assets import (
    FREEPASS_HEIGHT,
    FREEPASS_PNG_DARK_INK,
    FREEPASS_PNG_LIGHT_INK,
    FREEPASS_WIDTH,
    NIGCOMSAT_HEIGHT,
    NIGCOMSAT_PNG,
    NIGCOMSAT_WIDTH,
)

# Design tokens, mirrored from the frontend's globals.css. Duplicated deliberately: an
# email cannot import CSS, and a build step to sync them would be more machinery than a
# five-value list justifies. The comment is the contract.
BRAND = "#6a0dad"
BRAND_LIGHT = "#9a2ce9"
INK = "#0b001b"
TEXT = "#364153"
MUTED = "#6a7282"
FAINT = "#99a1af"
HAIRLINE = "#e6ddf5"
CANVAS = "#f8f2ff"

#: Fill for the FreePass wordmark in the footer.
#:
#: **Not `MUTED`.** The logo was drawn in `#6a7282`, a mid-slate chosen to be inoffensive on
#: both light and dark — and measured, it is readable on neither: 4.41:1 on the light canvas
#: and **3.60:1** against a dark client background, under the 4.5:1 threshold in both
#: directions. Reported as "the FreePass logo isn't clear".
#:
#: There is no single grey that fixes this. Darkening it helps light mode and destroys dark
#: mode — `#4a5565` measures 6.89:1 on canvas but 2.30:1 on `#1a1a1a`. So the fill is now
#: near-ink for a strong light-mode reading, and `_DARK_MODE_CSS` lifts it to near-white
#: where the client reports a dark scheme. Two values, each correct for its own background,
#: rather than one compromise that is wrong for both.
LOGO_INK = "#2b3444"
LOGO_INK_DARK = "#e8e6ef"

#: Per-type header icon.
#:
#: A distinct glyph per notification kind, so a recipient recognises *what* an email is
#: before reading a word — which matters most for the security notices, where "did I do
#: this?" is the whole question. Drawn as inline SVG paths at 22px: legible in a header
#: band without competing with the wordmark.
_ICONS: dict[str, str] = {
    # Envelope with a check: confirm your address.
    "verify": (
        '<path d="M3 6h18v12H3z" fill="none" stroke="#fff" stroke-width="1.7" '
        'stroke-linejoin="round"/>'
        '<path d="M3 7l9 6 9-6" fill="none" stroke="#fff" stroke-width="1.7" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
    ),
    # Satellite over a monitored plot: monitoring is now live.
    "welcome": (
        '<path d="M4 10a8 8 0 0 1 16 0" fill="none" stroke="#fff" stroke-width="1.7" '
        'stroke-linecap="round" opacity="0.55"/>'
        '<path d="M12 12 6 17v4h3v-3h6v3h3v-4z" fill="#fff"/>'
        '<circle cx="12" cy="18" r="1.6" fill="'"" + BRAND + ""'"/>'
    ),
    # Key: an API credential was created.
    "api_key": (
        '<circle cx="8" cy="12" r="4" fill="none" stroke="#fff" stroke-width="1.7"/>'
        '<path d="M12 12h9M18 12v4M15 12v3" fill="none" stroke="#fff" '
        'stroke-width="1.7" stroke-linecap="round"/>'
    ),
    # Padlock: a sign-in link.
    "sign_in": (
        '<path d="M6 11h12v9H6z" fill="none" stroke="#fff" stroke-width="1.7" '
        'stroke-linejoin="round"/>'
        '<path d="M9 11V8a3 3 0 0 1 6 0v3" fill="none" stroke="#fff" '
        'stroke-width="1.7" stroke-linecap="round"/>'
    ),
    # Arrow circling back: a password reset.
    "reset": (
        '<path d="M20 12a8 8 0 1 1-2.6-5.9" fill="none" stroke="#fff" '
        'stroke-width="1.7" stroke-linecap="round"/>'
        '<path d="M20 3v5h-5" fill="none" stroke="#fff" stroke-width="1.7" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
    ),
    # Two figures: a colleague joining a team.
    "invite": (
        '<circle cx="9" cy="8.5" r="3.2" fill="none" stroke="#fff" stroke-width="1.7"/>'
        '<path d="M3 20a6 6 0 0 1 12 0" fill="none" stroke="#fff" stroke-width="1.7" '
        'stroke-linecap="round"/>'
        '<path d="M17 5.8a3.2 3.2 0 0 1 0 5.4M20.5 20a6 6 0 0 0-2.2-4.4" fill="none" '
        'stroke="#fff" stroke-width="1.7" stroke-linecap="round" opacity="0.75"/>'
    ),
    # Shield with a check: an account security event.
    "security": (
        '<path d="M12 3 5 5.6v5.6c0 4.6 7 8.8 7 8.8s7-4.2 7-8.8V5.6z" fill="none" '
        'stroke="#fff" stroke-width="1.7" stroke-linejoin="round"/>'
        '<path d="M9 11.8l2 2 3.8-4" fill="none" stroke="#fff" stroke-width="1.8" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
    ),
    # Warning triangle: a hazard advisory.
    "alert": (
        '<path d="M12 4 2.5 20h19z" fill="none" stroke="#fff" stroke-width="1.7" '
        'stroke-linejoin="round"/>'
        '<path d="M12 10v4.5" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>'
        '<circle cx="12" cy="17.6" r="1.05" fill="#fff"/>'
    ),
}

#: The SHELTER mark, matching `frontend/public/shelter-mark.svg`.
#:
#: Reversed to white because it sits on the brand-coloured header band. Inline so it
#: renders before the recipient allows remote images.
_SHELTER_MARK = (
    '<svg width="26" height="26" viewBox="0 0 40 40" '
    'style="display:block" aria-hidden="true">'
    # Satellite: body plus two panels. Reversed to white for the brand-coloured header
    # band. Kept to three rectangles because email clients render inline SVG at whatever
    # size they please and detail does not survive it.
    '<rect x="17.7" y="2.6" width="4.6" height="4.1" rx="1.1" fill="#fff"/>'
    '<rect x="11.9" y="3.7" width="4.9" height="1.9" rx="0.65" fill="#fff" opacity="0.6"/>'
    '<rect x="23.2" y="3.7" width="4.9" height="1.9" rx="0.65" fill="#fff" opacity="0.6"/>'
    # Coverage arcs, spreading down from the satellite.
    '<path d="M13.2 12.9a9.2 9.2 0 0 1 13.6 0" fill="none" stroke="#fff" '
    'stroke-width="2.1" stroke-linecap="round" opacity="0.7"/>'
    '<path d="M8.8 16.4a14.6 14.6 0 0 1 22.4 0" fill="none" stroke="#fff" '
    'stroke-width="2.1" stroke-linecap="round" opacity="0.32"/>'
    # The shelter, and the plot it protects.
    '<path d="M20 18 8.6 27.6v8.2h5.6v-6.6h11.6v6.6h5.6v-8.2Z" fill="#fff"/>'
    '<circle cx="20" cy="32" r="2.5" fill="#fff"/>'
    "</svg>"
)



#: The ONE exception to "inline styles only", and it is scoped to a single property.
#:
#: A `@media (prefers-color-scheme: dark)` rule cannot be inlined — inline styles have no
#: media-query form — so the alternative is no dark-mode handling at all. Which is what we
#: had, and why the FreePass wordmark read as washed out in a dark client.
#:
#: **Treated as an enhancement, never as the mechanism.** Gmail's web client strips `<style>`
#: in several contexts, so this rule must not be load-bearing: the inline `fill="{LOGO_INK}"`
#: on the SVG is what always applies, and it is chosen to be legible even when a client
#: force-inverts the email. Where the rule survives — Apple Mail, iOS Mail, Outlook for Mac —
#: the logo additionally lifts to near-white on the inverted background.
#:
#: Deliberately limited to the logo fill. Restyling the card, the text or the brand band here
#: would create a second design that only some clients see, and a partially-applied dark theme
#: is worse than a light email a client inverts wholesale.
_DARK_MODE_CSS = """<style>
  @media (prefers-color-scheme: dark) {
    .fp-logo-light { display: none !important; width: 0 !important; max-height: 0 !important; }
    .fp-logo-dark  { display: block !important; width: 115px !important;
                     max-height: none !important; height: 23px !important; }
  }
</style>"""


def _header(kind: str, eyebrow: str, title: str, accent: str) -> str:
    """The brand band: SHELTER mark, wordmark, kind icon, and the email's own title.

    A table rather than flex, because Outlook ignores flex entirely and would stack these
    vertically.

    `accent` is the band colour — `BRAND` for account mail, the severity colour for an advisory. It
    is the only thing that varies; the mark, wordmark, icon and type are fixed so every email reads
    as the same product.
    """
    icon = _ICONS.get(kind, _ICONS["welcome"])

    return f"""\
<tr>
  <td style="background:{accent};padding:22px 26px 20px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
      <tr>
        <td width="34" valign="middle" style="padding-right:10px;">{_SHELTER_MARK}</td>
        <td valign="middle">
          <div style="font:700 15px/1 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
                      letter-spacing:.14em;color:#fff;">SHELTER</div>
        </td>
        <td align="right" valign="middle">
          <svg width="22" height="22" viewBox="0 0 24 24" style="display:block"
               aria-hidden="true">{icon}</svg>
        </td>
      </tr>
    </table>

    <div style="margin-top:18px;font:600 10px/1.4 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
                letter-spacing:.14em;text-transform:uppercase;color:rgba(255,255,255,.78);">
      {eyebrow}
    </div>
    <div style="margin-top:5px;font:700 20px/1.3 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
                color:#fff;">{title}</div>
  </td>
</tr>"""


def _footer() -> str:
    """Centred footer, identical on every email.

    ## Attribution order: FreePass logo first, then NIGCOMSAT

    **FreePass leads.** SHELTER is a FreePass product; NIGCOMSAT is the satellite and
    broadcast partner. The order used to be reversed — `NIGCOMSAT | <FreePass svg>` — and
    because NIGCOMSAT was text sitting immediately under the "Powered by" label while the
    logo trailed it, every email read as "Powered by NIGCOMSAT" with the FreePass mark
    looking like an afterthought. Reported from an aggregator activation email.

    The asymmetry is deliberate and not a shortcut: FreePass is inline **SVG** because we
    hold the vector, and inline SVG renders before a recipient allows images — which is
    exactly when they are deciding whether the mail is legitimate. NIGCOMSAT is set as a
    **text wordmark** because that asset is a raster, and a remote `<img>` would be blocked
    on first open and show as a broken-image placeholder next to our logo. A wordmark that
    always renders beats a logo that usually does not.
    """
    return f"""\
<tr>
  <td style="padding:26px;text-align:center;border-top:1px solid {HAIRLINE};">
    <div style="font:600 9px/1.4 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
                letter-spacing:.14em;text-transform:uppercase;color:{FAINT};">
      Powered by
    </div>

    <table role="presentation" cellpadding="0" cellspacing="0" border="0"
           align="center" style="margin:9px auto 0;">
      <tr>
        <td valign="middle" style="padding-right:11px;">
          <!--
            Two <img>, one shown per colour scheme. NOT inline <svg>.

            The wordmark was inline SVG so it would render before a recipient allows remote
            images. It showed nothing at all in Outlook: Microsoft's sanitiser STRIPS <svg>
            from incoming mail, so the markup never reaches the renderer. A base64 PNG keeps
            the no-fetch property and survives the sanitiser.

            The dark-ink asset is first and unconditional, so a client that strips <style>
            still shows a correctly-coloured logo on the white card. The light-ink one is
            hidden by default and only revealed under a dark scheme.
          -->
          <img src="data:image/png;base64,{FREEPASS_PNG_DARK_INK}"
               width="{FREEPASS_WIDTH}" height="{FREEPASS_HEIGHT}" alt="FreePass"
               class="fp-logo-light"
               style="display:block;width:{FREEPASS_WIDTH}px;height:{FREEPASS_HEIGHT}px;border:0;"/>
          <img src="data:image/png;base64,{FREEPASS_PNG_LIGHT_INK}"
               width="{FREEPASS_WIDTH}" height="{FREEPASS_HEIGHT}" alt="FreePass"
               class="fp-logo-dark"
               style="display:none;width:0;max-height:0;overflow:hidden;border:0;"/>
        </td>
        <td valign="middle" style="padding-right:11px;color:{HAIRLINE};">&#124;</td>
        <td valign="middle">
          <!--
            The real NIGCOMSAT emblem, embedded like the FreePass wordmark.

            It was a text wordmark because a remote <img> is blocked on first open — the same
            objection that put FreePass in inline SVG. Base64 removes it for both: the bytes
            travel inside the message. Not recoloured, because this is a multicolour mark and
            flattening it to one ink would destroy it rather than adapt it.

            `alt` is the wordmark, so a client that blocks even embedded images still shows
            "NIGCOMSAT" rather than an empty box.
          -->
          <img src="data:image/png;base64,{NIGCOMSAT_PNG}"
               width="{NIGCOMSAT_WIDTH}" height="{NIGCOMSAT_HEIGHT}" alt="NIGCOMSAT"
               style="display:block;width:{NIGCOMSAT_WIDTH}px;height:{NIGCOMSAT_HEIGHT}px;border:0;"/>
        </td>
      </tr>
    </table>

    <div style="margin-top:20px;padding-top:16px;border-top:1px solid {HAIRLINE};
                font:400 12px/1.7 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
                color:{MUTED};">
      &copy; FreePass Holding Co 2026<br/>
      SHELTER &mdash; satellite-enabled &amp; AI-powered early warning for flood,
      crop and health risk.
    </div>
  </td>
</tr>"""


def render(
    *,
    kind: str,
    eyebrow: str,
    title: str,
    body_html: str,
    accent: str | None = None,
) -> str:
    """Wrap a body fragment in the shared chrome.

    `body_html` is a sequence of block elements with inline styles — no wrapper table
    needed, the cell below provides it. Senders therefore describe only their own
    content, and the header, footer and outer shell stay identical across every
    notification.

    ## `accent` — the one thing an advisory may change, and the only thing

    Hazard advisories carry a severity, and the header band is coloured by it: an EMERGENCY reads red
    before a word is parsed. Account mail passes nothing and gets `BRAND`.

    It is a single parameter rather than a second `render_alert()` because the *chrome* must not
    fork. The advisory email previously built its own `<!doctype html>` — which is how it came to
    have no SHELTER mark, no footer, a different hairline colour and no dark-mode rule. A second
    entry point here would let exactly that happen again, one divergence at a time.

    Everything else stays fixed on purpose. The mark, wordmark, per-kind icon, footer attribution,
    card radius, canvas and type stack are identical, so a subscriber's welcome mail and their flood
    warning are visibly the same product — which is what makes the warning credible enough to act
    on.
    """
    accent = accent or BRAND
    return f"""\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="color-scheme" content="light"/>
<title>{title}</title>
{_DARK_MODE_CSS}
</head>
<body style="margin:0;padding:0;background:{CANVAS};">
<!-- Preheader: the grey text a client shows beside the subject in the inbox list. Left
     unset, clients scrape the first words of the body, which is usually "Hello <name>" —
     wasted space at the one moment the recipient decides whether to open. -->
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{eyebrow} &mdash; {title}</div>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:{CANVAS};">
  <tr>
    <td align="center" style="padding:26px 14px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
             style="max-width:560px;background:#fff;border-radius:16px;overflow:hidden;
                    border:1px solid {HAIRLINE};">
        {_header(kind, eyebrow, title, accent)}
        <tr>
          <td style="padding:26px;font:400 15px/1.65 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
                     color:{TEXT};">
            {body_html}
          </td>
        </tr>
        {_footer()}
      </table>
    </td>
  </tr>
</table>
</body>
</html>"""


def button(href: str, label: str) -> str:
    """A call-to-action button.

    A padded anchor rather than a `<button>`: form controls do not render in most email
    clients, and Outlook needs the background on the `<a>` itself rather than on a parent.
    """
    return (
        f'<p style="margin:0 0 22px;">'
        f'<a href="{href}" style="display:inline-block;background:{BRAND};color:#fff;'
        f"text-decoration:none;padding:13px 24px;border-radius:10px;"
        f'font:600 15px/1 -apple-system,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;">'
        f"{label}</a></p>"
    )


def note(text: str) -> str:
    """Small print — expiry, single-use, "ignore if you did not request this"."""
    return (
        f'<p style="margin:0;font:400 12px/1.6 -apple-system,\'Segoe UI\',Roboto,'
        f'Helvetica,Arial,sans-serif;color:{FAINT};">{text}</p>'
    )


def request_details(
    *,
    ip: str | None,
    os_name: str | None,
    browser: str | None,
    location: str | None,
) -> str:
    """A four-row table: IP address, OS, browser, estimated location.

    ## Why this belongs in every security email

    A sign-in link, a password reset or a new-key notice all raise the same question in the
    recipient's mind: *did I do this?* The answer is not in the email's wording — it is in
    whether the device and place look like theirs. Without these rows the recipient has no
    way to tell a legitimate notice from a phishing attempt or an actual intrusion, which
    makes the whole notice decorative.

    ## Presentation choices that matter in email specifically

      * **A table, and every cell inline-styled.** Outlook's Word engine ignores flex and
        strips `<style>`, so a definition list or a grid collapses into a run-on paragraph.
      * **Labels in a fixed-width left column.** The four values have very different lengths
        ("Mac 10" against "Warrington, England, United Kingdom"); ragged labels make the
        block unscannable at exactly the moment it needs to be read quickly.
      * **Unknown values render as an em dash, never blank.** An empty cell reads as a broken
        email; "—" states that the client did not send it, which is itself information.
      * **Monospace for the IP only.** It is the one value someone might compare character by
        character.

    The location is labelled *estimated* in the row itself rather than in a footnote, because
    a caveat the reader has to go looking for is a caveat they will not see. An IP lookup in
    Sub-Saharan Africa frequently resolves to a carrier's national gateway, so an unfamiliar
    city here is usually the database rather than an intruder — and a recipient who panics at
    a correct-but-imprecise city is a support call.
    """
    rows = (
        ("IP address", ip, True),
        ("Operating system", os_name, False),
        ("Browser", browser, False),
        ("Estimated location", location, False),
    )

    cells = ""
    for label, value, mono in rows:
        family = (
            "ui-monospace,'SF Mono',Menlo,Consolas,monospace"
            if mono
            else "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
        )
        shown = value or "&mdash;"
        cells += f"""
      <tr>
        <td style="padding:5px 12px 5px 0;vertical-align:top;white-space:nowrap;
                   font:600 11px/1.5 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
                   letter-spacing:.04em;text-transform:uppercase;color:{FAINT};">{label}</td>
        <td style="padding:5px 0;vertical-align:top;
                   font:400 13px/1.5 {family};color:{TEXT};">{shown}</td>
      </tr>"""

    return f"""
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       style="margin:0 0 22px;padding:14px 16px;background:{CANVAS};
              border:1px solid {HAIRLINE};border-radius:10px;">
  <tr>
    <td>
      <div style="margin:0 0 9px;font:600 10px/1.4 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
                  letter-spacing:.12em;text-transform:uppercase;color:{MUTED};">
        Where this request came from
      </div>
      <table role="presentation" cellpadding="0" cellspacing="0" border="0">{cells}
      </table>
      <div style="margin:10px 0 0;font:400 11px/1.55 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
                  color:{FAINT};">
        Location is estimated from the IP address and is often the mobile network&rsquo;s
        gateway rather than your exact town. If none of this looks like you, do not use the
        link above &mdash; sign in directly and review your activity log.
      </div>
    </td>
  </tr>
</table>"""


def card(rows: list[tuple[str, str]]) -> str:
    """The report card — the answer-first block that sits above the prose.

    A definition-list shape rather than a paragraph: the point of the card is that "Since last
    check" and "Confidence" are in the same place on every alert, so the eye learns where to go and
    stops reading them.

    Each row is its own `<tr>` because Outlook's Word engine collapses a `div`-based two-column
    layout into a run-on line. Returns `""` for no rows, so an assessment with nothing measurable
    produces a shorter email rather than an empty bordered box.
    """
    if not rows:
        return ""

    cells = "".join(
        f"<tr>"
        f'<td style="padding:5px 14px 5px 0;color:{MUTED};font-size:12px;'
        f'white-space:nowrap;vertical-align:top;">{label}</td>'
        f'<td style="padding:5px 0;color:{INK};font-size:13.5px;'
        f'font-weight:600;line-height:1.45;">{value}</td>'
        f"</tr>"
        for label, value in rows
    )
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;border-collapse:collapse;margin:0 0 20px;padding:14px;'
        f'background:{CANVAS};border:1px solid {HAIRLINE};border-radius:10px;">'
        f"{cells}</table>"
    )


def track_modules(items: list) -> str:
    """The per-track modules: one bordered block per measured dimension.

    ## Why separate blocks rather than more card rows

    The card answers *what do I do*. These answer *what is happening on my field*, and they are
    separate physical quantities with their own units and their own baselines — standing water in
    percent, soil water in m3/m3, rain in millimetres. Apple Weather's precipitation, UV and wind
    cards are separate for the same reason, and that separation is the pattern worth borrowing.

    ## Email cannot be tappable, so the disclosure is inverted

    On the web each module opens to reveal its detail rows. Email has no interaction that survives
    every client — `<details>` renders as permanently-open in Outlook and as nothing in some Gmail
    contexts — so the detail is **rendered inline, smaller and below**. Same content, one level
    flatter. A subscriber who reads the email and then opens the portal sees the same numbers in the
    same order rather than discovering figures the email withheld.

    Takes `list[Track]` (from `dispatch/tracks.py`), typed loosely so this module keeps importing
    nothing from `app.dispatch` — the layout stays a pure string builder.
    """
    if not items:
        return ""

    blocks = ""
    for t in items:
        detail = "".join(
            f"<tr>"
            f'<td style="padding:3px 10px 3px 0;color:{FAINT};font-size:11px;'
            f'vertical-align:top;white-space:nowrap;">{label}</td>'
            f'<td style="padding:3px 0;color:{MUTED};font-size:11.5px;'
            f'line-height:1.5;">{value}</td>'
            f"</tr>"
            for label, value in t.detail
        )
        detail_block = (
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'style="width:100%;border-collapse:collapse;margin:9px 0 0;padding-top:8px;'
            f'border-top:1px solid {HAIRLINE};">{detail}</table>'
            if detail
            else ""
        )

        blocks += f"""
      <tr>
        <td style="padding:0 0 10px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
                 style="border:1px solid {HAIRLINE};border-radius:10px;">
            <tr>
              <td style="padding:13px 15px;">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                  <tr>
                    <td style="font:600 10px/1.4 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
                               letter-spacing:.1em;text-transform:uppercase;color:{FAINT};">
                      {t.label}
                    </td>
                    <td align="right"
                        style="font:700 15px/1.2 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
                               color:{INK};white-space:nowrap;">{t.reading}</td>
                  </tr>
                </table>
                <div style="margin:7px 0 0;font:400 13px/1.55 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
                            color:{TEXT};">{t.meaning}</div>
                {detail_block}
              </td>
            </tr>
          </table>
        </td>
      </tr>"""

    return f"""
<p style="margin:24px 0 10px;font-weight:600;color:{INK};">What we measured</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{blocks}
</table>"""
