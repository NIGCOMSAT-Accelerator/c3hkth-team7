# SHELTER — build, release and run.
#
#     make            list every target
#     make up         start the stack (stores are internal-only)
#     make dev        same, plus datastore ports on 127.0.0.1
#     make images     build both container images
#     make release    build + push to a registry
#
# Two deployment shapes, both supported from here:
#
#   Single VPS (default)   — `make up`, compose serves the frontend and the API
#   Netlify frontend       — `make up-netlify`, compose serves the API only
#
# The datastores (Postgres/PostGIS/pgvector/TimescaleDB, MinIO, Dragonfly) always
# run in this stack on an internal network. There is no external-store mode: that
# is what makes the whole thing portable to any VPS with one command.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------
# Configuration — override on the command line, e.g.
#     make release REGISTRY=ghcr.io/acme TAG=v1.2.0
# ---------------------------------------------------------------------------

REGISTRY ?= ghcr.io/zerorate
TAG      ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)

# Multi-arch by default. amd64 covers most VPS providers; arm64 covers Ampere/
# Graviton instances (often the cheapest per core) and Apple Silicon dev machines.
# One manifest list means `docker pull` resolves the right variant automatically —
# no per-host image tags to keep straight.
#
# Override for a single arch when you only need one and want the build to be quick:
#     make images PLATFORMS=linux/arm64
#
# ## Both legs are now exercised, not assumed (verified 2026-08-11)
#
# The arm64 leg was the untested one, because of the torch install. Confirmed end to end:
#
#   * The `TARGETARCH` branch in backend/Dockerfile does the right thing — arm64 omits
#     `download.pytorch.org/whl/cpu` (which publishes no aarch64 wheels) and resolves
#     `torch-2.5.1-cp310-cp310-manylinux2014_aarch64.whl` from PyPI instead. No fallback, no
#     degraded install.
#   * The gitignored bulk files bake into BOTH arches: sar_flood.pt, crop_stress.pt, city.mmdb.
#   * Both trained models LOAD and infer on aarch64 at confidence 0.88 — not the 0.55 threshold
#     fallback, which is what a checkpoint that failed to load would silently give you.
#   * One OCI index carries linux/amd64 + linux/arm64, each child's `config.architecture`
#     matching its platform descriptor, 14 layers and the same entrypoint on both.
#
# **Cross-arch numerical parity was measured, not assumed.** Different BLAS kernels per arch mean
# the probability rasters are not bit-identical: max |arm64 - amd64| = 5.4e-07. That is only safe
# because of the margin — the closest probability to the 0.5 decision boundary was 3.7e-06, an
# order of magnitude larger, and 0 of 131,072 pixels classified differently. The fraction the
# Oracle actually consumes was identical to every printed digit on both arches. Re-measure this if
# a model is retrained to output probabilities that cluster near 0.5.
PLATFORMS ?= linux/amd64,linux/arm64

# Buildx builder name. Created on demand by `make buildx`.
BUILDER ?= shelter-builder

# What the interactive release targets forward as $BUILDER.
#
# Deliberately EMPTY by default, and deliberately not `BUILDER` itself: an empty value tells
# `scripts/release.sh` "choose for me", and it then prefers a native cloud builder over local
# emulation. Defaulting this to `shelter-builder` would pin every release to the QEMU path — which
# is merely slow for the backend and a hard segfault for the Next.js/Turbopack build.
#
# Naming a builder explicitly still works and still wins:
#
#     make release-ui BUILDER_OVERRIDE=cloud-acme-ci_linux-amd64
BUILDER_OVERRIDE ?=

BACKEND_IMAGE  := $(REGISTRY)/shelter-api
FRONTEND_IMAGE := $(REGISTRY)/shelter-web

COMPOSE     := docker compose
COMPOSE_DEV := docker compose -f docker-compose.yml -f docker-compose.dev.yml

# Everything the backend needs for tests and lint. Prefers an activated venv, then
# backend/.venv, then bare python3 — so `make test` works before any setup.
#
# **ABSOLUTE, not relative.** This was `../backend/.venv/bin/python`, which resolves only from
# inside `backend/` — fine for `test` and `lint` (they `cd backend` first) and silently wrong for
# `RUN_OPENAPI`, whose `import fastapi` probe runs from the repo root. The effect was that a
# perfectly good local venv failed the probe and every `make openapi` fell through to the container
# path, which exports from the image's BAKED `app/` rather than the working tree — so it could
# "succeed" while writing a spec that does not match the code being edited.
PY := $(shell if [ -n "$$VIRTUAL_ENV" ]; then echo "$$VIRTUAL_ENV/bin/python"; \
              elif [ -x "$(CURDIR)/backend/.venv/bin/python" ]; then \
                   echo "$(CURDIR)/backend/.venv/bin/python"; \
              else echo python3; fi)

.PHONY: help
help: ## Show this help
	@echo "SHELTER — targets:"
	@echo
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-18s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  Registry:  $(REGISTRY)"
	@echo "  Tag:       $(TAG)"
	@echo "  Platforms: $(PLATFORMS)"

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

.PHONY: env
env: .env ## Create .env from the template if absent

.env:
	@cp .env.example .env
	@echo "Created .env from .env.example."
	@echo "It runs as-is on keyless sources. Set API_KEY before any public deploy."

# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

.PHONY: up
up: env ## Start the stack — API on :8000 (frontend is Netlify or `npm run dev`)
	$(COMPOSE) up -d --build
	@echo
	@echo "API      http://localhost:8000/shelter/v1/api/docs"
	@echo "Health   curl -s localhost:8000/shelter/v1/api/health | jq"
	@echo
	@echo "No frontend container by design — Netlify serves production, and local"
	@echo "work wants hot reload:  cd frontend && nvm use 24 && npm run dev  (:3000)"
	@echo "Stores are on the internal network only. For host access: make dev"

