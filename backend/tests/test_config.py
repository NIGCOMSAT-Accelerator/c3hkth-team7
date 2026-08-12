"""Configuration contract.

The promise this file enforces: **every key in `.env.example` does something,
and everything that does something is in `.env.example`.**

Without these checks the two drift apart silently — someone fills in a key that
no code reads and wonders why nothing changed, or a new setting ships with no
documentation and is only discoverable by reading the source.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from app.config import Settings

BACKEND = pathlib.Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
ENV_EXAMPLE = REPO / ".env.example"

#: Consumed via the `cors_origin_list` property inside config.py itself, so it
#: never appears as `settings.cors_origins` elsewhere in the tree.
CONSUMED_INTERNALLY = {"cors_origins"}


def _app_source() -> str:
    return "\n".join(
        path.read_text()
        for path in (BACKEND / "app").rglob("*.py")
        if path.name != "config.py"
    )


def _env_example_keys() -> set[str]:
    text = ENV_EXAMPLE.read_text()
    return {
        match.group(1).lower()
        for match in re.finditer(r"^([A-Z][A-Z0-9_]*)=", text, re.MULTILINE)
    }


def test_env_example_exists():
    assert ENV_EXAMPLE.is_file(), "the repo must ship a .env.example"


def test_no_orphaned_settings():
    """Every setting must be read somewhere.

    A key that nothing consumes is worse than no key: it invites someone to
    configure a data source that will never actually be queried.
    """
    source = _app_source()
    orphans = sorted(
        name
        for name in Settings.model_fields
        if name not in CONSUMED_INTERNALLY and f"settings.{name}" not in source
    )
    assert not orphans, (
        "these settings are declared but never read — wire them up or delete "
        f"them: {orphans}"
    )


def test_every_setting_is_documented_in_env_example():
    """A setting absent from the template is undiscoverable without grepping."""
    documented = _env_example_keys()
    missing = sorted(
        name for name in Settings.model_fields if name not in documented
    )
    assert not missing, f"settings missing from .env.example: {missing}"


#: Keys in `.env.example` that are deliberately NOT pydantic settings.
#:
#: These are read by `docker-entrypoint.sh` before Python starts, so they cannot be
#: `Settings` fields — the process they configure does not exist yet when
#: `app/config.py` is imported. They are documented in `.env.example` anyway because
#: an operator tuning the deployment needs them in one place.
#:
#: The list is short and closed on purpose. Anything added here escapes the typo
#: check below, so a new entry must genuinely be consumed by the shell.
PROCESS_LEVEL_KEYS: dict[str, str] = {
    "web_concurrency": (
        "uvicorn worker count. Read by docker-entrypoint.sh, which refuses >1 while "
        "SCHEDULER_ENABLED=true because the watch loop would run once per worker."
    ),
    "forwarded_allow_ips": (
        "uvicorn --forwarded-allow-ips. Which proxy sources may set X-Forwarded-*."
    ),
    "graceful_timeout": (
        "uvicorn --timeout-graceful-shutdown. Seconds to drain in-flight requests."
    ),
}


#: Keys consumed by `docker-compose.yml` itself, not by the application or the entrypoint.
#:
#: A SEPARATE list from `PROCESS_LEVEL_KEYS` rather than an addition to it, because the two
#: are verified differently: those must appear in `docker-entrypoint.sh`, these must appear
#: in `docker-compose.yml`. Merging them would mean weakening whichever assertion is
#: stricter, which is how an exemption list stops catching anything.
RELEASE_LEVEL_KEYS: dict[str, str] = {
    "shelter_registry_host": (
        "Registry hostname remembered by scripts/release.sh so a second interactive "
        "release defaults to the same target instead of re-asking."
    ),
    "shelter_org": (
        "Registry organisation or user, remembered by scripts/release.sh. Inferred from "
        "git remote on first run."
    ),
    "shelter_repo": (
        "Image repository name, remembered by scripts/release.sh so the tag prompt "
        "defaults correctly on the next release."
    ),
}


def test_release_level_keys_are_read_by_the_release_script():
    """Same anti-staleness rule as the entrypoint and compose exemptions.

    These are written and read by `scripts/release.sh` alone. If the script stops touching
    one, the key is an orphan in `.env.example` and the typo check should catch it again.
    """
    script = (pathlib.Path("..") / "scripts" / "release.sh").read_text()

    missing = [key for key in RELEASE_LEVEL_KEYS if key.upper() not in script]
    assert not missing, (
        f"exempted as release-level but scripts/release.sh never references them: "
        f"{missing}. Either wire them up or remove the exemption."
    )

    for key, reason in RELEASE_LEVEL_KEYS.items():
        assert len(reason) > 40, f"{key} needs a real explanation of why it is exempt"


COMPOSE_LEVEL_KEYS: dict[str, str] = {
    "shelter_image": (
        "Container image reference for api/worker/worker-analyst. Unset means compose "
        "builds locally and tags shelter-api; set it to run a pulled registry image."
    ),
    "shelter_tag": (
        "Image tag paired with SHELTER_IMAGE. Defaults to latest so a fresh clone with "
        "no .env resolves to the locally built image."
    ),
    "frontend_port": (
        "Host port for the portal under `--profile ui`. Overridable because 3000 is the "
        "commonest occupied port on a developer machine — `next dev`, Grafana and Dokploy's "
        "own UI all take it — and a bind failure aborts the whole `up` with an error that "
        "names Docker rather than the conflict."
    ),
    "shelter_api_key": (
        "The scoped service-account key the frontend container presents. Read by compose "
        "into the frontend service's environment, not by pydantic — the BACKEND never reads "
        "it, which is the whole point: it is the credential the portal sends, not one the "
        "API holds."
    ),
    "next_public_site_url": (
        "Baked into the frontend image at BUILD time as a compose build arg. NEXT_PUBLIC_* "
        "is inlined by Next.js at compile time, so it cannot be injected at run time and is "
        "not a backend setting at all."
    ),
}


def test_compose_level_keys_are_read_by_compose():
    """The same anti-staleness check as the entrypoint exemptions, for compose vars.

    An entry here claims compose interpolates it. If nothing in the compose file
    references it, the key is an orphan wearing an exemption — exactly what the typo
    check exists to catch.
    """
    compose = (pathlib.Path("..") / "docker-compose.yml").read_text()

    missing = [key for key in COMPOSE_LEVEL_KEYS if key.upper() not in compose]
    assert not missing, (
        f"these are exempted as compose-level but docker-compose.yml never references "
        f"them: {missing}. Either wire them up or remove the exemption."
    )

    for key, reason in COMPOSE_LEVEL_KEYS.items():
        assert len(reason) > 40, f"{key} needs a real explanation of why it is exempt"


def test_env_example_has_no_unknown_keys():
    """Catches typos — `WORLDPOP_YAER=` would otherwise be silently ignored,
    because pydantic-settings is configured with `extra='ignore'`."""
    known = (
        set(Settings.model_fields)
        | set(PROCESS_LEVEL_KEYS)
        | set(COMPOSE_LEVEL_KEYS)
        | set(RELEASE_LEVEL_KEYS)
    )
    unknown = sorted(key for key in _env_example_keys() if key not in known)
    assert not unknown, f".env.example keys that match no setting: {unknown}"


def test_process_level_keys_are_read_by_the_entrypoint():
    """The exemption list must not become a dumping ground.

    Each entry claims to be consumed by the shell rather than by pydantic. This
    verifies that claim, so a setting cannot be parked here to dodge the typo check
    while nothing reads it — the same staleness trap the schema-contract allow-lists
    guard against.
    """
    entrypoint = pathlib.Path("docker-entrypoint.sh").read_text()

    missing = [
        key for key in PROCESS_LEVEL_KEYS if key.upper() not in entrypoint
    ]
    assert not missing, (
        f"these are exempted as process-level but the entrypoint never reads them: "
        f"{missing}. Either wire them up or remove the exemption."
    )

    for key, reason in PROCESS_LEVEL_KEYS.items():
        assert len(reason) > 40, f"{key} needs a real explanation of why it is exempt"


def test_process_level_keys_are_documented_in_env_example():
    """An operator tuning workers or proxy trust must find them with the rest."""
    documented = _env_example_keys()
    missing = [key for key in PROCESS_LEVEL_KEYS if key not in documented]
    assert not missing, f"process-level keys missing from .env.example: {missing}"


def test_no_secret_has_a_default_value():
    """Credentials must default to None.

    A baked-in default would make a missing credential look configured, and the
    feature would fail at delivery time instead of being cleanly skipped.
    """
    # Suffix match only. A substring test would flag `advisory_max_tokens`,
    # which is a token *count*, not a credential.
    secret_suffixes = ("_key", "_token", "_secret", "_password", "client_id")
    offenders = [
        name
        for name, field in Settings.model_fields.items()
        if name.endswith(secret_suffixes) and field.default not in (None, "")
    ]
    assert not offenders, f"credentials must default to None: {offenders}"


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("collection_s2", "sentinel-2-l2a"),
        ("collection_s1", "sentinel-1-rtc"),
        ("collection_dem", "cop-dem-glo-30"),
        ("collection_worldcover", "esa-worldcover"),
    ],
)
def test_catalogue_collection_defaults(field, expected):
    """Regression guard for the silent-failure bug: a wrong collection ID returns an EMPTY
    feature list rather than an error, so a typo here is invisible until nothing is ever found."""
    assert Settings.model_fields[field].default == expected


@pytest.mark.parametrize(
    "field", ["collection_s2_copernicus", "collection_s1_copernicus"]
)
def test_copernicus_sentinel_collections_are_empty(field):
    """**Empty on purpose — this endpoint serves no Sentinel data.**

    Verified live 2026-08-10: `/stac/collections` returns ten collections (`ccm-optical`,
    `ccm-sar`, eight CLMS burnt-area products) and a Sentinel search returns
    `HTTP 400 CollectionInQuerryDoesNotExist`. The previous defaults `SENTINEL-2` / `SENTINEL-1`
    were wrong, and the CLAUDE.md invariant built on them was wrong too.

    Empty matters behaviourally: `catalogs.chain_for` skips a catalogue whose `collection_for` is
    falsy, so this removes a guaranteed-failing request from every scan. A non-empty default here
    would reintroduce that waste — and imply Copernicus can answer, which it cannot.

    Sentinel access at Copernicus is via OData/OpenSearch, a separate adapter that is not built.
    """
    assert Settings.model_fields[field].default == ""


def test_severity_threshold_setting_is_a_valid_severity():
    """`NIGCOMSAT_ALWAYS_BROADCAST_AT` is a free-text string in the env; a typo
    would silently fall back to WARNING at runtime."""
    from app.models.enums import Severity

    default = Settings.model_fields["nigcomsat_always_broadcast_at"].default
    assert default in {s.value for s in Severity}
