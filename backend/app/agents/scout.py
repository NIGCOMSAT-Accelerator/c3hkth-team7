"""Agent 1 — Scout, a stateful poller.

Answers one question: what usable imagery exists over this AOI right now?

Queries the STAC catalogues for both Sentinel-2 (optical) and Sentinel-1 (SAR)
and decides whether optical is trustworthy. When a rainstorm has closed the
optical window — the exact condition under which floods happen — it flags
`optical_blinded` so the Analyst runs SAR-only instead of reporting nothing.

**Why "stateful" and what it is not.**

Scout records, per (area, source), when it last polled and last succeeded
(`source_poll_state`). That memory does three things:

* **Skips polls whose answer cannot have changed.** `app/eo/sources.py` declares
  each upstream's real publication cadence. Sentinel-1 revisits West Africa about
  every 6 days; SoilGrids and the DEM do not change at all. A stateless Scout on a
  6-hour cycle would re-read terrain ~1,460 times a year per AOI for an identical
  answer.
* **Backs off from a dead upstream** instead of hammering it every cycle across
  every subscriber at once.
* **Makes freshness auditable.** "When did we last actually see this area?" is a
  question asked during an incident, not from a dashboard.

It is **not** a new control loop, and adding one would contradict the property the
architecture depends on. Scout is still a queue-consuming stage: the scheduler
wakes, enqueues one job per area, and this runs once per job and hands to Analyst.
Its `next_stage` is a fixed class attribute. Statefulness is *memory between runs*,
not autonomy within one.

**On "pull all datasets locally".** The pixels are not downloaded — that would
defeat the windowed-COG design that makes this affordable at all (a Sentinel-2
scene is ~1 GB; an AOI is a few km² of it). What is cached to MinIO is the
*discovery result*: the scene references, their signed asset hrefs, and the cloud
triage. That is the expensive part to recompute — three catalogue round trips plus
SAS signing — and it is a few kB.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.agents.base import Agent
from app.config import settings
from app.eo import admin, stac
from app.eo import sources as source_registry
from app.models.enums import JobStage
from app.models.schemas import AreaOfInterest, ScoutResult
from app.store import objects, poll_state

#: Above this cloud fraction a Sentinel-2 scene contributes too few valid
#: pixels to support an agricultural decision, whatever the SCL mask salvages.
CLOUD_BLIND_THRESHOLD = 80.0

#: Minimum share of the AOI that must survive cloud masking to trust optical.
MIN_USABLE_OPTICAL_SCENES = 1

#: Registry key for the imagery chain's poll state. The chain fails over across
#: three catalogues (`element84` → `copernicus` → `planetary`) but produces one
#: answer, so freshness is tracked once under the primary rather than per
#: catalogue — otherwise a fallback success would leave the primary looking stale
#: forever.
_IMAGERY_KEY = "element84"

#: Hard ceiling on replaying a cached discovery, in minutes.
#:
#: Independent of, and much tighter than, the source's 6-hour cadence floor —
#: because a cached `SceneRef` holds *signed* asset hrefs. Planetary Computer SAS
#: tokens last about 45 minutes (`eo/auth.py`), so a discovery replayed after that
#: hands the Analyst hrefs that 403 on read. The Analyst would report no usable
#: imagery and the Oracle would decline to escalate — a silent downgrade caused
#: entirely by our own cache.
#:
#: 30 minutes leaves margin inside the shortest token life. Beyond it we re-search:
#: three catalogue round trips is a cheap price for hrefs that actually resolve.
_REPLAY_CEILING_MINUTES = 30


class ScoutAgent(Agent[AreaOfInterest, ScoutResult]):
    stage = JobStage.SCOUT
    next_stage = JobStage.ANALYST

    async def run(self, payload: AreaOfInterest) -> ScoutResult:
        aoi = payload

        source = source_registry.BY_KEY[_IMAGERY_KEY]
        state = await poll_state.get(aoi.id, _IMAGERY_KEY)

        # Fresh discovery within the revisit window: replay it rather than paying
        # three catalogue round trips for scenes that have not changed.
        if not poll_state.is_due(state, source) and state is not None:
            cached = await self._replay(aoi, state)
            if cached is not None:
                return cached

        # Resolve State / LGA / Ward if the AOI has none.
        #
        # Placed here rather than in the eight AOI-creation call sites: one integration point, and
        # it also covers an AOI that predates this feature — those would otherwise stay
        # unverifiable forever, since nothing revisits a registration.
        #
        # Runs only when a field is actually missing (`admin.enrich` returns immediately otherwise),
        # so the steady-state cost is zero rather than one ArcGIS call per AOI per cycle.
        #
        # This is a NAMING step, not a measurement one. It never raises, and an unresolved AOI is
        # monitored exactly as well — it is only harder for Fahis to verify, which is the
        # pre-existing state. So it deliberately sits outside the try/except that drives backoff.
        aoi = await admin.enrich(aoi)

        self.log.info(
            "searching imagery",
            extra={"aoi_id": aoi.id, "aoi": aoi.name, "bbox": aoi.bbox.as_list()},
        )

        try:
            optical, radar = await stac.search_both(aoi.bbox)
        except Exception as exc:
            # `search_both` swallows per-catalogue failures already, so reaching
            # here means something structural. Record it so backoff applies, then
            # re-raise into the broker's retry path.
            await poll_state.record_failure(aoi.id, _IMAGERY_KEY, str(exc))
            raise

        usable_optical = [
            scene
            for scene in optical
            if scene.cloud_cover is None or scene.cloud_cover < CLOUD_BLIND_THRESHOLD
        ]

        blinded = len(usable_optical) < MIN_USABLE_OPTICAL_SCENES

        if blinded and radar:
            self.log.info(
                "optical blinded by cloud; SAR carries this cycle",
                extra={
                    "aoi_id": aoi.id,
                    "optical_found": len(optical),
                    "optical_usable": len(usable_optical),
                    "radar_found": len(radar),
                },
            )
        elif blinded and not radar:
            # Both empty is a real gap, not a silent zero. The Analyst will
            # produce a low-confidence result and the Oracle will refuse to
            # escalate on it.
            self.log.warning(
                "no usable imagery from either sensor",
                extra={"aoi_id": aoi.id},
            )

        result = ScoutResult(
            aoi_id=aoi.id,
            # Carried forward so the Analyst can mask its measurements to the real field
            # rather than to the envelope the COG window necessarily uses. None for a
            # pin-and-radius AOI, where the two are the same shape.
            aoi_ring=aoi.geometry,
            # The window the Analyst must read. Without it the Analyst fell back to the scene
            # footprint — 154x the requested area over Ikorodu, enough to put the Atlantic in a
            # farmer's flood figure. See `ScoutResult.aoi_bbox`.
            aoi_bbox=aoi.bbox,
            optical=usable_optical,
            radar=radar,
            optical_blinded=blinded,
        )

        await self._remember(aoi, result)
        return result

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #

    async def _replay(
        self, aoi: AreaOfInterest, state: poll_state.PollState
    ) -> ScoutResult | None:
        """Rebuild a recent discovery from the object store.

        Returns None on any miss so the caller falls through to a live search — a
        stale or unreadable cache must never be the reason an area goes unassessed.

        **Asset hrefs are the binding constraint**, not the cadence. A cached
        `SceneRef` holds signed hrefs, and a Planetary Computer SAS token lasts
        ~45 minutes — so `_REPLAY_CEILING_MINUTES` gates this far more tightly than
        the source's 6-hour floor. Without that check, a replay inside the cadence
        window would hand the Analyst hrefs that 403, and the pipeline would report
        "no usable imagery" for a reason entirely of our own making.
        """
        if not state.cache_key or not objects.available():
            return None

        if state.last_success_at is None:
            return None

        age = datetime.now(timezone.utc) - state.last_success_at
        if age > timedelta(minutes=_REPLAY_CEILING_MINUTES):
            self.log.debug(
                "cached discovery too old to replay; signed hrefs may have expired",
                extra={"aoi_id": aoi.id, "age_minutes": round(age.total_seconds() / 60)},
            )
            return None

        raw = await objects.get(settings.s3_bucket_imagery, state.cache_key)
        if raw is None:
            return None

        try:
            payload = json.loads(raw)
            result = ScoutResult.model_validate(payload)
        except Exception as exc:
            self.log.debug(
                "discarding unreadable scout cache",
                extra={"aoi_id": aoi.id, "error": str(exc)},
            )
            return None

        self.log.info(
            "replaying cached imagery discovery",
            extra={
                "aoi_id": aoi.id,
                "optical": len(result.optical),
                "radar": len(result.radar),
                "last_success": (
                    state.last_success_at.isoformat() if state.last_success_at else None
                ),
            },
        )
        return result

    async def _remember(self, aoi: AreaOfInterest, result: ScoutResult) -> None:
        """Persist the discovery and its poll state.

        Cached only when the discovery found something: storing an empty result
        would let a transient catalogue outage suppress a real search for the whole
        cadence window.
        """
        cache_key: str | None = None
        cache_bytes: int | None = None

        found_anything = bool(result.optical or result.radar)

        if found_anything and objects.available():
            body = result.model_dump_json().encode()
            key = objects.imagery_key(
                # Keyed on the newest scene so a new pass produces a new object
                # rather than overwriting the previous discovery.
                _newest_item_id(result),
                aoi.id,
                "discovery",
                ext="json",
            )
            stored = await objects.put(
                settings.s3_bucket_imagery,
                key,
                body,
                content_type="application/json",
            )
            if stored is not None:
                cache_key = stored.key
                cache_bytes = stored.size_bytes

        if found_anything:
            await poll_state.record_success(
                aoi.id,
                _IMAGERY_KEY,
                cache_key=cache_key,
                cache_bytes=cache_bytes,
                metadata={
                    "optical_scenes": len(result.optical),
                    "radar_scenes": len(result.radar),
                    "optical_blinded": result.optical_blinded,
                    "collections": sorted(
                        {s.collection for s in (*result.optical, *result.radar)}
                    ),
                },
            )
        else:
            # Not an exception — the catalogues answered, with nothing. But it must
            # not count as success, or the cadence floor would suppress the retry
            # that might find the next pass.
            await poll_state.record_failure(
                aoi.id, _IMAGERY_KEY, "no scenes returned by any catalogue"
            )


def _newest_item_id(result: ScoutResult) -> str:
    """Item id of the most recent scene, for cache keying."""
    scenes = [*result.optical, *result.radar]
    if not scenes:
        return "empty"
    return max(scenes, key=lambda s: s.datetime).item_id
