# USAGE — Deployment, Operations & User Guide

**SHELTER** 
**Satellite Hazard & Early-warning Local Tactical Emergency Response**
— satellite early-warning for cascading climate disasters in Nigeria and
Sub-Saharan Africa. This document covers everything needed to run the system and to
operate it as a user. For the product mission, architecture and data sources, see
**[README.md](README.md)**.

Verified against the running stack on **2026-08-11**. Every command below was executed;
where a command has a caveat, the caveat is stated rather than omitted.

---

## Table of contents

1. [Prerequisites](#1-prerequisites)
2. [Zero-cost environment setup](#2-zero-cost-environment-setup)
3. [Build & run locally](#3-build--run-locally)
4. [The frontend](#4-the-frontend)
5. [Deployment](#5-deployment)
6. [User operating manual](#6-user-operating-manual)
7. [Aggregator & Partner API guide](#7-aggregator--partner-api-guide)
8. [Operations runbook](#8-operations-runbook)
9. [Tests & verification](#9-tests--verification)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Prerequisites

### The only hard requirement

| Dependency | Version | Why |
|---|---|---|
| **Docker Engine** | 24+ (tested on 29.5.3) | The whole backend stack |
| **Docker Compose** | v2+ (tested on v5.3.1) | `docker compose`, not `docker-compose` |

That is genuinely all you need for the backend. Postgres, Dragonfly, MinIO, the API and
both worker pools all come from the compose file. **No GDAL, PROJ, Python, or geospatial
toolchain on the host** — that is what the image is for.

### Only if you want the frontend or the no-Docker path

| Dependency | Version | Notes |
|---|---|---|
| **Node.js** | **≥ 20 and < 25** | Pinned in `frontend/package.json`. Node 26 ships an npm that crashes with `require(...) is not a function` in this project — use `nvm use 24` |
| **Python** | 3.10–3.12 | The image runs 3.10.20. **torch 2.5.1 publishes no 3.13/3.14 wheels**, so a newer interpreter cannot install the dependencies |
| `jq` | any | Only for pretty-printing the examples below |

### Hardware

**CPU only. No GPU anywhere.** Both PyTorch models are small (2 MB combined) and run
CPU inference in milliseconds. Multi-arch images are published for
`linux/amd64` and `linux/arm64`, and both legs are exercised — see §5.3.

- **Disk:** ~4 GB for images and volumes (the GeoIP city database alone is 130 MB)
- **RAM:** 4 GB comfortably runs the full stack

---

## 2. Zero-cost environment setup

### 2.1 One command

```bash
make env
```

This copies `.env.example` to `.env` and prints what it did. **It runs as-is** — there
are 229 settings and 169 of them ship with a working default. The 40 blank ones are all
optional credentials that add *fallbacks* to chains whose primary source is already
keyless.

### 2.2 What runs with no credentials at all

| Capability | Works with an empty `.env`? |
|---|---|
| Optical imagery (Sentinel-2, Landsat) | ✅ Element84 STAC — keyless |
| Radar imagery (Sentinel-1) | ✅ Planetary Computer — keyless, SAS auto-signed |
| Rainfall (GEFS → CHIRPS → IMERG → ERA5) | ✅ ClimateSERV is keyless; IMERG/ERA5 tokens only add fallbacks |
| Soil moisture (SMAP) | ✅ Keyless |
| Soil, land cover, terrain, population, OSM | ✅ All keyless |
| Malaria baseline, admin boundaries | ✅ Keyless |
| Both trained ML models | ✅ Baked into the image |
| GeoIP city lookup | ✅ Baked into the image |
| Postgres / Dragonfly / MinIO | ✅ Dev credentials in the compose file |
| **Risk scoring, severity, confidence** | ✅ **Deterministic — needs no model provider** |

### 2.3 What degrades, and how honestly

Nothing crashes when a credential is absent. Each gap has a documented fallback:

| Absent | Behaviour |
|---|---|
| `LLM_API_KEY` / `LLM_BASE_URL` | Advisories come from deterministic **English templates**, not from nothing. Chat returns 503 rather than a canned answer to a specific question |
| `app/ml/weights/*.pt` | Falls back to **documented physical thresholds** (SAR VV < −16 dB, NDVI < 0.35) at confidence **0.55** instead of 0.88. The weights are gitignored but **dockerbaked**, so an image build has them |
| `backend/data/city.mmdb` | The portal shows raw IPs instead of cities. Also gitignored-but-baked |
| `SEARCH_PROVIDER` | Fahis records `NOT_ATTEMPTED` — an outage, explicitly not a finding |
| `BREVO_API_KEY` / SMTP | Email dispatch returns a `SKIPPED` receipt. Nothing raises |

### 2.4 The settings you must set before a public deploy

```bash
API_KEY=            # blank in the template. Required in production — see §5.1
IAM_SESSION_SECRET= # signs portal sessions
POSTGRES_PASSWORD=  # the compose default is a DEV credential
MINIO_ROOT_PASSWORD=
```

`app/preflight.py` refuses to boot in `ENVIRONMENT=production` with the dev defaults
still in place. `tests/test_config.py` fails the build if a credential is given a
non-`None` default.

### 2.5 Optional: enrich advisories with an LLM

Two variables, any OpenAI-compatible provider:

```bash
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
LLM_MODEL=gemini-2.5-flash
LLM_API_KEY=...
```

```bash
LLM_BASE_URL=https://api.openai.com/v1              LLM_MODEL=gpt-4o
LLM_BASE_URL=https://api.anthropic.com/v1           LLM_MODEL=claude-opus-5
LLM_BASE_URL=http://localhost:8000/v1               # vLLM, no key
```

No rebuild needed — edit `.env` and run
`docker compose up -d --force-recreate api worker worker-analyst`. `/health` reports the
resolved provider. **Neither the daily budget nor a token ceiling can gate advisory
generation**, and a test enforces that: a warning must never fail to reach a farmer
because of spend.

---

## 3. Build & run locally

### 3.1 The whole backend

```bash
make up          # dragonfly + postgres + minio + api + 2 worker pools
make ps          # service status and health
make health      # pretty-printed /health
make logs        # follow API and worker logs
make down        # stop, keeping all data
make clean       # stop and DELETE all data
```

`make up` publishes **exactly one port: `api:8000`**. Postgres, MinIO and Dragonfly sit
on the internal `shelter` network with no host ports. That is deliberate and it is what
lets the stack lift onto any VPS with one command.

Expected `make health` output:

```json
{
  "status": "ok",
  "environment": "production",
  "redis": "up",
  "postgres": { "status": "up",
                "extensions": { "postgis": true, "vector": true, "timescaledb": true },
                "pending_migrations": [] },
  "object_store": { "configured": true, "status": "up" }
}
```

### 3.2 When you need the datastore ports

```bash
make dev         # same stack, ports bound to 127.0.0.1
```

Needed only for a host-side psql, a GUI client, or the no-Docker path in §3.5.
**Never safe on a public host** — it publishes the dev-default credentials committed in
the base compose file.

### 3.3 Scaling the slow stage

The Analyst is the bottleneck (COG reads plus forward passes) and is designed to back up
without stalling discovery:

```bash
make scale-analyst N=3
# or directly:
docker compose up -d --scale worker-analyst=3
```

### 3.4 Triggering work by hand

```bash
make scan        # run a watch cycle now, without waiting for the scheduler
make verify      # run the Fahis verification sweep now
make migrate     # apply pending migrations (the API also does this on boot)
make psql        # psql inside the stack, no host port needed
```

### 3.5 Backend without Docker

Only worth it if you are editing Python and want a fast reload loop. **You still want the
datastores from compose** — use `make dev` so they are reachable.

```bash
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# On macOS/Linux you now need GDAL/PROJ present for rasterio and pyproj.
uvicorn app.main:app --reload            # API + the autonomous watch loop, :8000
python -m app.queue.worker               # all five stages in one process
python -m app.queue.worker --stages analyst --concurrency 4
```

`pyproject.toml` sets `pythonpath = ["."]`, so `pytest` resolves `app.*` with no
`PYTHONPATH` export. Run it from `backend/`.

---

## 4. The frontend

```bash
cd frontend
nvm use 24                # Node must be >= 20 and < 25
npm install
npm run dev               # :3000
npm run build             # production build (also typechecks)
npm run typecheck         # tsc --noEmit alone
```

Point it at the API:

```bash
# frontend/.env.local
SHELTER_API_URL=http://localhost:8000/shelter/v1/api
SHELTER_API_KEY=<a platform key>
```

**Never rename these `NEXT_PUBLIC_*`.** `lib/api.ts` is `server-only` and the key
authorises subscriber registration and broadcasts — exposing it to the browser would
hand every visitor the ability to page a district. The subscribe flow is a Server Action
for the same reason.

Pages are `force-dynamic` so the dashboard shows live assessments, and `safeApi`
swallows backend errors — **all three public routes SSR with the backend offline**,
degrading the page instead of 500ing.

---

## 5. Deployment

### 5.1 Single VPS — the intended path

```bash
git clone <repo> && cd c3hkth-team7
make env
# edit .env: API_KEY, IAM_SESSION_SECRET, POSTGRES_PASSWORD, MINIO_ROOT_PASSWORD
make up          # API on :8000, datastores internal-only
```

**The compose stack is API-only, deliberately.** The frontend is not a compose service —
it deploys independently (Netlify, or `npm run build && npm start` on the same box), which
is what lets the backend lift onto any VPS with one command and keeps exactly one port
published.

Then put a reverse proxy in front of `:8000`. One location block is enough, because every
route lives under one prefix: `/shelter/v1/api`.

**Only the API container migrates.** `POSTGRES_AUTO_MIGRATE` is true there and false on
the workers, and `migrations.py` takes a `pg_advisory_lock` so co-booting replicas cannot
race on `CREATE TABLE`.

**The watch loop must run in exactly one process.** `SCHEDULER_ENABLED` is true on the
`api` container and false on the workers. Two schedulers means every scan is queued twice.

### 5.2 Publishing images

```bash
make check                       # lint + full suite. What CI runs
make release-api                 # interactive: SHELTER-API, multi-arch, pushed
make release-ui                  # interactive: SHELTER-UI,  multi-arch, pushed
make release-both                # both, prompting for each
make release-ci REGISTRY=ghcr.io/acme TAG=v1.0.0   # non-interactive, for CI
make image-local                 # single-arch, --load into the local daemon
```

Each prompts for the **registry host, organisation, repository, image tag and PAT**. The PAT is
read with no terminal echo and piped straight to `docker login --password-stdin`, so it never
appears in your shell history, in `make`'s command echo, or in a process listing.

**The tag is idempotent by construction:**

```
{service}_{YYYY-MM-DD}_{shortSHA}        api_2026-08-11_a1b2c3d
                                         ui_2026-08-11_a1b2c3d
```

Re-running a release on an unchanged tree produces byte-identical coordinates, so a repeated push is
a no-op rather than a second artefact claiming to be a different build. Two builds on the same day
from different commits get different tags; two from the same commit get the same one. The date is
**UTC**, so a release cut late in Lagos and pulled from Europe does not appear to be from two days.
A tree with uncommitted changes gets `-dirty` appended — the SHA then does not describe what was
built, and pretending otherwise is how an unreproducible image reaches production.

The chosen coordinates are remembered per service (`SHELTER_API_TAG`, `SHELTER_UI_TAG`), so
releasing one does not repoint the other.

### 5.2b Deploying to Dokploy

`docs/docker-compose.dokploy.yml` is the full stack as one manifest, with both public services
behind Traefik:

```
shelter.zerorate.io        ->  shelter-ui   (:3000)
shelter-api.zerorate.io    ->  shelter-api  (:8000)
```

Two networks, and the split is the security boundary:

| Network | Members | Why |
|---|---|---|
| `dokploy-network` (external) | `shelter-ui`, `shelter-api` only | Traefik's own network. `external: true` attaches to the existing one — without it compose creates a second network of the same name that Traefik is not on, and every route 404s |
| `shelter` (`internal: true`) | Postgres, Dragonfly, MinIO, both worker pools | `internal` removes the gateway from the bridge, so these cannot reach the internet or be reached from it |

The two public services join **both** — Traefik reaches them on one, Postgres on the other — which
is why each carries `traefik.docker.network=dokploy-network` explicitly. A multi-homed container
without that label is the classic "works after this restart, breaks after the next" routing fault.

Set these in Dokploy's environment panel, then paste the manifest and deploy:

```
SHELTER_API_IMAGE / SHELTER_API_TAG      from `make release-api`
SHELTER_UI_IMAGE  / SHELTER_UI_TAG       from `make release-ui`
API_KEY  IAM_SESSION_SECRET  POSTGRES_PASSWORD  MINIO_ROOT_PASSWORD
```

**No secret has a fallback default.** Compose refuses to interpolate without them, which is a clear
failure at deploy time rather than a stack that starts cleanly and is quietly insecure.

`tests/test_dokploy_manifest.py` asserts the manifest structurally — the Traefik ports match the
Dockerfiles' `EXPOSE`, no router points at an undeclared service, no datastore is on the public
network, and exactly one service schedules and migrates. `make validate` parses it too.

### 5.3 Multi-arch — verified on both architectures

`PLATFORMS ?= linux/amd64,linux/arm64`. Both legs are exercised, not assumed:

- The `TARGETARCH` branch in the Dockerfile omits `download.pytorch.org/whl/cpu` on
  arm64 — **that index publishes no aarch64 wheels**, so including it would fail the
  build rather than degrade it. arm64 resolves
  `torch-2.5.1-cp310-cp310-manylinux2014_aarch64.whl` from PyPI instead.
- Weights and `city.mmdb` bake into **both** arches; both models infer at **0.88** on
  aarch64 rather than dropping to the 0.55 threshold fallback.
- One OCI index carries both platforms, each child's `config.architecture` matching its
  descriptor.
- **Cross-arch numerical parity is measured**, not assumed: max divergence 5.4e-07
  against a 3.7e-06 margin to the 0.5 decision boundary, and **0 class flips in 131,072
  pixels**.

Two traps worth knowing:

- A multi-platform build **cannot** be `--load`ed into the local daemon. Use
  `make image-local` when you need a local image.
- `make buildx` creates a local QEMU builder. If you have a Docker Build Cloud builder
  with a native arm64 node, it prints how to opt in — but does **not** switch
  automatically, because building there uploads the build context, model weights and
  `city.mmdb` included. That is the release engineer's decision, not a default.

### 5.4 Retraining requires a rebuild

The weights are **baked, not mounted**. After retraining: `make rebuild` (or a fresh
`make release`), not a container restart.

### 5.5 Frontend on Netlify

The backend stack is API-only by design (`make up` does not start the frontend), so the
frontend can deploy independently. Set `SHELTER_API_URL` and `SHELTER_API_KEY` as
**server-side** environment variables in Netlify — not build-time public ones.

---

## 6. User operating manual

### 6.1 First-time navigation — sign up to first alert

```
  /subscribe  or  /auth/signup
        │
        ▼
  ① Create an account ──────────── individual (a farmer) or organisation (an aggregator)
        │                          A session is issued IMMEDIATELY, before email
        │                          verification — verification gates alert DELIVERY,
        │                          not navigation, because signup funnels die on an
        ▼                          inbox round trip
  ② /portal  ───────────────────── your home. Nothing is monitored yet, and the page
        │                          says so rather than showing an empty chart
        ▼
  ③ /portal/areas ─────────────── DEFINE YOUR FIRST PLOT  ← the spatial query
        │                          (§6.2 — four ways to do it)
        ▼
  ④ Confirmation card ─────────── state · LGA · hectares, with a required tick.
        │                          You cannot create an area without confirming the
        │                          location we resolved is the one you meant
        ▼
  ⑤ Scan queued ───────────────── you receive an email confirming 24/7 monitoring has
        │                          started. The first satellite pass is queued
        ▼
  ⑥ /portal/settings ──────────── choose where alerts arrive, per plot (§6.4)
        │
        ▼
  ⑦ /portal/alerts ────────────── THE ALERT QUEUE (§6.5)
```

### 6.2 Running a spatial query — defining a monitored area

Go to **`/portal/areas`** → *Add a plot*. **You describe a place; you never supply
geometry.** Four routes to the same validated record:

| Method | How | Best for |
|---|---|---|
| **1. Search by name** | Type "Kobape, Ogun" | A place with a name on a map |
| **2. Browse the admin cascade** | **State → LGA → Ward** dropdowns | When search finds nothing. The ward level is what makes a specific 140 ha farm findable instead of a whole district |
| **3. Drop a pin + radius** | Click the map, set a radius | A farm with no name |
| **4. Draw the outline** | Trace the field boundary | **Most accurate** — see below |

Then give it a **name you will recognise** and optionally the **crop** (this changes the
advisory wording — rice tolerates standing water very differently from tomatoes).

**Why drawing the outline matters.** Measured on real shapes:

```
square 1 km field    polygon  98.5 ha   envelope  98.5 ha   1.0x
L-shaped field       polygon  68.1 ha   envelope  98.5 ha   1.4x
riverside strip      polygon  72.9 ha   envelope 218.8 ha   3.0x
```

A riverside strip is the shape most flood-exposed smallholdings have. Without the
polygon, two-thirds of the pixels measured belong to someone else's land — which dilutes
a real signal and can turn a WARNING into a WATCH.

**If your search fails:** the picker falls through to the admin cascade automatically and
says so. It does not leave you at a dead end — that was a reported bug and is now the
documented path.

**If your location is refused:** areas must fall inside `-26°..52°E, -36°..28°N` (Africa
and the Sahel). A browser reporting the wrong position once created a monitored "farm" in
Warrington, England, which the pipeline would have measured perfectly happily. Search by
name instead of using *Use my current location*.

### 6.3 Setting filters — the thresholds you control

**Per plot, at `/portal/settings`:**

| Filter | Options | What it does |
|---|---|---|
| **Channel** | Email | Where alerts arrive. See §6.4 on why the list is short |
| **Address** | your email | Editable — a mistyped address used to be permanent |
| **Send from** | Advisory / Watch / Warning / Emergency | The severity floor. Default is **everything**, including quiet INFO readings |
| **For** | All plots / one named plot | A row naming a plot **replaces** the general ones for that plot — you never get the same alert twice |
| **Who sends it** *(aggregators)* | SHELTER / your webhook / both | §7.4 |

**The severity ladder**, and what each level means:

| Severity | Score | Meaning |
|---|---|---|
| **EMERGENCY** | ≥ 0.80 | Act today |
| **WARNING** | ≥ 0.60 | Act this week |
| **WATCH** | ≥ 0.40 | Watch closely |
| **ADVISORY** | ≥ 0.20 | Worth knowing |
| **INFO** | ≥ 0.00 | We looked; nothing needs doing |

**Why you receive INFO by default.** Silence for three weeks is indistinguishable from a
broken pipeline, and the service promises 24/7 watching. A quiet reading is proof of
life. It is about **one message a day**, not four, because the Herald suppresses an
equal-or-lower severity inside an 18-hour window — and **an escalation always gets
through immediately**, because the dedupe compares severities rather than merely checking
for a recent send.

To opt out per plot, set that plot's floor to `advisory` or higher.

### 6.4 Why only email

Seven channels are implemented. Exactly one — email — has ever delivered a real message.
The others have code, config and tests but no credentials, so a dispatch returns
`SKIPPED` and the subscriber receives **nothing**.

A picker that accepts your WhatsApp number and then delivers silence is worse than one
that does not offer it. So `MVP_CHANNELS = {email, webhook}` and the API refuses anything
else **at the moment you choose it**, with a 422 naming what does work. `webhook` is in
the set but not in the picker: it needs a signing secret and is configured under
Webhooks, where a free-text field would invite an unsigned URL.

### 6.5 Reviewing the alert queue

**`/portal/alerts`** — newest first. Every alert reads the same way, deliberately, so
the eye learns where to look:

```
┌────────────────────────────────────────────────────────────────────────┐
│  ⚠ WARNING · crop waterlogging          Alspecs Farms Kobape           │
│  Standing water on a third of your field                               │
├────────────────────────────────────────────────────────────────────────┤
│  Status               WARNING · crop waterlogging                      │  ① the answer
│  Since last check     Rising — was WATCH                               │  ② what changed
│  Compared with normal Wetter than this field usually is now            │  ③ vs normal
│  Confidence           High                                             │  ④ how sure
│  Soil water           Do not irrigate — drain if you can (0.49 m³/m³)  │  ⑤ the instruction
│  Last look            11 Aug 06:12 UTC · sentinel-1-rtc                │  ⑥ provenance
│  Next expected        Around 17 Aug                                    │
├────────────────────────────────────────────────────────────────────────┤
│  Radar measured water across 31% of your plot yesterday, and another   │
│  126 mm of rain is forecast this week. Your soil drains poorly, so     │
│  this will not clear on its own.                                       │
├────────────────────────────────────────────────────────────────────────┤
│  WHAT TO DO                                                            │
│  1. Clear the drainage channel on the low side today                   │
│  2. Do not irrigate — the root zone is already saturated               │
│  3. Delay fertiliser until the water has gone                          │
├────────────────────────────────────────────────────────────────────────┤
│  WHAT WE MEASURED                          ← tap any module to open it │
│  ▸ STANDING WATER                                              31%     │
│      A substantial part of your plot is under water. Clear drainage    │
│      now — most crops tolerate only two to three days of it.           │
│      How it was measured   SAR U-Net                                   │
│      Radar pass            10 Aug 2026 18:44 UTC                       │
│  ▸ SOIL WATER                                            Saturated     │
│  ▸ CROP HEALTH                                        18% stressed     │
│  ▸ MALARIA RISK                             29% background rate        │
│  ▸ RAIN                                        126 mm expected         │
├────────────────────────────────────────────────────────────────────────┤
│  WHY THIS WAS SENT   (the raw evidence the advisory was written from)   │
│  DELIVERED           email: you@example.com · SENT 11 Aug 06:15        │
│  VERDICT             Fahis: CONFIRMED · 2 independent sources          │
└────────────────────────────────────────────────────────────────────────┘
```

**Reading it well:**

- **The card is ordered by decision value, not by data availability.** Answer, change,
  baseline, confidence, instruction, provenance.
- **A missing row means unknown, never "normal".** A first assessment has no previous
  run; a cloudy cycle measured no soil water. We omit the row rather than print a
  reassuring default.
- **Modules are ordered by measured concern, not by a fixed list.** On a waterlogging
  alert, standing water leads. On a plot that measures saturated soil under a
  vegetation-anomaly classification — a real case, observed at Yenagoa — soil water
  leads, because the measurement outranks the label.
- **Confidence is a word.** A percentage invites arithmetic nobody should perform on a
  confidence.
- **"First satellite pass queued" is not an error.** It means exactly what it says.
- **The verdict is Fahis's**, recorded days later against independent reporting.
  `UNVERIFIED` is the common and honest outcome for a remote LGA — reading silence as
  `REFUTED` would record correct warnings as false alarms.

### 6.6 Asking questions about an alert

Herald's chat answers questions about alerts *you* received. Hazard figures come **only**
from your own measured assessments; web search supplies background context and may never
contribute a number about your hazard.

Most questions cost **zero tokens** — "what should I do", "how sure are you", "why did
you send this" are answered directly from the advisory's own actions, confidence and
evidence, and work with no model provider configured at all.

```bash
curl -s -X POST localhost:8000/shelter/v1/api/chat \
  -H "Authorization: Bearer $SESSION" -H 'Content-Type: application/json' \
  -d '{"question":"What should I do about the water on my field?"}'

curl -s localhost:8000/shelter/v1/api/chat/economics   # zero_token_share
```

---

## 7. Aggregator & Partner API guide

An aggregator — a cooperative, lender, ADP or insurer — chooses **one of two channels**,
and both produce the same records.

### 7.1 Channel A — the web UI

```
/portal/workspace                        create/see your projects
/portal/workspace/{id}/customers         onboard a farmer by hand, add their plots,
                                         and see ACTIVE MONITORING: every plot, which
                                         customer owns it, where its alerts go, and
                                         its alert queue
/portal/team                             colleagues and roles
```

### 7.2 Channel B — the Partner API

**You must mint your own key in the portal first.** There is no key issued at signup —
an account that never integrates should not hold a live credential nobody is watching.

```
/portal/api-keys  →  Create an API key
    Name         "Loan-book importer"
    Workspace    which project this key reaches (a key reaches ONE workspace)
    Permissions  ☑ Read customers        customers:read
                 ☐ Onboard and update    customers:write   ← needed to onboard
                 ☐ Trigger a scan        scan:trigger
                 ☐ Manage webhooks       webhooks:manage   ← needed for async delivery
    Expires      blank for none
```

**The secret is shown once.** It is stored as a hash; there is no reveal endpoint and no
recovery. Copy it immediately — if you lose it, revoke and mint another.

Scopes are bounded by your role **on that workspace**. A wider role on another project
does not apply, so an Owner of the Bayelsa pilot cannot mint a write key for the Kebbi
season where they are View-Only.

### 7.3 Onboarding a customer programmatically

```bash
export PK='shltky…'
export B=http://localhost:8000/shelter/v1/api

curl -s -X POST "$B/iam/customers" \
  -H "X-SHELTER-API-Key: $PK" -H 'Content-Type: application/json' \
  -d '{
    "email":"farmer@example.com",
    "first_name":"Adebayo","last_name":"Ogundimu",
    "phone":"+2348031234567","language":"en",
    "preferred_channel":"email",
    "external_ref":"COOP-2026-0417",
    "area":{"name":"Ogundimu Rice Plot","crop":"rice",
            "bbox":{"west":3.42,"south":7.14,"east":3.46,"north":7.18}}
  }'
```

The workspace is taken **from the key**, not from the URL. `external_ref` is your own
identifier — your loan id or member number — carried through so a reconciliation against
your system needs no join back through our membership table.

Note the split: `/iam/customers*` is the **key-authenticated Partner API**;
`/iam/workspaces/{id}/customers*` is the **session-authenticated web UI**. Same records,
two channels.

Then read the whole chain back:

```bash
curl -s "$B/iam/customers" -H "X-SHELTER-API-Key: $PK"
curl -s "$B/iam/customers/{account_id}/areas" -H "X-SHELTER-API-Key: $PK"
curl -s "$B/alerts" -H "X-SHELTER-API-Key: $PK"
```

### 7.4 Webhooks — required for asynchronous delivery

If you use the Partner API, **register a webhook**: it is how alerts reach you without
polling, and it is mandatory if you relay alerts yourself.

```bash
curl -s -X POST "$B/webhook/subscriptions" \
  -H "X-SHELTER-API-Key: $PK" -H 'Content-Type: application/json' \
  -d '{"name":"Partner relay","url":"https://you.example.com/shelter/alerts",
       "events":["alert.created"]}'
```

The **signing secret is returned once**. Verify every payload with it — a leaked secret
could forge flood alerts into a payout engine, which is why rotation is a hard cutover
with no grace period.

Your key needs `webhooks:manage`, and you see and manage **only your own**
subscriptions: another aggregator's id returns **404**, not 403, so the endpoint cannot
be used to discover whether their integration exists.

**Delivery mode, per plot** — the control that makes you the only voice reaching your
farmer:

| Mode | Behaviour |
|---|---|
| `direct` | SHELTER emails the subscriber. The default |
| `webhook` | **SHELTER contacts nobody directly.** Your webhook is the delivery, and you relay it in your own words |
| `both` | We contact them, and your webhook fires too, for your record |

Set it at `/portal/settings` per plot, or with
`PUT /subscribers/{id}/channels`. Choosing `webhook` with no endpoint registered would
leave nobody delivering the alert, so the API refuses it.

Inspect and manage your integration:

```bash
curl -s "$B/webhook/subscriptions" -H "X-SHELTER-API-Key: $PK"
curl -s "$B/webhook/subscriptions/{id}/deliveries" -H "X-SHELTER-API-Key: $PK"
curl -s -X POST "$B/webhook/subscriptions/{id}/test" -H "X-SHELTER-API-Key: $PK"
curl -s "$B/webhook/event-schema"      # payload shapes — no credential needed
```

**One gotcha, worth knowing before a demo:** a key on an account that is still
`pending_verification` fails authentication. Verify the account's email first.

---

## 8. Operations runbook

### 8.1 Is it working?

```bash
make health                                             # datastores + resolved provider
curl -s localhost:8000/shelter/v1/api/bootstrap | jq    # what is configured
curl -s localhost:8000/shelter/v1/api/verification/metrics | jq
```

A fresh deployment with no subscribers logs an explicit `BOOTSTRAP:` notice naming what
to do next and how many sources are configured. **A new install should read as healthy
and waiting, not broken.**

### 8.2 Exercising the satellite path on demand

`POST /risk/assess` runs Scout → Analyst → Oracle synchronously and hands back the
assessment. It is the fastest way to drive the STAC/COG reads, the rainfall chain and both
models against **live** endpoints without waiting for a watch cycle — which makes it the
right first call after a deploy, and the one to reach for when a source is suspected of
drifting.

Needs a **platform** key with `platform:assess` (`make iam-service-account` mints one; the
`SHELTER_API_KEY` the portal already uses carries it).

```bash
export PK='shltky…'
export B=http://localhost:8000/shelter/v1/api

curl -s -X POST "$B/risk/assess" \
  -H "X-SHELTER-API-Key: $PK" -H 'Content-Type: application/json' \
  -d '{
    "name":"Smoke test — Lokoja floodplain",
    "bbox":{"west":6.70,"south":7.75,"east":6.80,"north":7.85}
  }' | jq '{severity, score, confidence, hazard, data_sources, evidence}'
```

**Expect 10–40 seconds.** It is doing real work: a STAC search per product, windowed COG
range reads, and two forward passes.

What the output tells you, and it is worth reading rather than glancing at:

| Reading | Means |
|---|---|
| `confidence` ≈ **0.88** | Both models loaded. The weights are baked into the image |
| `confidence` ≈ **0.55** | Weights absent — threshold science, not inference. Expected on a git-only build |
| `data_sources` lists several feeds | The failover chains are answering |
| `evidence` naming an unavailable source | That leg degraded, and said so. Not a failure — this is the never-invent-data rule visible in the output |

Two things this call deliberately does **not** do: it dispatches nothing (the Herald never
sees it, so nobody is paged by a smoke test), and it takes a *geometry* rather than an area
id, so it does not belong to any subscriber and does not update a plot's timeline. For a
registered plot, use the portal's **Check now** on `/portal/areas`, or — for a partner
integration — `POST /iam/customers/{id}/areas/{aoi_id}/scan`, which queues instead of
blocking and is scoped to your own customers.

Areas above ~4 deg² are refused with an explanation rather than timing out.

### 8.3 Accuracy — the number that matters

```bash
curl -s localhost:8000/shelter/v1/api/verification/metrics | jq
```

```json
{
  "window_days": 90,
  "confirmed": 1, "partial": 0, "refuted": 0,
  "unverified": 2, "not_attempted": 0, "total": 3,
  "precision": 1.0,
  "coverage": 0.333,
  "note": "precision is computed over confirmed+refuted only; unverified means nobody
           reported it, not that nothing happened"
}
```

*(Real output from the running stack. `coverage: 0.333` is the honest reading of a young
deployment: one of three verifications found independent reporting.)*

Read it carefully: **precision is computed over CONFIRMED and REFUTED only**, with
`coverage` stated beside it. Including UNVERIFIED would measure news coverage, not model
accuracy. `precision` is `null` rather than `0` when nothing is measurable yet — the
honest value for a young deployment.

### 8.4 Following one area's journey

```bash
docker compose logs api worker worker-analyst | grep run_a1b2c3d4e5f6
```

One `run_id` spans Scout → Analyst → Oracle → Herald across five processes and four
streams. Fahis deliberately gets a fresh id — reusing the scan's would make one `run_id`
span a week — and `assessment_id` is the join.

### 8.5 Token spend

```bash
curl -s localhost:8000/shelter/v1/api/chat/economics | jq
# { "zero_token_share": 0.78, "mean_tokens_per_answer": 412, ... }
```

If `zero_token_share` is low, **widen the phrase list in `chat/answers.py`** rather than
loosening the classifier guards. `classify()` is deliberately conservative: a miss costs
one API call, a false positive gives a farmer a canned answer to a specific question.

### 8.6 Queue health

```bash
make logs | grep -iE "reclaim|dead-letter|stalled"
make psql   # then: SELECT severity, count(*) FROM assessments GROUP BY 1;
```

A crashed worker's in-flight job is reclaimed by `broker.reclaim_stalled` rather than
lost — that is why the queue is Streams with consumer groups and not Pub/Sub.

---

## 9. Tests & verification

```bash
make venv        # backend/.venv with test + lint deps (once)
make check       # lint + the full suite. What CI runs
make test        # the suite alone
make lint
make typecheck   # frontend
make validate    # docker compose config, base + dev override + all profiles
```

Current state, measured:

| Check | Result |
|---|---|
| Backend suite | **932 passed** |
| `ruff check app tests` | Clean |
| OpenAPI contract | 100 paths, 119 operations, in sync |
| Frontend | Typechecks, builds, SSRs all three public routes with the backend offline |
| `npm audit --omit=dev` | 0 vulnerabilities |
| Settings contract | Zero orphans; zero orphaned tables outside a declared allow-list |
| Migrations | 16, all applied |

Running the suite inside the container needs the repo mounted — the image bakes `app/`
and does not ship `tests/`:

```bash
docker compose run --rm -v "$(pwd):/repo:ro" -w /repo/backend api pytest
```

### What the tests actually guard

Most are structural, and that is the point — they encode decisions that are easy to
break by accident:

- `test_tenancy.py` sweeps **every** GET route and requires each to be scoped or listed
  as public with a stated reason
- `test_config.py` fails if a setting is declared but never read, missing from
  `.env.example`, or a credential has a non-`None` default
- `test_fahis.py` asserts structurally that the risk layer never imports `app.search`
- `test_mvp_channels.py` walks the route AST so the next unguarded write path fails the
  build
- `test_agentic_behaviour.py` runs a full agent offline with `FunctionModel`: a scripted
  model that forges `subscriber_id` still cannot read another subscriber

---

## 10. Troubleshooting

| Symptom | Cause & fix |
|---|---|
| `require(...) is not a function` on `npm install` | Node 26. `nvm use 24` |
| torch has no matching distribution | Python 3.13/3.14. Use 3.10–3.12 |
| Every cache call errors | Dragonfly needs `--dbnum=2` or higher, or `SELECT 1` fails |
| `pending_migrations` non-empty | `make migrate`. Only the API container auto-migrates |
| Container restart-loops on boot | `app/preflight.py` refuses production with dev defaults. Read the log line — it names the setting |
| Models report confidence 0.55 | Weights absent. Expected in a git-only tree; a built image has them (`make fresh-check` proves both directions) |
| Portal shows raw IPs | `city.mmdb` absent. Gitignored but dockerbaked |
| Assessment returns 0% on a cloudy day | Correct. NaN means "no data" and propagates; a fully clouded scene must not report 0% stress |
| `whatsapp delivery is not available yet` (422) | Working as designed — see §6.4 |
| Aggregator API key returns 401 | The account is `pending_verification`. Verify its email |
| Every EO read 403s | Planetary Computer assets must be SAS-signed, and tokens last ~45 min. Check the clock and `_REPLAY_CEILING_MINUTES` |
| Docker disk filling up | `make disk`, then `make prune` (data volumes untouched) |
| `docker compose exec api pytest` collects nothing | The image ships no `tests/`. Mount the repo — see §9 |

### Known limitations, stated plainly

- **Only email has delivered a real message.** WhatsApp, Telegram, Signal and Slack are
  implemented and refused at every write path until one of them actually delivers.
  NIGCOMSAT broadcast is designed and gated.
- **No live SearXNG or LLM call has been made against any provider.** Payload
  construction, the structured-output ladder, refusal parsing and the isolation
  boundaries are all tested — none of which needs a network. When you first point at a
  provider, watch for `structured output mode rejected; stepping down`: one line per
  rung is normal, repeated lines mean the ladder is probing on every call and is worth
  pinning with `LLM_STRUCTURED_OUTPUT_MODE`.
- **Copernicus Data Space serves no Sentinel data over STAC** (verified 2026-08-10). The
  rung defaults to empty so `chain_for` skips it rather than wasting a request per scan.
  Access there is via OData/OpenSearch — a separate adapter, not built.
- **Bulk plain-text upload for aggregators is not built.** Onboarding is one customer per
  call, or by hand in the portal.
- **Browser coverage** is Chrome and Safari on macOS. Edge, iOS and Android are untested.
