"""Developer documentation — `/shelter/v1/api/dev-docs`.

## What this is, and why it is not just `/docs`

`/docs` and `/openapi.json` describe the **whole** service, including endpoints an
aggregator must never call: `/iam/service-accounts` provisions platform credentials,
`/iam/audit/organisation` reads another tenant's activity shape, `/verification/sweep`
triggers fleet-wide work. Publishing those to the external developer community is
misleading at best — a partner reads the reference, writes an integration against
`POST /iam/service-accounts`, and gets a 401 they cannot diagnose because the docs said
it exists.

So this serves a **filtered spec**: the endpoints a partner is actually entitled to,
plus the authentication they need to reach them. Same source of truth — the live route
table — with an allow-list applied, so it cannot drift from the running service the way
a hand-written reference would.

## The filtering rule, stated precisely

An endpoint appears here only if a **commercial aggregator API key** can successfully
call it. That excludes three groups:

| Excluded | Why |
|---|---|
| Platform/service-account routes | Not obtainable by a partner; `platform:*` scopes are service-account only |
| Portal-session routes (`/iam/me`, signup, login, MFA) | A human's browser flow, not a machine integration |
| Fleet operations (`/risk/scan`, `/verification/sweep`) | Cost scales with every subscriber, not one tenant |

**Authentication is documented, not hidden.** A developer needs to know how to get and
send a key, so the auth scheme and the key lifecycle are described in the introduction
even though the *portal* endpoints that mint a key are not listed — those are a browser
flow, and pointing an integration at them would be wrong.

## Why ReDoc rather than Swagger UI here

Swagger UI is a *console*: its value is the "Try it out" button, which needs a
credential and mutates real data. That is right for an operator on `/docs` and wrong for
a public reference — a partner evaluating SHELTER should be able to read the contract
without a key, and without a form that appears to let them create subscribers.

ReDoc is a *reference*: three-column, no execution, readable on a phone, and it renders
long descriptions properly, which matters because the auth model needs explaining rather
than listing.
"""

from __future__ import annotations

import copy
import hashlib
import json

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import settings

router = APIRouter(tags=["developer-docs"])

#: Path prefixes an aggregator API key can legitimately call.
#:
#: An allow-list, not a deny-list, and that direction is deliberate: a new *internal*
#: endpoint should be invisible to partners by default. A deny-list would publish every
#: new route until someone remembered to exclude it — and forgetting is silent, whereas
#: forgetting to add to an allow-list is a partner asking why an endpoint is missing.
PARTNER_PATH_PREFIXES: tuple[str, ...] = (
    # Setting up monitoring, in the order a partner integration uses them.
    #
    # These were MISSING from the partner reference, which is the worst possible omission:
    # `/places/resolve` is the endpoint that means an importer never has to construct a
    # bounding box, and without it in the docs a partner would reasonably conclude they must
    # compute one from a place name themselves — the exact work this API exists to remove.
    #
    # All four are gated by `X-SHELTER-API-Key` (any valid key — aggregator or platform
    # service). They were briefly public on the reasoning that the signup form needed them
    # before an account existed; that was wrong, because the portal calls them through Server
    # Actions with the platform service key attached.
    "/places",              # resolve a place + size into a monitorable area
    # Onboard and read your own customers, AND manage their monitored areas:
    # `/iam/customers/{id}/areas` — list, add, rename, remove. Covered by this prefix, which is
    # why adding those routes needed no allow-list change. Verified by reading the generated
    # output rather than assumed, because a prefix that silently fails to match produces a
    # partner reference missing the endpoints an integrator most needs.
    "/iam/customers",
    "/alerts",              # read alerts for your customers
    "/risk/assess",         # assess one area on demand
    "/risk/areas",          # read an area's latest assessment
    "/verification/metrics",  # accountability figures
    "/webhook",             # subscribe to event delivery
    "/health",              # integration smoke test
    "/ready",
)

#: Explicitly excluded even though a prefix above would otherwise match.
#:
#: `/webhook/sweep` is an operator action that retries every pending delivery across all
#: tenants. `/verification/sweep` queues fleet-wide verification. Both are reachable only
#: with a platform scope, so listing them would document a guaranteed 403.
PARTNER_PATH_DENY: tuple[str, ...] = (
    "/webhook/sweep",
    "/verification/sweep",
    "/risk/scan",
    # Requires `platform:broadcast`, which is a SERVICE-account scope — a partner key
    # can never hold it. It also reaches subscribers directly, including the NIGCOMSAT
    # broadcast escalation, so documenting it to partners would advertise a capability
    # that is both unreachable and one we would not grant.
    #
    # Caught by reading the generated output rather than by reasoning: the `/alerts`
    # prefix legitimately covers the read endpoints and swept this one in with them,
    # which is exactly the failure an allow-list-plus-deny-list is meant to catch.
    "/alerts/dispatch",
)


def _is_partner_path(path: str) -> bool:
    """Whether an aggregator key can call this path."""
    relative = path.removeprefix(settings.api_prefix)
    if any(relative.startswith(d) for d in PARTNER_PATH_DENY):
        return False
    return any(relative.startswith(p) for p in PARTNER_PATH_PREFIXES)


