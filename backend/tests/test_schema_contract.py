"""Schema contract — the table-level analogue of `test_config.py`.

`test_config.py` enforces that every *setting* is read somewhere and documented.
Nothing enforced the same for *tables*, and the gap produced a real defect:
`agent_memory` shipped in migration 003 with HNSW and GiST indexes while nothing
read or wrote it, and three docstrings claimed Fahis wrote to it. Both the orphan
and the false claims went unnoticed because no test could see either.

The promise this file makes: **every table a migration creates is either reached
from Python, or explicitly declared as not-yet-reachable with a reason.**

An orphaned table is worse than a missing one. A missing table fails loudly on
first use; an orphan looks like a working feature — it has indexes, it has a
migration, it reviews as done — and quietly is not. That is exactly the failure
mode `test_config.py` was written to eliminate for settings, so the reasoning
carries over unchanged.

The allow-list is the point of the design, not a weakness in it. Provisioning a
table one release before the code that fills it is legitimate; doing so *silently*
is not. An entry here is a declaration with a name attached.
"""

from __future__ import annotations

import pathlib
import re

import pytest

MIGRATIONS = pathlib.Path("app/db/migrations")
APP = pathlib.Path("app")

#: Tables intentionally created ahead of the code that will use them.
#:
#: Each entry must say what will fill it and what is missing. Removing an entry
#: when the feature lands is part of landing the feature — a stale entry here
#: re-opens the exact hole this file closes.
KNOWN_UNREACHED: dict[str, str] = {
    "advisory_embeddings": (
        "RAG retrieval surface. Provisioned with HNSW + GiST in migration 003, but "
        "nothing writes or queries it yet: the embedding call exists "
        "(`llm/embeddings.py`) and is currently used only for chat-history "
        "retrieval. Blocked on deciding what the operator console retrieves — and "
        "deliberately NOT wired into the advisory path, because retrieved prose "
        "reaching a generated advisory is the failure the grounding rule prevents."
    ),
}

#: Created and managed entirely inside `db/migrations.py`'s own bookkeeping, so it
#: never appears in a `.sql` file and never needs application access.
LEDGER_TABLES = {"schema_migrations"}


def _created_tables() -> set[str]:
    """Every table a migration file creates."""
    tables: set[str] = set()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        tables.update(
            re.findall(
                r"CREATE TABLE IF NOT EXISTS\s+([a-z_][a-z0-9_]*)",
                path.read_text(),
                re.IGNORECASE,
            )
        )
    return tables


def _python_source() -> str:
    """All application Python, excluding the migration runner.

    The runner reads `.sql` files and manages its own ledger; including it would
    let a table appear "reached" purely because its DDL is executed.
    """
    return "\n".join(
        path.read_text()
        for path in sorted(APP.rglob("*.py"))
        if "db/migrations" not in str(path) and path.name != "migrations.py"
    )


def _tables_in_sql_statements(source: str) -> set[str]:
    """Tables named in SQL that application code actually executes.

    Matches the clauses that read or write — `FROM`, `JOIN`, `INTO`, `UPDATE`,
    `DELETE FROM` — rather than any mention of the name. A table referenced only
    in a prose docstring must not count as reached; that conflation is precisely
    what let the `agent_memory` claims survive.
    """
    patterns = (
        r"\bFROM\s+([a-z_][a-z0-9_]*)",
        r"\bJOIN\s+([a-z_][a-z0-9_]*)",
        r"\bINTO\s+([a-z_][a-z0-9_]*)",
        r"\bUPDATE\s+([a-z_][a-z0-9_]*)",
    )
    found: set[str] = set()
    for pattern in patterns:
        found.update(m.lower() for m in re.findall(pattern, source, re.IGNORECASE))
    return found


# --------------------------------------------------------------------------- #
# The contract
# --------------------------------------------------------------------------- #


def test_every_table_is_reached_or_declared():
    """The core guard.

    A table with no read and no write is a feature that reviews as done and isn't.
    """
    created = _created_tables()
    assert created, "no CREATE TABLE statements found — check the glob"

    reached = _tables_in_sql_statements(_python_source())

    orphans = sorted(
        table
        for table in created
        if table not in reached
        and table not in KNOWN_UNREACHED
        and table not in LEDGER_TABLES
    )

    assert not orphans, (
        "these tables are created by a migration but never read or written from "
        f"Python: {orphans}. Wire them up, or add an entry to KNOWN_UNREACHED "
        "explaining what will fill them and what is blocking it."
    )


