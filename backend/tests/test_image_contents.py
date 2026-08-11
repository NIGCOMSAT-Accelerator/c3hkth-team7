"""What the container image must and must not carry.

`.gitignore` and `.dockerignore` are INDEPENDENT mechanisms, and the whole deployment story depends
on that: the model weights and the GeoIP database are gitignored (too large, and DB-IP's licence is
not ours to redistribute) yet **must** be baked into the image, because a VPS operator pulls a tag
and expects a working platform — not one that silently degrades to threshold science and raw IPs.

Nothing tested this, and a real defect shipped as a result: `__pycache__/` in `.dockerignore` matched
only at the context root, so 346 `.pyc` files compiled by cpython-311/-312/-314 travelled into an
image running 3.10.

These are static assertions over the two ignore files and the Dockerfile. They deliberately do NOT
build an image — `make fresh-check` does that, takes minutes, and needs a daemon. The failures these
catch are all authoring mistakes visible in the text.
"""

from __future__ import annotations

import pathlib

DOCKERIGNORE = pathlib.Path(".dockerignore")
DOCKERFILE = pathlib.Path("Dockerfile")


def _patterns() -> list[str]:
    return [
        line.strip()
        for line in DOCKERIGNORE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


# --------------------------------------------------------------------------- #
# Must be baked IN
# --------------------------------------------------------------------------- #


def test_model_weights_are_not_excluded_from_the_build_context():
    """The weights are gitignored and must still reach the image.

    Without them `ml/inference` falls back to documented physical thresholds at confidence 0.55
    instead of 0.88 — and `CONFIDENCE_ESCALATION_FLOOR` is 0.65, so a weightless deployment can
    never raise an EMERGENCY. It would look healthy and under-warn, which is the worst shape of
    failure this platform has.
    """
    for pattern in _patterns():
        assert "weights" not in pattern, (
            f".dockerignore pattern {pattern!r} would strip the model weights from the image"
        )
        assert not pattern.endswith(".pt"), (
            f".dockerignore pattern {pattern!r} would strip the model weights from the image"
        )


def test_the_geoip_database_is_not_excluded_and_is_copied_explicitly():
    """`data/*.mmdb` must survive the context and be COPYed before the source tree.

    The dedicated COPY exists so the 130 MB database lands in its own layer and is not invalidated
    by every source edit. `data[/]*.mmdb` — bracket-globbed — is the trick that makes the COPY
    optional: a plain `COPY data/*.mmdb` FAILS THE BUILD when nothing matches, which is what
    happened to every clone that had not run `make geoip`.
    """
    for pattern in _patterns():
        assert not pattern.endswith(".mmdb"), (
            f".dockerignore pattern {pattern!r} would strip the GeoIP database"
        )
        assert pattern not in {"data", "data/", "/data"}, (
            f".dockerignore pattern {pattern!r} would strip the GeoIP database"
        )

    dockerfile = DOCKERFILE.read_text()
    assert "data[/]*.mmdb" in dockerfile, (
        "the optional-COPY glob is gone; a plain COPY of a missing path fails the whole build"
    )


# --------------------------------------------------------------------------- #
# Must be kept OUT
# --------------------------------------------------------------------------- #


def test_bytecode_patterns_are_recursive():
    """**The defect this file was written for.**

    `.dockerignore` uses Go's `filepath.Match`, not gitignore semantics: a bare `__pycache__/`
    matches only at the context ROOT. Nested ones sail through. Measured before the fix: 346 `.pyc`
    files from three foreign interpreters inside an image running 3.10.

    Inert — Python rejects a mismatched magic number — but it is stale bytecode shipped to
    production behind a pattern that read as though it prevented exactly that.
    """
    # Compared with the trailing slash normalised away: `**/__pycache__/` and `**/__pycache__`
    # both work, and pinning one spelling would fail on a harmless reformat.
    patterns = {p.rstrip("/") for p in _patterns()}
    for name in ("__pycache__", "*.py[cod]"):
        assert f"**/{name}" in patterns, (
            f"{name!r} must be written as '**/{name}' — a bare pattern only matches the "
            f"context root, so nested bytecode is copied into the image"
        )


def test_secrets_never_enter_the_image():
    """A baked `.env` would hand every credential to anyone who pulls the tag.

    Compose injects it at runtime instead. `.env.example` is explicitly re-included because it
    carries no secrets and documents every setting.
    """
    patterns = _patterns()
    assert ".env" in patterns
    assert ".env.*" in patterns
    assert "!.env.example" in patterns, (
        "the template is excluded too; it documents every setting and holds no secrets"
    )


def test_local_virtualenvs_are_excluded_recursively():
    """`backend/.venv` is ~2 GB with torch in it, and `notebooks/` has its own.

    Recursive for the same reason as the bytecode patterns — `make venv` creates `backend/.venv`,
    which IS at the context root, but `notebooks/.venv` is not.
    """
    assert "**/.venv" in {p.rstrip("/") for p in _patterns()}


# --------------------------------------------------------------------------- #
# There is no EO cache to bake, and baking one would be a defect
# --------------------------------------------------------------------------- #


def test_no_eo_cache_is_baked_into_the_image():
    """Scene discoveries must NEVER ship in an image, and cannot: they are not files.

    The EO cache is runtime state — cached `SceneRef`s in MinIO and TTL'd values in Dragonfly db1 —
    so there is nothing on disk for the build to pick up. That is not an accident of implementation;
    it is required:

    A cached `SceneRef` holds **SAS-signed hrefs**, and a Planetary Computer token lasts ~45 minutes
    (`_REPLAY_CEILING_MINUTES = 30` exists to stay inside that). An image baked on Monday and
    deployed on Friday would carry hrefs that 403 on every read, the Analyst would measure nothing,
    and per the confidence invariant the Oracle would decline to escalate — a silent downgrade
    caused entirely by our own cache.

    So this asserts the absence of a filesystem cache path, which is what keeps that impossible.
    """
    for module in pathlib.Path("app/eo").glob("*.py"):
        source = module.read_text()
        assert "CACHE_DIR" not in source, (
            f"{module} declares a filesystem cache; scene hrefs are SAS-signed and expire in "
            f"~45 minutes, so a baked or persisted cache would serve hrefs that 403"
        )


def test_the_image_carries_no_tests():
    """Excluded on purpose — and the consequence is worth pinning, because it surprises people.

    `docker compose exec api pytest` collects NOTHING in this image. Tests run from the source
    tree, mounted or local. A future edit that "helpfully" ships `tests/` would make the image
    larger and the failure mode more confusing, not less.
    """
    assert "tests/" in _patterns()