#: Introduction rendered at the top of the reference.
#:
#: Written as onboarding rather than as a schema dump, because the questions a partner
#: has before reading any endpoint are: how do I authenticate, what am I allowed to see,
#: and what happens when something goes wrong. Those are answered here so the endpoint
#: list is not the first thing they have to interpret.
DEVELOPER_INTRO = """
The SHELTER REST API gives partners programmatic access to satellite hazard intelligence
for the areas they manage — flood inundation, crop stress, and the 7-day outlook that
follows from them.

## Who this API is for

**Commercial aggregators** — cooperatives, insurers, state agencies, NGOs — who serve
many individual subscribers and need to onboard them, read their assessments, and receive
alerts in their own systems.

Individual subscribers use the web portal and have no API access. That is deliberate:
a farmer has nothing to integrate, and a credential they cannot use is a credential that
can only be phished out of them.

## Authentication

Every request carries a scoped API key:

```http
GET /shelter/v1/api/iam/customers
X-SHELTER-API-Key: shltky...
```

Keys are 64 characters, prefixed `shltky` (or `shlttk` for test keys). Create one in the
portal under **Developers → API keys**.

**A key is shown exactly once, at creation.** Only a SHA-256 hash is stored, so it cannot
be recovered by you, by us, or by anyone with our database. If you lose it, rotate it.

### Scopes

Keys carry least-privilege scopes, and requesting a wider one is a deliberate act:

| Scope | Allows |
|---|---|
| `customers:read` | Read your own customers, their areas and their alerts |
| `customers:write` | Create and update your own customers |
| `scan:trigger` | Request an immediate assessment for one of your customers' areas |
| `webhooks:manage` | Create and manage webhook subscriptions |

`customers:read` and `customers:write` are granted by default. The other two have side
effects — real satellite catalogue quota, or redirecting an event stream — so they must be
asked for explicitly.

### Rotation

Rotating mints a replacement and puts the current key into a grace window (24 hours by
default), so you can deploy the new key and verify it before the old one stops working.
Set the grace to zero if a key has leaked.

## Setting up monitoring — the whole integration in two calls

**You never need to construct a bounding box.** That is the point of this section, and it is
the thing most partners assume they have to do.

A cooperative's member list has a name, a phone number, a village and a size — not
coordinates. So resolve the words first, then onboard:

### 1. Turn a place and a size into a monitorable area

```http
POST /shelter/v1/api/places/resolve
Content-Type: application/json

{ "place": "Argungu", "size": "5 hectares" }
```

```json
{
  "area": {
    "name": "Argungu",
    "bbox": { "west": 4.44634, "south": 12.69576, "east": 4.44840, "north": 12.69778 },
    "country": "NG", "admin1": "Kebbi State", "admin2": "Argungu",
    "hectares": 5.0
  },
  "resolved_place": "Argungu, Kebbi State, Nigeria",
  "size_description": "about 7 football pitches",
  "size_is_estimate": false,
  "monitoring_cadence": "Checked on every satellite pass — usually every 5 to 6 days…"
}
```

Send `lat`/`lon` instead of `place` if you have coordinates — they are strictly better.

**Sizes are accepted in the units people actually use**, and an unrecognised one is never an
error: `5 hectares`, `5ha`, `12 acres`, `2 plots`, `20 km2`, `medium`, or blank. Anything
unparseable resolves to 2 ha with `size_is_estimate: true`, because a 422 for "about five and
a bit" is a dead end for someone who answered honestly.

`size_description` exists so a human can confirm the result. Nobody can verify "4.86
hectares"; anyone can judge whether a shape on a map is about seven football pitches.

### 2. Onboard the customer with that area

```http
POST /shelter/v1/api/iam/customers
X-SHELTER-API-Key: shltky…

{
  "first_name": "Amina", "last_name": "Bello",
  "email": "amina@example.org",
  "phone": "+2348031234567",
  "language": "ha",
  "external_ref": "COOP-2026-0417",
  "area": { ...the `area` object from step 1, verbatim... }
}
```

That is a live autonomous pipeline. Scout searches Sentinel-1 and Sentinel-2 for that
footprint, Analyst measures it, Oracle scores it, Herald delivers — on every satellite pass,
without anyone triggering it.

### If you already hold coordinates or field outlines

`POST /shelter/v1/api/places/preview` validates one without saving it, and reports
`envelope_ratio`. Above about 1.5, sending the true outline instead of a rectangle measurably
changes the reading — a riverside strip is typically 3×, and its envelope dilutes a real
flood signal by that factor. Use it as a pre-flight per row: it runs the *same* validation as
the write path, so a preview that passes cannot be refused on submission, and a 400-row batch
becomes a report you can hand back rather than 37 opaque failures.

`GET /shelter/v1/api/places/search` and `/places/reverse` are there when you need a place
name resolved on its own, or a pin turned into `country` / `admin1` / `admin2`.

### Address- and building-level input, and the resolution that bounds it

`/places/search` returns the feature's **own outline** in `ring`, with its true area in
`ring_hectares` and a `monitoring` verdict. So resolving "Wuse Market, Abuja" gives you the
market's actual perimeter rather than a rectangle around it — which is what lets you show a
customer their own plot and have them confirm it.

Read `monitoring` before submitting `ring` as an AOI. Three cases, all normal:

    monitoring.outline_is_monitorable  true   -> submit ring verbatim
    monitoring.enlarged                true   -> the outline is a BUILDING, below the floor
    monitoring == null                        -> no outline (a street or a village node)

The floor is physical, not a policy: Sentinel is 10 m/pixel, so a typical building footprint is
~0.15 ha — about **14 pixels** — and a "flooded fraction" over that is dominated by edge effects
and geolocation error. Measured, Kano Central Mosque resolves to a 17-vertex ring of **0.1473 ha**
against a `MIN_AOI_HECTARES` of 0.5.

**This does not mean rejecting a precise address.** It means keeping the location and widening the
measurement: `monitoring.monitored_hectares` is what will actually be watched, and
`monitoring.note` is a sentence written for an end user that you can display verbatim. An
administrative boundary hits the same wall from the other side — Argungu LGA is 101,270 ha against
a 250,000 ha ceiling, Kano State 2,035,580 ha and therefore over it — and reports
`outline_is_monitorable: false` with wording that asks for the plot inside.

### Type-ahead, if the deployment has it

`GET /shelter/v1/api/places/suggest?q=Argun` returns prefix matches for an address box:

    { "results": [{ "label": "Argungu", "detail": "Kebbi, Nigeria", "lat": …, "lon": … }],
      "available": true, "attribution": "© OpenStreetMap contributors" }

**Check `available` before showing "no matches".** It is `false` when the deployment runs no
Photon instance, and an empty `results` means the same thing either way — so treat `false` as
"fall back to `/places/search`", never as "this address does not exist".

Suggestions carry **no geometry** on purpose. They are for completing a text box; the outline and
the administrative hierarchy come from `/places/search` once a user has chosen. Rate limited per
credential per hour and far above interactive typing — if you are bulk-resolving addresses, use
`/places/search` or `/places/resolve`, which are cached and intended for it.

### When a place name finds nothing — browse instead

OpenStreetMap's Nigerian coverage is good for towns and thin for villages. `places/search` for
"Kobape, Ogun State" returns **zero results** while its LGA resolves fine, so an empty result set
is the normal rural case rather than an error.

Three endpoints let you browse to an administrative area instead, which is usually the better
shape for a bulk import anyway:

    GET /places/admin/states                          -> 37 names
    GET /places/admin/lgas?state=Ogun                 -> 20 names
    GET /places/admin/wards?state=Ogun&lga=Obafemi Owode  -> 12 names
    GET /places/admin/extent?state=...&lga=...&ward=...   -> where to put the map

**Names are matched leniently.** Case, spacing, punctuation and the `State` / `LGA` suffixes are
all normalised, and GRID3's own published alternates are consulted — so every one of these
resolves to the same 12 wards:

    Obafemi Owode      Obafemi-Owode      Obafemi/Owode
    Obafemi Owode LGA  obafemi owode      Obafemi Owode Local Government Area

The same applies to states: `Ogun State`, `Federal Capital Territory` and `Abuja` all resolve
(GRID3 spells the FCT `Fct`). What is deliberately **not** done is fuzzy matching — an unknown
name returns an empty list rather than the nearest guess, because monitoring the wrong district on
an 85% similarity is worse than a row you can see failed.

Two things to code against:

  * **`/admin/wards` returns `[]` for 13 states** — Lagos, Rivers, FCT, Anambra, Edo, Ondo, Ekiti,
    Imo, Benue, Plateau, Taraba, Akwa Ibom, Cross River, Ebonyi. GRID3 has no ward layer there and
    geoBoundaries publishes no ADM3 for Nigeria, so treat empty as "skip this tier", not as an
    error.
  * **`/admin/extent` is never a monitoring area.** `is_monitorable_area` is always `false` and
    `note` says which tier it returned. An LGA is ~58 x 63 km and a ward ~18 x 16 km; submitting
    either as an AOI would average a whole district into one reading, and the write path rejects
    anything over ~4 deg squared. Use it to position a map, then send a point and a size to
    `/places/resolve`.

**Every `/places/*` endpoint requires your API key** — the same
`X-SHELTER-API-Key` as the rest of this API. No particular scope is needed: there is nothing
tenant-owned in "where is Argungu?", so the gate is there for attribution and rate control
rather than authorisation.

Two reasons it is gated, both of which affect you:

- `search` and `reverse` proxy a third-party geocoder under a one-request-per-second policy
  enforced across the whole deployment. An open endpoint in front of that is one anyone can
  exhaust, and the block would degrade place search for every subscriber.
- Your consumption is attributable, which is what lets us discuss capacity and terms against
  real numbers rather than estimates.

Every field, and what each optional one changes downstream, is documented on the operation
itself — expand `POST /places/resolve` and `POST /risk/assess` below. If something is
ambiguous, ask us rather than inferring it from a response: we would rather fix the
description than have an integration built on a guess.

## Managing monitored areas

A customer may have **any number of areas** — a farmer with four scattered plots is the normal
case, and each is assessed independently on every satellite pass. There is no per-customer limit.

```http
GET    /iam/customers/{account_id}/areas                 list their plots
POST   /iam/customers/{account_id}/areas                 add one, scanned immediately
PATCH  /iam/customers/{account_id}/areas/{aoi_id}        rename or re-crop
DELETE /iam/customers/{account_id}/areas/{aoi_id}        stop monitoring one plot
POST   /iam/customers/{account_id}/areas/{aoi_id}/scan   assess one plot now
```

### Requesting an immediate assessment

Every area is assessed on the ~6-hour watch loop without you asking. `…/scan` puts one at the
front of the queue, for the case where **you know something we do not**: a member reports water
in the field, a dam release is announced, a plot was onboarded during a developing flood.

Requires the `scan:trigger` scope, which is not granted by default.

```http
POST /iam/customers/A7K2M9P4QX/areas/aoi_091d52d4/scan
X-SHELTER-API-Key: shltky_…
```

```json
202 Accepted
{
  "aoi_id": "aoi_091d52d4",
  "job_id": "job_3f8a1c2e",
  "queued_at": "2026-08-11T09:14:22Z",
  "detail": "Queued for Riverside field. …"
}
```

**202, and the assessment is not in the response.** A scan is 10–40 seconds of satellite reads
and inference. Holding the connection open would make your integration inherit our upstream
latency, and a client timeout at 30s would abandon a scan that was going to succeed — leaving you
with no result and the quota already spent. So the result arrives the way every other assessment
does: as a `shelter.alert` webhook if it crosses the subscriber's threshold, and on
`GET /risk/areas/{aoi_id}` either way.

If you genuinely need a reading in the response, `POST /risk/assess` is synchronous — but it takes
a geometry rather than an area id, so it is not scoped to your customers and does not update the
plot's timeline.

**Rate-limited per area** (4/hour by default), with `Retry-After` on the 429. This is not an
arbitrary ceiling: Sentinel-1 revisits West Africa about every 6 days, so scanning one plot every
minute re-reads the same imagery for an identical answer, on free upstreams every deployment
shares. A 429 does not affect scheduled monitoring, which continues regardless.

Two refusals are deliberate, both `409`: a customer with no subscription has nothing to scan, and
a **paused** subscription would be measured and then never alerted on — a 202 promising a webhook
that cannot arrive is indistinguishable from a broken integration.

### Which edits are safe, and which to avoid

**`name` and `crop` are safe to change at any time.** The update happens in place, the `aoi_id`
survives, and the plot's entire assessment history stays attached *and* stays meaningful — the
ground being described has not changed.

**Geometry is deliberately not editable through this API.** Moving or resizing an area would
leave one timeline mixing measurements of two different footprints under a single name: a
"65% of the area under standing water" reading from last week would describe land your customer
may no longer farm. Interpreting that series afterwards is guesswork, and it is the kind of
guesswork that ends in a wrong lending or payout decision.

**So for a different piece of ground, add a new area.** It costs nothing extra to hold several,
each keeps a clean history from its first pass, and you can remove the one that is out of use.
`DELETE` keeps the past assessments — they were true when measured — and simply stops future
monitoring and billing for that plot.

The one area you cannot remove is a customer's last: a subscription with none is active but
watching nowhere, which reads as working while delivering nothing. Detach the customer instead.

## Tenancy and isolation

Your key sees **only the customers you serve**. A subscriber may be served by several
aggregators at once — their cooperative, their insurer, a state programme — and each sees
only its own relationship.

Requesting a subscriber you do not serve returns **404, not 403**. That is intentional: a
403 would confirm the record exists, which would let one aggregator enumerate another's
customers.

## Errors

| Status | Meaning |
|---|---|
| `401` | Key missing, invalid, revoked or expired |
| `403` | Key is valid but lacks the required scope — the message names it |
| `404` | Not found, or not yours |
| `422` | Request body or path parameter failed validation |
| `429` | Rate limited; retry after the interval in the response |
| `503` | A dependency is unavailable; the operation was not performed |

Errors never partially apply. A 4xx or 5xx means nothing was created or changed.

## Webhooks

Rather than polling, subscribe an HTTPS endpoint and receive events as they happen.
Delivery is **at-least-once** with exponential backoff, so your handler must be
idempotent — deduplicate on the `X-SHELTER-Delivery` header.

Payloads are signed. Verify `X-SHELTER-Signature` before acting: compute
`HMAC-SHA256(timestamp + "." + raw_body)` with your endpoint secret and compare in
constant time. Reject timestamps older than five minutes — the timestamp is what stops a
captured payload being replayed.

### The event, and how to route on severity

There is **one alert event**, `shelter.alert`, and severity is a field inside it rather than part
of the event name. That is deliberate: filter with `min_severity` on your subscription and you
automatically receive everything **at or above** that level, including categories added later. An
event-name filter would silently miss a new category — a partner subscribed to `watch` and
`warning` would not receive `emergency` at all.

```
min_severity: "watch"   ->  watch, warning, emergency
min_severity: null      ->  everything, including info
```

### Reading the intelligence

Every payload carries an `intelligence` block. It exists so you do not have to encode our
thresholds in your own system — switch on `category`, and use the rest to render or escalate.

| Field | Use |
|---|---|
| `category` | The **stable machine token**: `info`, `advisory`, `watch`, `warning`, `emergency`. Switch on this. |
| `label`, `meaning`, `response`, `urgency` | Human wording, safe to display. May be improved over time; do not parse. |
| `confidence` | 0-1, how sure SHELTER is **of its own measurement** — not the probability of the hazard. |
| `confidence_band` | `high`, `good`, `limited`, `low`. Prefer this to a numeric threshold of your own: ours move as models are trained. |
| `severity_capped` | `true` when confidence sat below the escalation floor, so this **could not have been raised above Watch** however severe the measurement. A capped Watch means the data was the ceiling, not the hazard. |
| `track` | `agricultural`, `environmental` or `public_health`. |

`explanations` carries the same three plain-language surfaces a subscriber sees in their email and
portal, so an async integration shows identical wording to the farmer's own view.

### Payload samples — one per category

Every sample is the same event with a different `category`. Fields are elided with `…` only where
they repeat the shape shown above.

**`info` — a routine reading, no action needed**

```json
{
  "event": "shelter.alert",
  "delivery_id": "whd_8f2c1a94",
  "sent_at": "2026-08-10T06:45:07Z",
  "data": {
    "alert_id": "alert_2c9f1b70",
    "severity": "info",
    "hazard": "crop_vegetation_anomaly",
    "intelligence": {
      "category": "info",
      "label": "Info",
      "meaning": "A routine reading. Conditions were measured and nothing needs attention.",
      "response": "No action needed. Useful for tracking how a season is developing.",
      "urgency": "No time pressure",
      "confidence": 0.412,
      "confidence_band": "limited",
      "severity_capped": true,
      "track": "agricultural"
    },
    "explanations": {
      "crop": "The satellite reading shows this plot growing differently from what is normal for the season.",
      "drivers": "This risk level rests on the satellite imagery alone: rainfall data was not available this cycle.",
      "irrigation": "We cannot advise on irrigation this cycle: no soil-moisture measurement was available."
    },
    "advisory": { "headline": "…", "body": "…", "actions": ["…"] },
    "assessment": { "aoi_id": "aoi_091d52d4", "evidence": ["…"], "…": "…" }
  }
}
```

**`advisory` — worth knowing, plan around it**

```json
{
  "event": "shelter.alert",
  "data": {
    "severity": "advisory",
    "hazard": "crop_drought_stress",
    "intelligence": {
      "category": "advisory",
      "label": "Advisory",
      "meaning": "Something has changed that is worth knowing, but it does not threaten the crop yet.",
      "response": "Plan around it rather than reacting to it.",
      "urgency": "Within a few days",
      "confidence": 0.71,
      "confidence_band": "good",
      "severity_capped": false,
      "track": "agricultural"
    },
    "explanations": { "…": "…" },
    "advisory": { "…": "…" },
    "assessment": { "…": "…" }
  }
}
```

**`watch` — prepare now, inspect if you can**

```json
{
  "event": "shelter.alert",
  "data": {
    "severity": "watch",
    "hazard": "flood_inundation",
    "intelligence": {
      "category": "watch",
      "label": "Watch",
      "meaning": "Conditions that could develop into a problem are present now. Not yet a threat, but the direction matters.",
      "response": "Prepare what would be slow to arrange later — drainage, labour, somewhere dry for stored produce. Inspect the area if possible.",
      "urgency": "Next day or two",
      "confidence": 0.55,
      "confidence_band": "limited",
      "severity_capped": true,
      "track": "environmental"
    },
    "explanations": {
      "crop": "Two percent of the plot has standing water. This reading is from radar, which sees through clouds.",
      "drivers": "The watch risk is because 2% of the plot has standing water. Rainfall could not be measured this cycle.",
      "irrigation": "Hold. Two percent of the plot has standing water. Irrigating now could damage the crop."
    },
    "advisory": { "…": "…" },
    "assessment": { "…": "…" }
  }
}
```

**`warning` — act today**

```json
{
  "event": "shelter.alert",
  "data": {
    "severity": "warning",
    "hazard": "flood_forecast",
    "intelligence": {
      "category": "warning",
      "label": "Warning",
      "meaning": "A hazard is likely to affect this area. The measurement and the outlook agree.",
      "response": "Act now. Move what can be moved, clear drainage, and notify others farming nearby.",
      "urgency": "Today",
      "confidence": 0.88,
      "confidence_band": "high",
      "severity_capped": false,
      "track": "environmental"
    },
    "explanations": { "…": "…" },
    "advisory": { "…": "…" },
    "assessment": { "…": "…" }
  }
}
```

**`emergency` — act immediately**

```json
{
  "event": "shelter.alert",
  "data": {
    "severity": "emergency",
    "hazard": "flood_inundation",
    "intelligence": {
      "category": "emergency",
      "label": "Emergency",
      "meaning": "A severe hazard is happening or imminent, with high confidence.",
      "response": "Act immediately and follow official emergency guidance. SHELTER informs that decision; it does not replace it.",
      "urgency": "Immediately",
      "confidence": 0.93,
      "confidence_band": "high",
      "severity_capped": false,
      "track": "environmental"
    },
    "explanations": { "…": "…" },
    "advisory": { "…": "…" },
    "assessment": { "…": "…" }
  }
}
```

### One caveat worth designing around

Until trained model weights are deployed, inference falls back to documented physical thresholds
at confidence 0.55 — below the escalation floor. So on the current deployment **`warning` and
`emergency` are not reachable**: every alert arrives as `watch` at most, with
`severity_capped: true`. Build your handler for all five categories, but do not wait on a
`warning` to test your escalation path.

## Data provenance

Every assessment carries the evidence behind it. Figures come from Copernicus Sentinel-1
radar and Sentinel-2 optical imagery, CHIRPS rainfall, WorldPop and OpenStreetMap — all
open data. Where a source was unavailable, the assessment says so rather than
substituting an estimate.

Hazard assessments are probabilistic forecasts, not guarantees.
"""


