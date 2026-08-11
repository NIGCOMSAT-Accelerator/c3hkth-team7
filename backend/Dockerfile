# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder — compile wheels once so the runtime image carries no toolchain.
# GDAL/PROJ headers are needed to build rasterio and pyproj; they are large,
# which is exactly why they don't belong in the final image.
#
# Multi-arch: built for linux/amd64 and linux/arm64 (`make release`). `TARGETARCH`
# is supplied automatically by buildx and is what makes the torch index selection
# below correct on both.
#
# NOTE: deliberately NOT `--platform=$BUILDPLATFORM`. That pattern speeds up
# cross-compilation for Go-style toolchains, but here the builder produces
# *compiled* wheels (rasterio, pyproj, torch) which must match the target's ABI —
# building them on an amd64 host for an arm64 image would emit amd64 binaries that
# fail at import. So the builder runs under emulation on the target platform, which
# is slower and correct. The buildx registry cache absorbs the cost after the first
# build.
# ---------------------------------------------------------------------------
FROM python:3.10-slim-bookworm AS builder

ARG TARGETARCH

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gdal-bin \
        libgdal-dev \
        libgeos-dev \
        libproj-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .

# Torch index is architecture-dependent, and getting this wrong fails the build
# rather than degrading:
#
#   amd64 — PyPI's default torch wheel bundles CUDA (~2 GB of libraries this
#           deployment cannot use), so point at the CPU-only index.
#   arm64 — download.pytorch.org/whl/cpu publishes NO aarch64 wheels. Adding that
#           index on ARM makes pip find nothing and fail. PyPI's own
#           manylinux2014_aarch64 wheel is already CPU-only (there is no CUDA
#           build for ARM Linux), so the default index is both correct and smaller.
RUN if [ "$TARGETARCH" = "amd64" ]; then \
        pip wheel --wheel-dir /wheels \
          --extra-index-url https://download.pytorch.org/whl/cpu \
          -r requirements.txt ; \
    else \
        pip wheel --wheel-dir /wheels -r requirements.txt ; \
    fi


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:3.10-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # GDAL tuning for remote COG reads. Without these, every windowed read
    # re-lists the remote directory and the range-request optimisation is lost.
    GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.TIF,.tiff,.jp2" \
    GDAL_HTTP_MULTIPLEX=YES \
    GDAL_HTTP_VERSION=2 \
    VSI_CACHE=TRUE \
    VSI_CACHE_SIZE=10000000

# Runtime shared libraries only — no headers, no compiler.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgdal32 \
        libgeos-c1v5 \
        libproj25 \
        libexpat1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Run unprivileged. The container reaches out to public catalogues and takes
# no inbound traffic other than the API port.
RUN useradd --create-home --uid 10001 shelter
WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

# The GeoIP city database, in its own layer BEFORE the source.
#
# ## Why the directory is created rather than copied
#
# `COPY data/ ./data/` fails outright when the directory is absent — "/data": not found —
# and `backend/data/` is gitignored, so it IS absent in every fresh clone. That made the
# repository unbuildable for anyone but the machine that ran `make geoip`, which is the
# opposite of the intent.
#
# `mkdir` plus a glob COPY is the shape that works both ways. The `[...]` wildcard is the
# trick: `COPY data/*.mmdb` errors when nothing matches, whereas a bracket expression makes
# the pattern optional — Docker treats a glob with no matches as a no-op rather than a
# failure. So the layer exists either way, holding the database on a machine that has one and
# nothing on a machine that does not.
#
# ## Why it is copied before the source at all
#
# The file is ~124MB. In the same layer as the application code, every one-line Python change
# would re-copy and re-push all of it. Its own layer stays cached until the database itself is
# refreshed, which is monthly at most.
#
# `iam/geo.py` degrades to showing raw IP addresses when the file is missing — the same
# discipline as `ml/inference.py` falling back to documented physical thresholds without
# weights. An image built on a machine with no database is fully functional, just less
# friendly.
RUN mkdir -p /app/data
COPY --chown=shelter:shelter data[/]*.mmdb /app/data/

COPY --chown=shelter:shelter . .

# Set the exec bit explicitly. git preserves it, but a build from a source tarball
# or a checkout with core.fileMode=false would not, and the failure mode is a
# container that exits immediately with "permission denied" on every start.
RUN chmod +x /app/docker-entrypoint.sh

USER shelter
EXPOSE 8000

# The healthcheck follows API_PREFIX rather than hardcoding it, because the prefix
# is configurable and a baked-in path would 404 forever on a deployment that changed
# it — leaving the container permanently unhealthy for a reason nothing explains.
# Defaulted so the image works standalone, with no compose environment.
ENV API_PREFIX=/shelter/v1/api
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://localhost:8000${API_PREFIX}/ready" || exit 1

# ENTRYPOINT + CMD rather than a bare CMD, so `docker run <image> worker --stages
# analyst` selects a role while the startup validation still runs. The entrypoint
# validates configuration before the port opens and `exec`s the real process so it
# becomes PID 1 and receives SIGTERM directly — without that, `docker stop` signals
# the shell, uvicorn never shuts down gracefully, and every stop drops in-flight
# requests after the full timeout.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["api"]
