"""The exported OpenAPI contract.

`openapi.json` is committed at the repo root, and `make check` fails when it drifts
from the routes. Three reasons that matters more here than in a typical service:

1. **Aggregators integrate against it.** A removed field or renamed path breaks a
   partner's client silently. A committed spec makes every contract change visible in
   the diff that caused it.
2. **It is ungated**, so it is the first thing an evaluating partner reads. A stale
   spec is worse than no spec: it describes an API that no longer exists.
3. **Clients are generated from it offline**, without our stack running — which is why
   the exporter must not need GDAL, torch, or a database.
"""

from __future__ import annotations

import json
import pathlib

import pytest

SPEC = pathlib.Path("../openapi.json")


@pytest.fixture(scope="module")
def spec() -> dict:
    assert SPEC.exists(), "openapi.json is missing — run `make openapi`"
    return json.loads(SPEC.read_text())


def test_committed_spec_matches_the_routes():
    """**The guard that earns its keep.**

    Wired into `make check`, so adding an endpoint and forgetting to export fails the
    build with a one-line fix rather than shipping a stale contract to integrators.
    """
    from app.openapi_export import build_schema

    rendered = json.dumps(build_schema(), indent=2, sort_keys=True) + "\n"

    assert SPEC.read_text() == rendered, (
        "openapi.json is stale. Run `make openapi` and commit the result."
    )


def test_export_is_deterministic():
    """Same routes must produce byte-identical output.

    Without `sort_keys`, Python's dict ordering would make every export show as a
    diff — which trains reviewers to ignore the diff, and then a real contract change
    slips through unnoticed.
    """
    from app.openapi_export import build_schema

    first = json.dumps(build_schema(), indent=2, sort_keys=True)
    second = json.dumps(build_schema(), indent=2, sort_keys=True)
    assert first == second


def test_exporter_needs_no_geospatial_stack():
    """It must build with rasterio and torch unavailable.

    **This is a canary for a wider discipline.** `app/api/routes/*.py` keeps
    `agents.pipeline` imports inside handler bodies precisely so the route definitions
    stay importable without GDAL. A regression there breaks this exporter first — and
    it broke exactly this way while being written: four routers imported the pipeline
    at module scope and had to be deferred.
    """
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.split(".")[0] in ("rasterio", "torch"):
            raise ImportError(f"{name} blocked for this test")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = blocked
    try:
        # Re-import inside the block so the module-level imports are re-evaluated.
        import importlib

        import app.openapi_export as exporter

        importlib.reload(exporter)
        schema = exporter.build_schema()
    finally:
        builtins.__import__ = real_import

    assert schema["paths"], "schema must build with no geospatial stack"


def test_every_router_is_exported(spec: dict):
    """A router mounted in `main.py` but missing from the exporter would be invisible
    to every integrator — the endpoint would work and be undocumented."""
    paths = " ".join(spec["paths"])

    for surface in ("/health", "/subscribers", "/risk/", "/alerts", "/chat",
                    "/verification/", "/webhook", "/iam/"):
        assert surface in paths, f"{surface} is missing from the exported spec"


def test_exporter_mounts_the_same_routers_as_main():
    """The exporter builds its own app rather than importing `app.main`, to avoid
    dragging in the scheduler. That duplication is the cost — so the two lists must be
    asserted equal, or a new router silently ships undocumented."""
    import re

    main_source = pathlib.Path("app/main.py").read_text()
    export_source = pathlib.Path("app/openapi_export.py").read_text()

    mounted = set(re.findall(r"app\.include_router\((\w+)\.router", main_source))
    exported_block = export_source.split("for router in (")[1].split(")")[0]
    exported = {name.strip() for name in exported_block.split(",") if name.strip()}

    assert mounted == exported, (
        f"main.py mounts {sorted(mounted)} but the exporter builds "
        f"{sorted(exported)} — a router missing here ships undocumented"
    )