def _reachable_schemas(paths: dict, schemas: dict) -> dict:
    """The transitive closure of `$ref`s from the partner paths.

    ## Why the schemas are pruned, when they used to be kept whole

    This function replaces a deliberate earlier decision to leave `components.schemas`
    intact, on the reasoning that an unreferenced schema is harmless in a reference document
    while a *missing* one breaks every `$ref` still pointing at it.

    That reasoning held while the unreferenced leftovers were things like `RiskAssessment` —
    models a partner already sees elsewhere. It stopped holding when the IAM module gained
    workspaces and RBAC: the partner reference was publishing `InviteWrite`, `TeamMember`,
    `WorkspaceGrant`, `RoleOption`, `WorkspacePublic`, `WorkspaceWrite`, `RoleGuide` and
    `TrackInfo` — the full shape of internal team-management, including the field names of an
    invitation payload — with no partner path referencing any of them.

    Nothing was *reachable*: every one of those endpoints is `PortalSession`-gated and absent
    from `paths`. But a schema block is documentation. Publishing the structure of an internal
    authorisation model tells a reader what exists and what to probe for, and it invites a
    partner to build against an endpoint they can never call.

    So the closure is computed properly. Starting from the retained paths, every `$ref` is
    followed until no new schema appears — which keeps every reference resolvable (the
    original concern) while publishing nothing a partner path does not actually use.
    """
    seen: set[str] = set()
    # Walk the paths as raw JSON rather than reasoning about OpenAPI structure: a `$ref` can
    # appear in a request body, a response, a parameter, an array's `items`, a `$defs` entry,
    # or nested inside `anyOf`/`allOf`. Matching on the key is exhaustive by construction,
    # where enumerating the legal positions would silently miss one.
    def walk(node: object) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[-1]
                if name not in seen:
                    seen.add(name)
                    # Recurse into the schema itself — a retained model may reference others.
                    walk(schemas.get(name, {}))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(paths)
    return {name: schemas[name] for name in sorted(seen) if name in schemas}