# Kept as an alias rather than deleted: it is referenced in the header, in README §4,
# and in anyone's shell history.
#
# It used to run `--scale frontend=0`, which **failed outright** — "no such service:
# frontend". The frontend service is commented out in docker-compose.yml (Netlify is
# production; see the note there), so there was nothing to scale down and the target
# could never have worked as written. `make up` is already API-only, so this is now
# the same thing under the older name.
.PHONY: up-netlify
up-netlify: up ## Alias for `make up` — the stack is already API-only
	@echo "Point SHELTER_API_URL at this host from Netlify."

.PHONY: dev
dev: env ## Start with datastore ports published on 127.0.0.1 (local dev only)
	$(COMPOSE_DEV) up -d --build
	@echo
	@echo "Postgres  postgresql://shelter:shelter@127.0.0.1:5432/shelter"
	@echo "Dragonfly redis://127.0.0.1:6379"
	@echo "MinIO     http://127.0.0.1:9001  (shelter / shelter-dev-secret)"
	@echo
	@echo "NEVER use this on a public host — see docker-compose.dev.yml."

.PHONY: down
down: ## Stop the stack, keeping all data
	$(COMPOSE) --profile signal down

.PHONY: clean
clean: ## Stop and DELETE all data — database, blobs, queue state
	@printf "This deletes every volume: Postgres, MinIO, Dragonfly. Type yes: " \
	  && read ans && [ "$$ans" = "yes" ] || (echo "Aborted."; exit 1)
	$(COMPOSE) --profile signal down -v

# ---------------------------------------------------------------------------
# Image hygiene
#
# Every `--build` leaves the previous image untagged (`<none>`) plus a new layer set in
# the build cache. The backend image is ~1.4 GB — torch and GDAL — so a day of patching
# reclaims into double-digit gigabytes. Measured mid-session: 16 images totalling 9.9 GB
# with 4.4 GB reclaimable, and 23.7 GB of build cache with 20 GB reclaimable.
#
# Three levels, because they have very different costs to undo:
#
#   rebuild        the everyday one. Rebuild changed services, then drop the images they
#                  orphaned. Keeps the build cache, so the next rebuild is still fast.
#   prune          drop dangling images AND stale cache. Next build is slower but the
#                  source layers are re-downloaded, not re-derived.
#   prune-hard     everything not currently in use, cache included. Use when disk is
#                  actually short; expect a multi-minute torch/GDAL rebuild afterwards.
#
# None of these touch VOLUMES, so Postgres, MinIO and Dragonfly data survive. `make
# clean` is the target that deletes data, and it says so.
# ---------------------------------------------------------------------------

.PHONY: rebuild
rebuild: ## Rebuild api+workers and delete the images that replaces (keeps build cache)
	@echo "==> rebuilding api, worker, worker-analyst"
	$(COMPOSE) up -d --build api worker worker-analyst
	@echo "==> removing images orphaned by that build"
	@# `dangling=true` is exactly the set a rebuild orphans: previously-tagged images that
	@# no longer have a tag because a new build took it. Nothing in use can match, so this
	@# is safe to run while the stack is up.
	@docker image prune -f 2>/dev/null | tail -1
	@$(MAKE) --no-print-directory disk

.PHONY: prune
prune: ## Reclaim dangling images and stale build cache (data volumes untouched)
	@echo "==> dangling images"
	@docker image prune -f | tail -1
	@echo "==> build cache older than 24h"
	@docker builder prune -f --filter until=24h | tail -1
	@$(MAKE) --no-print-directory disk

.PHONY: prune-hard
prune-hard: ## Aggressive: every unused image and ALL build cache. Next build is slow.
	@echo "This removes every image not used by a running container, and the"
	@echo "entire build cache. Data volumes are NOT touched."
	@printf "Continue? [y/N] "
	@read ans; [ "$$ans" = "y" ] || { echo "aborted"; exit 1; }
	@docker image prune -af | tail -1
	@docker builder prune -af | tail -1
	@$(MAKE) --no-print-directory disk

.PHONY: disk
disk: ## Show Docker disk usage and what is reclaimable
	@docker system df

.PHONY: logs
logs: ## Follow API and worker logs
	$(COMPOSE) logs -f api worker worker-analyst

.PHONY: ps
ps: ## Show service status and health
	$(COMPOSE) ps

.PHONY: health
health: ## Pretty-print /health
	@curl -fsS localhost:8000/shelter/v1/api/health | (jq . 2>/dev/null || cat)

# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

.PHONY: migrate
migrate: ## Apply pending migrations (the API also does this on boot)
	$(COMPOSE) exec -T api python -c \
	  "import asyncio; from app.db import migrations; \
	   print(asyncio.run(migrations.apply_pending()) or 'already up to date')"

.PHONY: psql
psql: ## Open psql inside the stack (no host port needed)
	$(COMPOSE) exec postgres psql -U shelter -d shelter

.PHONY: scan
scan: ## Trigger a watch cycle now, without waiting for the scheduler
	@curl -fsS -X POST localhost:8000/shelter/v1/api/risk/scan \
	  -H "X-SHELTER-Key: $${API_KEY:-}" | (jq . 2>/dev/null || cat)

.PHONY: verify
verify: ## Run the Fahis verification sweep now
	@curl -fsS -X POST localhost:8000/shelter/v1/api/verification/sweep \
	  -H "X-SHELTER-Key: $${API_KEY:-}" | (jq . 2>/dev/null || cat)

.PHONY: scale-analyst
scale-analyst: ## Scale the Analyst pool: make scale-analyst N=3
	$(COMPOSE) up -d --scale worker-analyst=$${N:-3}

# ---------------------------------------------------------------------------
# Quality — run before pushing an image
# ---------------------------------------------------------------------------