def test_security_schemes_are_declared(spec: dict):
    """Guarded endpoints must not appear public in the contract.

    Without declared schemes a generated client sends no credential at all, which is the
    single most misleading omission possible in a document handed to integrators.

    The names asserted here are the ones the GUARDS declare (`app/api/security_schemes.py`).
    An earlier version of this test asserted `ServiceAccountKey` — a name the exporter
    invented in a hand-written block that overwrote the app's real schemes. Both the block
    and that assertion are gone: the schemes now come from the routers, so this test fails if
    a guard stops declaring one rather than if a hand-maintained copy drifts.
    """
    schemes = spec["components"]["securitySchemes"]

    # The two API-key schemes share a header and differ in audience: a partner's own key
    # versus a platform service account. Both are real and both must be documented, because
    # several endpoints accept either.
    assert schemes["AggregatorApiKey"]["name"] == "X-SHELTER-API-Key"
    assert schemes["PlatformApiKey"]["name"] == "X-SHELTER-API-Key"
    assert schemes["PortalSession"]["scheme"] == "bearer"
    # The legacy header is documented as deprecated rather than hidden: a partner reading an
    # older integration guide needs to know why it stopped working.
    assert "Deprecated" in schemes["LegacySharedKey"]["description"]


def test_spec_describes_the_canonical_prefix(spec: dict):
    """The committed document must not vary with whoever ran the export.

    Pinned to the shipped default rather than the local `API_PREFIX`: otherwise a
    developer whose `.env` still says `/api/v1` exports a spec describing paths the
    production service does not serve, and `--check` flip-flops between them and CI.
    One prefix for the whole service is also what keeps the reverse-proxy config a
    single location block.
    """
    from app.openapi_export import CANONICAL_PREFIX

    assert all(p.startswith(CANONICAL_PREFIX) for p in spec["paths"])
    assert CANONICAL_PREFIX == "/shelter/v1/api"


def test_export_ignores_a_local_prefix_override(monkeypatch):
    """Exporting with a different API_PREFIX set must produce the same document."""
    from app.openapi_export import build_schema

    baseline = json.dumps(build_schema(), sort_keys=True)
    monkeypatch.setattr("app.config.settings.api_prefix", "/totally/different")
    assert json.dumps(build_schema(), sort_keys=True) == baseline


def test_docs_are_ungated():
    """The contract must be readable without a credential.

    An aggregator evaluating SHELTER, or generating a client, must be able to read the
    spec *before* they have a key — gating it would mean every integration starts with
    a support request. The spec describes the API's shape, not any subscriber's data.
    """
    main_source = pathlib.Path("app/main.py").read_text()

    app_block = main_source.split("app = FastAPI(")[1].split(")\n")[0]
    assert "docs_url" in app_block
    # No dependency may guard the app itself, which would gate the docs too.
    assert "dependencies=" not in app_block


def test_spec_carries_no_secret_values(spec: dict):
    """A schema is built from type annotations, but a careless `Field(example=...)` or
    a default on a credential setting could leak one. Cheap to assert, and the cost of
    being wrong is a secret published on an ungated endpoint."""
    import re

    rendered = json.dumps(spec)

    # Match a prefix followed by enough entropy to be a real credential, not the bare
    # prefix — which appears legitimately in the auth documentation as `shltky…`.
    # An earlier version asserted on the prefix alone and failed on its own docstring,
    # which would also have passed with a real secret present.
    patterns = {
        "API key": r"(?:shltky|shlttk)[A-Za-z0-9._~-]{40,}",
        "Brevo API key": r"xkeysib-[A-Za-z0-9]{20,}",
        "Brevo SMTP key": r"xsmtpsib-[A-Za-z0-9]{20,}",
        "Mongo URI with credentials": r"mongodb\+srv://[^:\s]+:[^@\s]+@",
        "webhook secret": r"whsec_[A-Za-z0-9_-]{20,}",
    }
    for label, pattern in patterns.items():
        found = re.search(pattern, rendered)
        assert found is None, f"the exported spec contains a {label}: {found.group()[:16]}…"


