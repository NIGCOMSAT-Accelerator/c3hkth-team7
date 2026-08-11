# syntax=docker/dockerfile:1

# Optional. Netlify remains the default frontend deployment (`netlify.toml`); this
# exists for a single-VPS install where `docker compose --profile frontend up`
# should bring up the whole product.
#
# Node is pinned to 22 rather than `lts` or 24: CLAUDE.md records that Node 26
# ships an npm which crashes in this project with `require(...) is not a function`,
# and an unpinned tag would eventually drift into it.

# ---------------------------------------------------------------------------
# Dependencies — cached separately from source so a code edit doesn't reinstall.
# ---------------------------------------------------------------------------
FROM node:22-alpine AS deps

WORKDIR /app
COPY package.json package-lock.json ./

# `npm ci` not `npm install`: honours the lockfile exactly, which is what makes
# the build reproducible.
RUN npm ci


# ---------------------------------------------------------------------------
# Build — produces the standalone server bundle.
# ---------------------------------------------------------------------------
FROM node:22-alpine AS builder

WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

ENV NEXT_TELEMETRY_DISABLED=1

# NEXT_PUBLIC_* are inlined at build time, so this one has to be present now
# rather than injected at run time. Everything server-side (SHELTER_API_URL,
# SHELTER_API_KEY) is read at request time and must NOT be baked in — see
# frontend/lib/api.ts, which is `server-only` for exactly that reason.
ARG NEXT_PUBLIC_SITE_URL=http://localhost:3000
ENV NEXT_PUBLIC_SITE_URL=${NEXT_PUBLIC_SITE_URL}

RUN npm run build


# ---------------------------------------------------------------------------
# Runtime — standalone output only, no toolchain, no full node_modules.
# ---------------------------------------------------------------------------
FROM node:22-alpine AS runtime

WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# Run unprivileged. The container serves HTTP and reads no host filesystem.
RUN addgroup -g 10002 -S shelter && adduser -u 10002 -S shelter -G shelter

# `standalone` contains the pruned node_modules and a server.js entry point; static/
# and public/ are NOT included in it and must be copied alongside.
#
# ## The standalone output nests, and WHERE it nests is not fixed
#
# Next derives the workspace root from `turbopack.root` in next.config.ts, which is pinned to the
# parent of `frontend/`. So the output lands under a directory named after this app:
# `.next/standalone/frontend/server.js`, not `.next/standalone/server.js`.
#
# **This has now broken in both directions.** Copying the parent put the entry point at
# `/app/frontend/server.js` and the container crash-looped on `Cannot find module '/app/server.js'`
# — a MODULE_NOT_FOUND with an empty requireStack, which gives no hint that the cause is a
# directory level. Then hard-coding `/frontend/` broke the build itself with
# `"/app/.next/standalone/frontend": not found`, because the nesting depends on a config value
# rather than on anything visible here.
#
# So the level is DISCOVERED rather than assumed: wherever `server.js` actually is, that directory
# is the app root. `WORKDIR /app` stays the root either way, so the CMD below remains a bare
# `node server.js` and does not encode the nesting.
COPY --from=builder --chown=shelter:shelter /app/.next/standalone/ /tmp/standalone/
RUN set -eu; \
    entry="$(find /tmp/standalone -maxdepth 3 -name server.js -print -quit)"; \
    [ -n "$entry" ] || { echo "no server.js in the standalone output"; exit 1; }; \
    echo "standalone app root: $(dirname "$entry")"; \
    cp -a "$(dirname "$entry")"/. /app/; \
    rm -rf /tmp/standalone; \
    chown -R shelter:shelter /app
COPY --from=builder --chown=shelter:shelter /app/.next/static ./.next/static
# public/ holds the logos and favicon; standalone does not bundle it.
COPY --from=builder --chown=shelter:shelter /app/public ./public

USER shelter
EXPOSE 3000

# Hits the landing page rather than a dedicated endpoint: `/` is SSR and calls
# safeApi.health(), so a 200 here proves the render path works even when the
# backend is down (safeApi degrades rather than throwing).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    # `${PORT}`, not a literal 3000. The port is overridable — a Dokploy host whose own web UI
    # already occupies 3000 runs this on 3100 — and a healthcheck pinned to 3000 would report a
    # perfectly working container as permanently unhealthy, which Traefik then refuses to route to.
    CMD wget -qO- "http://127.0.0.1:${PORT:-3000}/" >/dev/null 2>&1 || exit 1

CMD ["node", "server.js"]
