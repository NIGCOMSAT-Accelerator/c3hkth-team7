"""Persistence layer contracts.

Everything here is offline — no Postgres, no Redis, no MinIO. The point is to
guard the *rules* the storage layer is built on, the ones that are cheap to break
in a later edit and expensive to notice at runtime:

* a cache write without a TTL would leak memory on the instance that also holds
  the job queue,
* a migration inserted out of order would silently never run,
* a stored presigned URL would expire and leave a dead link in the database,
* an object-store failure must degrade a voice note, not break dispatch.

The live-database paths are covered by running the stack, not here.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from app.config import settings
from app.db import migrations
from app.store import cache, objects

# --------------------------------------------------------------------------- #
# Cache TTL contract
# --------------------------------------------------------------------------- #


async def test_cache_rejects_non_positive_ttl():
    """A TTL-less cache write must be impossible.

    Eviction is disabled server-wide so the db0 job streams can't be evicted —
    see app/queue/redis_client.py. That makes an untimed key permanent, i.e. a
    slow leak on the instance holding the queue. The guard is the only thing
    standing in for an evictor.
    """
    for bad in (0, -1):
        with pytest.raises(ValueError, match="TTL must be positive"):
            await cache.set_json("shelter:cache:test", {"a": 1}, bad)
        with pytest.raises(ValueError, match="TTL must be positive"):
            await cache.set_text("shelter:cache:test", "x", bad)


def test_cache_set_json_ttl_is_required_positionally():
    """`ttl_seconds` must have no default.

    A default would let a caller omit it and silently get a permanent key, which
    is exactly the failure the previous test guards against at runtime. This
    catches it at the signature level, where a future edit is likely to reach.
    """
    import inspect

    for fn in (cache.set_json, cache.set_text):
        param = inspect.signature(fn).parameters["ttl_seconds"]
        assert param.default is inspect.Parameter.empty, (
            f"{fn.__name__}.ttl_seconds must stay required — a default would "
            "allow permanent cache keys on a non-evicting server"
        )


def test_cache_keys_are_namespaced():
    assert cache.key("subscriber", "sub_1") == f"{settings.cache_prefix}:subscriber:sub_1"


def test_cache_uses_scan_not_keys():
    """`KEYS` blocks the server, and this database shares an instance with the
    job streams. `delete_prefix` must use SCAN."""
    source = pathlib.Path(cache.__file__).read_text()
    assert "scan_iter" in source
    assert re.search(r"\.keys\(", source) is None


# --------------------------------------------------------------------------- #
# Migrations
# --------------------------------------------------------------------------- #


def test_migrations_are_discovered_in_order():
    names = [p.name for p in migrations.discover()]
    assert names, "no migration files found"
    assert names == sorted(names), "migrations must sort into apply order"


def test_migration_filenames_are_numbered_uniquely():
    """Duplicate prefixes make apply order ambiguous, and a lower number added
    after a higher one has already run will never be applied."""
    prefixes = [p.name.split("_", 1)[0] for p in migrations.discover()]
    assert all(p.isdigit() for p in prefixes), f"unnumbered migration: {prefixes}"
    assert len(prefixes) == len(set(prefixes)), f"duplicate prefixes: {prefixes}"


def test_embedding_dimension_is_substituted():
    """pgvector encodes dimensionality in the column type, so it cannot be a
    runtime parameter — the runner has to substitute it before execution."""
    vector_sql = (migrations.MIGRATIONS_DIR / "003_vector.sql").read_text()
    assert "${EMBEDDING_DIMENSIONS}" in vector_sql

    rendered = migrations._render(vector_sql)
    assert "${EMBEDDING_DIMENSIONS}" not in rendered
    assert f"VECTOR({settings.embedding_dimensions})" in rendered


def test_no_unsubstituted_placeholders_survive_render():
    for path in migrations.discover():
        rendered = migrations._render(path.read_text())
        leftover = re.findall(r"\$\{([A-Z_]+)\}", rendered)
        assert not leftover, f"{path.name} has unsubstituted placeholders: {leftover}"


def test_migrations_use_advisory_lock():
    """Several replicas boot at once and all call apply_pending. Without the lock
    they race on CREATE TABLE."""
    source = pathlib.Path(migrations.__file__).read_text()
    assert "pg_advisory_lock" in source
    assert "pg_advisory_unlock" in source


def test_enum_labels_match_the_python_enums():
    """Postgres enum labels are the wire contract.

    `002_core.sql` declares them by hand, so a new member added to enums.py
    without a migration would fail on insert — at dispatch time, in production.
    """
    from app.models.enums import (
        Channel,
        DeliveryStatus,
        HazardType,
        Severity,
        SubscriberKind,
    )

    sql = (migrations.MIGRATIONS_DIR / "002_core.sql").read_text()

    for enum_cls, type_name in (
        (Severity, "severity"),
        (HazardType, "hazard_type"),
        (Channel, "channel"),
        (SubscriberKind, "subscriber_kind"),
        (DeliveryStatus, "delivery_status"),
    ):
        match = re.search(
            rf"CREATE TYPE {type_name} AS ENUM \((.*?)\);", sql, re.DOTALL
        )
        assert match, f"no CREATE TYPE for {type_name}"
        declared = set(re.findall(r"'([a-z_]+)'", match.group(1)))
        expected = {m.value for m in enum_cls}
        assert declared == expected, (
            f"{type_name} labels drifted from {enum_cls.__name__}: "
            f"missing {expected - declared}, extra {declared - expected}"
        )


# --------------------------------------------------------------------------- #
# Object storage
# --------------------------------------------------------------------------- #


def test_object_store_unconfigured_is_not_an_error():
    """Without credentials the store reports unavailable rather than raising —
    a missing voice note must not stop a text advisory."""
    assert objects.available() == bool(
        settings.s3_access_key and settings.s3_secret_key
    )


async def test_object_operations_no_op_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "s3_access_key", None)
    monkeypatch.setattr(settings, "s3_secret_key", None)

    assert await objects.put("b", "k", b"data") is None
    assert await objects.get("b", "k") is None
    assert await objects.exists("b", "k") is False
    assert await objects.delete("b", "k") is False
    assert await objects.presigned_url("b", "k") is None
    assert await objects.ping() is False
    assert await objects.ensure_buckets() == []


def test_audio_key_separates_languages():
    """One alert can be voiced in several languages; they must not collide."""
    assert objects.audio_key("alert_1", "ha") != objects.audio_key("alert_1", "en")
    assert objects.audio_key("alert_1", "ha").endswith(".mp3")


def test_imagery_key_is_deterministic_and_scene_scoped():
    first = objects.imagery_key("S2A_MSIL2A_20260801T101031", "aoi_1", "red")
    again = objects.imagery_key("S2A_MSIL2A_20260801T101031", "aoi_1", "red")
    other_scene = objects.imagery_key("S2B_MSIL2A_20260802T101031", "aoi_1", "red")
    other_aoi = objects.imagery_key("S2A_MSIL2A_20260801T101031", "aoi_2", "red")

    assert first == again, "same inputs must map to the same key or caching breaks"
    assert first != other_scene
    assert first != other_aoi


def test_object_keys_contain_no_path_traversal():
    """Keys are built from ids that reach us over HTTP. A `..` segment would let
    a crafted id write outside its prefix."""
    key = objects.imagery_key("../../etc/passwd", "aoi_1", "red")
    assert ".." not in key

    audio = objects.audio_key("alert_1", "en")
    assert ".." not in audio and not audio.startswith("/")


def test_alerts_store_audio_keys_not_urls():
    """Presigned URLs expire. Persisting one leaves a dead link in the database,
    so the durable reference must be the key."""
    schema = (migrations.MIGRATIONS_DIR / "002_core.sql").read_text()
    assert "audio_key" in schema
    assert "audio_url" not in schema

    repo = pathlib.Path("app/store/repository.py").read_text()
    assert "set_alert_audio" in repo


# --------------------------------------------------------------------------- #
# Monitoring job lifecycle
# --------------------------------------------------------------------------- #


def test_area_mutations_invalidate_the_subscriber_cache():
    """**The bug that made edits invisible on the dashboard.**

    `get_subscriber` is cache-first and the portal reads areas through it, so an area write
    that does not invalidate persists to Postgres and stays hidden until the TTL lapses. The
    subscriber sees their old plot name and concludes the save failed — the write worked and the
    UI said otherwise, which is worse than an error.

    Asserted structurally: each mutation must reach `_sub_cache_key`. `save_subscriber` and
    `delete_subscriber` already did, which is exactly why the omission in the new targeted
    writes was easy to miss.
    """
    import pathlib

    source = pathlib.Path("app/store/repository.py").read_text()

    for function in ("add_area", "update_area", "delete_area"):
        start = source.index(f"async def {function}(")
        end = source.index("\nasync def ", start + 10)
        body = source[start:end]
        assert "_sub_cache_key" in body, (
            f"{function} must invalidate the subscriber cache, or the portal shows stale areas"
        )


def test_deleting_an_area_keeps_its_assessments():
    """No cascade, deliberately.

    An assessment records what a satellite measured on a date; removing the area does not make
    that untrue. A subscriber who drops a plot after a flood season must not thereby erase the
    record that they were warned — that history is the service's own accountability, and
    Fahis's verdicts hang off it.
    """
    import pathlib

    source = pathlib.Path("app/store/repository.py").read_text()
    start = source.index("async def delete_area(")
    end = source.index("\nasync def ", start + 10)
    body = source[start:end]

    assert "DELETE FROM assessments" not in body, (
        "removing an area must not delete its assessment history"
    )

    # And no FK from `assessments.aoi_id` may cascade. Checked per-COLUMN, not per-file:
    # `source_poll_state.aoi_id` DOES cascade, and correctly so — poll state for a deleted area
    # is meaningless, whereas a past measurement is not.
    import re

    for sql in pathlib.Path("app/db/migrations").glob("*.sql"):
        for line in sql.read_text().splitlines():
            if not re.search(r"\baoi_id\b.*REFERENCES\s+areas_of_interest", line, re.I):
                continue
            if "assessments" in sql.name or "assessment" in line.lower():
                assert "ON DELETE CASCADE" not in line.upper(), (
                    f"{sql.name}: assessments must not cascade on area deletion — {line.strip()}"
                )


def test_the_last_area_cannot_be_removed():
    """A subscription with no areas is active but watching nowhere.

    That reads as working while delivering nothing, which is the failure mode this whole
    codebase is careful about. Refused with a distinct exception so the route can answer 409
    with an explanation rather than a bare 404.
    """
    from app.store.repository import LastAreaError

    assert issubclass(LastAreaError, Exception)

    import pathlib

    source = pathlib.Path("app/store/repository.py").read_text()
    start = source.index("async def delete_area(")
    end = source.index("\nasync def ", start + 10)
    assert "LastAreaError" in source[start:end]


def test_area_routes_check_ownership():
    """The area id is in the path, so it must be proved to belong to the named subscriber.

    Without this, knowing an AOI id would be enough to edit or delete somebody else's monitored
    plot. 404 rather than 403 on a mismatch, so the id space cannot be probed.
    """
    import pathlib
    import re

    source = pathlib.Path("app/api/routes/subscribers.py").read_text()

    for match in re.finditer(
        r'@router\.(patch|delete)\(\s*\n?\s*"(/\{subscriber_id\}/areas/\{aoi_id\})"', source
    ):
        start = match.end()
        end = source.index("\n@router.", start) if "\n@router." in source[start:] else len(source)
        body = source[start:end]
        assert "owned[0] != subscriber_id" in body, (
            f"{match.group(1).upper()} {match.group(2)} must verify the area belongs to this "
            "subscriber"
        )