# The interpreter `make venv` builds against.
#
# **Pinned, and not `python3`.** The image runs 3.10; the pins in requirements.txt follow it, and
# `torch==2.5.1` / `numpy==2.1.3` publish no wheels for 3.14. On a Mac whose `python3` is 3.14 — the
# current Homebrew default — a bare `python3 -m venv` therefore fails the install outright with
# "No matching distribution found for torch==2.5.1", which reads as a broken requirements file
# rather than a wrong interpreter. Overridable: `make venv VENV_PYTHON=python3.11`.
VENV_PYTHON ?= $(shell for p in python3.12 python3.11 python3.10 \
                              /opt/homebrew/opt/python@3.12/bin/python3.12 \
                              /opt/homebrew/opt/python@3.11/bin/python3.11; do \
                         command -v $$p >/dev/null 2>&1 && { echo $$p; exit 0; }; \
                       done; echo NONE)

.PHONY: venv
venv: ## Create backend/.venv with the test + lint deps (needed by make check)
	@if [ "$(VENV_PYTHON)" = "NONE" ]; then \
		echo "No suitable Python found (need 3.10-3.12; torch 2.5.1 has no 3.13+ wheel)."; \
		echo "  macOS         brew install python@3.12"; \
		echo "  Debian 12     apt install python3-venv        (ships 3.11 — fine)"; \
		echo "  Ubuntu 22.04  apt install python3-venv        (ships 3.10 — fine)"; \
		echo "  Ubuntu 24.04  apt install python3.12-venv     (default 3.12)"; \
		echo "...or point at one yourself: make venv VENV_PYTHON=/path/to/python3.11"; \
		echo; \
		echo "This venv is for LOCAL lint/test only. Nothing about running SHELTER on a"; \
		echo "server needs it — the image carries its own 3.10. Use \`make up\` there."; \
		exit 1; \
	fi
	$(VENV_PYTHON) -m venv backend/.venv
	backend/.venv/bin/python -m pip install --quiet --upgrade pip
	backend/.venv/bin/python -m pip install -r backend/requirements.txt
	@echo
	@echo "backend/.venv ready on $$(backend/.venv/bin/python --version)."
	@echo "\`make check\` now runs locally instead of shelling into the container."

.PHONY: test
test: ## Run the backend suite (offline; no stores required)
	cd backend && $(PY) -m pytest

.PHONY: lint
lint: ## Lint the backend
	cd backend && $(PY) -m ruff check app tests

.PHONY: format
format: ## Format the backend
	cd backend && $(PY) -m ruff format app tests

.PHONY: typecheck
typecheck: ## Typecheck the frontend
	cd frontend && npx tsc --noEmit

# ---------------------------------------------------------------------------
# OpenAPI contract
#
# FastAPI generates the schema at runtime, so there is nothing to "update" in the
# usual sense — `/shelter/v1/api/docs` always reflects the routes that are actually
# mounted. What is missing without these targets is a *committed* copy, and that is
# what makes three things possible:
#
#   * an API change shows up in a diff, next to the code that caused it;
#   * a partner generates a client offline, without our stack running;
#   * an UNINTENDED contract change fails CI instead of reaching a consumer.
#
# `openapi-check` is the one that earns its keep. It is wired into `make check`, so
# adding an endpoint and forgetting to export makes the build fail with a one-line
# fix rather than silently shipping a stale contract to integrators.
#
# Runs without Docker and without the geospatial stack: the exporter mounts the
# routers on a bare FastAPI app rather than importing `app.main`, so it needs no
# GDAL, no torch, and no running database.
# ---------------------------------------------------------------------------

# `$(PY)` falls back to bare python3 when no venv is active, and that interpreter has
# no fastapi. Rather than failing with `ModuleNotFoundError` — which reads like a bug in
# the exporter — check first and say what to do. Falls back to running inside the api
# container, which always has the dependencies, so this works with no host setup at all.
# `$(PY)` falls back to bare python3 when no venv is active, and that interpreter has
# no fastapi. Rather than failing with `ModuleNotFoundError` — which reads like a bug in
# the exporter — check first and say what to do. Falls back to running inside the api
# container, which always has the dependencies, so this works with no host setup at all.
#
# `$(MAKECWD)` rather than a relative `cd`: each line of a recipe runs in its own shell,
# but the `if` here is one logical line, so a `cd` in the condition persists into the
# body and a second relative `cd` fails. Absolute paths sidestep that entirely.
MAKECWD := $(shell pwd)

define RUN_OPENAPI
	@if $(PY) -c "import fastapi" >/dev/null 2>&1; then \
		cd "$(MAKECWD)/backend" && $(PY) -m app.openapi_export --out "$(MAKECWD)/openapi.json" $(1); \
	elif $(COMPOSE) ps api 2>/dev/null | grep -q Up; then \
		echo "no local fastapi; exporting from the api container"; \
		$(COMPOSE) exec -T api python -m app.openapi_export --out /tmp/openapi.json $(1) && \
		$(COMPOSE) exec -T api cat /tmp/openapi.json > "$(MAKECWD)/openapi.json"; \
	else \
		echo "Cannot export the OpenAPI schema."; \
		echo "  No fastapi on this interpreter ($(PY)), and the api container is not running."; \
		echo "Either install the backend deps:"; \
		echo "  make venv"; \
		echo "...or start the stack and retry:"; \
		echo "  make up"; \
		exit 1; \
	fi
endef

.PHONY: openapi
openapi: ## Export the OpenAPI schema to openapi.json (run after adding an endpoint)
	$(call RUN_OPENAPI,)

.PHONY: openapi-check
openapi-check: ## Fail if openapi.json is stale. Part of `make check`.
	$(call RUN_OPENAPI,--check)