def test_known_unreached_entries_are_still_unreached():
    """The allow-list must not go stale.

    Once a table is genuinely wired, its entry has to go — otherwise the exemption
    silently covers a future regression in code that currently works.
    """
    reached = _tables_in_sql_statements(_python_source())

    stale = sorted(table for table in KNOWN_UNREACHED if table in reached)

    assert not stale, (
        f"these tables are now reached from Python: {stale}. Remove them from "
        "KNOWN_UNREACHED — the exemption is hiding real coverage."
    )


def test_known_unreached_entries_exist_as_tables():
    """Guards the other direction: an entry naming a table that no longer exists
    is dead weight that makes the exemption list untrustworthy."""
    created = _created_tables()

    phantom = sorted(table for table in KNOWN_UNREACHED if table not in created)

    assert not phantom, (
        f"KNOWN_UNREACHED names tables no migration creates: {phantom}"
    )


def test_every_exemption_carries_a_reason():
    """An exemption without a stated blocker is just a silenced test."""
    for table, reason in KNOWN_UNREACHED.items():
        assert len(reason) > 80, (
            f"the exemption for {table!r} needs a real explanation of what will "
            "fill it and what is blocking that"
        )


# --------------------------------------------------------------------------- #
# Docstrings must not claim reachability a table does not have
# --------------------------------------------------------------------------- #


#: Public functions that exist ahead of their caller. Each must say so in its own
#: docstring — the test below checks that, so a reader of the function learns it is
#: inert without having to grep for callers.
#:
#: Same reasoning as KNOWN_UNREACHED: shipping the storage half of a feature early
#: is fine, letting it read as finished is not.
#: `recall` is deliberately absent: `corrections_for` delegates to it, so it has a
#: real caller. Only the outermost entry points are listed — a helper reached from
#: one of them is exercised, even if the whole chain is currently dormant.
#: `imagery_key` is deliberately absent: the stateful Scout now calls it to cache
#: each discovery, so it has a real caller. Its removal from this list is what the
#: staleness test below is for.
UNCALLED_BY_DESIGN: dict[str, list[str]] = {
    "app/store/repository.py": ["subscribers_intersecting", "set_alert_audio"],
    "app/store/memory.py": ["corrections_for"],
    "app/store/objects.py": ["put_audio", "audio_url", "export_key"],
}


def test_uncalled_functions_say_so_in_their_docstring():
    """A function with no callers must admit it.

    `subscribers_intersecting` reads as the spatial query the scheduler uses; it
    isn't called at all. `put_audio` reads as shipping voice notes; nothing
    synthesises speech. Neither docstring was wrong about *what the code does* —
    both were wrong about whether anything does it, which is the harder error to
    notice and the one that makes a review sign off on a half-built feature.
    """
    import ast

    offenders: list[str] = []

    for module, names in UNCALLED_BY_DESIGN.items():
        path = pathlib.Path(module)
        tree = ast.parse(path.read_text())

        by_name = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }

        for name in names:
            node = by_name.get(name)
            if node is None:
                offenders.append(f"{module}: {name} no longer exists")
                continue

            doc = ast.get_docstring(node) or ""
            # Either the function itself admits it, or the module header does —
            # objects.py states it once for four functions, which reads better than
            # repeating it in each.
            module_doc = ast.get_docstring(tree) or ""
            admits = any(
                phrase in doc or phrase in module_doc
                for phrase in ("No callers", "no caller", "not yet", "STATUS:")
            )
            if not admits:
                offenders.append(f"{module}: {name}")

    assert not offenders, (
        "these functions have no callers but their docstrings do not say so — a "
        f"reader will take them for working features: {offenders}"
    )


def test_uncalled_list_does_not_go_stale():
    """Once something is actually called, its entry must go.

    Otherwise the list quietly licenses a future regression in working code — the
    same staleness trap as KNOWN_UNREACHED.
    """
    source = _python_source()

    still_uncalled: list[str] = []
    for module, names in UNCALLED_BY_DESIGN.items():
        for name in names:
            # A call site is any `name(` that is not the definition itself.
            calls = len(re.findall(rf"\b{name}\s*\(", source)) - len(
                re.findall(rf"def\s+{name}\s*\(", source)
            )
            if calls > 0:
                still_uncalled.append(f"{module}:{name} ({calls} call sites)")

    assert not still_uncalled, (
        "these are now called — remove them from UNCALLED_BY_DESIGN and drop the "
        f"'no callers' note from their docstrings: {still_uncalled}"
    )