def test_operation_count_is_plausible(spec: dict):
    """A guard against a silently truncated export.

    If a router failed to import, the spec would still be valid JSON with fewer paths
    — and nothing else would notice. This is a floor, not an exact count, so adding
    endpoints does not fail it.
    """
    operations = sum(
        1
        for methods in spec["paths"].values()
        for method in methods
        if method in ("get", "post", "put", "patch", "delete")
    )
    assert operations >= 50, f"only {operations} operations — did a router fail to import?"


# --------------------------------------------------------------------------- #
# Partner developer documentation — /dev-docs
#
# A FILTERED spec. `/docs` describes the whole service including endpoints a partner
# can never call; publishing those is worse than omitting them, because a developer
# integrates against one and gets a 401 the docs said was impossible.
# --------------------------------------------------------------------------- #


def test_partner_spec_excludes_internal_endpoints():
    """The filtering rule: an endpoint appears only if an aggregator key can call it.

    Three groups are excluded — platform/service-account routes (a partner cannot hold
    a `platform:*` scope), portal-session routes (a browser flow, not an integration),
    and fleet operations (cost scales with every subscriber, not one tenant).
    """
    from app.api.routes.devdocs import partner_schema

    paths = set(partner_schema()["paths"])
    relative = {p.split("/api", 1)[-1] for p in paths}

    forbidden = [
        "/iam/service-accounts",   # provisions platform credentials
        "/iam/login",              # portal browser flow
        "/iam/signup/individual",
        "/iam/me",
        "/iam/audit/organisation",
        "/iam/api-keys",           # minting is a portal action
        "/verification/sweep",     # fleet-wide
        "/risk/scan",              # fleet-wide
        "/webhook/sweep",          # operator action across all tenants
        "/alerts/dispatch",        # needs platform:broadcast
        "/bootstrap",              # frontend handshake
        "/auth/",                  # sign-in flows
    ]
    leaked = [f for f in forbidden if any(r.startswith(f) for r in relative)]
    assert not leaked, f"internal endpoints exposed to partners: {leaked}"


def test_partner_spec_includes_what_an_integration_needs():
    """Filtering must not be so aggressive that the API becomes unusable."""
    from app.api.routes.devdocs import partner_schema

    relative = {p.split("/api", 1)[-1] for p in partner_schema()["paths"]}

    for required in ("/iam/customers", "/alerts", "/risk/assess", "/webhook",
                     "/verification/metrics", "/health"):
        assert any(r.startswith(required) for r in relative), f"{required} is missing"


def test_partner_spec_documents_only_the_credential_a_partner_holds():
    """Documenting the portal session or the legacy shared key would invite an
    integration built on a credential a partner cannot obtain.

    Asserts the PROPERTY rather than a scheme name. The previous version pinned the literal
    string `ServiceAccountKey` — a name nothing in the codebase declared, invented by the
    filter itself — so the test passed while the emitted document contained a dangling
    reference no console could resolve. A name-based assertion cannot catch that; a
    "references resolve and exclude the wrong credentials" assertion can.
    """
    from app.api.routes.devdocs import partner_schema

    spec = partner_schema()
    schemes = spec["components"]["securitySchemes"]

    # Every declared scheme must be the partner header, never the bearer session.
    assert schemes, "the partner spec must declare at least one credential"
    for name, scheme in schemes.items():
        assert scheme["type"] == "apiKey", f"{name} is not an API key scheme"
        assert scheme["name"] == "X-SHELTER-API-Key", f"{name} uses the wrong header"

    # The two a partner cannot hold must be absent.
    assert "PortalSession" not in schemes
    assert "LegacySharedKey" not in schemes


