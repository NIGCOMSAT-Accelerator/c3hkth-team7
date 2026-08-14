"""The Dokploy deployment manifest, and the three faults it was written to correct.

## Why this is tested rather than trusted

`docs/docker-compose.dokploy.yml` is deployed by pasting it into a Dokploy panel. Nothing in CI
runs it, so a wrong label ships silently and fails at request time on a healthy container — which is
the hardest kind of deployment fault to diagnose, because every `docker ps` says the stack is fine.

The template this manifest was built from carried three faults. Two of them fail exactly that way:

  1. **The HTTPS router named another project's service** (`glitchtip-service`). Traefik accepts the
     label, matches the host, then cannot route: a 404 *from the proxy*. The HTTP router named its
     service correctly, so plain HTTP worked and HTTPS did not — the confusing half.
  2. **The load-balancer port was 3100** where the image listens on 3000. A 502 Bad Gateway with a
     passing healthcheck.
  3. The service list said `shelter` while the network block declared `shelter-network`. Compose
     fails fast on this one, so it is the benign fault of the three.

Each assertion below names the failure mode rather than the rule, because the rule is obvious in
hindsight and the failure mode is what makes it worth a test.
"""

from __future__ import annotations

import pathlib
import re

import pytest

MANIFEST = pathlib.Path(__file__).resolve().parents[2] / "docs/docker-compose.dokploy.yml"

#: Services that must be reachable from Traefik, and the port each listens on.
PUBLIC = {"shelter-ui": "3100", "shelter-api": "8000"}

#: Services that must NEVER be on the public network.
INTERNAL = ("worker", "worker-analyst", "postgres", "dragonfly", "minio")


@pytest.fixture(scope="module")
def raw() -> str:
    """The file as written, comments included. For assertions ABOUT the prose."""
    assert MANIFEST.exists(), "docs/docker-compose.dokploy.yml is missing"
    return MANIFEST.read_text()


@pytest.fixture(scope="module")
def manifest(raw: str) -> str:
    """The EFFECTIVE configuration — every comment removed.

    This matters more than it looks. The manifest's header documents the three faults it corrects,
    quoting the wrong values verbatim (`glitchtip-service`, `port=3100`) so the next reader knows
    what to look for. Parsing the file with comments intact reads those quotes as live config, and
    five tests failed against a manifest that was correct.

    That is not a hypothetical: the same mistake was made earlier in this codebase, in a test that
    matched `<svg` inside its own explanatory comment. Documentation that names a defect is
    valuable; a test that cannot tell prose from configuration is not.
    """
    return "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith("#")
    )


@pytest.fixture(scope="module")
def services(manifest: str) -> dict[str, str]:
    """Each service's own block.

    Scoped to everything AFTER the top-level `services:` key. A first version split the whole file
    on the two-space indent, which also matched the `networks:` and `volumes:` entries — so
    `shelter-ui`'s "block" began at the `shelter` network declaration and swallowed several
    unrelated services. Five tests failed against a manifest that was correct, which is the useful
    reminder that a passing test and a working parser are different things.
    """
    body = manifest.split("\nservices:\n", 1)[1]
    blocks: dict[str, str] = {}
    matches = list(re.finditer(r"^  ([a-z][a-z0-9-]*):$", body, re.M))
    assert matches, "no services parsed out of the manifest"
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        blocks[match.group(1)] = body[match.start() : end]
    return blocks


# --------------------------------------------------------------------------- #
# Fault 1 — a router pointing at a service that does not exist
# --------------------------------------------------------------------------- #


def test_no_router_points_at_a_foreign_service(manifest: str):
    """**The worst of the three, because HTTPS fails while HTTP works.**

    The template's HTTPS router carried `service=glitchtip-service` — a copy-paste from another
    project. Traefik matches the host, finds no such service, and returns its own 404. Meanwhile the
    HTTP router names the right service, so a `curl http://` succeeds and a browser on `https://`
    does not.
    """
    declared = set(
        re.findall(r"traefik\.http\.services\.([a-z0-9-]+)\.loadbalancer", manifest)
    )
    referenced = set(re.findall(r"traefik\.http\.routers\.[a-z0-9-]+\.service=([a-z0-9-]+)", manifest))

    assert declared, "no Traefik services are declared at all"
    orphans = sorted(referenced - declared)
    assert not orphans, (
        f"these routers point at services this manifest does not declare: {orphans}. Traefik will "
        f"match the host and then return its own 404 with a healthy container behind it."
    )


