# SHELTER

**Satellite Hazard & Early-warning Local Tactical Emergency Response**

> A 7-day warning gives farmers what satellites never did: time to harvest.

**Challenge theme — Climate Resilience Intelligence:** *Building Satellite-Enabled
Early Warning & Alert Services for Environmental, Agricultural, and Health
Intelligence Across Nigeria & Africa.*

Africa's sovereign "amber alert" network for cascading climate disasters.
Live at **[shelter.zerorate.io](https://shelter.zerorate.io)** — a ZeroRate by
FreePass CDN subdomain, with NIGCOMSAT-1R broadcast as the offline last mile.

Built for the **NIGCOMSAT Accelerator 3.0 × FreePass Cohort Hackathon 2026**.

---

## Try it in one command

```bash
git clone <this-repo> && cd c3hkth-team7
make env            # writes .env from the template — no credentials needed
make up             # Postgres + Dragonfly + MinIO + API + 2 worker pools
make health         # confirm every store is up
```

Nothing above needs a credential, a cloud account, or a paid dataset. Confirm the
service is answering:

```bash
curl -s localhost:8000/shelter/v1/api/bootstrap | jq        # what is configured
curl -s localhost:8000/shelter/v1/api/health | jq           # every datastore
curl -s localhost:8000/shelter/v1/api/verification/metrics  # our own accuracy record
```

Then sign up — self-service, no key required — and the portal walks you through
defining a farm and running your first assessment:

```bash
curl -s -X POST localhost:8000/shelter/v1/api/iam/signup/individual \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","first_name":"Ada","last_name":"Farmer",
       "password":"Str0ng-Passw0rd-9xQ","phone":"+2348012345678",
       "language":"en","preferred_channel":"email"}' | jq .session
```

Defining an area triggers a real STAC search, windowed COG reads over Sentinel-1 and
Sentinel-2, two PyTorch forward passes, and a fused risk assessment with a 7-day
outlook. **Step-by-step walkthrough, deployment and the operating manual:
[USAGE.md](USAGE.md).**

### Where to find what

| Document | Contents |
|---|---|
| **README.md** (this file) | Product mission, personas, features, system architecture, data sources, verification status |
| **[USAGE.md](USAGE.md)** | Prerequisites, zero-cost `.env`, build & deploy commands, and the first-time user manual |
| **[CLAUDE.md](CLAUDE.md)** | Engineering invariants — the rules that break things subtly if you don't know them |
| **[openapi.json](openapi.json)** | The full API contract: 100 paths, 119 operations. `make openapi-check` fails the build if it drifts |
| **[docs/](docs/)** | Dataset audit, EO smoke tests against live endpoints, persistence design, AOI input contract |

---

## 1. The problem

Nigeria continues to face interconnected challenges relating to climate
resilience, food security, disaster preparedness, and public health. Seasonal
flooding affects agricultural productivity, damages infrastructure, displaces
communities, and creates environmental conditions that contribute to malaria
transmission.

Across Africa and Nigeria, critical decisions in agriculture, disaster response,
public health, and urban infrastructure suffer from **fragmented data access and
delayed situational awareness**. Satellites continuously generate valuable
information on vegetation health, rainfall, soil moisture, surface water,
atmospheric conditions, and land-use change. Although this data is increasingly
available through open satellite platforms, it remains fragmented, technically
complex, and underutilised for local decision-making. Turning it into actionable
intelligence requires software capable of automating data ingestion, processing,
analysis, and alert generation.

Floods, failed harvests, and malaria outbreaks cost Nigeria lives and
livelihoods every year, yet the satellite data that could anticipate these
threats rarely reaches the people who need it in time.

### Why existing warnings miss

Three failures compound, and each one is a design constraint for SHELTER:

1. **Optical satellites go blind exactly when it matters.** Sentinel-2 cannot
   see through a rainstorm. The cloud hiding the ground is the cloud causing the
   flood, so optical-only systems report nothing during the event they exist to
   detect.
2. **The damage doesn't stop at the water line.** Standing water becomes
   waterlogged roots in days, a lost harvest in weeks, and a malaria surge in
   roughly six. Each stage is predictable from the one before — and each is
   warned about separately, or not at all.
3. **Internet alerts arrive after the network fails.** A flood takes out power
   and backhaul. The channel most warning systems depend on is the one the
   disaster removes first.

---

## 2. Who this is for

Three personas, and the product is shaped by the differences between them. They are
listed in the order the system was designed around — the farmer first, because every
other surface exists to reach them.

### Persona 1 — Amina, smallholder farmer (the end user)

**2.1 ha of rice outside Yenagoa, Bayelsa. Feature phone. No app.**

She does not open a dashboard, will not read a GeoTIFF, and cannot act on a
probability. What she needs is one sentence in the first few seconds — *is my field
in trouble, and what do I do today?* — and enough evidence underneath it that she can
decide whether to believe us before spending a day's labour on drainage.

What the product does for her:

| Her need | How SHELTER answers it |
|---|---|
| Time to act | A 7-day outlook, not a same-day bulletin. Harvesting early is only an option if you know early. |
| Plain language | The advisory leads with the action. `app/explain/` writes "clear the drainage channel on the low side today", never "inundated fraction 0.31". |
| A reason to trust it | The report card shows what changed since the last look, how it compares with normal for *her* field, our confidence as a word, and which satellite saw it and when. |
| Reachability when the network dies | Email today; NIGCOMSAT-1R one-way broadcast is the designed last mile for when a flood takes out power and backhaul. |
| No false alarms | Confidence gates severity — a cloud-limited or untrained read **cannot** raise an EMERGENCY (`CONFIDENCE_ESCALATION_FLOOR = 0.65`). |

**She never sees a map projection, a band index, or a threshold.** Everything in
`app/eo/` exists so that she does not have to.

### Persona 2 — an aggregator (the commercial channel)

**A cooperative, an agricultural lender, a state ADP, or an insurer with hundreds or
thousands of farmers on their books.**

They already own the customer relationship and often want to be the only voice
reaching it. So the platform is multi-tenant by workspace, and an aggregator can
either operate through the portal or drive everything programmatically:

- **Workspaces** — separate projects (a Bayelsa flood pilot, a Kebbi rice season) with
  their own activated intelligence tracks, their own colleagues, and their own API keys.
- **`Workspace → Customer → Monitored area → Alert queue`** — the whole chain in one
  view, including which customer each plot belongs to and exactly where its alerts go.
- **Per-plot delivery mode** — `direct` (SHELTER contacts the farmer), `webhook` (the
  aggregator relays it and SHELTER contacts nobody directly), or `both`.
- **Partner API** — mint a scoped key in the portal, then onboard customers and create
  areas over HTTP. Register a webhook for the asynchronous alert stream.
- **Least privilege** — four scopes (`customers:read`, `customers:write`,
  `scan:trigger`, `webhooks:manage`), bounded by the caller's role *on that workspace*.

### Persona 3 — an operator (NIGCOMSAT / ZeroRate operations)

Runs the service. Needs the queue depth, the delivery receipts, the source health,
and — uniquely — **an honest accuracy record**. That last one is what `Fahis` exists
for: it returns days after an alert, searches independent reporting for the hazard we
warned about, and records a verdict. `GET /verification/metrics` reports precision
over the trainable verdicts with coverage stated beside it, and `precision` is `null`
rather than `0` when nothing is measurable yet.

---

## 3. Product features

### 3.1 The user interface — 23 routes

Next.js App Router, server-side rendered, dark/light with WCAG-checked contrast.
`safeApi` swallows backend errors so a downed API degrades a page instead of 500ing.

**Public**

| Route | What it does |
|---|---|
| `/` | Landing: the problem, the pipeline, the intelligence tracks |
| `/subscribe` | Server-Action signup — no API key ever reaches the browser |
| `/dashboard` | Live operations view: assessments, alert queue, source health |

**Subscriber portal** (`/portal/*`)

| Route | What it does |
|---|---|
| `areas` | **Add and manage monitored plots.** The spatial-query surface — see §3.3 |
| `alerts` | The alert queue: report card, per-track modules, evidence, delivery receipts, Fahis verdict |
| `settings` | Per-plot alert delivery — channel, address, severity floor, delivery mode |
| `webhooks` | Register endpoints, rotate signing secrets, inspect delivery history |
| `api-keys` | **Mint, scope and revoke Partner API keys** (secret shown once) |
| `workspace` | Projects and their activated tracks |
| `workspace/[id]/customers` | Customers, their plots, and the full monitoring chain |
| `team` | Colleagues, roles, invitations |
| `activity` · `security` · `compliance` | Audit log, TOTP + trusted devices, data-handling record |

### 3.2 Open datasets — 16 upstreams, all free, no paid platform

Declared as data in `app/eo/sources.py`: env key, credential requirement, real
publication cadence, and failover target. **Every chain has at least one keyless
source**, so the whole system runs with no credentials at all.

| Role | Sources (in failover order) |
|---|---|
| **Optical imagery** | Element84 Sentinel-2 L2A → Planetary Computer → Landsat 8/9 C2-L2 |
| **Radar imagery** | Sentinel-1 RTC/GRD (Planetary Computer, SAS-signed) |
| **Rainfall** | ClimateSERV GEFS *(forecast)* → CHIRPS → GPM IMERG → ERA5 *(antecedent)* |
| **Soil moisture** | SMAP L3 volumetric — the irrigation signal |
| **Soil** | SoilGrids (clay/sand → drainage class) |
| **Land cover** | ESA WorldCover 10 m |
| **Terrain** | Copernicus DEM 30 m |
| **Population** | WorldPop constrained 100 m |
| **Infrastructure** | OpenStreetMap (Overpass) |
| **Surface-water baseline** | JRC Global Surface Water |
| **Health baseline** | Malaria Atlas Project *Pf*PR |
| **Admin boundaries** | GRID3 Nigeria → geoBoundaries (Africa fallback) |

Two rules that make this trustworthy rather than merely plumbed:

- **Absent data never becomes an implied claim.** `rainfall._flat_series` returns a
  zero series flagged `unavailable` rather than a climatological guess, and the Oracle
  lowers confidence when it fires. An empty `ExposureSummary.sources` means *unknown*,
  not "nobody lives there".
- **Forecast and antecedent are different things.** Only GEFS predicts; CHIRPS, IMERG
  and ERA5 report how wet the ground already is. `forecast_is_prediction` carries the
  distinction all the way to the wording, because "126 mm expected" and "126 mm already
  fell" call for opposite actions.

### 3.3 Supported spatial queries

**Users describe places; they do not supply geometry.** Four ways to define a plot,
all resolving to the same validated record:

| Method | Endpoint | Notes |
|---|---|---|
| **Search by name** | `GET /places/search` | Nominatim, rate-limited to 1 req/s |
| **Browse the admin cascade** | `GET /places/admin/{states,lgas,wards}` + `/extent` | State → LGA → **Ward** — the drill-down that makes a 140 ha farm findable when search fails |
| **Drop a pin + radius** | `POST /places/resolve` | Bbox is the geometry; masking is a no-op |
| **Draw a polygon** | `POST /places/resolve` with a ring | The true outline, applied as a raster mask |

Then, on the resolved area:

| Query | Endpoint |
|---|---|
| Assess now, synchronously | `POST /risk/assess` |
| Queue a scan through the pipeline | `POST /risk/scan` |
| Latest assessment + full history | `GET /risk/areas/{aoi_id}` |
| Reverse-geocode a coordinate | `GET /places/reverse` |
| Preview before saving | `POST /places/preview` |

**Why the polygon matters, measured on real shapes:**

```
square 1 km field    polygon  98.5 ha   envelope  98.5 ha   1.0x
L-shaped field       polygon  68.1 ha   envelope  98.5 ha   1.4x
riverside strip      polygon  72.9 ha   envelope 218.8 ha   3.0x
```

The strip is the shape most flood-exposed smallholdings actually have. Assessing its
envelope means two-thirds of the pixels feeding `inundated_fraction` belong to someone
else's land — which dilutes a real signal toward the threshold and can turn a WARNING
into a WATCH.

**Two guards on the input**, both from real incidents:

- **Service-area check** on the centroid: `-26°..52°E, -36°..28°N`. A mis-reported
  browser geolocation created a monitored "farm" in Warrington, England — which the
  pipeline would have happily measured.
- **Gridded-index verification.** SMAP is on an equal-**area** grid (EPSG:6933), so
  latitude is not linear in row. The obvious arithmetic put Kano's cell ~480 km away
  and returned a perfectly plausible 0.264 m³/m³ from Ibadan. `_verified_cell` reads
  the granule's own coordinates back and **refuses the sample** beyond 0.15°.

### 3.4 Configurable parameter thresholds

All in `app/config.py` (pydantic-settings, `.env`-driven) unless marked as a module
constant. `tests/test_config.py` fails the build if a setting is declared but never
read, or missing from `.env.example`.

**Risk model** — `app/agents/oracle.py`

```
SEVERITY_THRESHOLDS      0.80 EMERGENCY · 0.60 WARNING · 0.40 WATCH · 0.20 ADVISORY · 0.00 INFO
W_OBSERVED  0.55         measured hazard dominates — it is observed, not predicted
W_FORECAST  0.30         7-day rainfall outlook
W_EXPOSURE  0.15         population + infrastructure; modulates, never originates
CONFIDENCE_ESCALATION_FLOOR  0.65    below this, severity is capped at WATCH
NO_FORECAST_CONFIDENCE       0.75    penalty when no genuine forecast was available
```

**Detection** — physical fallbacks used when model weights are absent

| Parameter | Default | Meaning |
|---|---|---|
| SAR water threshold | VV < −16 dB | Trained model reports 0.88 confidence; this fallback 0.55 |
| Crop-stress threshold | NDVI < 0.35 | Below canopy vigour a cereal should show after establishment |
| `SOILGRIDS_HEAVY_CLAY_THRESHOLD` | 350.0 g/kg | Impeded drainage → waterlogging multiplier 1.25 |
| `MALARIA_ENDEMIC_THRESHOLD` | 0.05 | *Pf*PR above which the cascade may be asserted |
| `MAX_SCENE_AGE_DAYS` | 20 | Older imagery is not evidence of today |
| `FORECAST_HORIZON_DAYS` | 7 | The outlook window |

**Delivery** — `app/agents/herald.py` and config

| Parameter | Default | Meaning |
|---|---|---|
| `DISPATCH_FLOOR` | `INFO` | 24/7 proof-of-life: quiet readings are delivered too |
| `RESEND_WINDOW_HOURS` | 18 | Dedupe on `(subscriber, aoi, hazard)`. **Escalation always gets through** |
| `min_severity` (per binding) | `info` | Per-plot, per-channel opt-out |
| `min_score` (per binding) | `null` | **The subscriber's own dial** — minimum risk score 0–1, ANDed with `min_severity`. `null` means the severity ladder governs alone. Filters delivery only; see §3.5 |
| `SCHEDULER_INTERVAL_SECONDS` | 21600 (6 h) | Watch-loop cadence. `SCHEDULER_JITTER_SECONDS` 300 spreads the load |
| `NIGCOMSAT_MAX_PAYLOAD_BYTES` | 280 | Truncated on *encoded* bytes — advisories may be multi-byte |
| `NIGCOMSAT_ALWAYS_BROADCAST_AT` | `warning` | Broadcast regardless of terrestrial success at/above this severity |

**Verification** — `app/agents/fahis.py`

| Parameter | Default | Meaning |
|---|---|---|
| `FAHIS_REPORTING_LAG_DAYS` | 3 | Wait before looking — reporting is not instant |
| `FAHIS_SEARCH_WINDOW_DAYS` | 14 | How far around the event to search |

### 3.5 User-configurable parameters — who can change what, and why

§3.4 lists every threshold in the system. This answers the question it does not: **which of
them can a user move, and which are deliberately ours.** The split is not an oversight, and
the reasoning is in the invariant at the end of this section.

#### What a user configures, in the UI

The left column is the judging criterion verbatim, so nothing here has to be inferred.

| Criterion | Control the user gets | Where | What it changes |
|---|---|---|---|
| *"the UI must let the user adjust the relevant filters, **thresholds**, or **trigger conditions**"* | Severity floor, **per plot and per channel** — five levels, from *advisory and up* to *emergency only* | `/portal/settings` → *Where alerts arrive* | `Subscriber.channels_for` filters dispatch on it (`models/schemas.py`). A row naming one plot **overrides** the general rows for that plot rather than adding to them, so a per-field threshold cannot double-send |
| *"…adjust the relevant **thresholds**"* — the numeric one | **Sensitivity dial**: a minimum risk **score** (0–1) per binding, offered as four named steps | `/portal/settings` → *Sensitivity* | `ChannelBinding.min_score`, ANDed with the severity floor in `channels_for`. Fills the 0.20-wide band between WATCH (0.40) and WARNING (0.60) where the five-step ladder treats every subscriber identically |
| *"…adjust the relevant **filters**"* | Webhook subscriptions: `min_severity`, event type, and `aoi_ids` | `/portal/webhooks` | `webhooks/engine.py` applies all three before delivery |
| *"if a solution surfaces a specific insight…"* | Intelligence track activation, per workspace | `/portal/workspace` | `iam/tracks.hazards_for` maps tracks → hazards. A track that would deliver nothing is labelled "next phase" rather than silently accepted |
| *"lets users **explore and query** spatial data"* | Plot geometry: place search, admin cascade (state → LGA → **ward**), pin + radius, or a drawn polygon | `/subscribe`, `/portal/areas` | `POST /places/preview` validates live as you draw. The ring becomes a raster mask, so an L-shaped field is measured as its true **68.1 ha** and not its **98.5 ha** envelope |
| *"the system must **query or filter the underlying open data streams dynamically** in response to user input"* | **Check now** — assesses that plot against live catalogues on demand | `/portal/areas`, per plot | `POST /risk/assess` → Scout → Analyst → Oracle, 10–40 s of real STAC search and COG reads. Dispatches nothing |
| | Same intent for partner integrations: queued, tenant-scoped | `POST /iam/customers/{id}/areas/{aoi}/scan` | Scope `scan:trigger`, rate-limited per area |
| *"an **asynchronous alert mechanism**… triggered deterministically by the user's configured thresholds"* | Channel and advisory language | `/portal/settings` | Redis Streams → `app/dispatch/`. Channels are refused at the point of choosing unless they have actually delivered (`MVP_CHANNELS`) — a picker that accepts a phone number and then delivers silence is worse than one that does not offer it |

**Nothing on a dashboard is a fixed output.** Editing a plot's geometry re-scans it
immediately (`subscribers.py:604`), because the previous assessment describes different
ground. Adding a plot queues a scan rather than waiting for the next cycle. Both are the
system re-querying live open data in response to user input.

#### What is operator-side, and the invariant that requires it

A user can adjust **which readings reach them** — including numerically, via the sensitivity dial
above. What they cannot adjust is **how a reading is computed**. That line is the whole design, and
it is what makes a user-facing numeric threshold safe here:

> `min_score` is applied in `dispatch/router.deliver`, *after* the assessment has been computed and
> persisted. It can only ever **remove a delivery**. So a raised dial suppresses a message, never a
> measurement — the reading is still in Postgres, still on the plots page, still in the history. A
> subscriber opts out of being *messaged*, not out of being *watched*, and `tests/test_min_score_dial.py`
> asserts that as a property: for every severity and score, the filtered channel set is a **subset**
> of the unfiltered one.

The **scientific** thresholds in §3.4 — `CONFIDENCE_ESCALATION_FLOOR`, the risk weights, the
SAR −16 dB and NDVI < 0.35 fallbacks, the inundation and soil-moisture bands in
`dispatch/tracks.py` — stay `.env`-driven operator config, on the other side of that line:

- **Confidence gates severity, and a user must not be able to ungate it.**
  `OracleAgent._severity` caps at `WATCH` below `CONFIDENCE_ESCALATION_FLOOR` (0.65), so an
  untrained deployment (0.55 fallback) or a cloud-limited read **cannot** raise an EMERGENCY.
  A user-movable floor would let someone dial that cap away and receive an EMERGENCY the
  measurement does not support. The cost of a false emergency in a farming community is a
  wasted harvest and an audience that stops listening to the next real one.
- **`score` / `confidence` / `severity` must stay deterministic functions of measured
  inputs.** That is what makes `tests/test_oracle.py` possible with no provider configured,
  and what lets a WARNING be defended to a state agriculture officer. Per-user detection
  thresholds would mean the same imagery yielded different severities for two subscribers on
  the same field — and no defensible answer to "why did mine say WATCH?".
- **A threshold is only meaningful with its calibration.** SAR −16 dB and NDVI < 0.35 are
  published physical cut-offs; the trained models supersede them at 0.88 confidence against
  0.55. Exposing them as free parameters would invite tuning against a single season's
  outcome — which is retraining, and belongs in `make rebuild` with an evaluation, not in a
  form field.

So the *detection* thresholds are **visible but not editable**: `TrackModules` renders every band
from the one server-side definition in `dispatch/tracks.py`, so a subscriber can read exactly which
cut-off produced their alert (`tests/test_tracks.py` greps both renderers to keep the numbers in one
place).

**One documented limit of the dial**, stated because it would otherwise surprise: the NIGCOMSAT
broadcast escalation is not subject to it. At or above `NIGCOMSAT_ALWAYS_BROADCAST_AT` (default
WARNING) `_should_escalate` fires on severity alone, so a raised dial silences a subscriber's own
channels while a district-wide burst may still reach them. A one-way broadcast addresses a beam, not
a person — there is no per-subscriber addressing in it to filter on, and the same burst reaches
everyone in the footprint whether they subscribe or not. A preference about being messaged is not a
mechanism for opting out of a public emergency signal, and the UI says so rather than overselling
the control.

### 3.6 The report card — answer first, then the evidence

Every alert renders the same way in email and in the portal, from one server-side
definition (`dispatch/base.card_fields` + `dispatch/tracks`), so the two surfaces
cannot describe one plot differently:

1. **Status** — severity and hazard
2. **Since last check** — rising / easing / unchanged, and what it was
3. **Compared with normal** — for this field, this time of year
4. **Confidence** — a word, not a percentage (a percentage invites arithmetic nobody
   should perform on a confidence)
5. **Soil water** — the measured irrigation instruction
6. **Last look / next expected** — which platform saw it, when, and when the next pass is due
7. **Per-track modules** — standing water, crop health, soil water, rain, malaria; each
   openable, each ordered by measured concern rather than a fixed list

**A field is omitted when its input is unknown, never defaulted.** A first assessment
has no previous run; a cloudy cycle measured no soil water. Printing "no change" or
"normal" there would assert something false.

---

## 4. System architecture

### 4.1 End-to-end data flow — ingestion → processing → alert queue

```
╔══════════════════════════════════════════════════════════════════════════════════════╗
║  ① INGESTION — open spatial data, fetched programmatically, zero cost                ║
╚══════════════════════════════════════════════════════════════════════════════════════╝

   OPTICAL              RADAR              RAINFALL             CONTEXT
   Sentinel-2 L2A       Sentinel-1 RTC     GEFS (forecast)      SMAP L3 soil moisture
   Landsat 8/9 C2       (SAS-signed)       CHIRPS ┐             SoilGrids  · WorldCover
        │                    │             IMERG  ├ antecedent  Copernicus DEM
        │                    │             ERA5   ┘             WorldPop · OSM · JRC-GSW
        │                    │                  │               Malaria Atlas · GRID3
        └────────┬───────────┴──────────────────┴──────────────────────┬───────────────┘
                 │                                                     │
        STAC /search across a failover chain                    REST / OGC / Overpass
        + Planetary Computer SAS signing                        (per-source adapters)
                 │                                                     │
                 └──────────────────────────┬──────────────────────────┘
                                            ▼
                            ┌───────────────────────────────┐
                            │  app/eo/  — 17 adapters       │
                            │  stac · cog · indices · auth  │
                            │  rainfall · soil_moisture     │
                            │  exposure · soil · health     │
                            └───────────────┬───────────────┘
                                            │  HTTP RANGE reads: only the AOI window
                                            │  is pulled out of each COG, never a scene
╔═══════════════════════════════════════════▼══════════════════════════════════════════╗
║  ② PROCESSING — five agents, one Redis stream each, fully decoupled                  ║
╚══════════════════════════════════════════════════════════════════════════════════════╝

  shelter:scout        shelter:analyst       shelter:oracle       shelter:herald
  ┌──────────────┐     ┌──────────────┐      ┌──────────────┐     ┌──────────────┐
  │  ① SCOUT     │────▶│  ② ANALYST   │─────▶│  ③ ORACLE    │────▶│  ④ HERALD    │
  │  discover    │     │  measure     │      │  decide      │     │  deliver     │
  ├──────────────┤     ├──────────────┤      ├──────────────┤     ├──────────────┤
  │ STAC search  │     │ windowed COG │      │ 0.55 observed│     │ advisory via │
  │ cloud triage │     │ NDVI/NDMI/   │      │ 0.30 forecast│     │ LLM, grounded│
  │ per-source   │     │  NDWI + SAR  │      │ 0.15 exposure│     │ in evidence  │
  │ poll state   │     │ 2× PyTorch   │      │      ↓       │     │ ONLY         │
  │ MinIO cache  │     │ forward pass │      │ score → sev  │     │ + template   │
  │              │     │              │      │ conf GATES   │     │   fallback   │
  │ NO model call│     │ NO model call│      │ NO model call│     │ suppression  │
  └──────────────┘     └──────────────┘      └──────────────┘     └──────┬───────┘
   skips polls whose    measurements ONLY    the ONLY place that         │
   answer cannot have   — no severity        decides severity            │
   changed; backs off                                                    │
   1h→24h from a dead   ┌─── deterministic, reproducible, testable ───┐  │
   upstream             └── no provider needed for score/confidence ──┘  │
                                                                         │
╔════════════════════════════════════════════════════════════════════════▼═════════════╗
║  ③ ALERT QUEUE & DELIVERY                                                            ║
╚══════════════════════════════════════════════════════════════════════════════════════╝
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              ▼                         ▼                         ▼
     ┌─────────────────┐      ┌──────────────────┐      ┌──────────────────┐
     │  alerts table   │      │  TERRESTRIAL     │      │  ▲ NIGCOMSAT-1R  │
     │  (append-only)  │      │  email ✅        │      │  one-way sat     │
     │                 │      │  webhook ✅      │      │  broadcast       │
     │  + receipts     │      │  whatsapp ·      │      │  ≤280 bytes      │
     │  + report card  │      │  telegram ·      │      │  fires when ALL  │
     │  + per-track    │      │  signal · slack  │      │  terrestrial     │
     │    modules      │      │  (built, gated)  │      │  failed, or at   │
     └────────┬────────┘      └──────────────────┘      │  EMERGENCY       │
              │                                          └──────────────────┘
              │  Portal /portal/alerts  ·  Ops /dashboard  ·  Partner webhook
              │
╔═════════════▼════════════════════════════════════════════════════════════════════════╗
║  ④ ACCOUNTABILITY — Fahis runs BACKWARD, days later, off the main line                ║
╚══════════════════════════════════════════════════════════════════════════════════════╝

     assessments.verify_after  ──(scheduler sweeps)──▶  ┌──────────────┐
     (a Postgres column, not a delayed queue message)   │  ⑤ FAHIS     │
                                                        │  verify      │
     SearXNG (self-hosted) ─── independent reporting ──▶│              │
                                                        └──────┬───────┘
     CONFIRMED · PARTIAL · REFUTED · UNVERIFIED · NOT_ATTEMPTED       │
     (UNVERIFIED is the DEFAULT — a remote LGA flood may              │
      never be reported, and reading silence as REFUTED               │
      would record correct warnings as false alarms)                  │
                                                                      ▼
                                              writes ONLY to  verifications
                                                              agent_memory
                                              next_stage IS None ── it can
                                              never reach an advisory
```

### 4.2 Why the direction of data flow is a safety property

Fahis is the one component whose isolation is enforced *structurally* rather than by
convention. It searches the open web, and web prose must never become a number a farmer
acts on:

- `next_stage is None` — it cannot enqueue anything downstream.
- It writes to `verifications` and `agent_memory` and nowhere else.
- `tests/test_fahis.py` asserts that `oracle`, `analyst`, `scout` and **all of
  `app/eo/`** never import `app.search`.

The same rule applies to advisory generation: the model is handed
`RiskAssessment.evidence` and nothing else, a test asserts that other assessment fields
never reach the prompt, and every failure path returns a deterministic English template.
**This codebase has violated the grounding rule twice** — a hard-coded "35% of the area
is cropland" and a 75/25 split of an OSM union count — so both were removed and the
boundary is now tested rather than trusted.

### 4.3 Persistence — four stores, one rule each

| Store | Holds | Rule |
|---|---|---|
| **Postgres** + PostGIS/pgvector/Timescale | System of record: subscribers, areas, assessments, alerts, receipts, verifications, chat | Nothing above it is durable |
| **Dragonfly db0** | Job streams, dead-letter, dedupe keys | **Never evicted.** A dropped entry is a satellite scan that silently never ran |
| **Dragonfly db1** | Cache | **Every write carries a TTL**, enforced as a required argument |
| **MinIO** | Scene-discovery cache, imagery crops, exports | Persist the **key**; mint URLs per request |
| MongoDB `shelter_IAM` | Identity, workspaces, memberships, API keys, attribution | Identity only — never hazard data |

Two non-obvious consequences, both learned the hard way:

- **Eviction policy is server-wide on Redis and Dragonfly**, not per-database. So db0/db1
  does *not* isolate the queue from an evictor configured for the cache's benefit. Eviction
  is therefore off (`--cache_mode=false`), and `store/cache.py` makes `ttl_seconds` a
  required argument: with no evictor, an untimed key is permanent.
- **Scene-discovery caching has a 30-minute replay ceiling.** A cached `SceneRef` holds
  *SAS-signed* hrefs and Planetary Computer tokens last ~45 minutes. Replaying after that
  hands the Analyst hrefs that 403, the Analyst measures nothing, and the Oracle declines
  to escalate — a silent downgrade caused entirely by our own cache.

### 4.4 Streams, not Pub/Sub — and why streams at all

Redis Streams with consumer groups, so a crashed worker's in-flight job can be reclaimed
(`broker.reclaim_stalled`) instead of vanishing. Pub/Sub is fire-and-forget, so a worker
dying mid-job would lose it silently; its only correct use here is SSE dashboard fan-out.

**Two execution paths, same agent objects** (`app/agents/pipeline.py`):

- `run_inline` / `assess` — synchronous, used by `POST /risk/assess` and the tests
- `enqueue_*` — queued, used by the scheduler

Change agent behaviour and both paths get it. That is why there is no "test mode" branch
anywhere in an agent.

### 4.5 Trace correlation — one `run_id` per scan

One area's journey is five processes, four streams, and minutes to days. `run_id` makes it
a single log query:

```
run_id=run_a1b2c3d4e5f6      # Scout → Analyst → Oracle → Herald, end to end
```

Minted in `enqueue_scan`, copied at every hand-off, and bound into a `ContextVar` so a
`logging.Filter` stamps it onto **every** record — including `app/eo/*` and
`app/dispatch/*`, which know nothing about pipelines. `job.id` cannot do this job: it is
regenerated per stage and again per retry. Fahis deliberately gets a fresh id, because
reusing the scan's would make one `run_id` span a week; `assessment_id` is the join.

### 4.6 Multi-tenancy — scope comes from the credential

Four GET routes once returned the whole platform to a bare `curl`. Only the first was
reported; the rest were found by sweeping for the same shape. `app/api/audience.py` now
resolves one of three callers, most specific first:

1. **Portal session** — checked **first**, deliberately. The frontend attaches its service
   key to every request, so checking the key first made any browser request resolve as
   *unrestricted* — and a subscriber could change another's alert delivery by editing the
   id in the URL.
2. **Platform key** with `platform:read` — unrestricted, when it arrives *alone*.
3. **Aggregator key** with `customers:read` — its workspace's customers, via the
   membership edge.

Three rules that were each the bug:

- **`permitted_subscriber_ids is None` means unrestricted; an empty set means nothing.**
  `if not permitted` matches both, and that is precisely how a brand-new aggregator with
  zero customers saw an unrelated farmer's alerts on first login.
- **A supplied `subscriber_id` may only narrow.** It is a filter, not an authorisation.
- **Cross-tenant reads return 404, not 403**, with the same message as a genuine miss —
  a 403 would turn any id in a URL into a membership oracle.

`tests/test_tenancy.py` sweeps **every** GET route and requires each to be scoped or
listed in `PUBLIC_READS` with a stated reason. That sweep is what makes the fifth
occurrence fail the build instead of shipping.

---

## 5. How it works — the solution in depth

SHELTER narrows the challenge to a specific problem area, user, and output:

| | |
|---|---|
| **Problem area** | Track A — Agricultural Intelligence, running on Track B's SAR flood engine underneath |
| **Target users** | Smallholder farmers and cooperatives; district emergency responders; state agriculture and public-health officers |
| **Validated output** | A 7-day, per-area hazard assessment with severity, confidence, the evidence behind it, and the cascade it is expected to trigger — delivered as a plain-language advisory over seven channels |

Sentinel-1 C-band radar sees through cloud and rain, so the pipeline keeps
working through the storm that blinds optical systems. The Oracle names the
cascade — flood today, crop loss in weeks, malaria in roughly six — while there
is still time to act on all three. NIGCOMSAT-1R broadcast delivers the warning
with no consumer internet at the receiving end.

### Architecture at a glance

**It is a pipeline workflow — a fixed linear sequence, parallel across datasets,
with zero agentic loops.** Two of the five stages are agentic internally; no
orchestration layer is needed or wanted.

That is a deliberate design position, not a limitation:

- **Every successor is a hardcoded class attribute** (`ScoutAgent.next_stage =
  JobStage.ANALYST`). There is no branching, no re-planning, no runtime routing
  decision anywhere. The graph is a straight line, so there is no DAG to solve —
  which is why no orchestration framework is used.
- **Three of five stages make no model call at all.** Scout does STAC discovery,
  Analyst does windowed COG reads plus two PyTorch forward passes, Oracle does
  weighted arithmetic over five measured inputs. Classic ML and maths, local and
  deterministic. An agent framework has nothing to offer them, and keeping it out
  is what makes `score`/`confidence`/`severity` reproducible and defensible.
- **Only Herald's chat and Fahis's adjudication are agentic**, and only *within*
  their own stage (a bounded tool loop and a single structured call). Those two run
  on Pydantic-AI; nothing else does, and a test asserts the boundary.
- **Autonomy lives in the schedule, not the control flow.** Nobody triggers the
  system: the watch loop wakes every 6 hours and sweeps every subscriber unprompted,
  and Fahis returns days later to check its own work. That is autonomous
  *operation* — not autonomous reasoning about what to do next.

**Where the parallelism actually is** — three independent axes:

| Axis | Mechanism |
|---|---|
| **Across datasets** | One job per area on its own stream. Area A can be in Oracle while area B is still in Analyst. `--scale worker-analyst=3` multiplies it |
| **Within a job** | Oracle fires rainfall/exposure/soil/health concurrently; Analyst runs optical and radar legs together; dispatch fans across 7 channels |
| **Within a subscriber** | All their areas gathered together, so one failing area doesn't cost them the others |

**Scout is a stateful machine.** It records, per (area, source), when it last
polled and last succeeded (`source_poll_state`), and `app/eo/sources.py` declares
each upstream's real publication cadence. So Scout skips polls whose answer cannot
have changed — terrain has not moved; Sentinel-1 revisits every ~6 days — and backs
off exponentially from a dead upstream instead of hammering it every cycle. Each
discovery is cached to MinIO so a re-run inside the revisit window replays instead
of re-searching three catalogues.

Statefulness here means **memory between runs**, not a new loop: Scout is still a
queue-consuming stage that runs once per job and hands to Analyst.

**All state runs in one Docker Compose stack**, on an internal network. Postgres
(PostGIS + pgvector + TimescaleDB), MinIO and Dragonfly publish **no host ports** —
the only published surface is the API on `:8000`. That is what
makes it lift onto any VPS with one command, with no separately-hardened datastore to
forget about.

---

Section 10 of the brief requires a distributed, cloud-aware system with a clear
separation across three functional layers. SHELTER maps onto them directly:

| Required layer | SHELTER component |
|---|---|
| **Spatial Processing** | `Scout` + `Analyst` — STAC discovery, windowed COG reads, PyTorch inference (`app/agents/`, `app/eo/`, `app/ml/`) |
| **Alerts & Notification** | `Oracle` + `Herald` — risk fusion, advisory generation, 7-channel dispatch (`app/agents/oracle.py`, `app/advisory/`, `app/dispatch/`) |
| **User Interface** | Next.js SSR dashboard and activation flow (`frontend/`) |
| *Accountability* | `Fahis` — after-the-fact ground-truth verification (`app/agents/fahis.py`) |
| *Supporting infrastructure* | Redis/Dragonfly Streams broker, Postgres system of record, MinIO blobs, containerised workers, autonomous scheduler |

The agents are decoupled — each consumes its own stream and enqueues the next.
They never call each other.

```
                    ┌──────────── open data, zero cost ────────────┐
                    │ Sentinel-1 GRD · Sentinel-2 L2A · CHIRPS     │
                    │ WorldPop · OpenStreetMap · Copernicus DSE    │
                    └───────────────────┬──────────────────────────┘
                                        │  STAC search + COG range reads
   ┌────────────┐   ┌────────────┐   ┌──▼─────────┐   ┌────────────┐
   │  1 SCOUT   │──▶│  2 ANALYST │──▶│  3 ORACLE  │──▶│  4 HERALD  │
   │  discover  │   │  measure   │   │  decide    │   │  deliver   │
   └────────────┘   └────────────┘   └────────────┘   └─────┬──────┘
    STAC search      windowed COG     risk fusion +          │
    cloud triage     + PyTorch        7-day forecast         │
                                      + exposure             │
   └──── Spatial Processing ────┘   └──── Alerts & Notification ────┘
                                                             ▼
                          WhatsApp · Telegram · Signal · Email · Slack
                          · Webhook · ▲ NIGCOMSAT-1R broadcast
                                                             │
   ┌─────────────────────────────────────────────────────────┘
   │  …then, days later, once the window has closed:
   ▼
   ┌────────────┐
   │  5 FAHIS   │  searches independent reporting for the hazard we predicted
   │   verify   │  and records a verdict. Writes ONLY to verifications and
   └────────────┘  agent memory — never back into an advisory.
```

| Agent | Job | Key detail |
|---|---|---|
| **Scout** | What imagery exists? | Flags `optical_blinded` when cloud closes the optical window and hands the cycle to radar rather than reporting nothing. |
| **Analyst** | What do the pixels say? | Reads *only* the AOI window out of each Cloud-Optimized GeoTIFF by HTTP range request — never a whole scene. Two PyTorch models produce standing-water extent and crop-stress fraction. |
| **Oracle** | What does it mean, for whom? | Fuses observed hazard (55%), 7-day rainfall (30%) and exposure (15%). Confidence gates severity — a low-confidence read **cannot** raise an emergency. |
| **Herald** | Who needs to know? | Generates the advisory in the subscriber's language, grounded strictly in the Oracle's evidence, then fans out across channels. |
| **Fahis** | *Were we right?* | Runs days after delivery. Searches independent reporting for the hazard, in that place, in that window, and records a verdict. The only source of ground truth the system has. |

#### Fahis — the accountability agent

Every early-warning system claims accuracy. Almost none can show it. Without
trained weights (`app/ml/weights/*.pt` are absent by default) SHELTER's inference
falls back to documented physical thresholds, and **nothing else in the pipeline
ever learns whether a warning was correct.** Fahis is the answer to that.

It is **off the main line, and the direction of data flow is a safety property.**
Scout → Analyst → Oracle → Herald runs forward to delivery. Fahis runs backward,
writes to `verifications` and `agent_memory`, and has `next_stage = None` so it
cannot enqueue anything downstream. If verification could reach the advisory path,
unattributed web prose would be one hop from a number a farmer acts on — the exact
failure the grounding rule exists to prevent.

**The verdict taxonomy is the part that matters.** The naive design —
confirmed/refuted/unknown — is wrong, because a flood in a remote LGA may never be
reported by anything indexable:

| Verdict | Meaning |
|---|---|
| `confirmed` | Independent sources describe this hazard, in this area, in this window |
| `partial` | Right area, wrong hazard or clearly different severity |
| `refuted` | A source **affirmatively states** it did not occur — rare, and it should be |
| `unverified` | Nothing found either way — **the expected outcome for most rural areas** |
| `not_attempted` | Search was unavailable; no conclusion was attempted |

**Absence of evidence is not evidence of absence.** Reading silence as `refuted`
would record correct warnings as false alarms and train the system on noise. So
`unverified` is the default, and it is enforced twice: the prompt asks for caution,
then `_guard_verdict` checks the model's work — downgrading `refuted` → `unverified`
with no credible source, and `confirmed` → `partial` when only low-tier sources
support it.

Only `confirmed` and `refuted` are trainable. `GET /verification/metrics` computes
precision over those two alone and reports **`coverage` beside it**; including
`unverified` would measure news coverage rather than model accuracy. Precision is
`null` rather than `0` when nothing is yet measurable.

Search is **operator-chosen** — self-hosted SearXNG or managed Tavily, set with
`SEARCH_PROVIDER` and shipped in neither the compose file nor as a default. See
§6 for the trade-off. Whichever you pick, our own domain is excluded from results,
or verification could "confirm" a SHELTER alert by finding SHELTER's own published
alert.

#### Herald chat — explaining an alert

A farmer who receives *"31% of your cropland is under standing water"* reasonably
asks why, how sure we are, and what waterlogging does to rice. `POST /chat`
answers, with tools over their own alerts and their area's history.

**One hard rule:** hazard figures come *only* from the subscriber's own
assessments. Web search supplies background context — what a hazard does, what an
agency announced — and may never contribute a number about their hazard. Enforced
in the prompt, in the tool descriptions, and structurally: `subscriber_id` is
*closed over* rather than being a tool argument, so the model has no parameter in
which to ask for someone else's data.

**Why COGs matter.** A Sentinel-2 scene is ~1 GB; an AOI is a few km² of it.
COGs are internally tiled with an index header, so `rasterio` fetches exactly
the tiles covering the bounding box. Multi-terabyte catalogues are queried and
sliced in memory without downloading a single whole orbit.

### Against the brief's expected characteristics (Section 9)

| Requirement | Where it lives |
|---|---|
| Ingest open EO datasets automatically | `app/scheduler.py` watch loop, every 6h with jitter; `app/eo/stac.py` |
| Analyse satellite-derived environmental information | `app/eo/indices.py`, `app/ml/inference.py` |
| Generate actionable indicators, not raw imagery | `RiskAssessment` — severity, score, evidence, cascade |
| Allow users to define thresholds and alerts | Per-channel `min_severity` on each subscriber binding |
| Provide intuitive dashboards | `frontend/app/dashboard` |
| Deliver automated alerts and notifications | `app/dispatch/` — 7 channels + broadcast escalation |
| *Beyond the brief:* demonstrate the warnings were **correct** | `app/agents/fahis.py` + `GET /verification/metrics` |

### Design decisions worth knowing

- **Nothing is invented.** When the rainfall API doesn't answer, the forecast is
  a flat zero series flagged `unavailable` — not a climatological guess.
  Fabricated numbers in an advisory a farmer acts on are worse than a gap.
- **Confidence gates escalation.** Untrained-model fallbacks and cloud-limited
  reads cap severity at WATCH. The cost of a false emergency in a farming
  community is a wasted harvest.
- **Graceful degradation everywhere.** No API key → template advisories. No
  weights → documented threshold heuristics. One dead channel → the other six
  still deliver.
- **Broadcast is the escalation, not an afterthought.** It fires when every
  terrestrial channel failed, or at WARNING and above regardless.
- **The system checks its own work.** Fahis verifies past alerts against
  independent reporting and biases hard toward `unverified` — a verification agent
  eager to conclude manufactures a ground truth that is really just search-index
  coverage, and because the output looks like data, nobody notices until a model
  has been trained on it.
- **Provider-portable inference.** `app/llm/` speaks only OpenAI-compatible
  `/v1/chat/completions`, so switching frontier providers is `LLM_BASE_URL` plus
  `LLM_API_KEY` — OpenAI, Anthropic, Gemini, DeepSeek, or a self-hosted vLLM.
  A test fails the build if a vendor-only parameter ever appears in that package.

---

## 6. Data sources — the full audit

All sources are free and open. **No proprietary or paid geospatial platform is
required.** Drawn from Section 8 of the hackathon brief.

### Wired and consumed by the running pipeline

Every dataset below has a read path in code. **There are no decorative config
keys** — `tests/test_config.py` fails the build if a setting is ever declared
without being consumed, or documented without existing.

| Dataset | Role in SHELTER | Credential |
|---|---|---|
| **Sentinel-1 GRD** (SAR) | Cloud-independent flood mapping — the Track B engine | none |
| **Sentinel-2 L2A** (optical) | NDVI/NDMI/NDWI, crop-stress inference, SCL cloud masking | none |
| **ESA WorldCover** | **Real** cropland / water / built-up fractions by counting classified pixels | none |
| **Copernicus DEM GLO-30** | Low-lying fraction — where water collects first | none |
| **CHIRPS + GEFS** via **SERVIR ClimateSERV** | 7-day rainfall forecast — the forward-looking half of the risk score | none |
| **NASA GPM IMERG** | Antecedent rainfall, fallback #2 | `NASA_EARTHDATA_TOKEN` |
| **Copernicus ERA5** | Antecedent rainfall, fallback #3 | `ERA5_CDS_KEY` |
| **WorldPop** | Population inside the footprint | none |
| **OpenStreetMap** (Overpass) | Settlements and health facilities, counted separately | none |
| **SoilGrids** (ISRIC) | Drainage class — how long standing water persists | none |
| **Malaria Atlas Project** | Baseline prevalence, gating the malaria cascade | none |
| **AWS Registry of Open Data** | Element84 `earth-search` — the default STAC catalogue | none |
| **Copernicus Data Space** | Authoritative ESA hub, STAC fallback #2 | optional OAuth |
| **Microsoft Planetary Computer** | STAC fallback #3; serves WorldCover | optional key |

**Everything runs with no credentials at all.** The two optional rainfall
tokens only add fallbacks to a chain whose primary source is keyless.

**Every source above is declared explicitly in `app/eo/sources.py`** — its env keys,
whether it needs a credential, its real publication cadence, and what it falls back
to. That registry is what lets Scout decide which sources are *due* rather than
re-polling everything every cycle, and it is reported on `/health` so an operator can
see which feeds are live without reading logs.

`tests/test_datasources.py` holds it to four contracts: every declared env key
exists as a setting, every credential key is genuinely a secret defaulting to
`None`, **every chain has at least one keyless source** (the promise that the
pipeline runs on `.env.example` unchanged), and no failover chain cycles.

Three failover chains keep a scan alive when one endpoint is degraded:

```
imagery   Element84 -> Copernicus Data Space -> Planetary Computer
rainfall  ClimateSERV GEFS (forecast) -> CHIRPS -> GPM IMERG -> ERA5 (observed)
exposure  WorldPop | WorldCover | Copernicus DEM | OpenStreetMap (independent)
```

### Forecast vs. antecedent — the distinction that prevents fabrication

Only **GEFS** produces a genuine forecast. CHIRPS, IMERG and ERA5 are
observational: they report *antecedent wetness* — how saturated the ground
already is — which is a real flood-risk signal but **is not a prediction**.

`RainfallOutlook.forecast_available` keeps the two apart. When it is false the
Oracle weights the signal lower, drops confidence by 25%, and the advisory says
"X mm already fell in the past week; no forward forecast was available" rather
than inventing a number. A climatological guess would put fabricated rainfall
into a message a farmer acts on.

### Web search is not a data source

Fahis and chat query a web search backend, and it is deliberately walled off from
everything above.

**The backend is your choice, and nothing search-related ships in
`docker-compose.yml`.** Verification needs to look at the outside world; which
door it looks through is an operator decision, not a service we impose on every
deployment.

```
SEARCH_PROVIDER=searxng   SEARXNG_URL=https://search.example.io
SEARCH_PROVIDER=tavily    TAVILY_API_KEY=tvly-…
SEARCH_PROVIDER=none      # default
```

| | What you get | What it costs you |
|---|---|---|
| **`searxng`** | Self-hosted. Every query stays on infrastructure you control — and every query names a Nigerian district and a hazard, so that matters more here than it would elsewhere. Same reasoning as self-hosting MinIO and the Signal gateway. | You run and maintain an instance. |
| **`tavily`** | Managed. Working verification in minutes, nothing to operate. `TAVILY_API_BASE` is overridable for a self-hosted or proxied deployment. | Queries leave your infrastructure. |
| **`none`** *(default)* | The pipeline runs normally. Fahis records `NOT_ATTEMPTED` — an outage, never a non-finding. | Precision is unmeasurable: `verification_metrics` reports `precision: null` rather than a made-up zero. |

**They are not interchangeable behind one URL**, which the naming invites you to
assume. SearXNG is `GET /search?q=…&format=json`; Tavily is `POST /search` with
the key in the JSON body, `include_domains` as an array and `days` as an integer.
Verified against the stock SearXNG image: a Tavily-shaped POST returns an **HTML
search page**, ignoring every field in the body. So each provider has its own
adapter in `app/search/client.py`, both normalising into the same `SearchResult` —
`agents/fahis.py` never learns which one answered.

If you self-host SearXNG, one gotcha costs an hour if you meet it cold: **its JSON
API is off by default**, so a stock instance returns `403` for `?format=json` — and
a Tavily-shaped `POST` returns an HTML page rather than an error, which is worse
because it looks like a success.

Enable JSON either in `settings.yml`:

```yaml
search:
  formats: [html, json]
```

…or by environment on the container, which is easier to keep in a compose file:

```yaml
environment:
  - SEARXNG_BASE_URL=${SEARXNG_BASE_URL}
  - SEARXNG_SECRET=${SEARXNG_SECRET}
  - SEARXNG_SEARCH_SAFE_SEARCH=0
  - SEARXNG_SEARCH_FORMATS=['json','html']    # this is the one that matters
```

`SEARXNG_SECRET` is required by the image and unrelated to SHELTER —
`SEARXNG_API_KEY` on our side is a separate, optional bearer token for an instance
sitting behind an authenticating proxy.

Point `SEARXNG_URL` at the instance root (no `/search` suffix); the client appends
the path.

Web results are unattributed prose of unknown recency. Feeding them into a risk
score would break two properties at once: **reproducibility** — `score`,
`confidence` and `severity` are deterministic functions of measured inputs, which
is what makes `test_oracle.py` possible and lets a WARNING be defended to a state
agriculture officer — and the **never-invent-data** rule, since a search snippet
has no typed quantity to extract without inventing the extraction.

So search reaches exactly two places, both outside the assessment path: Fahis
(after the fact, writing only verdicts) and chat (background context for a human
who asked). `tests/test_fahis.py` asserts that `oracle`, `analyst`, `scout` and
every module in `app/eo/` never import `app.search`.

### Not integrated in this MVP — audited, with reasons

Every remaining source in the brief was **tested against its live endpoint**
(2026-08-08) — a ✅ below means the endpoint was actually called, not that a docs
page claimed it.

The useful finding: **five are reachable through catalogues already wired here**, so
adding them is a registry entry plus an adapter, not new infrastructure.

| Source | Access | Verdict |
|---|---|---|
| **JRC Global Surface Water** | `jrc-gsw` on Planetary Computer, keyless ✅ | **Best next addition.** A permanent-water baseline is exactly what the SAR flood model lacks — a river that is always there currently looks like new inundation |
| **Landsat 8/9** | `landsat-c2-l2` on Element84, keyless ✅ | **Add.** Third optical sensor, ~halves the revisit gap — attacks the cloud-blindness problem directly |
| **DHS/MIS** | Indicator API keyless ✅ (4,140 Nigeria indicators, no key) | **Add.** The brief implies gated access; only *microdata* is. A second malaria opinion removes a single point of failure |
| **GRID3 Nigeria** | ArcGIS Hub API, keyless ✅ | Add — fills `admin1`/`admin2`, declared but never populated |
| **NASA SMAP** | NASA CMR, Earthdata login | Add later — rainfall-independent wetness, the weakest current input. HDF5, so moderate work |
| **GHSL** | Direct HTTPS, keyless ✅ | Later — corroborates WorldPop |
| **MODIS** | `modis-*-061` on PC, keyless ✅ | Defer — 250–500 m is too coarse for a smallholder plot. Real value is its long archive for seasonal norms |
| **SRTM / NASADEM** | `nasadem` on PC, keyless ✅ | Skip — Copernicus DEM GLO-30 is the same 30 m and newer |
| **FEWS NET** | Unverified — portal is a JS app | Skip until manually checked; likely PDF-only |
| **TROPOMI / Sentinel-5P** | `sentinel-5p-l2-netcdf` on PC, keyless ✅ | **Out of scope.** Measures NO₂/SO₂/aerosols — air quality, not flood, crop or malaria risk |

**One cost trap in the brief.** `registry.opendata.aws/usgs-landsat` is
**Requester Pays**: the Landsat licence is genuinely free, but reads bill *your* AWS
account and cannot be anonymous. Use Element84 or `landsatlook.usgs.gov/stac-server`
— both keyless, both verified. Every other source in the brief is free with no paid
tier on the paths used here.

---

## 7. Tests

```bash
make check                                   # lint + all 932 tests. What CI runs
cd backend && pytest tests/test_oracle.py    # one file
cd backend && pytest -k low_confidence_cannot # one test
```

**Everything runs offline** — no database, queue, object store, search engine or
inference endpoint required. Full commands and the in-container variant are in
**[USAGE.md §9](USAGE.md#9-tests--verification)**.

The suites and what each protects:

| Suite | Guards |
|---|---|
| `test_config` | Every setting is read somewhere **and** documented in `.env.example`; no credential has a default |
| `test_oracle` | Severity thresholds, confidence gating, cascade rules |
| `test_indices` | NaN propagation — a fully clouded scene must not report 0% stress |
| `test_datasources` | Per-catalogue collection IDs, the silent-failure guard, and the source-registry contracts (keyless primary per chain, no failover cycles) |
| `test_persistence` | Cache TTL is unforgeable, migrations are ordered, Postgres enum labels match `models/enums.py`, keys not URLs are stored |
| `test_fahis` | `unverified` ≠ `refuted`, the verdict guards, and that Fahis/`app/eo`/Oracle can never reach web search |
| `test_search_and_chat` | Own-domain exclusion, and that `subscriber_id` is never a chat tool argument |
| `test_llm_portability` | No vendor-only parameter or SDK in `app/llm/`; both advisory grounding rules survive the provider refactor |
| `test_token_economics` | Deterministic answers fire on routine questions and **not** on specific ones; budgets fail open; advisory generation is never budget-gated |
| `test_agentic_behaviour` | **Behavioural, not structural** — runs full chat and Fahis agents offline via `FunctionModel`. A model that forges `subscriber_id` still can't read another subscriber; a `refuted` over a blog is still downgraded. Also asserts the framework stays out of the deterministic pipeline |
| `test_tracing` | `run_id` reaches modules that never mention it, concurrent runs don't interleave, every hand-off carries it, and legacy envelopes still parse |
| `test_schema_contract` | Every migration table is read or written from Python, or declared in an allow-list with a reason; functions with no callers must say so in their docstring. Catches half-built features that review as done |

Frontend:

```bash
cd frontend
npm run typecheck
npm run build
```

---

## 8. Verification status

What was actually run, and what wasn't:

| Check | Result |
|---|---|
| `pytest` | ✅ **932/932 pass** |
| `ruff check app tests` | ✅ All checks passed |
| **Config contract** | ✅ 201 settings, **zero orphans** — enforced by `tests/test_config.py` |
| `docker compose config` | ✅ valid — base, `+dev` override, and all three profiles (`make validate`) |
| **Fresh-clone start** | ✅ `docker compose config` valid with **no `.env` present** — verified by moving the file aside. `env_file` is `required: false` on all three app services |
| Datastore exposure | ✅ only `api:8000` published; Postgres/MinIO/Dragonfly internal-only. The frontend is not a compose service |
| `/ready` under failure | ✅ returns **503**, not 200 — verified with Dragonfly down and with Postgres down. A 200 carrying `ready: false` passed `curl -fsS` and reported broken deployments as healthy |
| Frontend `tsc --noEmit` | ✅ clean |
| Frontend `npm run build` | ✅ clean, no warnings |
| SSR smoke test, backend offline | ✅ `/`, `/dashboard`, `/subscribe` all HTTP 200 and degrade gracefully |
| `npm audit --omit=dev` | ✅ 0 vulnerabilities (required upgrading Next 15.1.6 → 16.3.0; the pinned version had a CVE plus transitive high-severity `postcss` / `sharp` advisories) |
| Chart palette | ✅ validated — worst-adjacent CVD ΔE 23.3 light / 22.2 dark; severity steps all clear AA text contrast in both modes |
| Optional-dependency check | ✅ backend imports and resolves to the template path with the `anthropic` SDK uninstalled |
| **OpenAPI contract** | ✅ 100 paths, 119 operations, in sync — `make openapi-check` fails the build if it drifts |
| **Migrations** | ✅ 16 applied, `pending_migrations: []` on a live stack |
| **Multi-arch image** | ✅ both `linux/amd64` and `linux/arm64` built; weights load and infer at **0.88** on aarch64; cross-arch divergence 5.4e-07 against a 3.7e-06 decision margin, **0 class flips in 131,072 pixels** |
| **Trained models** | ✅ SAR flood IoU **0.780** vs 0.579 for the −16 dB heuristic; crop-stress precision **0.764** vs 0.344 for NDVI < 0.35 |
| **Docker image build** | ⚠️ **not run** — multi-minute torch + GDAL compile |
| **Live satellite pipeline** | ⚠️ **not run end-to-end** — needs network egress to the catalogues |
| **Channel delivery** | ⚠️ **not run** — no credentials configured |
| **Live Postgres / MinIO / Dragonfly** | ⚠️ **not run** — the persistence tests cover contracts (TTL enforcement, migration ordering, enum parity, object-key derivation) but prove no SQL statement executes |
| **Live SearXNG / LLM call** | ⚠️ **not run against any provider** — payload construction and the structured-output ladder's logic are tested, but no request has been sent |

First live run will surface SQL errors before anything else. Likeliest candidates:
the `GENERATED ALWAYS AS ... ::geography` cast in `002_core.sql`, the
`create_hypertable` call against an existing table, and the JSONB path expressions
in `repository.alert_counters`.

On a first LLM call, watch for `structured output mode rejected; stepping down` in
the logs — one line per rung is normal on a provider without schema support;
repeated lines mean the ladder is probing every call and you should pin
`LLM_STRUCTURED_OUTPUT_MODE`.

### Bugs found and fixed

| Bug | Why it mattered |
|---|---|
| Copernicus searched with Element84 collection IDs (`sentinel-2-l2a` vs `SENTINEL-2`) | **Silent** — that catalogue always returned zero scenes, so the failover chain was really only two deep |
| `requires_signing` declared but never acted on | Every Planetary Computer COG read would 403 — WorldCover is Planetary-only, so exposure would have failed outright |
| `cropland_hectares = area × 1,236,000 × 0.35` | A fabricated 35% cropland assumption presented as measurement |
| OSM settlements/facilities split 75/25 by ratio | Same — one number invented from another |
| `WHATSAPP_TEMPLATE_NAME` read via `os.getenv` | `os.getenv` does **not** read `.env`; only pydantic-settings does. Locally the template silently never applied and Meta dropped every message |
| Email `Message-ID` never set | `provider_message_id` always null, so deliveries were unauditable |
| Oracle transitively imported `rasterio` | Made the risk layer untestable without GDAL; split into `app/eo/geometry.py` |
| 13 orphaned config keys | Filling them in did nothing — the exact failure mode this round was meant to eliminate |

### Confidence levels by module

| Confidence | Modules | Basis |
|---|---|---|
| **High** | `oracle`, `indices`, `geometry`, `soil._classify`, `catalogs`, `auth.sign_href`, `config`, `fahis` verdict logic, `store/cache`, `llm` payload construction | Unit-tested pure logic |
| **Medium** | `stac`, `cog`, `exposure`, `rainfall` (ClimateSERV), `dispatch/*`, `store/repository`, `db/migrations`, `search/client` | Written from published API contracts; SQL and request shapes reviewed but not executed |
| **Lower** | `rainfall._imerg_antecedent`, `rainfall._era5_antecedent`, `health`, `llm.complete_json` step-down heuristics | Fallback adapters written from docs, explicitly flagged in their docstrings. All are optional links in chains whose primary source degrades cleanly |

`docker compose up -d --build` then the `/risk/assess` call in
**[USAGE.md §8.2](USAGE.md#82-exercising-the-satellite-path-on-demand)** is the fastest way to
exercise the medium/lower tiers against live endpoints — one call drives every STAC/COG read, the
whole rainfall chain and both models, and `evidence` names any leg that degraded.

---

## 9. Repository layout

```
backend/
  app/
    agents/      scout (stateful poller) · analyst · oracle · herald · fahis
                 pipeline · base                                       7 modules
    eo/          sources (the dataset registry) · catalogs · auth · stac · cog
                 indices · geometry · admin (GRID3 + geoBoundaries)
                 exposure (WorldCover + DEM + WorldPop + OSM)
                 rainfall (GEFS → CHIRPS → IMERG → ERA5)
                 soil_moisture (SMAP, projection-verified) · soil · health   17
    ml/          models (SARFloodUNet, CropStressNet) · inference        2
    stats/       SPI, seasonal baselines, calibration                    4
    dispatch/    one module per channel + router + base
                 tracks (per-track report-card modules)                 10
    advisory/    provider-agnostic generator with template fallback      1
    explain/     deterministic explainers — crop · drivers · irrigation  4
    agentic/     Pydantic-AI agents — chat + Fahis adjudication ONLY
                 provider (the one place LLM_BASE_URL portability lives)  3
    llm/         OpenAI-compatible transport · embeddings · token budget  3
    search/      SearXNG client (Fahis + chat only, never the risk path)  1
    chat/        cost cascade · answers (zero-token) · pgvector context   2
    email/       SHARED email chrome — layout + base64 logo assets       2
    iam/         accounts · sessions · TOTP · API keys · workspaces
                 teams + RBAC · memberships · attribution · mailer      21
    webhooks/    engine · publisher · store · schemas                    4
    queue/       redis/dragonfly clients (db0 streams, db1 cache)
                 broker · worker                                         3
    db/          asyncpg pool · migration runner · migrations/*.sql (16)  2
    api/         audience (tenancy resolver) · security_schemes
                 area_input (geography + MVP-channel guards) · deps    4
      routes/    health · subscribers · risk · alerts · chat · places
                 verification · webhooks · iam · devdocs                10
    store/       repository (Postgres) · cache (db1, TTL required)
                 objects (MinIO) · poll_state · memory                   5
    models/      schemas · enums · intelligence                          3
    config.py    201 settings, contract-tested
    scheduler.py the watch loop — the only autonomy in the system
    tracing.py   run_id contextvar + logging filter
  tests/         41 files, 932 tests. Notable guards:
                 test_config (settings contract) · test_tenancy (route sweep)
                 test_fahis (import boundary) · test_mvp_channels (AST audit)
                 test_workspace_chain · test_tracks · test_agentic_behaviour
                 test_indices (NaN propagation) · test_llm_portability
frontend/
  app/           23 routes — landing · dashboard · subscribe
                 auth/* (signup, login, verify, reset, invite)
                 portal/* (areas, alerts, settings, webhooks, api-keys,
                           workspace, team, activity, security, compliance)
  components/    AreaPicker · TrackModules · RiskTimeline · SeverityBadge
                 VerdictPanel · SessionGuard · ThemeToggle
  lib/           server-only API client · shared types · session · portal
docs/            dataset audit · EO smoke tests · persistence design
                 AOI input contract · frontend journey review
Makefile                 build · release (multi-arch) · run · operations
docker-compose.yml       API + workers + all state, internal network only
docker-compose.dev.yml   local override — publishes store ports on 127.0.0.1
openapi.json             100 paths, 119 operations — contract-tested
.env.example             229 keys; 169 with working defaults
```

---

## 10. Licence & attribution

Contains modified Copernicus Sentinel data. CHIRPS © UCSB Climate Hazards
Center. Population data © WorldPop. Map data © OpenStreetMap contributors
(ODbL). Rainfall forecasting via SERVIR ClimateSERV (NASA/USAID).

**Forecasts are estimates, not guarantees.** SHELTER is decision support; it
does not replace official government warnings.