def test_docstrings_do_not_claim_writes_to_unreached_tables():
    """Catches the specific defect this file was written after.

    Three docstrings stated that Fahis "writes to agent_memory" while nothing did.
    Prose is not executable, so nothing contradicted it — a reader had no way to
    tell the claim from a fact.

    Present-tense write claims are therefore only allowed for tables that are
    genuinely written. For anything on the exemption list, the prose has to be
    explicit that the capability is provisioned rather than active.
    """
    write_claim = re.compile(
        r"writes? (?:to|into) [`']?(" + "|".join(KNOWN_UNREACHED) + r")[`']?",
        re.IGNORECASE,
    )

    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        for match in write_claim.finditer(path.read_text()):
            offenders.append(f"{path}: {match.group(0)!r}")

    assert not offenders, (
        "these files claim a write to a table nothing writes to — state that the "
        f"table is provisioned but not yet populated instead: {offenders}"
    )


# --------------------------------------------------------------------------- #
# Deployment contract — a fresh clone must start with one command
#
# The reviewer path is: `git clone` -> `docker compose up -d` -> open two URLs.
# Each of these guards one thing that silently broke that path.
# --------------------------------------------------------------------------- #


def _compose() -> str:
    return pathlib.Path("../docker-compose.yml").read_text()


def test_env_file_is_optional():
    """`.env` is gitignored, so a fresh clone does not have one.

    Compose treats a missing `env_file` as fatal, so without `required: false`
    the very first command a reviewer runs fails outright.
    """
    compose = _compose()

    assert "required: false" in compose, (
        "env_file must be declared with `required: false` — .env is gitignored and "
        "a fresh clone has none"
    )
    # The bare list form is the one that fails hard.
    assert "env_file: [.env]" not in compose


def test_frontend_block_stays_commented_and_documented():
    """Superseded `test_frontend_is_in_the_default_stack`.

    That earlier test asserted the frontend WAS in the default stack. The decision was
    reversed: this stack is the backend service, Netlify is the production frontend, and
    a container here holds port 3000 so `npm run dev` cannot bind — which actively
    blocks local frontend work.

    The block is kept commented rather than deleted so a developer who does want it can
    uncomment one section instead of reconstructing it.
    """
    compose = _compose()

    assert "# frontend:" in compose, (
        "the commented frontend service must remain, so it can be re-enabled"
    )
    assert "\n  frontend:" not in compose, "the frontend must not be active"
    # And the reasoning must travel with it.
    assert "npm run dev" in compose


def test_startup_prints_the_urls():
    """`up -d` detaches, so nothing otherwise tells a reviewer where to connect.

    Only the URLs this stack actually serves. It used to print a :3000 web app that the
    backend-only stack does not start, which reads as a broken deployment rather than a
    deliberate omission.
    """
    compose = _compose()

    assert "  welcome:" in compose
    welcome = compose.split("  welcome:")[1]
    # The docs live under the API prefix, so a proxy needs one location block.
    assert "localhost:8000/shelter/v1/api/docs" in welcome, (
        "the API docs URL is not printed"
    )
    # It must wait for health, or it prints before anything can serve.
    assert "service_healthy" in welcome
    # And print once rather than looping.
    assert 'restart: "no"' in welcome


def test_ready_endpoint_fails_the_healthcheck_when_unhealthy():
    """`curl -fsS` only fails on a non-2xx status.

    A 200 carrying `{"ready": false}` reported every broken deployment as healthy,
    which would release `depends_on: service_healthy` against an unreachable
    database.
    """
    source = pathlib.Path("app/api/routes/health.py").read_text()

    ready = source.split("async def ready(")[1]
    assert "HTTP_503_SERVICE_UNAVAILABLE" in ready, (
        "/ready must set a 503 status when not ready, not just return ready: false"
    )


def test_api_declares_a_healthcheck_in_compose():
    """`welcome` and the URL banner depend on this existing.

    Two failure modes it guards:

    - A `depends_on: service_healthy` on a service with no health check makes
      Compose fail the dependency outright, so `welcome` would never run and a
      reviewer would see no URLs at all.
    - The probe must follow `API_PREFIX`. A hardcoded path would 404 forever on any
      deployment that changed the prefix, leaving the container permanently
      unhealthy for a reason nothing in the logs explains.
    """
    compose = _compose()

    api = compose.split("\n  api:")[1].split("\n  worker:")[0]
    assert "healthcheck:" in api, (
        "the api service needs a healthcheck — `welcome` waits on service_healthy "
        "and Compose fails that dependency outright when none is declared"
    )
    assert "${API_PREFIX:-/shelter/v1/api}" in api, (
        "the healthcheck URL must follow API_PREFIX; hardcoding the path breaks any "
        "deployment that changes the prefix"
    )