def test_partner_spec_has_no_dangling_security_references():
    """Every scheme an operation names must be declared in this document.

    This is what the console actually needs. Several routes accept either a scoped service
    key or the legacy shared key, so their operations list both — and `LegacySharedKey` is
    deliberately excluded here. Leaving the reference behind produced a document where ReDoc
    could not resolve the requirement and rendered no Authorize control, and where a strict
    `$ref` consumer such as openapi-generator errors on the whole file.
    """
    from app.api.routes.devdocs import partner_schema

    spec = partner_schema()
    declared = set(spec["components"]["securitySchemes"])

    referenced: set[str] = set()
    for requirement in spec.get("security", []):
        referenced |= set(requirement)
    for item in spec["paths"].values():
        for operation in item.values():
            if isinstance(operation, dict):
                for requirement in operation.get("security", []):
                    referenced |= set(requirement)

    assert referenced <= declared, (
        f"operations reference schemes this document does not declare: "
        f"{sorted(referenced - declared)}"
    )


def test_partner_spec_publishes_no_internal_models():
    """**The partner reference must not describe the shape of internal IAM.**

    Paths were already filtered by an allow-list, so none of these endpoints were *reachable*
    from the partner document. But `components.schemas` was passed through whole, which meant
    the reference published `InviteWrite`, `TeamMember`, `WorkspaceGrant`, `RoleOption` and
    five more — the full field-level shape of team management and workspace RBAC — to every
    integrator reading it.

    A schema block is documentation. Publishing an internal authorisation model tells a reader
    what exists and what to probe for, and invites a partner to build against an endpoint that
    will always refuse them. So the schemas are pruned to the transitive closure of what the
    retained paths reference.

    Matched on substrings rather than an exact list so a *new* internal model — a future
    `WorkspaceBilling`, say — is caught by this test rather than needing it updated.

    ## The one allow-list, and why a substring guard needs one

    `"track"` is here to catch `TrackInfo`, which is the intelligence-track *subscription* surface:
    which tracks an organisation is buying and whether activating one delivers anything. That is
    workspace billing and belongs on the internal console.

    `AssessmentTrack` is a different sense of the word entirely — the per-track modules of a report
    card (standing water, crop health, soil water), reachable from `Alert`, which partners
    legitimately receive on `GET /alerts`. Pruning it would leave the partner reference describing
    an `Alert` with an undocumented field.

    So the collision is resolved by naming the exception rather than by dropping `"track"`, which
    would silently stop guarding `TrackInfo`. Anything else matching still fails.
    """
    from app.api.routes.devdocs import partner_schema

    published = set(partner_schema()["components"]["schemas"])
    forbidden = ("workspace", "team", "invite", "invitation", "grant", "role", "track",
                 "member", "audit", "session", "totp", "password")
    #: Partner-facing despite matching a forbidden substring. Each entry must be reachable from a
    #: retained partner path — that is the test below.
    permitted = {"AssessmentTrack"}

    leaked = sorted(
        name
        for name in published - permitted
        if any(word in name.lower() for word in forbidden)
    )
    assert not leaked, (
        f"the partner reference publishes internal models: {leaked}. These belong on the "
        f"internal console only — prune them via _reachable_schemas."
    )

    # An allow-list entry is a hole unless it is justified, so each one must be genuinely reachable
    # from a partner-facing path. `AssessmentTrack` qualifies via `Alert` on `GET /alerts`; a future
    # entry added to silence this test for an unreachable model fails here instead.
    document = json.dumps(partner_schema())
    for name in permitted:
        assert name in published, (
            f"{name} is allow-listed but not published; drop it from `permitted` rather than "
            f"carrying a stale exception"
        )
        assert f'"#/components/schemas/{name}"' in document, (
            f"{name} is allow-listed as partner-facing but nothing in the partner document "
            f"references it — so it is internal after all, and should be pruned"
        )


