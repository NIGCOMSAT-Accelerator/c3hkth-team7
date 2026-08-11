"""OpenAPI security schemes — the three credentials, declared so the consoles know them.

## The bug this fixes

Every guard in this codebase reads its credential with `Header(alias="X-SHELTER-API-Key")`.
That works at runtime, but to OpenAPI a header parameter is just a parameter — so the
generated document declared **zero** security schemes across 76 operations. The consequence
was visible in both consoles and easy to miss in review:

  * **No Authorize button.** A partner reading `/dev-docs` could not authenticate, so every
    "Try it" produced a 401 and the reference looked broken rather than gated.
  * **No padlocks.** Nothing distinguished a gated endpoint from an open one, so a reader had
    to infer the auth model from prose.
  * **Generated clients had no auth.** `openapi-generator` emits credential handling from
    `securitySchemes`; with none declared it produces a client that cannot authenticate, and
    the partner writes the header plumbing by hand.

Declaring them here fixes all three at once, and the header parameter disappears from the
operation signature because FastAPI recognises the security dependency instead.

## Why three schemes rather than one

They are not interchangeable, and the separation is the security model:

| Scheme | Header | Who holds it |
|---|---|---|
| `AggregatorApiKey` | `X-SHELTER-API-Key` | a commercial partner, scoped to their own customers |
| `PlatformApiKey` | `X-SHELTER-API-Key` | a service account — the portal, CI |
| `PortalSession` | `Authorization: Bearer` | a signed-in human, in the web portal |

The first two share a header but not an audience, and a route that accepts either must say
so — which is exactly the case for `/places/*`. Collapsing them into one scheme would lose
the distinction a reader needs: "any valid key" and "your partner key" are different
statements about what a route will accept.

`PortalSession` is declared for completeness of the internal console. It is deliberately
**absent** from the partner reference, where documenting it would advertise an authentication
path a partner cannot use.
"""

from __future__ import annotations

from fastapi.security import APIKeyHeader, HTTPBearer

#: A commercial partner's key. Scoped to their own customers by the `memberships` edge.
#:
#: `auto_error=False` on all of these: the guards raise their own `HTTPException` with a
#: message written for the caller ("API key is invalid, revoked or expired" — one message for
#: every failure, so a caller cannot probe which keys exist). Letting the scheme raise instead
#: would replace that with FastAPI's generic "Not authenticated" and lose the
#: non-enumeration property the guards are careful about.
aggregator_api_key = APIKeyHeader(
    name="X-SHELTER-API-Key",
    scheme_name="AggregatorApiKey",
    description=(
        "A commercial aggregator's API key, 64 characters prefixed `shltky` "
        "(`shlttk` for test keys). Create one in the portal under **Developers → API "
        "keys** — it is shown exactly once, and only a SHA-256 hash is stored.\n\n"
        "Scoped to your own customers: every query resolves the `memberships` edge inside "
        "the query, so another tenant's customer is never a candidate result."
    ),
    auto_error=False,
)

#: A platform service account — the Next.js server, CI. Same header, different audience.
platform_api_key = APIKeyHeader(
    name="X-SHELTER-API-Key",
    scheme_name="PlatformApiKey",
    description=(
        "A platform service-account key, carrying `platform:*` scopes. Used by the SHELTER "
        "portal's own server and by CI — not issued to partners.\n\n"
        "Provisioned with `make iam-service-account`. Deliberately never granted "
        "`platform:broadcast`, so a leaked frontend environment cannot page a district."
    ),
    auto_error=False,
)

#: A signed-in human's portal session. Not available to partners.
portal_session = HTTPBearer(
    scheme_name="PortalSession",
    description=(
        "A portal session token from `POST /iam/login`, sent as "
        "`Authorization: Bearer <jwt>`.\n\n"
        "For signed-in humans in the web portal, not for integrations. Subject to a "
        "15-minute idle timeout enforced server-side, so a token unused for that long is "
        "refused even though its own expiry has not passed."
    ),
    auto_error=False,
)


#: The legacy shared secret, sent as `X-SHELTER-Key`.
#:
#: Distinct from `X-SHELTER-API-Key` and that distinction is load-bearing: this one says
#: "trusted caller", the other says "this specific tenant", and every scoped query depends on
#: telling them apart. Sharing a header would make that impossible.
#:
#: Being retired in favour of scoped service accounts — see `IAM_LEGACY_SHARED_KEY_ENABLED`.
#: Documented so an existing integration can still find it, and marked deprecated so nobody
#: builds a new one against it.
legacy_shared_key = APIKeyHeader(
    name="X-SHELTER-Key",
    scheme_name="LegacySharedKey",
    description=(
        "**Deprecated.** The single shared secret from `API_KEY`, granting every platform "
        "capability at once — including broadcast.\n\n"
        "Use a scoped service-account key in `X-SHELTER-API-Key` instead: "
        "`make iam-service-account`. Set `IAM_LEGACY_SHARED_KEY_ENABLED=false` once nothing "
        "sends this."
    ),
    # No `deprecated=` argument: APIKeyHeader does not accept one. The bold "Deprecated" in
    # the description above is what a reader sees, which is the part that matters.
    auto_error=False,
)