def partner_schema() -> dict:
    """The filtered OpenAPI document.

    Built from the live app's own schema, so it cannot drift from the running service.
    Paths are filtered by `PARTNER_PATH_PREFIXES` and component schemas are pruned to the
    transitive closure of what those paths reference — see `_reachable_schemas`.
    """
    from app.main import app

    # DEEP-COPIED, and this is a correctness fix rather than hygiene.
    #
    # `app.openapi()` builds the schema once and returns the same cached dict every call. This
    # function then edits operations in place — stripping security requirements and prepending the
    # authentication note — so it was mutating the LIVE schema on every request.
    #
    # The visible effect: the auth note accumulated. A partner who reloaded ReDoc three times saw
    # "**Authentication required.**" three times on every gated endpoint, and the internal Swagger
    # console inherited the same corruption because it reads the same object.
    #
    # It surfaced when an ETag was added and two identical requests produced different hashes —
    # the spec was not deterministic because reading it changed it.
    full = copy.deepcopy(app.openapi())

    paths = {p: item for p, item in full.get("paths", {}).items() if _is_partner_path(p)}

    # Keep only the schemes a partner can actually hold, BY NAME from the real document.
    #
    # This used to invent a scheme called `ServiceAccountKey` — a name nothing else in the
    # codebase ever declared. Harmless while no operation declared security at all, and a
    # dangling reference the moment they did: operations pointed at `AggregatorApiKey` /
    # `PlatformApiKey`, the components block offered only `ServiceAccountKey`, and ReDoc
    # rendered no Authorize control because it could not resolve the reference.
    #
    # Filtering the real names means the two can never drift again: a scheme renamed in
    # `api/security_schemes.py` either appears here or is deliberately excluded, and a
    # typo produces an empty list rather than a broken link.
    #
    # `PortalSession` and `LegacySharedKey` are excluded deliberately: documenting a
    # credential a partner cannot hold invites an integration built on the wrong one.
    schemes = full.get("components", {}).get("securitySchemes", {})
    PARTNER_SCHEME_NAMES = ("AggregatorApiKey", "PlatformApiKey")
    partner_schemes = {
        name: schemes[name] for name in PARTNER_SCHEME_NAMES if name in schemes
    }

    # Strip security references to schemes this document does not declare.
    #
    # Several routes accept EITHER a scoped service key or the legacy shared key, so their
    # operations list both. `LegacySharedKey` is deliberately absent here — documenting a
    # deprecated omnipotent credential to partners would invite an integration built on it —
    # but leaving the reference behind makes it dangling, and a consumer that resolves
    # `$ref`s strictly (`openapi-generator`, some linters) errors on the whole document.
    #
    # Every affected operation still lists a scheme a partner CAN hold, so nothing becomes
    # unauthenticable; the requirement is unchanged, only the undocumented alternative is
    # removed. An operation left with no scheme at all falls back to the global `security`.
    for item in paths.values():
        for operation in item.values():
            if not isinstance(operation, dict) or "security" not in operation:
                continue
            kept = [
                requirement
                for requirement in operation["security"]
                if set(requirement) <= set(partner_schemes)
            ]
            if kept:
                operation["security"] = kept
            else:
                operation.pop("security")

    # Name the header in each gated operation's own description.
    #
    # The spec is already correct — `security` is declared and the schemes resolve — but that
    # is not what a reader sees. ReDoc renders alternative requirements (`[{A}, {B}]`, meaning
    # "A **or** B") as a compact lock affordance that is easy to miss, and it does not repeat
    # the header name inside the operation body where someone is reading the request shape.
    #
    # So the requirement is stated in prose too, at the top of the description, where it
    # cannot be missed. Belt and braces: the machine-readable `security` drives the Authorize
    # button and generated clients, and this line answers "what header do I send?" for a human
    # skimming one endpoint.
    #
    # Injected here rather than written into every route's docstring so it cannot drift: a
    # route that gains or loses a guard gets the correct line automatically, and a docstring
    # copy would be one more thing to keep in step.
    for item in paths.values():
        for operation in item.values():
            if not isinstance(operation, dict):
                continue
            names = {
                name for requirement in operation.get("security", []) for name in requirement
            }
            if not names:
                continue

            note = (
                "> **Authentication required.** Send your key as the "
                "`X-SHELTER-API-Key` header:\n>\n"
                "> ```http\n"
                "> X-SHELTER-API-Key: shltky...\n"
                "> ```\n\n"
            )
            operation["description"] = note + (operation.get("description") or "")

    return {
        "openapi": full.get("openapi", "3.1.0"),
        "info": {
            "title": "SHELTER Partner API",
            "version": settings.app_version,
            "description": DEVELOPER_INTRO,
            "contact": {"name": "SHELTER support", "url": settings.public_site_url},
        },
        "servers": [
            {
                "url": (settings.api_base_url or settings.public_site_url).rstrip("/"),
                "description": "Production",
            }
        ],
        "paths": paths,
        "components": {
            **full.get("components", {}),
            # Only what the retained paths actually reference. Everything else — the IAM
            # workspace and RBAC models most importantly — stays on the internal console.
            "schemas": _reachable_schemas(
                paths, full.get("components", {}).get("schemas", {})
            ),
            "securitySchemes": partner_schemes,
        },
        # Applied globally as a floor, so an operation that somehow declares nothing still
        # shows the key requirement rather than reading as open. Per-operation `security`
        # overrides this, which is what puts the right padlock on each route.
        #
        # Both names listed because a partner key and the portal's service key both work on
        # the shared endpoints — `/places/*` most importantly.
        "security": [{name: []} for name in partner_schemes],
        "tags": [
            {"name": "customers", "description": "Onboard and manage your subscribers"},
            {"name": "alerts", "description": "Hazard alerts for your customers"},
            {"name": "risk", "description": "On-demand assessment"},
            {"name": "webhook", "description": "Event delivery to your systems"},
            {"name": "verification", "description": "How accurate past warnings were"},
        ],
    }