def test_partner_spec_has_no_dangling_schema_references():
    """Pruning must not break a `$ref`.

    This is the risk the pruning introduces, and the reason the schemas were originally passed
    through whole: a missing schema breaks every reference still pointing at it, and a strict
    consumer (openapi-generator, some linters) errors on the entire document rather than on the
    one model. The closure walk is what makes pruning safe, and this asserts it stayed correct.
    """
    import json
    import re

    from app.api.routes.devdocs import partner_schema

    spec = partner_schema()
    declared = set(spec["components"]["schemas"])
    referenced = set(
        re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(spec))
    )

    assert referenced <= declared, (
        f"partner spec references schemas it does not declare: "
        f"{sorted(referenced - declared)}"
    )


def test_partner_spec_still_documents_what_a_partner_needs():
    """Pruning must not be over-eager either.

    The models a partner genuinely posts and reads have to survive — otherwise this document
    stops being usable and the failure is quiet, because an absent schema renders as an empty
    body rather than as an error.
    """
    from app.api.routes.devdocs import partner_schema

    published = set(partner_schema()["components"]["schemas"])
    # Customer onboarding and area resolution are the two flows an aggregator integrates.
    assert published, "partner spec publishes no schemas at all"
    assert any("Resolve" in name or "Area" in name for name in published), (
        f"the area-resolution models are missing from the partner reference: {sorted(published)}"
    )


def test_gated_operations_declare_a_security_scheme():
    """A guarded endpoint must show a padlock, not a bare header parameter.

    Reading a credential with `Header(alias=...)` works at runtime but is invisible to
    OpenAPI — it emits a plain parameter, so the console offers no Authorize button and a
    generated client cannot authenticate. Every guard therefore uses `Security(...)`.

    `/places/*` is the canary: it is the surface a partner integration starts from, and it
    was the one where the omission was first noticed.
    """
    from app.main import app

    spec = app.openapi()

    for path, item in spec["paths"].items():
        if "/places/" not in path:
            continue
        for method, operation in item.items():
            if method not in {"get", "post"}:
                continue
            assert operation.get("security"), f"{method.upper()} {path} declares no scheme"
            # And the credential must NOT also appear as an ordinary parameter, which is what
            # happens if a route mixes `Header(...)` with a security dependency.
            names = {p["name"] for p in operation.get("parameters", [])}
            assert "X-SHELTER-API-Key" not in names, (
                f"{method.upper()} {path} exposes the key as a plain parameter"
            )


def test_partner_spec_explains_authentication():
    """A developer's first three questions are how to authenticate, what they may see,
    and what happens on failure. All three are answered before the endpoint list."""
    from app.api.routes.devdocs import partner_schema

    intro = partner_schema()["info"]["description"]

    assert "X-SHELTER-API-Key" in intro
    assert "shltky" in intro, "the key format must be shown"
    assert "shown exactly once" in intro
    assert "404, not 403" in intro, "the enumeration-safe error must be explained"
    assert "at-least-once" in intro, "webhook delivery semantics must be stated"


def test_partner_filtering_uses_an_allow_list():
    """Direction matters: a new *internal* endpoint must be invisible by default.

    A deny-list would publish every new route until someone remembered to exclude it —
    and forgetting is silent, whereas forgetting to extend an allow-list surfaces as a
    partner asking why an endpoint is missing.
    """
    from app.api.routes.devdocs import PARTNER_PATH_DENY, PARTNER_PATH_PREFIXES

    assert PARTNER_PATH_PREFIXES, "there must be an explicit allow-list"
    # The deny-list exists only to carve exceptions out of a matching prefix.
    assert "/alerts/dispatch" in PARTNER_PATH_DENY