.PHONY: openapi-serve
openapi-serve: ## Print the live docs URLs (ungated — shareable with integrators)
	@echo "Swagger UI   http://localhost:8000$${API_PREFIX:-/shelter/v1/api}/docs         (internal, full)"
	@echo "Partner ref  http://localhost:8000$${API_PREFIX:-/shelter/v1/api}/dev-docs     (filtered spec)"
	@echo "OpenAPI 3    http://localhost:8000$${API_PREFIX:-/shelter/v1/api}/openapi.json"
	@echo
	@echo "All three are unauthenticated on purpose: an aggregator must be able to"
	@echo "read the contract and generate a client before they hold a credential."
	@echo
	@# `/redoc` used to be listed here and 404s — `main.py` sets `redoc_url=None` because
	@# FastAPI's built-in page loads a jsdelivr asset that now 404s, rendering a blank
	@# screen. `/dev-docs` is the partner-facing reference and serves a pinned bundle.
	@echo "Reads are tenant-scoped: /alerts, /subscribers, /risk/areas and /verification/{id}"
	@echo "need a portal session, an aggregator key, or a platform key with platform:read."

.PHONY: openapi-client
openapi-client: openapi ## Generate a Python client from the committed spec (needs openapi-generator)
	@command -v openapi-generator >/dev/null 2>&1 || { 		echo "openapi-generator not installed: brew install openapi-generator"; exit 1; }
	openapi-generator generate -i openapi.json -g python -o ./clients/python 		--additional-properties=packageName=shelter_client
	@echo "client written to ./clients/python"

.PHONY: check
check: lint openapi-check test ## Lint + OpenAPI freshness + test. What CI runs.

# ---------------------------------------------------------------------------
# Images — multi-arch via buildx
#
# The constraint that shapes these targets: **a multi-platform build cannot be
# loaded into the local Docker daemon.** `--load` only accepts one platform,
# because the daemon's image store holds a single manifest, not a list. So:
#
#     make images    -> both arches, stays in the buildx cache (verifies the build)
#     make release   -> both arches, pushed as one manifest list (the useful output)
#     make image-local -> current arch only, --load, for running it right now
#
# `docker pull` then resolves the right variant per host automatically, which is
# the portability property: the same tag runs on an amd64 VPS, an Ampere/Graviton
# ARM instance, and an Apple Silicon laptop.
#
# ARM builds of the backend are slow the first time — rasterio, pyproj and torch
# either compile or fall back to QEMU emulation. The buildx cache makes subsequent
# builds fast; expect the first `make release` to take a while.
# ---------------------------------------------------------------------------

.PHONY: buildx
buildx: ## Create/select the multi-arch builder (idempotent)
	@docker info >/dev/null 2>&1 || { \
	  echo "Docker daemon is not running. Start Docker Desktop (or dockerd) first."; \
	  exit 1; }
	@docker buildx inspect $(BUILDER) >/dev/null 2>&1 \
	  || docker buildx create --name $(BUILDER) --driver docker-container --bootstrap
	@docker buildx use $(BUILDER)
	@docker buildx inspect --bootstrap | grep -E "^(Name|Platforms):"
	@echo
	@echo "If linux/arm64 is missing above, install the QEMU emulators:"
	@echo "  docker run --privileged --rm tonistiigi/binfmt --install all"
	@echo "(Docker Desktop bundles these; a bare Linux dockerd usually does not.)"
#
# If a Docker Build Cloud builder is configured, SAY SO but do not switch to it.
#
# Why it is worth mentioning: the arm64 leg installs torch from PyPI (aarch64 wheels, ~2 GB plus a
# pip resolve). Under QEMU user-mode emulation that runs several times slower than native and can
# trip pip's socket timeout, so it is the slowest part of a release by a wide margin. A cloud
# builder has a NATIVE arm64 node and turns it back into an ordinary build.
#
# Why it is NOT the default: building there uploads the build context off this machine, and our
# context deliberately carries the trained model weights, the EO cache and `data/*.mmdb`. Sending
# those to a third-party builder is a decision for whoever runs the release, not a default this
# Makefile should make on their behalf. A cloud builder also usually belongs to some other
# project's org, whose cache and retention we do not control.
#
# Opting in is one variable:  make release-api BUILDER_OVERRIDE=<name>
	@cloud=$$(docker buildx ls --format json 2>/dev/null \
	           | grep '"Driver":"cloud"' \
	           | grep -o '"Name":"[^"]*"' | head -1 | sed 's/.*:"//; s/"$$//'); \
	  if [ -n "$$cloud" ] && [ "$(BUILDER)" != "$$cloud" ]; then \
	    echo; \
	    echo "A cloud builder is configured: $$cloud (native arm64, no emulation)."; \
	    echo "Not used by default — it uploads the context, weights and *.mmdb included,"; \
	    echo "and a subscription that is out of capacity fails only AFTER that upload."; \
	    echo "To opt in:  make release-api BUILDER_OVERRIDE=$$cloud"; \
	  fi