def test_dragonfly_snapshot_cron_is_a_single_quoted_argument():
    """**Regression test for a stack-wide startup failure.**

    Dragonfly exited 1 with `Illegal value ... cron expression must have six fields`,
    which failed `depends_on` for api and both workers — so nothing started at all,
    over a snapshot schedule.

    Two causes compounded:

      1. The command used a folded block (`>`), which joins lines with spaces. Compose
         then word-split the result, so `--snapshot_cron=* * * * *` arrived as the flag
         plus five bare `*` arguments and the flag saw only the first one.
      2. The error text is misleading: "six fields" counts the flag itself, so a real
         six-field cron (`0 * * * * *`) is *also* rejected. Verified against v1.25.1 —
         five-field expressions start clean, six-field ones do not.

    So the value must be five fields AND a single quoted list entry.
    """
    compose = _compose()

    dragonfly = compose.split("  dragonfly:")[1].split("\n  postgres:")[0]

    assert '"--snapshot_cron=' in dragonfly, (
        "snapshot_cron must be a single quoted argv entry — unquoted, compose "
        "word-splits it and the flag receives only the first field"
    )
    cron = dragonfly.split('"--snapshot_cron=')[1].split('"')[0]
    assert len(cron.split()) == 5, (
        f"expected five crontab fields, got {len(cron.split())}: {cron!r}. "
        "Despite the error message saying six, v1.25.1 rejects six-field forms."
    )
    # A folded block would reintroduce the word-splitting bug.
    assert "command: >" not in dragonfly


def test_dragonfly_thread_count_is_pinned():
    """Dragonfly requires >=256MB per io thread and sizes its pool from the host's core
    count — so `--maxmemory=1gb` started fine on a small box and failed on a 12-core
    laptop with "There are 8 threads, so 2.00GiB are required."

    A machine-dependent startup failure is the worst kind to debug, so the thread count
    is pinned rather than left to the host.
    """
    compose = _compose()
    dragonfly = compose.split("  dragonfly:")[1].split("\n  postgres:")[0]

    assert "--proactor_threads=" in dragonfly


def test_workers_do_not_inherit_the_api_healthcheck():
    """The image's HEALTHCHECK curls the API's /ready endpoint.

    Correct for `api`, impossible for a worker: a worker consumes a queue and runs no
    web server, so the probe can never pass and the container sits permanently
    "unhealthy". That is worse than no healthcheck — it trains an operator to ignore the
    one signal that matters, and `docker compose ps` stops being a reliable read.
    """
    compose = _compose()

    for service in ("  worker:", "  worker-analyst:"):
        # The service body, up to the next top-level key.
        body = compose.split(service)[1]
        body = body[: body.index("\n  #") if "\n  #" in body else len(body)]
        assert "disable: true" in body, (
            f"{service.strip()} must disable the inherited healthcheck"
        )


def test_frontend_is_not_in_the_default_stack():
    """This stack is the backend service.

    The frontend is commented out deliberately: Netlify is the production frontend, and
    local frontend work wants `npm run dev` for hot reload — a container in the default
    stack holds port 3000 and makes that fail to bind.
    """
    import subprocess

    result = subprocess.run(
        ["docker", "compose", "config", "--services"],
        capture_output=True, text=True, cwd="..",
    )
    if result.returncode != 0:
        pytest.skip("docker compose unavailable in this environment")

    services = set(result.stdout.split())
    assert "frontend" not in services, (
        "the frontend must stay commented out — it is not part of the backend stack"
    )
    # The backend surface must still be complete.
    assert {"api", "worker", "worker-analyst", "postgres", "dragonfly", "minio"} <= services


def test_welcome_does_not_advertise_a_service_it_does_not_start():
    """The banner printed a Web app URL on :3000 that this stack no longer serves —
    which reads as a broken deployment rather than a deliberate omission."""
    compose = _compose()
    welcome = compose.split("  welcome:")[1]

    assert "localhost:3000" not in welcome, (
        "the banner must not advertise the frontend; this stack does not start it"
    )
    assert "localhost:8000" in welcome
    # And it must say where the frontend actually is.
    assert "npm run dev" in welcome
    # The partner reference is the surface an aggregator needs; the banner is where an
    # operator looks for it.
    assert "dev-docs" in welcome