def test_redoc_default_is_disabled():
    """FastAPI's built-in ReDoc loads `cdn.jsdelivr.net/npm/redoc@next/...`, which now
    404s — so `/redoc` rendered a blank white page with no error. It is disabled rather
    than left broken, and `/dev-docs` serves a pinned bundle instead."""
    source = pathlib.Path("app/main.py").read_text()

    assert "redoc_url=None" in source
    devdocs = pathlib.Path("app/api/routes/devdocs.py").read_text()
    # Match the script tag, not prose. An earlier version asserted `redoc@next` was
    # absent from the whole file and tripped on the comment explaining why it was
    # replaced — which would also have passed with the broken URL still in the tag.
    # `<script src=` narrows to the actual tag. Matching the bare CDN host also hit
    # the docstring naming the broken URL, so the test failed on its own explanation.
    script = [
        ln for ln in devdocs.splitlines()
        if "<script src=" in ln and "redoc" in ln
    ]
    assert script, "the ReDoc bundle must be loaded from a pinned CDN URL"
    assert all("redoc@2" in ln for ln in script), script
    assert not any("redoc@next" in ln for ln in script)


def test_docs_pages_use_the_consortium_favicon():
    """FastAPI defaults to fastapi.tiangolo.com's icon, so an operator with several API
    consoles open cannot tell our tab from any other FastAPI service."""
    main = pathlib.Path("app/main.py").read_text()
    assert "swagger_favicon_url" in main
    assert "dev-docs/favicon.svg" in main


def test_committed_spec_declares_the_same_schemes_as_the_app():
    """The committed contract's AUTH MODEL must match the running service, not just its paths.

    This is the check whose absence let a real defect survive for the whole session.
    `test_committed_spec_matches_the_routes` compares paths and operations — so when the
    exporter overwrote the app's real security schemes with a hand-written block naming a
    phantom `ServiceAccountKey`, the freshness check passed while the committed file:

      * declared a scheme nothing in the codebase defines,
      * omitted the two real API-key schemes, and
      * left every operation referencing names the document did not define — which makes a
        strict `$ref` consumer such as `openapi-generator` error on the whole file.

    A contract check that ignores the auth model cannot notice the auth model breaking.
    """
    from app.main import app
    from app.openapi_export import DEFAULT_PATH

    committed = json.loads(DEFAULT_PATH.read_text())
    live = app.openapi()

    committed_schemes = set(committed.get("components", {}).get("securitySchemes", {}))
    live_schemes = set(live.get("components", {}).get("securitySchemes", {}))

    assert committed_schemes == live_schemes, (
        f"committed spec declares {sorted(committed_schemes)} but the app declares "
        f"{sorted(live_schemes)}. Run `make openapi`."
    )

    # And no operation may reference a scheme the document does not declare.
    referenced: set[str] = set()
    for requirement in committed.get("security", []):
        referenced |= set(requirement)
    for item in committed["paths"].values():
        for operation in item.values():
            if isinstance(operation, dict):
                for requirement in operation.get("security", []):
                    referenced |= set(requirement)

    assert referenced <= committed_schemes, (
        f"committed spec references undeclared schemes: "
        f"{sorted(referenced - committed_schemes)}"
    )


def test_partner_spec_references_no_internal_documents():
    """`docs/` is internal and gitignored — it must never be cited to a partner.

    A path an integrator cannot open is worse than no reference at all: they go looking, find
    nothing, and conclude the API is under-documented. It also names internal design notes,
    which is information about the system's seams rather than about its contract.

    The intro string and every operation description are checked, because both render in the
    partner console and the app-level description is inherited verbatim by the filtered spec.
    """
    import json

    from app.api.routes.devdocs import partner_schema

    text = json.dumps(partner_schema())
    assert "docs/" not in text or "dev-docs" in text, "sanity: the check below is meaningful"

    import re

    leaked = sorted(set(re.findall(r"docs/[a-z0-9-]+\.md", text)))
    assert not leaked, (
        f"the partner reference cites internal documents: {leaked}. Those live in a "
        "gitignored directory a partner cannot open — describe the field on the operation "
        "instead."
    )
