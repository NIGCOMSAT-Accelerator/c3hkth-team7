"""Shared email chrome, for every sender in the platform.

## Why this is not under `app/iam/`

It was, and that was the wrong home the moment a second subsystem needed it.

`iam/mailer.py` sends eleven kinds of account mail and all eleven went through `layout.render`, so
they shared a header, a footer and one set of design tokens. `dispatch/email_channel.py` sends the
**hazard advisories** — the actual product — and it hand-rolled its own `<!doctype html>` with its
own header, no footer, no SHELTER mark, a different hairline colour and no dark-mode handling.

So the one email a subscriber receives *because the service is doing its job* was the one that did
not look like the service. A farmer who got a "welcome to SHELTER" mail and then a flood warning saw
two different products, and the warning was the one that looked less legitimate — which matters,
because deciding whether to act on it starts with deciding whether it is real.

Importing the layout from its old home under `app/iam/` would have worked (both modules are pure
string builders with no dependencies) and would have been wrong: it makes advisory delivery
structurally dependent on the identity subsystem, and it invites the next reader to conclude that
dispatch may reach into `app.iam` generally. `tests/test_email_branding.py` asserts the direction —
`app/email/` imports from neither `app.iam` nor `app.dispatch`.

Layout only. No transport, no templates, no content: `iam/mailer.py` and `dispatch/email_channel.py`
each still own their own bodies and their own sending.
"""