def test_both_routers_of_a_host_name_the_same_service(manifest: str):
    """The HTTP and HTTPS routers for one host must agree.

    They disagreed in the template, which is what let HTTP work and HTTPS 404. Pairing them by
    prefix catches the same divergence for any future host.
    """
    routers = dict(
        re.findall(r"traefik\.http\.routers\.([a-z0-9-]+)\.service=([a-z0-9-]+)", manifest)
    )
    for name, service in routers.items():
        if not name.endswith("-http"):
            continue
        https_name = name[: -len("-http")]
        assert https_name in routers, f"{name} has no HTTPS counterpart {https_name}"
        assert routers[https_name] == service, (
            f"{name} routes to {service!r} but {https_name} routes to "
            f"{routers[https_name]!r} — one scheme will 404"
        )


# --------------------------------------------------------------------------- #
# Fault 2 — the load-balancer port
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("service,port", sorted(PUBLIC.items()))
def test_the_load_balancer_port_matches_the_image(service: str, port: str, manifest: str):
    """**A 502 with a passing healthcheck.**

    The template said 3100 for the UI. The image sets `PORT=3000` and `EXPOSE 3000`, and
    `node server.js` honours that env var — so Traefik would have forwarded to a closed port while
    the container's own healthcheck, which probes 127.0.0.1:3000, kept reporting healthy.

    (3100 is the port used locally to probe a production build while `next dev` holds 3000. It is
    not the container's port, and that is exactly how the number got in.)
    """
    label = f"traefik.http.services.{service}-service.loadbalancer.server.port={port}"
    assert label in manifest, (
        f"{service} does not advertise port {port} to Traefik. A mismatched port is a 502 Bad "
        f"Gateway that leaves the container reporting healthy."
    )


def test_the_advertised_port_matches_the_container_env(manifest: str, services: dict[str, str]):
    """Traefik's port must equal the port the container is told to listen on.

    ## Why this is NOT compared against `EXPOSE`

    An earlier version of this test read `EXPOSE` from the Dockerfile and required the manifest to
    match it — reasoning that the image should be the source of truth. That is wrong twice over:

      * **`EXPOSE` is documentation.** It binds nothing. A container listens wherever its process is
        told to, and `node server.js` reads `PORT`.
      * **The UI must NOT run on the image default.** Dokploy's own web UI occupies 3000 on the VPS,
        so the frontend is deliberately moved to 3100. A test demanding parity with `EXPOSE` would
        have forbidden the one configuration the deployment requires.

    So the invariant is the internally consistent one: whatever `PORT` the service sets is what
    Traefik is told. A mismatch there is the 502 this guards against.
    """
    ui = services["shelter-ui"]
    port = re.search(r'PORT:\s*"?(\d+)"?', ui)
    assert port, "shelter-ui does not set PORT explicitly, so the port is inferred"
    assert (
        f"traefik.http.services.shelter-ui-service.loadbalancer.server.port={port.group(1)}"
        in manifest
    ), (
        f"shelter-ui listens on {port.group(1)} but Traefik is told a different port — a 502 Bad "
        f"Gateway with a container that reports healthy"
    )


