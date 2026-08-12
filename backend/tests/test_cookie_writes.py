"""Session cookies may only be written where Next.js permits it.

## The bug this file exists to prevent, twice over

Next.js seals the cookie store during a Server Component render. Read it in Next's own source
(`server/web/spec-extension/adapters/request-cookies.js`): `RequestCookiesAdapter.seal` returns a
proxy whose `set`, `delete` and `clear` are replaced by a function whose only behaviour is
`throw new ReadonlyRequestCookiesError()`. There is no condition — a page render cannot write a
cookie, because headers may already have streamed and there is no response left to attach
`Set-Cookie` to.

This has now caused **two production incidents in the same file**:

1. `setFlash("verified")` on the email-confirmation path — a hard 500 after the address had already
   been confirmed.
2. `setSession(...)` on the magic-link sign-in path — reproduced locally against a freshly minted,
   valid token: the page rendered *"That sign-in link is invalid, already used, or has expired"*,
   and the token **was consumed**, so retrying could not work. The judge who reported it clicked
   within 60 seconds, which is precisely what ruled out expiry.

The second was worse than the first because it *looked* like a backend problem. It was not: POSTing
the same token to `/iam/auth/magic-link/verify` returns a correct 400 once spent, and the API had
already issued the session. The failure was entirely in where the cookie was written.

## Why a grep is the right shape for this

There is no type error to catch it. `app/auth/actions.ts` is marked `"use server"`, so every export
genuinely *is* a Server Action — the functions were legal, they were merely *called* from the one
context that cannot write a cookie. TypeScript cannot see that, and the failure is invisible in a
build: it happens at request time, in a `catch` that reports it as a bad link.

So the guard is structural, and it checks the property that actually matters: **no `page.tsx` may
reach a session write.**
"""

from __future__ import annotations

import pathlib
import re

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"

#: Helpers that write a cookie, directly or transitively.
#:
#: `setSession`/`clearSession` call `cookies().set()`/`.delete()`; `setFlash` did the same before it
#: was removed and is kept here so reintroducing it under the old name fails immediately.
COOKIE_WRITERS = ("setSession", "clearSession", "setFlash", "sessionCookieOptions")

#: Actions whose only legal caller is a form submission or a Route Handler.
#:
#: These exchange a credential AND write a cookie, so calling one during a render burns the
#: credential and then fails. Named individually rather than inferred, because that is the list a
#: reviewer can check against the file.
CREDENTIAL_REDEEMERS = ("redeemMagicLink", "redeemVerification")


def _pages() -> list[pathlib.Path]:
    return sorted(FRONTEND.glob("app/**/page.tsx"))


def _code(path: pathlib.Path) -> str:
    """Source with comments and docstring-style block comments removed.

    **Necessary, not tidiness.** These files explain the bug they were written to fix, so they
    legitimately contain the strings `setSession(`, `token` and `link-failed` in prose. Two of the
    tests below were written without this and failed against correct code — matching their own
    documentation. A guard that cannot tell an explanation from a call is worse than no guard,
    because the obvious way to silence it is to delete the explanation.
    """
    source = path.read_text()
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)  # block comments, incl. JSDoc
    source = re.sub(r"^\s*//.*$", "", source, flags=re.M)  # whole-line comments
    source = re.sub(r"//.*$", "", source, flags=re.M)  # trailing comments
    return source


def test_the_frontend_checkout_is_present():
    """Guard the guard: a backend-only checkout would make every test below vacuous."""
    assert _pages(), f"no page.tsx found under {FRONTEND}/app — the sweep would pass trivially"


def test_no_page_writes_a_session_cookie():
    """**The regression test for both incidents.**

    A `page.tsx` is a Server Component. If it calls a cookie writer the call throws at request time
    — and both times it happened, the throw was swallowed by a `catch` that reported something else
    entirely, so nothing failed loudly.

    The fix for the magic link was to move the whole flow into `app/auth/verify/route.ts`, a Route
    Handler, which owns its `Response` and can therefore set `Set-Cookie`.
    """
    offenders: list[str] = []

    for page in _pages():
        source = _code(page)
        for writer in COOKIE_WRITERS:
            # Word boundary so `setSessionToken` or a comment mentioning it in prose does not match
            # a call. `\b` on both sides of an identifier followed by `(`.
            if re.search(rf"\b{writer}\s*\(", source):
                offenders.append(f"{page.relative_to(FRONTEND)} calls {writer}()")

    assert not offenders, (
        "A page render cannot write a cookie — Next.js seals the store and `set` throws "
        "unconditionally. Move the flow to a Route Handler (see app/auth/verify/route.ts).\n  "
        + "\n  ".join(offenders)
    )


def test_no_page_redeems_a_single_use_credential():
    """A page must not spend a token it cannot finish using.

    Stricter than the cookie rule and deliberately so. These actions redeem an emailed token — the
    backend deletes it atomically — and then write a session. A render can do the first half and
    never the second, which is exactly how a user got "already used" for a link they had just
    received, with no way to recover.

    A GET that consumes a token is worth being careful about for a second reason the codebase
    already documents on `team_invite_url`: mail scanners and link previews fetch URLs before a
    human clicks. The Route Handler accepts that trade for sign-in because the alternative is a
    dead-end POST in a mail client, but it should be a *considered* trade, made in one known place.
    """
    offenders: list[str] = []

    for page in _pages():
        source = _code(page)
        for action in CREDENTIAL_REDEEMERS:
            if re.search(rf"\b{action}\s*\(", source):
                offenders.append(f"{page.relative_to(FRONTEND)} calls {action}()")

    assert not offenders, (
        "Redeeming an emailed token from a render spends it and then fails to write the session.\n  "
        + "\n  ".join(offenders)
    )