@router.get("/dev-docs/openapi.json", include_in_schema=False)
async def partner_openapi(request: Request) -> Response:
    """The filtered spec. Ungated — a partner must be able to generate a client
    before they hold a credential."""
    schema = partner_schema()

    # Validated rather than blindly cached.
    #
    # This was `max-age=300` on the reasoning that the spec "changes only on deploy". True in
    # production, false during active development — and the failure is confusing rather than
    # visible: the container is current, the endpoint returns new content, and the browser renders
    # a five-minute-old copy. It cost real time on this project.
    #
    # `no-cache` does NOT mean "do not store". It means "revalidate before using", so the browser
    # still caches the ~200 KB body and a repeat visit gets a 304 with no payload. Bandwidth is
    # preserved; staleness is not possible.
    #
    # The ETag is derived from the content, so it changes exactly when the spec does — including a
    # deploy that only reworded a description, which a version-derived tag would miss.
    body = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    etag = f'W/"{hashlib.sha256(body.encode()).hexdigest()[:32]}"'

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})

    return JSONResponse(
        schema,
        headers={
            "Cache-Control": "no-cache",
            "ETag": etag,
            # So a partner can generate a client from a browser-based tool.
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.get("/dev-docs", response_class=HTMLResponse, include_in_schema=False)
async def developer_docs() -> HTMLResponse:
    """The partner API reference, rendered with ReDoc.

    Hand-written HTML rather than FastAPI's `get_redoc_html`, for two reasons:

      1. **FastAPI's default CDN URL is broken.** It points at
         `cdn.jsdelivr.net/npm/redoc@next/...`, which now returns 404 — verified — so
         `/redoc` renders a blank page. Pinning `redoc@2` fixes it, and pinning a major
         version rather than `latest` means a breaking ReDoc release cannot silently
         blank the page again.
      2. It lets the page carry the consortium favicon and point at the *filtered*
         spec rather than the full one.
    """
    spec_url = f"{settings.api_prefix}/dev-docs/openapi.json"

    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>SHELTER Partner API — Developer Documentation</title>
  <meta name="description"
        content="Integrate satellite hazard intelligence: flood, crop stress and the
                 7-day outlook, for the areas you manage."/>
  <link rel="icon" type="image/svg+xml" href="{settings.api_prefix}/dev-docs/favicon.svg"/>
  <link rel="preconnect" href="https://cdn.jsdelivr.net"/>
  <style>
    /* Brand tokens lifted from the portal's design system so the reference does not
       look like a different product. */
    :root {{
      --brand-600: #6a0dad;
      --brand-500: #9a2ce9;
      --ink: #0b001b;
      --hairline: #34075626;
    }}
    body {{ margin: 0; padding: 0; }}
    /* ReDoc renders into #redoc; the banner sits above it so the consortium
       attribution is visible without waiting for the bundle to parse. */
    .devdocs-banner {{
      display: flex; align-items: center; gap: 14px;
      padding: 14px 22px;
      border-bottom: 1px solid var(--hairline);
      background: #fff;
      font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }}
    .devdocs-banner__title {{
      font-size: 14px; font-weight: 700; letter-spacing: .12em;
      text-transform: uppercase; color: var(--ink);
    }}
    .devdocs-banner__sub {{
      font-size: 12px; color: #6a7282; margin-left: auto; text-align: right;
      line-height: 1.4;
    }}
    .devdocs-banner__sub a {{ color: var(--brand-600); text-decoration: none; }}
    @media (max-width: 640px) {{
      .devdocs-banner__sub {{ display: none; }}
    }}
  </style>
</head>
<body>
  <div class="devdocs-banner">
    <svg width="26" height="26" viewBox="0 0 40 40" aria-hidden="true"
         style="color: var(--brand-600)">
      <rect x="17.7" y="2.6" width="4.6" height="4.1" rx="1.1" fill="currentColor"/>
      <rect x="11.9" y="3.7" width="4.9" height="1.9" rx=".65" fill="currentColor" opacity=".55"/>
      <rect x="23.2" y="3.7" width="4.9" height="1.9" rx=".65" fill="currentColor" opacity=".55"/>
      <path d="M13.2 12.9a9.2 9.2 0 0 1 13.6 0" fill="none" stroke="currentColor"
            stroke-width="2.1" stroke-linecap="round" opacity=".62"/>
      <path d="M8.8 16.4a14.6 14.6 0 0 1 22.4 0" fill="none" stroke="currentColor"
            stroke-width="2.1" stroke-linecap="round" opacity=".26"/>
      <path d="M20 18 8.6 27.6v8.2h5.6v-6.6h11.6v6.6h5.6v-8.2Z" fill="currentColor"/>
      <circle cx="20" cy="32" r="2.5" fill="currentColor"/>
    </svg>
    <span class="devdocs-banner__title">SHELTER Partner API</span>
    <span class="devdocs-banner__sub">
      NIGCOMSAT &times; FreePass ZeroRate<br/>
      <a href="{spec_url}">OpenAPI schema</a>
    </span>
  </div>

  <div id="redoc"></div>

  <!-- redoc@2, pinned. FastAPI's default `redoc@next` URL now 404s, which is why
       /redoc rendered blank; a major-version pin means a breaking release cannot
       silently blank this page either. -->
  <script src="https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js"
          crossorigin="anonymous"></script>
  <script>
    Redoc.init(
      "{spec_url}",
      {{
        scrollYOffset: 56,
        hideDownloadButton: false,
        expandResponses: "200,201",
        // Required properties first: a developer scanning a schema needs to know what
        // they must send before what they may send.
        requiredPropsFirst: true,
        theme: {{
          colors: {{ primary: {{ main: "#6a0dad" }} }},
          typography: {{
            fontFamily: '-apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif',
            headings: {{ fontWeight: "700" }},
            code: {{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }},
          }},
          sidebar: {{ backgroundColor: "#fbf8ff" }},
        }},
      }},
      document.getElementById("redoc"),
      function () {{
        // Fallback if the CDN is blocked or the bundle fails to parse. Without this the
        // page is a blank white screen with no indication of what went wrong — exactly
        // the failure mode that made the original /redoc look broken.
        var el = document.getElementById("redoc");
        if (el && !el.children.length) {{
          el.innerHTML =
            '<p style="padding:32px;font-family:sans-serif;color:#6a7282">' +
            'The reference viewer could not load. The machine-readable schema is ' +
            'always available at <a href="{spec_url}">{spec_url}</a>.</p>';
        }}
      }}
    );
  </script>
</body>
</html>""",
        # Same reasoning as the spec above: revalidate rather than serve a stale shell. The page
        # is small, and a cached shell pointing at a changed spec is the harder bug to see.
        headers={"Cache-Control": "no-cache"},
    )


#: The consortium favicon, inlined as SVG.
#:
#: Served from the backend rather than linked to the frontend for one reason: the docs
#: pages must be readable when the portal is not deployed — a partner evaluating the API
#: may only ever have the API host. A cross-origin favicon would 404 for them.
#:
#: SVG rather than the PNGs: it is ~1 KB against 13 KB for both rasters, scales to any
#: tab density, and `prefers-color-scheme` inside the SVG handles the dark-tab case that
#: a PNG cannot.
_CONSORTIUM_FAVICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <style>
    .mark { fill: #6a0dad; }
    .sat  { stroke: #6a0dad; }
    @media (prefers-color-scheme: dark) {
      .mark { fill: #b86bf2; }
      .sat  { stroke: #b86bf2; }
    }
  </style>
  <!-- The SHELTER mark, scaled from the 40x40 portal glyph. At favicon sizes the two
       partner wordmarks are illegible, so the product mark carries the identity and the
       partner attribution lives on the page banner where it can actually be read. -->
  <g transform="translate(12 12) scale(1)">
    <rect class="mark" x="17.7" y="2.6" width="4.6" height="4.1" rx="1.1"/>
    <rect class="mark" x="11.9" y="3.7" width="4.9" height="1.9" rx=".65" opacity=".55"/>
    <rect class="mark" x="23.2" y="3.7" width="4.9" height="1.9" rx=".65" opacity=".55"/>
    <path class="sat" d="M13.2 12.9a9.2 9.2 0 0 1 13.6 0" fill="none"
          stroke-width="2.2" stroke-linecap="round" opacity=".62"/>
    <path class="sat" d="M8.8 16.4a14.6 14.6 0 0 1 22.4 0" fill="none"
          stroke-width="2.2" stroke-linecap="round" opacity=".28"/>
    <path class="mark" d="M20 18 8.6 27.6v8.2h5.6v-6.6h11.6v6.6h5.6v-8.2Z"/>
    <circle class="mark" cx="20" cy="32" r="2.5"/>
  </g>
</svg>"""


@router.get("/dev-docs/favicon.svg", include_in_schema=False)
async def docs_favicon() -> HTMLResponse:
    """Favicon for the docs pages.

    Long cache: it changes only when the brand does. Served as `image/svg+xml` via a
    plain Response — `HTMLResponse` with an overridden media type avoids adding an import
    for a single one-line handler.
    """
    return HTMLResponse(
        _CONSORTIUM_FAVICON,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )
