#!/usr/bin/env bash
#
# Interactive release: prompts for registry coordinates and a PAT, then builds and pushes
# the multi-arch manifest.
#
# ## Why a script rather than a Makefile recipe
#
# Each line of a Makefile recipe runs in its own shell, so a `read` on one line and the use
# of its variable on the next simply does not work — the value is gone. Chaining everything
# with `&&` and `\` produces a recipe nobody can read or debug, and `make` also echoes the
# whole command, which would print the PAT.
#
# ## The PAT is never written to disk and never echoed
#
# Read with `read -s` (no terminal echo), piped to `docker login --password-stdin`, and never
# assigned into a file or an exported variable. Passing it as `--password` would put it in the
# process table, visible to `ps` for any user on the machine.
#
# Docker stores its own credential after login (in ~/.docker/config.json, or the OS keychain
# where a credential helper is configured) — that is Docker's business and the reason a
# second release does not prompt again.
#
# ## Defaults come from git and from .env, so the common case is four Enters
#
# Nothing here is mandatory to type. The organisation defaults to the GitHub owner parsed
# from `git remote`, the tag to the short commit SHA, and both are remembered in `.env` for
# next time.

set -euo pipefail

# ---------------------------------------------------------------------------- #
# Presentation
# ---------------------------------------------------------------------------- #

if [ -t 1 ]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
    RED=$'\033[31m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
    # Not a terminal — a piped or logged run must not be full of escape codes.
    BOLD=""; DIM=""; GREEN=""; YELLOW=""; RED=""; CYAN=""; RESET=""
fi

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s%s%s\n' "$CYAN" "$RESET" "$BOLD" "$*" "$RESET"; }
warn() { printf '%s!  %s%s\n' "$YELLOW" "$*" "$RESET"; }
die()  { printf '%s✗  %s%s\n' "$RED" "$*" "$RESET" >&2; exit 1; }
ok()   { printf '%s✓%s  %s\n' "$GREEN" "$RESET" "$*"; }

# `read -r -p` writes the prompt to stdout, which would pollute a captured value. Prompting
# on stderr keeps `$(prompt ...)` clean.
prompt() {
    local label="$1" default="${2:-}" answer
    if [ -n "$default" ]; then
        printf '%s   %s %s[%s]%s: ' "$RESET" "$label" "$DIM" "$default" "$RESET" >&2
    else
        printf '%s   %s: ' "$RESET" "$label" >&2
    fi
    read -r answer
    printf '%s' "${answer:-$default}"
}

confirm() {
    local answer
    printf '   %s [y/N]: ' "$1" >&2
    read -r answer
    [ "$answer" = "y" ] || [ "$answer" = "Y" ]
}

cd "$(dirname "$0")/.."

# ---------------------------------------------------------------------------- #
# Defaults, inferred so the common case needs no typing
# ---------------------------------------------------------------------------- #

# Existing .env values win over inferred ones: someone who set them last time meant it.
env_value() {
    # `|| true` on the grep is load-bearing under `set -e`: a key that is simply absent
    # makes grep exit 1, and inside `$(...)` that aborts the whole script silently — which
    # is exactly what happened the first time this ran on a .env with none of these keys.
    [ -f .env ] || return 0
    grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true
}

# Parse the owner from `git remote`, handling both SSH and HTTPS forms:
#   git@github.com:zerorate/shelter.git   ->  zerorate
#   https://github.com/zerorate/shelter   ->  zerorate
infer_owner() {
    local url
    url="$(git config --get remote.origin.url 2>/dev/null || true)"
    [ -n "$url" ] || return 0
    printf '%s' "$url" \
        | sed -E 's#^git@[^:]+:##; s#^https?://[^/]+/##; s#\.git$##' \
        | cut -d/ -f1 || true
}

# ---------------------------------------------------------------------------- #
# Which service is being released
# ---------------------------------------------------------------------------- #
#
# `make release-api` and `make release-ui` both land here, differing only in this argument. One
# script rather than two because everything except the build context and the default repository
# name is identical — the login, the tag scheme, the confirmation, the .env write-back — and two
# copies would drift on exactly the security-relevant parts.
SERVICE="${1:-api}"

case "$SERVICE" in
    api)
        CONTEXT="./backend"
        DEFAULT_REPO_NAME="shelter-api"
        SERVICE_LABEL="SHELTER-API"
        # The backend image bakes the trained weights and the GeoIP database — see §Contents.
        SHOW_CONTENTS=1
        ;;
    ui)
        CONTEXT="./frontend"
        DEFAULT_REPO_NAME="shelter-ui"
        SERVICE_LABEL="SHELTER-UI"
        SHOW_CONTENTS=0
        ;;
    *)
        die "unknown service '${SERVICE}' — expected 'api' or 'ui'"
        ;;
esac

[ -d "$CONTEXT" ] || die "build context ${CONTEXT} does not exist"
[ -f "${CONTEXT}/Dockerfile" ] || die "no Dockerfile in ${CONTEXT}"

# Per-service .env keys, so releasing the UI does not overwrite the API's remembered coordinates.
# That happened with a single shared SHELTER_TAG: pushing the UI rewrote the tag compose used for
# the API, and the next `docker compose up` pulled an API image that did not exist.
ENV_PREFIX="SHELTER_$(printf '%s' "$SERVICE" | tr '[:lower:]' '[:upper:]')"

DEFAULT_REGISTRY_HOST="$(env_value "${ENV_PREFIX}_REGISTRY_HOST")"
[ -n "$DEFAULT_REGISTRY_HOST" ] || DEFAULT_REGISTRY_HOST="$(env_value SHELTER_REGISTRY_HOST)"
DEFAULT_REGISTRY_HOST="${DEFAULT_REGISTRY_HOST:-ghcr.io}"

DEFAULT_ORG="$(env_value "${ENV_PREFIX}_ORG")"
[ -n "$DEFAULT_ORG" ] || DEFAULT_ORG="$(env_value SHELTER_ORG)"
[ -n "$DEFAULT_ORG" ] || DEFAULT_ORG="$(infer_owner)"
DEFAULT_ORG="${DEFAULT_ORG:-zerorate}"

DEFAULT_REPO="$(env_value "${ENV_PREFIX}_REPO")"
DEFAULT_REPO="${DEFAULT_REPO:-$DEFAULT_REPO_NAME}"

# ---------------------------------------------------------------------------- #
# The tag: {service}_{YYYY-MM-DD}_{shortSHA}
# ---------------------------------------------------------------------------- #
#
# Three parts, each earning its place:
#
#   * **service** — the same registry account may hold both images; a tag that names its service
#     cannot be pulled for the wrong one by a typo in a compose file.
#   * **date** — what a human reads first when deciding which of two tags is current. A bare SHA
#     is unorderable by eye.
#   * **short SHA** — what makes it IDEMPOTENT. Re-running a release on an unchanged tree produces
#     byte-identical coordinates, so a repeated push is a no-op rather than a second artefact
#     claiming to be a different build. Two builds on the same day from different commits get
#     different tags; two builds from the same commit get the same one.
#
# UTC deliberately: a release cut at 23:30 in Lagos and pulled from a European host must not
# appear to be from two different days.
#
# `-dirty` is appended when the tree has uncommitted changes, because the SHA then does not
# describe what was built and silently pretending otherwise is how an unreproducible image ends
# up in production.
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
if ! git diff --quiet HEAD 2>/dev/null || ! git diff --cached --quiet HEAD 2>/dev/null; then
    GIT_SHA="${GIT_SHA}-dirty"
fi
DEFAULT_TAG="${SERVICE}_$(date -u +%Y-%m-%d)_${GIT_SHA}"

say ""
say "${BOLD}SHELTER — release ${SERVICE_LABEL}${RESET}"
say "${DIM}Builds linux/amd64 + linux/arm64 from ${CONTEXT} and pushes one manifest list.${RESET}"

# ---------------------------------------------------------------------------- #
# 1. Coordinates
# ---------------------------------------------------------------------------- #

step "Registry"
REGISTRY_HOST="$(prompt 'Registry host      ' "$DEFAULT_REGISTRY_HOST")"
ORG="$(prompt 'Organisation / user' "$DEFAULT_ORG")"
REPO="$(prompt 'Repository name    ' "$DEFAULT_REPO")"
TAG="$(prompt 'Image tag          ' "$DEFAULT_TAG")"

# ghcr.io requires a lowercase path and rejects anything else with an opaque 403 at push
# time — long after the build has been paid for. Normalising here turns that into a note.
LOWER_ORG="$(printf '%s' "$ORG" | tr '[:upper:]' '[:lower:]')"
LOWER_REPO="$(printf '%s' "$REPO" | tr '[:upper:]' '[:lower:]')"
if [ "$LOWER_ORG" != "$ORG" ] || [ "$LOWER_REPO" != "$REPO" ]; then
    warn "Registries require lowercase paths — using ${LOWER_ORG}/${LOWER_REPO}"
    ORG="$LOWER_ORG"; REPO="$LOWER_REPO"
fi

IMAGE="${REGISTRY_HOST}/${ORG}/${REPO}"

# ---------------------------------------------------------------------------- #
# 2. What is going in
# ---------------------------------------------------------------------------- #

step "Contents"
# Backend-only. The bulk artefacts — the GeoIP database, the trained weights — are baked into the
# API image and have nothing to do with the frontend, so reporting them during a UI release would
# state something true about a different image.
if [ "$SHOW_CONTENTS" = "0" ]; then
    ok "Next.js standalone build — no baked model weights or GeoIP database"
    say "     ${DIM}The UI image is small; everything large lives in the API image${RESET}"
    if [ -f frontend/package-lock.json ]; then
        ok "package-lock.json present — the build is reproducible"
    else
        warn "no package-lock.json — npm will resolve fresh versions at build time"
        confirm "Continue with an unlocked dependency tree?" || die "aborted"
    fi
elif [ -f backend/data/city.mmdb ]; then
    ok "GeoIP city database present ($(du -h backend/data/city.mmdb | cut -f1)) — baked in"
    say "     ${DIM}Deployments will resolve IPs to city names with no licence key${RESET}"
else
    warn "backend/data/city.mmdb is missing"
    say "     ${DIM}The image builds and runs, but the portal shows raw IP addresses${RESET}"
    say "     ${DIM}instead of city names. Run \`make geoip\` first to include it.${RESET}"
    confirm "Continue without city lookup?" || die "aborted"
fi

if [ "$SHOW_CONTENTS" = "1" ]; then
    if compgen -G "backend/app/ml/weights/*.pt" >/dev/null 2>&1; then
        ok "Model weights present — baked in"
    else
        say "   ${DIM}No model weights: inference falls back to documented physical${RESET}"
        say "   ${DIM}thresholds (SAR VV < -16 dB, NDVI < 0.35) at confidence 0.55.${RESET}"
    fi
fi

# ---------------------------------------------------------------------------- #
# 3. Authentication
# ---------------------------------------------------------------------------- #

step "Authentication"

# An existing credential is reused rather than re-prompted. `docker login` writes to
# ~/.docker/config.json (or a keychain helper), so a second release in the same session
# should not ask again.
already_authed=false
if [ -f "${HOME}/.docker/config.json" ] \
   && grep -q "$REGISTRY_HOST" "${HOME}/.docker/config.json" 2>/dev/null; then
    already_authed=true
fi

if [ "$already_authed" = true ]; then
    ok "Already authenticated to ${REGISTRY_HOST}"
    if confirm "Sign in again with a different token?"; then
        already_authed=false
    fi
fi

if [ "$already_authed" = false ]; then
    say "   ${DIM}A GitHub PAT (classic) needs the ${RESET}write:packages${DIM} scope.${RESET}"
    say "   ${DIM}Create one: https://github.com/settings/tokens/new?scopes=write:packages${RESET}"
    say ""

    PAT_USER="$(prompt 'Username           ' "$ORG")"

    # -s so the token is not echoed to the terminal or captured in scrollback.
    printf '%s   %s: ' "$RESET" 'PAT (hidden)       ' >&2
    read -rs PAT
    printf '\n' >&2
    [ -n "$PAT" ] || die "no token given"

    # --password-stdin, never --password: an argument is visible in the process table to
    # every user on the machine for the lifetime of the command.
    if printf '%s' "$PAT" | docker login "$REGISTRY_HOST" -u "$PAT_USER" --password-stdin >/dev/null 2>&1; then
        ok "Signed in to ${REGISTRY_HOST} as ${PAT_USER}"
    else
        die "login failed — check the token has write:packages and has not expired"
    fi
    # Drop it from the environment immediately. Docker has stored what it needs.
    unset PAT
fi

# ---------------------------------------------------------------------------- #
# 4. Confirm, then go
# ---------------------------------------------------------------------------- #

step "Ready"
say "   Image      ${BOLD}${IMAGE}:${TAG}${RESET}"
say "   Platforms  linux/amd64, linux/arm64"
# Stated positively, because the absence of `latest` is a deliberate choice and a reader who
# expects it should learn why here rather than wonder whether the push half-failed.
say "   ${DIM}One tag only — \`latest\` is not published. It would bill a second manifest per${RESET}"
say "   ${DIM}release and name a different image after each one. Alias it deliberately with${RESET}"
say "   ${DIM}\`make retag TAG=${TAG} ALIAS=latest\` if you want a floating pointer.${RESET}"
say ""
confirm "Build and push?" || die "aborted"

step "Tests"
# Before the build, not after: the suite runs offline in seconds, and there is no reason to
# spend an ARM build cycle on an image that fails it.
make --no-print-directory check || die "checks failed — nothing was pushed"

step "Build and push"

# ---------------------------------------------------------------------------- #
# Builder selection — and why the UI build needs a NATIVE one
# ---------------------------------------------------------------------------- #
#
# The `docker-container` driver builds foreign architectures through an emulator, and WHICH
# emulator decides whether the frontend builds at all.
#
# ## On Apple Silicon: enable Rosetta, or the UI build cannot succeed
#
# Under QEMU the Next.js build dies:
#
#     [linux/amd64 builder 5/5] RUN npm run build
#     ▲ Next.js 16.3.0 (Turbopack)
#     qemu: uncaught target signal 11 (Segmentation fault) - core dumped
#
# Turbopack is a native Rust binary and QEMU mistranslates its threading/SIMD. `next build
# --webpack` avoids Turbopack but then *stalls* indefinitely under QEMU — measured at 0% CPU after
# 26 minutes — so this is not a bundler problem to work around in the Dockerfile.
#
# **Docker Desktop's Rosetta backend fixes it completely.** Measured on an M4 Pro, macOS 26.5:
# the amd64 `npm run build` compiles in 6.1s and the whole builder stage finishes in 18s, with no
# emulator warnings. A full `linux/amd64,linux/arm64` build exits 0.
#
#     Docker Desktop → Settings → General →
#         "Use Rosetta for x86_64/amd64 emulation on Apple Silicon"   ✓
#
# (Same as `UseVirtualizationFrameworkRosetta: true` in
# ~/Library/Group Containers/group.com.docker/settings-store.json. Docker Desktop must be
# restarted for it to take effect.)
#
# So a local multi-arch release IS possible on Apple Silicon — a cloud or native builder is a
# speed and capacity choice, not a requirement.
#
# Order of preference — LOCAL FIRST, deliberately:
#
#   1. $BUILDER, if the caller named one explicitly (make release-ui BUILDER_OVERRIDE=…)
#   2. shelter-builder, created on demand — local, no account, no quota, and with Rosetta enabled
#      it builds both architectures correctly
#
# A cloud builder is NOT auto-selected, even when one is configured. It looks faster and is, until
# the subscription runs out: every node still reports `running`, `buildx inspect --bootstrap` still
# succeeds, and the build is then refused with `build cannot proceed concurrent build limit of 0
# reached` — *after* the context upload. There is no cheap pre-flight that distinguishes the two
# states, so auto-preferring it converts a working local build into a late failure on someone
# else's billing cycle. Opting in is one variable:
#
#     make release-ui BUILDER_OVERRIDE=cloud-acme-ci
BUILDER_NAME="${BUILDER:-}"