def test_the_verify_route_is_a_handler_and_not_a_page():
    """The specific fix, pinned.

    `page.tsx` and `route.ts` cannot coexist in one App Router directory, so this also asserts the
    old file is gone rather than merely unused.
    """
    directory = FRONTEND / "app/auth/verify"
    assert (directory / "route.ts").exists(), "the magic-link flow must be a Route Handler"
    assert not (directory / "page.tsx").exists(), (
        "page.tsx and route.ts conflict in the same route; the page version is the broken one"
    )


def test_the_verify_handler_sets_the_cookie_on_its_own_response():
    """`response.cookies.set(...)`, not `cookies().set(...)`.

    The distinction is the entire fix. A handler that reached for the request-scoped store would
    throw in exactly the same way the page did, so being *in* a Route Handler is necessary but not
    sufficient — it has to use the response API.
    """
    source = _code(FRONTEND / "app/auth/verify/route.ts")

    assert "response.cookies.set(" in source, (
        "the handler must set the cookie on the Response it returns"
    )
    # `setSession` goes through `cookies()`, which is request-scoped and read-only here.
    assert not re.search(r"\bsetSession\s*\(", source), (
        "setSession() uses the request cookie store; a handler must use response.cookies"
    )


def test_a_failed_link_does_not_carry_the_token_onward():
    """The failure path redirects rather than rendering, and the redirect drops the query string.

    Two benefits, and the second is why it is asserted: a user who reloads the error page or pastes
    it into a support chat is not carrying a credential. The old page rendered the error *at* the
    token's own URL.
    """
    source = _code(FRONTEND / "app/auth/verify/route.ts")

    assert "/auth/link-failed" in source

    # A reason code is fine; the token itself must never be forwarded.
    #
    # Scoped to the failure URL's own template literal — an earlier version matched
    # `link-failed[^"\']*token` across newlines and reached `session.access_token` forty lines
    # later, failing against correct code. The `[^`]*` class stops at the closing backtick.
    for url in re.findall(r"`[^`]*link-failed[^`]*`", source):
        assert "token" not in url, f"the failure redirect must not include the token: {url}"
    assert (FRONTEND / "app/auth/link-failed/page.tsx").exists(), (
        "the handler redirects here; without the page a failed link 404s"
    )


def test_the_failure_page_distinguishes_spent_from_unavailable():
    """One message for every failure is what made this bug so hard to report.

    The old copy said *"invalid, already used, or has expired — request a new one"* for a token the
    backend had just accepted, and told the user to do the one thing that could not help. A 5xx or
    an unreachable API is not a spent link, and advising a replacement there burns a working link
    for nothing.
    """
    source = _code(FRONTEND / "app/auth/link-failed/page.tsx")

    for reason in ("unavailable", "missing"):
        assert f'"{reason}"' in source, f"no branch for reason={reason}"

    handler = _code(FRONTEND / "app/auth/verify/route.ts")
    assert "status >= 500" in handler, (
        "the handler must separate a server-side failure from a spent token"
    )


# --------------------------------------------------------------------------- #
# The SECOND production failure of the same fix
# --------------------------------------------------------------------------- #


def test_redirects_are_not_built_from_the_request_url():
    """**Verified in production, and it passed every local test.**

    `NextResponse.redirect` needs an absolute URL. The obvious base is `request.url` — and that is
    the address the *container* was reached on, not the one the browser used. On the VPS the Next
    server binds `HOSTNAME=0.0.0.0` / `PORT=3100` behind Traefik, so the emitted header was:

        location: https://0.0.0.0:3100/auth/link-failed?reason=spent

    ...which no browser can resolve. The redemption had already SUCCEEDED — the first click reached
    `/dashboard` on the unreachable host — so it presented as the magic link still being broken when
    only the redirect target was.

    It passed locally because `request.url` IS correct with nothing proxying: bound origin and public
    origin coincide. That is exactly why this needs a structural guard rather than a manual check —
    the bug is invisible in the environment where it is most convenient to test.

    Same class as the `links.ts` bug (a Docker service name reaching an `href`), which is why the
    codebase now has two guards against internal addresses escaping into client-facing values.
    """
    source = _code(FRONTEND / "app/auth/verify/route.ts")

    assert not re.search(r"new URL\([^)]*request\.url", source), (
        "request.url is the container's bind address behind a proxy — build redirects from the "
        "forwarded host (see publicUrl()) instead"
    )
    assert "x-forwarded-host" in source, (
        "the public origin must come from the proxy's own header where it is available"
    )


def test_the_public_origin_has_a_fallback_for_every_deployment_shape():
    """Three rungs, because no single source is present everywhere.

    A proxy that does not forward the headers, and a local `npm start` with no proxy at all, are both
    legitimate. Losing either rung means a redirect that works in one place and 404s in the other.
    """
    source = _code(FRONTEND / "app/auth/verify/route.ts")

    assert "x-forwarded-host" in source, "rung 1: what the proxy says the client asked for"
    assert "NEXT_PUBLIC_SITE_URL" in source, "rung 2: the configured public origin"
    assert "nextUrl.origin" in source, "rung 3: no proxy in front"