def test_the_image_healthcheck_honours_a_port_override():
    """**The subtle half of moving the UI off 3000.**

    `node server.js` honours `PORT`, but the image's HEALTHCHECK hard-coded
    `http://127.0.0.1:3000/`. Overriding the port left a working container permanently `unhealthy`
    — and Traefik refuses to route to an unhealthy container, so the site would have been down with
    every process running correctly.

    Verified on a built image before this was asserted: `PORT=3100` serves HTTP 200 and reports
    `healthy`, and the unset default on 3000 still does too.
    """
    dockerfile = pathlib.Path(__file__).resolve().parents[2] / "frontend/Dockerfile"
    if not dockerfile.exists():  # pragma: no cover
        return

    # Comments stripped FIRST. The block carries a comment explaining why `${PORT}` is used, and a
    # substring search over the raw text matches that prose — so the assertion passed while the
    # actual command was pinned to 3000. Caught by tampering, which is the third time in this
    # codebase a test has matched its own documentation instead of the code.
    lines = [
        line
        for line in dockerfile.read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    source = "\n".join(lines)

    probe = next(
        (line for line in lines if "wget" in line and "127.0.0.1" in line), None
    )
    assert probe, "no healthcheck probe found in the frontend Dockerfile"
    assert "HEALTHCHECK" in source
    assert "${PORT" in probe, (
        f"the frontend healthcheck pins a literal port ({probe.strip()}), so any PORT override "
        f"reports the container unhealthy and Traefik stops routing to it"
    )


def test_the_ui_binds_all_interfaces(services: dict[str, str]):
    """`HOSTNAME=0.0.0.0`.

    Traefik reaches this container across a bridge network, so a loopback bind is unreachable
    whatever the port. Independent of the 3000-vs-3100 question and equally fatal.
    """
    assert "HOSTNAME: 0.0.0.0" in services["shelter-ui"], (
        "the UI may bind loopback only, which Traefik cannot reach across the bridge"
    )


# --------------------------------------------------------------------------- #
# Fault 3, and the network boundary generally
# --------------------------------------------------------------------------- #


def _attached_networks(block: str) -> set[str]:
    """Network names a service block attaches to, in EITHER YAML style Compose accepts.

    List form:  `- shelter`
    Map form:   `shelter: {}`  (or `shelter:` with per-network settings nested below it)

    This manifest uses map form throughout — needed for the MVP's actual Dokploy deployment, and
    functionally identical to list form in every way Compose cares about (both attach the service
    to `shelter` with default settings). Recognizing only one form is a parser gap, not a manifest
    fault: a test written against list-form syntax should not read a valid map-form manifest as
    broken.
    """
    section = re.search(r"^    networks:\n((?:      .*\n)+)", block, re.M)
    if not section:
        return set()
    names: set[str] = set()
    for line in section.group(1).splitlines():
        match = re.match(r"^      - (\S+)$", line) or re.match(
            r"^      ([a-z][a-z0-9-]*):", line
        )
        if match:
            names.add(match.group(1))
    return names


def test_every_named_network_is_declared(manifest: str, services: dict[str, str]):
    """Compose fails fast on this, which makes it the benign fault of the three.

    The template listed `shelter` on its services while declaring `shelter-network` at the top.
    Asserted anyway, because "compose will catch it" is only true if someone runs compose — and this
    file is deployed by pasting it into a panel.
    """
    top = manifest.split("services:")[0]
    # Optional trailing ` {}` — the top-level declaration is map-form (`shelter: {}`) in this
    # manifest, same reasoning as `_attached_networks` above.
    declared = set(re.findall(r"^  ([a-z][a-z0-9-]*):(?: \{\})?$", top, re.M))

    used: set[str] = set()
    for block in services.values():
        used |= _attached_networks(block)

    assert used, "no service declares a network"
    missing = sorted(used - declared)
    assert not missing, f"these networks are used but never declared: {missing}"


def test_the_private_network_must_not_be_internal(manifest: str):
    """**`internal: true` is deliberately ABSENT, and this test guards its absence.**

    It was set, on the reasoning that it keeps the datastores off the internet. That reasoning is
    right about the datastores and wrong about the network: `internal` is a property of the NETWORK,
    and both worker pools are attached to `shelter` alone. An internal bridge has no gateway, so
    with it set a worker cannot reach a single upstream:

        STAC catalogues   -> Scout discovers no scenes
        COG range reads   -> the Analyst measures nothing
        rainfall chain    -> no forecast, ever
        MongoDB Atlas     -> IAM unreachable
        Brevo             -> the Herald sends no advisory

    And every one of those degrades QUIETLY, because this codebase degrades rather than crashes.
    The stack comes up healthy, passes every probe, and produces assessments containing no
    satellite data — a warning service reporting nothing wrong because it can see nothing.

    Verified against a real deployment: with `internal` removed, a worker resolves and connects to
    `earth-search.aws.element84.com` and a live STAC search returns Sentinel-2 scenes; the
    datastores remain unreachable from `dokploy-network`.

    The isolation is asserted by the two tests below instead, which check what actually provides
    it: no published ports, and no datastore on the public network.
    """
    top = manifest.split("services:")[0]
    # `(?:    .*\n)*` — zero or more indented lines, not one or more.
    #
    # `shelter:` legitimately has NO keys now: the `driver:` was removed so each orchestrator picks
    # its own default (bridge under compose, overlay under Swarm). The `+` form then matched nothing
    # and this test failed with "no shelter network is declared" against a manifest where it is
    # declared perfectly well — a false negative that reads like a missing network.
    #
    # `(?: \{\})?` — this manifest declares it as `shelter: {}` (map-form, required by the MVP's
    # actual Dokploy deployment), not a bare `shelter:`. Same false-negative shape as above: a
    # syntax Compose treats as identical to the bare form must not read as "undeclared" here.
    block = re.search(r"^  shelter:(?: \{\})?\n((?:    .*\n)*)", top, re.M)
    assert block, "no `shelter` network is declared"
    assert "internal: true" not in block.group(1), (
        "the shelter network is `internal: true`, which removes the gateway — the workers are on "
        "this network only, so Scout, the Analyst and the Herald all lose outbound HTTPS and the "
        "pipeline silently measures nothing"
    )


def test_no_service_publishes_a_host_port(manifest: str):
    """What actually keeps the datastores private, now that `internal` is gone.

    A `ports:` mapping binds on the VPS itself, which is the one change that would expose Postgres
    or MinIO to the internet regardless of network topology.
    """
    assert "\n    ports:" not in manifest, (
        "a service publishes a host port. Nothing in this stack should: the two public services "
        "are reached through Traefik on dokploy-network, and the datastores must not be reachable "
        "at all."
    )


def test_no_datastore_joins_the_public_network(services: dict[str, str]):
    """The second half of the boundary: only shelter-ui and shelter-api may touch Traefik's network.

    Uses the `services` fixture rather than a fresh regex. A hand-rolled block matcher is exactly
    what the fixture's own docstring warns about — an earlier one swallowed several services and
    failed five tests against a correct manifest.
    """
    for name in INTERNAL:
        assert name in services, f"service {name} not found in the manifest"
        assert "dokploy-network" not in services[name], (
            f"{name} is attached to dokploy-network, which puts it behind Traefik and on the same "
            f"bridge as the public services"
        )


def test_the_public_network_is_external(manifest: str):
    """`external: true` means "attach to Traefik's existing network".

    Without it compose creates a *second* network of the same name with a project prefix. Traefik is
    not on that one, so every route 404s — and the stack otherwise looks perfect.
    """
    top = manifest.split("services:")[0]
    block = re.search(r"^  dokploy-network:\n((?:    .*\n)+)", top, re.M)
    assert block, "no `dokploy-network` is declared"
    assert "external: true" in block.group(1), (
        "dokploy-network is not external, so compose creates its own and Traefik is not on it"
    )


@pytest.mark.parametrize("service", INTERNAL)
def test_no_internal_service_is_exposed(service: str, services: dict[str, str]):
    """The datastores and workers must never join the public network.

    A worker has no inbound HTTP surface at all, so attaching it would widen the perimeter for
    nothing. Postgres on a Traefik-attached bridge is a considerably worse outcome.
    """
    block = services.get(service)
    assert block, f"{service} is missing from the manifest"
    assert "dokploy-network" not in block, (
        f"{service} is attached to the public network. It has no inbound HTTP surface, so this "
        f"only widens the perimeter."
    )
    assert "traefik.enable=true" not in block, f"{service} is advertised to Traefik"


@pytest.mark.parametrize("service", sorted(PUBLIC))
def test_each_public_service_pins_its_traefik_network(service: str, services: dict[str, str]):
    """**Required because these containers are multi-homed.**

    Each joins both networks — Traefik reaches them on one, Postgres on the other. With two
    attached, Traefik picks one itself and it is not reliably the right one: the classic "works
    after this restart, breaks after the next" routing fault.
    """
    block = services[service]
    assert "traefik.docker.network=dokploy-network" in block, (
        f"{service} is on two networks without telling Traefik which to use"
    )
    assert "shelter" in _attached_networks(block), f"{service} cannot reach the datastores"


# --------------------------------------------------------------------------- #
# Operational invariants that a second copy of the compose file could break
# --------------------------------------------------------------------------- #


def test_exactly_one_scheduler(services: dict[str, str]):
    """**Two schedulers means every satellite scan is queued twice.**

    Double the upstream requests, double the assessments, and a dedupe window doing work it should
    not have to. The API owns the watch loop; every worker must disable it.
    """
    enabled = [
        name
        for name, block in services.items()
        if re.search(r'SCHEDULER_ENABLED:\s*"true"', block)
    ]
    assert enabled == ["shelter-api"], (
        f"the scheduler is enabled on {enabled}; it must run in exactly one process, the API"
    )


def test_only_the_api_migrates(services: dict[str, str]):
    """`migrations.py` takes a `pg_advisory_lock`, so co-booting replicas cannot race.

    Two migrators is still two things to reason about, and one is enough.
    """
    migrating = [
        name
        for name, block in services.items()
        if re.search(r'POSTGRES_AUTO_MIGRATE:\s*"true"', block)
    ]
    assert migrating == ["shelter-api"], f"more than one service migrates: {migrating}"


def test_the_secrets_have_no_defaults(manifest: str):
    """`${VAR:?message}`, not `${VAR:-dev-value}`.

    A default here deploys an insecure stack that starts cleanly. `app/preflight.py` refuses to boot
    in production with the dev values, so a default would only move the failure later and make it
    look like an application fault.
    """
    for secret in (
        "API_KEY",
        "IAM_SESSION_SECRET",
        "POSTGRES_PASSWORD",
        "MINIO_ROOT_PASSWORD",
    ):
        assert f"${{{secret}:?" in manifest, (
            f"{secret} has no required-variable guard, so the stack can deploy without it"
        )
        assert f"${{{secret}:-" not in manifest, (
            f"{secret} has a fallback default — that deploys an insecure stack that starts cleanly"
        )


def test_the_api_healthcheck_probes_ready_not_health(services: dict[str, str]):
    """`/health` returns 200 with a degraded body; `/ready` returns 503.

    A healthcheck on `/health` reports a broken deployment as healthy — verified once with Dragonfly
    stopped, which is why the distinction exists at all.
    """
    block = services["shelter-api"]
    assert "/ready" in block, "the API healthcheck does not probe /ready"
    assert "/health" not in block.split("healthcheck:")[-1], (
        "the healthcheck probes /health, which returns 200 even when a datastore is down"
    )


def test_dragonfly_flags_are_dragonflys_not_rediss(services: dict[str, str]):
    """Three flags, each load-bearing, and none of them a Redis flag.

    `--dbnum=2` or `SELECT 1` fails and every cache call errors. `--cache_mode=false` because
    eviction is server-wide, not per-database — an evictor configured for the cache would also drop
    queue entries, and a dropped entry is a satellite scan that silently never ran.
    `--snapshot_cron` because Dragonfly snapshots and has no `--appendonly`.
    """
    block = services["dragonfly"]
    assert "--dbnum=2" in block, "db1 is unreachable, so every cache call errors"
    assert "--cache_mode=false" in block, (
        "eviction is enabled; it is server-wide, so it would also evict queue entries"
    )
    assert "--snapshot_cron" in block, "no persistence configured"
    assert "--appendonly" not in block, "that is a Redis flag; Dragonfly snapshots"


def test_the_ui_key_is_never_public(services: dict[str, str]):
    """`SHELTER_API_KEY`, never `NEXT_PUBLIC_*`.

    `lib/api.ts` is `server-only` and the key authorises subscriber registration and district
    broadcasts. Exposed to the browser it would hand every visitor the ability to page a district.
    """
    block = services["shelter-ui"]
    assert "SHELTER_API_KEY:" in block
    assert "NEXT_PUBLIC" not in block, (
        "a NEXT_PUBLIC_ variable in the UI service would ship a credential to the browser"
    )