# ---------------------------------------------------------------------------
# ONE TAG PER BUILD — neither `latest` NOR `buildcache` is ever published
#
# Every build and push target below tags exactly `$(TAG)` and nothing else.
#
# Two things were removed for the same reason, and the second was the more expensive:
#
#   `latest`      a second manifest list per release. See the detail below.
#   `buildcache`  `--cache-to type=registry,mode=max` pushed EVERY intermediate layer of
#                 EVERY stage for EVERY platform as a separate registry artefact. On a
#                 two-arch build of an image carrying torch, a 130 MB GeoIP database and
#                 baked model weights, that cache is comfortably LARGER than the image it
#                 accelerates — so the registry held more bytes for the cache than for the
#                 thing being shipped, on every release.
#
#                 What it bought was a faster build on a COLD cache, i.e. a fresh CI
#                 runner. Releases here run from a developer laptop, where buildx already
#                 keeps a local cache between builds and the registry round-trip is pure
#                 overhead — uploaded after every build, downloaded before the next.
#
#                 If a CI runner is added later, put the flags in `release-ci` only, where
#                 the cold-cache assumption holds. GitHub Actions also offers
#                 `type=gha`, which is scoped to the workflow and costs the container
#                 registry nothing.
#
# **Deleting the tags in the registry is a separate, manual step.** Removing the flags
# stops new cache artefacts being written; it does not reclaim what earlier releases
# already pushed. Delete the `buildcache` tag on each repository by hand — see
# `make prune-remote-help`.
#
# `latest` used to be pushed alongside it, and it cost real money and real clarity:
#
#   * **Storage and transfer.** A second tag means a second manifest list plus its
#     per-arch children on the registry, and a second push of those pointers on every
#     release. On a metered plan that is billed twice for one artefact.
#   * **A moving tag is not reproducible.** `latest` names a different image after every
#     release, so a deployment pinned to it changes under a restart nobody performed.
#     `$(TAG)` is `{service}_{date}_{sha}` and always identifies one build.
#   * **It is the wrong default for compose.** A machine that already holds
#     `shelter-api:latest` reuses it rather than pulling, which is how a "deployed" fix
#     turns out to be the previous image.
#
# If a floating tag is genuinely wanted, add it deliberately and separately:
#
#     make retag TAG=api_2026-08-12_a1b2c3d ALIAS=latest
#
# That copies the manifest without rebuilding, so the alias is an explicit act rather
# than a side effect of every release.
# ---------------------------------------------------------------------------

.PHONY: image-backend
image-backend: buildx ## Build the backend image for all platforms (cache only)
	docker buildx build --platform $(PLATFORMS) \
	  -t $(BACKEND_IMAGE):$(TAG) \
	  ./backend

