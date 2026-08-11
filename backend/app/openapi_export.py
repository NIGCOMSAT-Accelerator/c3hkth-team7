"""Export the OpenAPI schema to a file — `python -m app.openapi_export`.

## Why export at all, when FastAPI already serves `/openapi.json`

The served document requires a running stack. That makes three things impossible:

1. **Reviewing an API change in a diff.** Adding a route, renaming a field or
   widening a type currently shows up nowhere in a pull request. A committed
   `openapi.json` makes every contract change visible next to the code that caused
   it — which matters most for the aggregator API, where a partner's integration
   breaks silently if a field is removed.
2. **Generating a client offline.** A partner running `openapi-generator` should not
   need our Postgres, Dragonfly and MinIO to be up first.
3. **Detecting an *accidental* change.** `--check` fails when the committed file is
   stale, so an unintended contract change fails CI instead of reaching a consumer.

## Why this does not import the app the obvious way

`app.main` imports the scheduler, which imports the agent pipeline, which imports
`rasterio` and `torch`. Building the schema needs none of that — only the route
definitions — but importing `app.main` drags in the whole geospatial stack. That is
fine inside the container and fatal in a lightweight CI job.

So the routers are imported directly and mounted on a bare `FastAPI()`. The routers
themselves only need `pydantic` and the models, which is why `app/api/routes/*.py`
keeps its heavy imports inside handler bodies. **A regression there would break this
exporter first**, which makes it a useful canary for that discipline.

## Determinism

`sort_keys=True` and a trailing newline. Without sorting, Python's dict ordering
would produce a different file on a different interpreter or after an unrelated edit,
and every export would show as a diff — which trains people to ignore the diff, and
then a real contract change slips through.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.config import Settings

# The description and tag groupings, imported rather than restated.
#
# This module used to carry a THIRD copy, and it had drifted: the committed `openapi.json` —
# the file a partner generates a client from — was the poorest of the three, missing the
# setup guidance and every tag description.
#
# Importing `app.main` is safe here for the same reason the routers are: nothing in it pulls
# the geospatial stack in at module scope, which is the constraint this exporter exists to
# respect.
from app.main import API_DESCRIPTION, API_TAGS

#: Where the committed schema lives.
#:
#: Repo root rather than `backend/`, because it is a contract for consumers — the
#: frontend, partner integrations, `openapi-generator` — not a backend implementation
#: detail. Anyone looking for the API surface should find it without knowing the
#: internal layout.
DEFAULT_PATH = Path(__file__).resolve().parents[2] / "openapi.json"

#: The prefix the committed document describes.
#:
#: Pinned to the shipped default rather than read from the environment: otherwise the
#: exported file depends on whoever ran the export, and `--check` would flip-flop
#: between a developer whose .env still says `/api/v1` and CI. Read from the field
#: default so it cannot drift from `config.py`.
CANONICAL_PREFIX = Settings.model_fields["api_prefix"].default


def build_schema() -> dict:
    """The OpenAPI document, built from the route definitions alone.

    Deliberately does not import `app.main`: that pulls in the scheduler, the agent
    pipeline, rasterio and torch, none of which the schema needs. The trade-off is
    that the app-level metadata below has to be restated here rather than inherited —
    and `tests/test_openapi.py` asserts the two agree, so they cannot drift.
    """
    from fastapi import FastAPI

    from app.api.routes import (
    alerts,
    chat,
    devdocs,
    health,
    iam,
    places,
    risk,
    subscribers,
    verification,
    webhooks,
)
    from app.config import settings
    # The committed spec must not vary with whoever ran the export.
    #
    # `API_PREFIX` is configurable, so exporting on a machine whose .env still says
    # `/api/v1` produces a document describing paths the production service does not
    # serve — and the freshness check would then flip-flop between developers. The
    # canonical prefix is pinned to the *default* rather than the local value, so the
    # committed contract always describes a standard deployment.
    #
    # An operator who genuinely runs a custom prefix reads the live
    # `/openapi.json`, which is generated from their own settings.
    prefix = CANONICAL_PREFIX

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        # Imported from `app.main` rather than restated. This file used to carry its own
        # third copy of the description, and it had already drifted: the committed
        # `openapi.json` — the file a partner actually generates a client from — was the
        # poorest of the three, missing the setup guidance and every tag description.
        #
        # `app.main` imports no geospatial code at module scope, so this does not compromise
        # the reason the exporter builds its own bare app.
        description=API_DESCRIPTION,
        openapi_tags=API_TAGS,
        docs_url=f"{prefix}/docs",
        openapi_url=f"{prefix}/openapi.json",
    )

    for router in (
        health, subscribers, risk, alerts, chat, verification, webhooks, iam, devdocs,
        places,
    ):
        app.include_router(router.router, prefix=prefix)

    schema = app.openapi()

    # Security schemes come from the routers now, not from a hand-written block here.
    #
    # There WAS one, and it existed for a real reason: FastAPI infers nothing from a
    # `Header()` dependency, so the exported document showed every guarded endpoint as
    # public. Hand-declaring them was a reasonable workaround at the time.
    #
    # It is now obsolete and was actively wrong. The guards use `Security(...)`
    # (`app/api/security_schemes.py`), so `app.openapi()` emits the real schemes —
    # `AggregatorApiKey`, `PlatformApiKey`, `PortalSession`, `LegacySharedKey` — and the block
    # here overwrote them with a scheme called `ServiceAccountKey` that nothing declares. The
    # committed file therefore carried a phantom name and lost the two real API-key schemes,
    # while every operation referenced names the document did not define.
    #
    # The freshness check compares PATHS, so it passed throughout. That is why this survived:
    # a contract check that ignores the auth model cannot notice the auth model breaking.

    return schema


def write(path: Path, schema: dict) -> None:
    path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.openapi_export", description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_PATH)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the file on disk differs from the current routes. "
        "For CI: an unintended contract change should fail the build, not reach a "
        "consumer.",
    )
    args = parser.parse_args(argv)

    try:
        schema = build_schema()
    except Exception as exc:
        print(f"could not build the schema: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    rendered = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    paths = schema.get("paths", {})
    operations = sum(
        1
        for methods in paths.values()
        for method in methods
        if method in ("get", "post", "put", "patch", "delete")
    )

    if args.check:
        if not args.out.exists():
            print(f"{args.out} does not exist. Run `make openapi`.", file=sys.stderr)
            return 1
        if args.out.read_text() != rendered:
            print(
                f"{args.out.name} is stale — the routes have changed since it was "
                f"exported.\nRun `make openapi` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print(f"{args.out.name} is current ({len(paths)} paths, {operations} operations)")
        return 0

    write(args.out, schema)
    print(f"wrote {args.out}")
    print(f"  {len(paths)} paths, {operations} operations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