if [ -z "$BUILDER_NAME" ]; then
    BUILDER_NAME="shelter-builder"
    docker buildx inspect "$BUILDER_NAME" >/dev/null 2>&1 \
        || docker buildx create --name "$BUILDER_NAME" --driver docker-container --bootstrap >/dev/null
    # A local container builder emulates the other architecture, which is fine — PROVIDED the
    # emulator is Rosetta and not QEMU. Checked rather than assumed, and only for the UI, because
    # that is the build QEMU breaks; the backend merely crawls.
    #
    # The probe is the setting itself rather than a test build: reading a JSON key costs nothing,
    # whereas discovering the answer by building takes ~25 minutes to fail.
    if [ "$SERVICE" = "ui" ] && [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then
        rosetta_setting="${HOME}/Library/Group Containers/group.com.docker/settings-store.json"
        if grep -q '"UseVirtualizationFrameworkRosetta": *true' "$rosetta_setting" 2>/dev/null; then
            ok "Rosetta is enabled — cross-arch UI build works (measured: amd64 compile in ~6s)"
        else
            warn "Docker Desktop is using QEMU, not Rosetta, for amd64"
            say "   ${DIM}The Next.js build WILL fail under QEMU: Turbopack segfaults, and${RESET}"
            say "   ${DIM}--webpack stalls indefinitely. Enable Rosetta and restart Docker:${RESET}"
            say "   ${DIM}  Settings → General → \"Use Rosetta for x86_64/amd64 emulation\"${RESET}"
            say "   ${DIM}Or build a single architecture natively:${RESET}"
            say "   ${DIM}  make release-ui PLATFORMS=linux/arm64${RESET}"
            confirm "Continue anyway?" || die "aborted"
        fi
    fi
fi

docker buildx use "$BUILDER_NAME" 2>/dev/null || die "cannot select builder ${BUILDER_NAME}"
say "   ${DIM}Builder: ${BUILDER_NAME}${RESET}"

# --push rather than build-then-push: buildx assembles the manifest list during the push, so
# a separate `docker push` would have nothing local to send.
# ONE tag. `latest` is deliberately not pushed: it doubles the manifests billed per release,
# and a moving tag means a deployment pinned to it changes under a restart nobody performed.
# `make retag TAG=… ALIAS=latest` adds an alias without rebuilding, when one is actually wanted.
# PLATFORMS is overridable so a single-arch escape hatch exists when emulation is the blocker:
#
#     make release-ui PLATFORMS=linux/arm64        # host arch only, no emulation, always works
#
# A Dokploy VPS is usually amd64, so check `make manifest` matches your host before relying on a
# single-arch push — an image built arm64-only will not run there.
#
# NO REGISTRY BUILD CACHE, and this is the same argument as `latest` above.
#
# `--cache-to type=registry,ref=…:buildcache,mode=max` pushes a SECOND artefact per release —
# every intermediate layer of every stage, for every platform. With `mode=max` on a two-arch
# build of an image carrying torch, a 130 MB GeoIP database and baked weights, that cache is
# comfortably larger than the image itself. So the registry stored more bytes for the cache than
# for the thing being shipped, on every single release.
#
# What it buys is a faster build on a machine with a COLD local cache — a fresh CI runner. This
# project releases from a developer laptop (`make release-both`), where buildx already keeps a
# local cache between builds and the registry round-trip is pure overhead: it has to be uploaded
# after every build and downloaded before the next one.
#
# If a CI runner is ever added, put the cache flags in `release-ci` where the cold-cache
# assumption actually holds, rather than here. GitHub Actions also offers `type=gha`, which is
# scoped to the workflow and costs the container registry nothing.
docker buildx build \
    --platform "${PLATFORMS:-linux/amd64,linux/arm64}" \
    --push \
    -t "${IMAGE}:${TAG}" \
    "$CONTEXT"

# ---------------------------------------------------------------------------- #
# 5. Remember the choices
# ---------------------------------------------------------------------------- #

step "Saving to .env"

set_env() {
    local key="$1" value="$2"
    [ -f .env ] || touch .env
    if grep -qE "^${key}=" .env; then
        # A temp file plus mv rather than `sed -i`: the in-place flag differs between GNU
        # and BSD sed, and this has to work on both macOS and Linux.
        grep -vE "^${key}=" .env > .env.tmp && mv .env.tmp .env
    fi
    printf '%s=%s\n' "$key" "$value" >> .env
}

set_env "${ENV_PREFIX}_REGISTRY_HOST" "$REGISTRY_HOST"
set_env "${ENV_PREFIX}_ORG" "$ORG"
set_env "${ENV_PREFIX}_REPO" "$REPO"
set_env "${ENV_PREFIX}_IMAGE" "$IMAGE"
set_env "${ENV_PREFIX}_TAG" "$TAG"
# The unprefixed keys stay in step for the API only, because docker-compose.yml resolves
# ${SHELTER_IMAGE} and predates the split. Writing them for the UI too would repoint the API's
# services at the frontend image.
if [ "$SERVICE" = "api" ]; then
    set_env SHELTER_REGISTRY_HOST "$REGISTRY_HOST"
    set_env SHELTER_ORG "$ORG"
    set_env SHELTER_REPO "$REPO"
    set_env SHELTER_IMAGE "$IMAGE"
    set_env SHELTER_TAG "$TAG"
fi
ok "Remembered — next release defaults to these, and compose now resolves this image"

say ""
say "${GREEN}${BOLD}Pushed${RESET} ${IMAGE}:${TAG}"
say ""
say "${BOLD}On the deployment host:${RESET}"
say "   ${DIM}# .env${RESET}"
say "   SHELTER_IMAGE=${IMAGE}"
say "   SHELTER_TAG=${TAG}"
say ""
say "   make deploy-pull"
say ""
say "${DIM}Locally, \`docker compose up -d\` now runs this image. To go back to building${RESET}"
say "${DIM}from source, clear SHELTER_IMAGE in .env.${RESET}"
say ""