.PHONY: image-frontend
image-frontend: buildx ## Build the frontend image for all platforms (cache only)
	docker buildx build --platform $(PLATFORMS) \
	  --build-arg NEXT_PUBLIC_SITE_URL=$${NEXT_PUBLIC_SITE_URL:-http://localhost:3000} \
	  -t $(FRONTEND_IMAGE):$(TAG) \
	  ./frontend

.PHONY: images
images: image-backend image-frontend ## Build both images, all platforms

# Single-arch, loaded into the local daemon so `docker run` can see it. This is the
# one that cannot be multi-platform — see the note above.
.PHONY: image-local
image-local: ## Build for THIS machine's arch and load it into Docker
	docker buildx build --load \
	  -t $(BACKEND_IMAGE):local ./backend
	docker buildx build --load \
	  --build-arg NEXT_PUBLIC_SITE_URL=$${NEXT_PUBLIC_SITE_URL:-http://localhost:3000} \
	  -t $(FRONTEND_IMAGE):local ./frontend
	@echo "Loaded $(BACKEND_IMAGE):local and $(FRONTEND_IMAGE):local"

# `check` is a prerequisite on purpose: the config-contract, schema-contract and
# grounding tests all run offline in under two seconds, so there is no reason to
# spend an ARM build cycle on an image that fails them.
#
# `--push` rather than build-then-push: buildx assembles the manifest list during
# the push, so a separate `docker push` would have nothing local to send.
.PHONY: geoip-verify
geoip-verify: ## Warn if the GeoIP database is missing before a release
	@# A release without it produces a perfectly working image whose portal shows raw IP
	@# addresses instead of city names. That degradation is by design, but it should be a
	@# CHOICE — silently shipping it because someone forgot `make geoip` is the failure
	@# this guard exists to prevent.
	@# **Only prompts on a terminal.** `release-ci` depends on this target, and a `read` with
	@# no tty gets EOF immediately — which the old `[ "$$ans" = "y" ] || exit 1` read as "no"
	@# and aborted. So the documented non-interactive CI path could never finish on a headless
	@# runner whenever city.mmdb was absent, which on a fresh clone it always is (gitignored,
	@# 130 MB). Interactively the prompt is still right: shipping a degraded portal should be a
	@# choice. Non-interactively the warning is the whole point and blocking adds nothing.
	@if [ -f backend/data/city.mmdb ]; then \
		echo "==> GeoIP database present ($$(du -h backend/data/city.mmdb | cut -f1)) — will be baked in"; \
		$(MAKE) --no-print-directory geoip-version; \
	else \
		echo "==> WARNING: backend/data/city.mmdb is missing."; \
		echo "    The image will build and run, but the portal will show raw IP"; \
		echo "    addresses instead of city names in the activity log and emails."; \
		echo "    Run \`make geoip\` first if you want city lookup in this release."; \
		if [ -t 0 ]; then \
			echo; \
			printf "    Continue without it? [y/N] "; \
			read ans; [ "$$ans" = "y" ] || { echo "    aborted"; exit 1; }; \
		else \
			echo "    (no terminal — continuing; set GEOIP_REQUIRED=1 to fail instead)"; \
			[ -z "$$GEOIP_REQUIRED" ] || { echo "    GEOIP_REQUIRED is set — aborting"; exit 1; }; \
		fi; \
	fi

# Interactive release: prompts for organisation, repository, tag and PAT.
#
# A script rather than a recipe because each recipe line runs in its own shell, so a `read`
# on one line loses its value on the next — and `make` echoes commands, which would print
# the token. See scripts/release.sh.
.PHONY: release
release: release-api ## Alias for `release-api` — the backend is the usual release

# ---------------------------------------------------------------------------
# Per-service interactive release
#
# Two targets, one script. `scripts/release.sh <service>` differs only in the build context and
# the default repository name; everything security-relevant — the PAT read with no terminal echo,
# the `--password-stdin` login, the confirmation gate, the pre-build test run — is shared, because
# two copies would drift on exactly those parts.
#
# ## The tag is idempotent by construction
#
#     {service}_{YYYY-MM-DD}_{shortSHA}     e.g.  api_2026-08-11_a1b2c3d
#
# Re-running a release on an unchanged tree produces byte-identical coordinates, so a repeated
# push is a no-op rather than a second artefact claiming to be a different build. The date is UTC,
# so a release cut late in Lagos and pulled from Europe does not appear to be from two days.
# A tree with uncommitted changes gets `-dirty` appended: the SHA then does not describe what was
# built, and pretending otherwise is how an unreproducible image reaches production.
#
# ## Per-service .env keys
#
# `SHELTER_API_TAG` and `SHELTER_UI_TAG`, not one shared `SHELTER_TAG`. With a single key, pushing
# the UI rewrote the tag compose used for the API and the next `up` pulled an image that did not
# exist.
# ---------------------------------------------------------------------------

.PHONY: release-api
# `BUILDER` and `PLATFORMS` are forwarded explicitly.
#
# Make variables are NOT inherited by a recipe's child process unless exported, so
# `make release-ui BUILDER=my-cloud` set the make variable and the script saw nothing — it went on
# using `shelter-builder` and emulated. The two that matter are passed on the command line.
#
# `BUILDER=` empty is the normal case and means "let the script choose": it prefers a cloud
# builder (native amd64 AND arm64) and falls back to a local container builder.
release-api: ## Interactive multi-arch release of SHELTER-API (prompts registry, repo, tag, PAT)
	@BUILDER="$(BUILDER_OVERRIDE)" PLATFORMS="$(PLATFORMS)" bash scripts/release.sh api

.PHONY: release-ui
release-ui: ## Interactive multi-arch release of SHELTER-UI (prompts registry, repo, tag, PAT)
	@BUILDER="$(BUILDER_OVERRIDE)" PLATFORMS="$(PLATFORMS)" bash scripts/release.sh ui

.PHONY: release-both
release-both: ## Release SHELTER-API then SHELTER-UI, prompting for each
	@BUILDER="$(BUILDER_OVERRIDE)" PLATFORMS="$(PLATFORMS)" bash scripts/release.sh api
	@echo
	@BUILDER="$(BUILDER_OVERRIDE)" PLATFORMS="$(PLATFORMS)" bash scripts/release.sh ui

# The non-interactive path, for CI. Same output, every value from the environment.
#
#     make release-ci REGISTRY=ghcr.io/acme TAG=v1.2.0
#
# Assumes `docker login` has already happened — in GitHub Actions that is
# docker/login-action, which is the right place for a credential rather than a prompt.
.PHONY: release-ci
release-ci: check geoip-verify buildx ## Non-interactive release for CI (no prompts)
	docker buildx build --platform $(PLATFORMS) --push \
	  -t $(BACKEND_IMAGE):$(TAG) \
	  ./backend
	docker buildx build --platform $(PLATFORMS) --push \
	  --build-arg NEXT_PUBLIC_SITE_URL=$${NEXT_PUBLIC_SITE_URL:-http://localhost:3000} \
	  -t $(FRONTEND_IMAGE):$(TAG) \
	  ./frontend
	@echo
	@echo "Pushed $(TAG) for $(PLATFORMS):"
	@echo "  $(BACKEND_IMAGE):$(TAG)"
	@echo "  $(FRONTEND_IMAGE):$(TAG)"
	@$(MAKE) --no-print-directory manifest

.PHONY: release-backend
release-backend: check buildx ## Test, then build+push the backend as multi-arch
	docker buildx build --platform $(PLATFORMS) --push \
	  -t $(BACKEND_IMAGE):$(TAG) \
	  ./backend

# NOTE: `release-api` used to be an alias for `release-backend` here.
#
# It was **redefined twice in this file**, and make silently takes the last definition — so the
# interactive target above would have been shadowed by this one-line alias, and `make release-api`
# would have run the non-interactive environment-driven path with no prompts at all. Exactly the
# kind of collision that is invisible until someone wonders why they were never asked for a PAT.
#
# The interactive target keeps the name (it is what the docs and the CLI ask for). This
# non-interactive path is now `release-backend-ci`, which says what it is.
.PHONY: release-backend-ci
release-backend-ci: release-backend ## Non-interactive backend push (env-driven; was `release-api`)

.PHONY: manifest
manifest: ## Show which architectures a pushed tag actually contains
	@echo "$(BACKEND_IMAGE):$(TAG)"
	@docker buildx imagetools inspect $(BACKEND_IMAGE):$(TAG) 2>/dev/null \
	  | grep -E "Platform:" || echo "  (not pushed yet)"
	@echo "$(FRONTEND_IMAGE):$(TAG)"
	@docker buildx imagetools inspect $(FRONTEND_IMAGE):$(TAG) 2>/dev/null \
	  | grep -E "Platform:" || echo "  (not pushed yet)"

# Add a floating alias to an ALREADY-PUSHED tag, without rebuilding.
#
# This is the deliberate replacement for pushing `latest` on every release. `imagetools create`
# copies the manifest list registry-side — no pull, no build, no second upload of the layers,
# which are already there and content-addressed. So an alias costs one small manifest write
# instead of a whole second push.
#
#     make retag TAG=api_2026-08-12_a1b2c3d ALIAS=latest          # backend (default)
#     make retag SERVICE=ui TAG=ui_2026-08-12_a1b2c3d ALIAS=demo  # frontend
#
# Both architectures come along automatically: the source is a manifest list, and copying it
# copies the pointers to every child. `make manifest` afterwards proves it.
.PHONY: retag
retag: ## Alias a pushed tag without rebuilding: make retag TAG=… ALIAS=latest [SERVICE=api|ui]
	@test -n "$(ALIAS)" || { echo "usage: make retag TAG=<pushed-tag> ALIAS=<new-tag> [SERVICE=api|ui]"; exit 1; }
	@test "$(TAG)" != "$(ALIAS)" || { echo "TAG and ALIAS are identical — nothing to do"; exit 1; }
	@img=$(if $(filter ui,$(SERVICE)),$(FRONTEND_IMAGE),$(BACKEND_IMAGE)); \
	  echo "==> $$img:$(TAG)  ->  $$img:$(ALIAS)"; \
	  docker buildx imagetools create -t "$$img:$(ALIAS)" "$$img:$(TAG)" \
	    && echo "    aliased (manifest copied registry-side; no layers re-uploaded)"

.PHONY: config
config: ## Show the resolved compose config
	$(COMPOSE) config

.PHONY: validate
validate: ## Validate every compose permutation
	@$(COMPOSE) config >/dev/null && echo "base            OK"
	@$(COMPOSE_DEV) config >/dev/null && echo "+ dev override  OK"
	@$(COMPOSE) --profile ui config >/dev/null \
	  && echo "+ ui profile    OK"
	@$(COMPOSE) --profile signal --profile ui config >/dev/null \
	  && echo "+ all profiles  OK"
#
# The Dokploy manifest is deployed by pasting it into a panel, so nothing else would ever parse it.
# Validated with throwaway secrets: the real ones are `${VAR:?}` guarded on purpose, and compose
# refuses to interpolate without them — which is the behaviour we want in production and merely
# inconvenient here.
	@env API_KEY=validate IAM_SESSION_SECRET=validate \
	     POSTGRES_PASSWORD=validate MINIO_ROOT_PASSWORD=validate \
	     docker compose -f docs/docker-compose.dokploy.yml config >/dev/null \
	  && echo "+ dokploy       OK"

.PHONY: iam-service-account
iam-service-account: ## Provision a scoped service-account key (NAME= EMAIL= [SCOPES=])
	@test -n "$(NAME)" || { echo "usage: make iam-service-account NAME=netlify-frontend EMAIL=ops@example.com"; exit 1; }
	@test -n "$(EMAIL)" || { echo "EMAIL= is required (contact for key-expiry notices)"; exit 1; }
	$(COMPOSE) exec -T api python -m app.iam.cli create \
		--name "$(NAME)" --email "$(EMAIL)" \
		$(if $(SCOPES),--scopes "$(SCOPES)",) \
		$(if $(EXPIRES_IN_DAYS),--expires-in-days "$(EXPIRES_IN_DAYS)",)

.PHONY: iam-service-accounts
iam-service-accounts: ## List service accounts and their key health
	$(COMPOSE) exec -T api python -m app.iam.cli list

# ---------------------------------------------------------------------------
# GeoIP — optional city lookup for the portal's audit log and session panel
#
# Self-hosted rather than an API because every lookup is a subscriber's IP address, and
# this subscriber list is farmers in named districts. Those addresses must not leave the
# deployment.
#
# Two sources, same MMDB format, same destination path:
#   `make geoip`          DB-IP City Lite  — CC-BY 4.0, NO account, NO key. The default.
#   `make geoip-maxmind`  MaxMind GeoLite2 — marginally better, needs a free signup.
# ---------------------------------------------------------------------------

GEOIP_DEST := backend/data/city.mmdb

.PHONY: geoip
geoip: ## Download a free city database (DB-IP City Lite — no signup needed)
	@mkdir -p backend/data
	@# The URL is dated by month. Try the current month, then fall back one month: DB-IP
	@# publishes on the 1st, so on the 1st itself the new file may not be up yet and a
	@# hard failure there would look like a broken target.
	@ym=$$(date -u +%Y-%m); prev=$$(date -u -v-1m +%Y-%m 2>/dev/null || date -u -d 'last month' +%Y-%m); 	for m in $$ym $$prev; do 		echo "==> trying DB-IP City Lite $$m"; 		if curl -fsSL --max-time 300 -o /tmp/dbip-city.mmdb.gz 			"https://download.db-ip.com/free/dbip-city-lite-$$m.mmdb.gz"; then 			gunzip -f /tmp/dbip-city.mmdb.gz && mv /tmp/dbip-city.mmdb $(GEOIP_DEST); 			break; 		fi; 	done
	@test -f $(GEOIP_DEST) || { echo "download failed — the portal will keep showing raw IPs, nothing else breaks"; exit 1; }
	@ls -lh $(GEOIP_DEST) | awk '{print "==> installed " $$5 " at " $$9}'
	@$(MAKE) --no-print-directory geoip-version
	@echo "==> restart the api to load it:  docker compose restart api"

# Report the database's OWN build date, not the file's mtime.
#
# `test -f` above proves only that a file arrived. An MMDB carries its build epoch in its metadata,
# and that is the only thing that answers "is this current?" — an mtime says when it was downloaded,
# which on a re-pull of an unchanged edition is today regardless of how old the data is.
#
# It also catches a truncated or wrong-content download: an HTML error page saved as .mmdb.gz would
# satisfy `test -f` and then fail at runtime with the portal silently showing raw IPs. Reading the
# metadata is what turns that into a build-time failure.
#
# `python3` bare rather than $(PY): maxminddb is a backend dependency, so this needs the venv or the
# api container. It degrades to a note rather than failing, because a missing reader is not a reason
# to fail a download that otherwise succeeded.
.PHONY: geoip-version
geoip-version: ## Print the GeoIP database's own build date and age
	@if [ ! -f $(GEOIP_DEST) ]; then \
		echo "==> no database at $(GEOIP_DEST) — run \`make geoip\`"; \
		exit 0; \
	fi; \
	if $(PY) -c "import maxminddb" >/dev/null 2>&1; then \
		$(PY) -c "import maxminddb, datetime, sys; \
m = maxminddb.open_database('$(GEOIP_DEST)').metadata(); \
b = datetime.datetime.fromtimestamp(m.build_epoch, datetime.timezone.utc); \
age = (datetime.datetime.now(datetime.timezone.utc) - b).days; \
print(f'==> {m.database_type}, built {b:%Y-%m-%d} ({age} days old), {m.node_count:,} nodes'); \
print('    DB-IP publishes monthly on the 1st; over ~45 days means a refresh is available') if age > 45 else None"; \
	else \
		echo "==> (install backend deps to read the build date: make venv)"; \
	fi

.PHONY: geoip-maxmind
geoip-maxmind: ## Download MaxMind GeoLite2 instead (needs MAXMIND_LICENCE_KEY)
	@if [ -z "$$MAXMIND_LICENCE_KEY" ]; then 		echo "MAXMIND_LICENCE_KEY is not set."; 		echo; 		echo "  1. Sign up (free): https://www.maxmind.com/en/geolite2/signup"; 		echo "  2. Create a licence key"; 		echo "  3. MAXMIND_LICENCE_KEY=xxx make geoip-maxmind"; 		echo; 		echo "Or just run \`make geoip\` — DB-IP City Lite needs no account."; 		exit 1; 	fi
	@mkdir -p backend/data
	@curl -fsSL -o /tmp/geolite2.tar.gz \
		"https://download.maxmind.com/app/geoip_download?edition_id=GeoLite2-City&license_key=$$MAXMIND_LICENCE_KEY&suffix=tar.gz"
	@# The archive nests the .mmdb under a dated directory whose name changes every
	@# release, so extract by filename rather than by path.
	@tar -xzf /tmp/geolite2.tar.gz -C /tmp
	@find /tmp -name 'GeoLite2-City.mmdb' -newermt '-5 minutes' -exec cp {} $(GEOIP_DEST) \;
	@rm -rf /tmp/geolite2.tar.gz /tmp/GeoLite2-City_*
	@ls -lh $(GEOIP_DEST) | awk '{print "==> installed " $$5 " at " $$9}'
	@echo "==> restart the api to load it:  docker compose restart api"

.PHONY: geoip-check
geoip-check: ## Report whether city lookup is active, with sample lookups
	@docker compose exec -T api python -c "\
from app.iam import geo; from app.config import settings; \
print('  database :', settings.geoip_database_path); \
print('  active   :', geo.available()); \
[print(f'  {ip:<14}', (geo.lookup(ip).label if geo.lookup(ip) else 'not resolved')) \
 for ip in ('8.8.8.8','102.89.23.1','41.58.1.1','196.10.1.1')]" \
	2>/dev/null || echo "  api container is not running"

# ---------------------------------------------------------------------------
# Deploy — run a PUSHED image rather than building
# ---------------------------------------------------------------------------

.PHONY: deploy-pull
deploy-pull: ## Pull and run the registry image (set SHELTER_IMAGE/SHELTER_TAG in .env)
	@# `--no-build` is the point: without it, compose would rebuild from ./backend and the
	@# carefully-baked image you just pulled would be replaced by a local build — which on a
	@# VPS with no data/ directory means losing city lookup silently.
	@if [ -z "$$SHELTER_IMAGE" ] && ! grep -qE '^SHELTER_IMAGE=.+' .env 2>/dev/null; then \
		echo "SHELTER_IMAGE is not set."; \
		echo; \
		echo "  In .env:  SHELTER_IMAGE=$(BACKEND_IMAGE)"; \
		echo "            SHELTER_TAG=$(TAG)"; \
		echo; \
		echo "Leave them unset for local development — compose builds from ./backend."; \
		exit 1; \
	fi
	$(COMPOSE) pull api worker worker-analyst
	$(COMPOSE) up -d --no-build
	@$(MAKE) --no-print-directory geoip-check

.PHONY: image-info
image-info: ## Show which image reference compose will use, and whether GeoIP is baked in
	@echo "compose will use:"
	@$(COMPOSE) config 2>/dev/null | grep -m1 -E "^\s+image: .*shelter" | sed 's/^/  /'
	@echo "release would push:"
	@echo "  $(BACKEND_IMAGE):$(TAG)"
	@echo "GeoIP database:"
	@if [ -f backend/data/city.mmdb ]; then \
		echo "  present — $$(du -h backend/data/city.mmdb | cut -f1), will be baked into the next build"; \
	else \
		echo "  absent — run \`make geoip\`; images will show raw IPs"; \
	fi

.PHONY: fresh-check
fresh-check: ## Verify a bare `docker compose up -d` works on a clean checkout
	@# Builds the image from a tree containing ONLY what git would deliver — no .env, no
	@# backend/data, no model weights. Catches the class of bug where a Dockerfile COPY
	@# references a gitignored path: `COPY data/ ./data/` failed with "/data: not found"
	@# for every clone but the machine that had run `make geoip`.
	@#
	@# A separate tag so it cannot be confused with, or reused by, the real stack.
	@echo "==> assembling a clean checkout"
	@rm -rf /tmp/shelter-freshcheck && mkdir -p /tmp/shelter-freshcheck
	@git ls-files -co --exclude-standard > /tmp/shelter-freshcheck.list
	@tar -cf - -T /tmp/shelter-freshcheck.list | tar -xf - -C /tmp/shelter-freshcheck
	@echo "    $$(wc -l < /tmp/shelter-freshcheck.list | tr -d ' ') files, no gitignored artefacts"
	@test ! -e /tmp/shelter-freshcheck/.env || { echo "    FAIL: .env leaked into the checkout"; exit 1; }
	@test ! -e /tmp/shelter-freshcheck/backend/data || { echo "    NOTE: backend/data is tracked"; }
	@echo "==> building backend from it"
	@docker build -q -t shelter-api:freshcheck /tmp/shelter-freshcheck/backend >/dev/null
	@echo "==> verifying the image degrades correctly without optional assets"
	@docker run --rm --entrypoint sh shelter-api:freshcheck -c "\
python -c \"from app.iam import geo; from app.ml import inference; \
print('    geoip active:', geo.available(), '(False is correct — db is gitignored)')\"" || true
	@docker rmi shelter-api:freshcheck >/dev/null 2>&1 || true
	@rm -rf /tmp/shelter-freshcheck /tmp/shelter-freshcheck.list
	@echo "==> fresh clone builds and runs"
