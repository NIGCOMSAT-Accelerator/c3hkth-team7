"""Agent 2 — Analyst.

Turns scene references into measurements. Reads only the AOI window out of each
COG, computes indices, runs the two PyTorch models, and reports physical
quantities. It makes no judgement about severity — that is the Oracle's job.

The optical and radar legs run concurrently and either may come back empty;
`source` on the result records which sensors actually contributed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.agents.base import Agent
from app.config import settings
from app.eo import cog, indices, terrain
from app.eo.cog import CogReadError
from app.logging_config import get_logger
from app.ml import inference
from app.models.enums import JobStage
from app.models.schemas import AnalystResult, BBox, SceneRef, ScoutResult
from app.stats import anomaly as anomaly_mod
from app.store import repository

if TYPE_CHECKING:                      # numpy is only needed for a type hint here
    import numpy as np

log = get_logger(__name__)

#: Minimum share of the AOI a scene must cover before its measurement is trusted.
#:
#: A scene clipped to the edge of an AOI measures mostly the part that overlaps, and reports it as
#: though it described the whole field. At 40% the reading is over less than half the land and the
#: honest answer is "not measured" — the Oracle treats an absent measurement as unknown, whereas a
#: partial one it cannot distinguish from a complete one.
#:
#: Not 100%: Sentinel-2 tiles and Sentinel-1 frames have real edges, and an AOI straddling two
#: tiles would otherwise never be measurable at all. 0.4 keeps a straddling AOI usable while
#: rejecting the sliver case.
MIN_AOI_COVERAGE = 0.4


def _read_window(scout: ScoutResult, scene: SceneRef) -> BBox:
    """The window to read: the AOI, clipped to what this scene actually covers.

    ## Why this function exists

    Both legs used to pass `scene.bbox` — the satellite footprint. Over Ikorodu that is 154x the
    requested area and includes the Atlantic, so a SAR water mask reported two thirds of a farmer's
    plot as flooded when it was measuring ocean. See `ScoutResult.aoi_bbox`.

    ## Why the intersection rather than the AOI alone

    `cog.read_window` clips to the raster and raises `CogReadError` when the intersection is empty
    — the guard that could never fire while the AOI *was* the scene. Intersecting here keeps that
    guard meaningful and makes partial coverage measurable, which `run` then checks against
    `MIN_AOI_COVERAGE`.

    ## The fallback, and why it is not silent

    `aoi_bbox` is None on a `ScoutResult` serialised by the previous release and still sitting on a
    stream. Falling back to `scene.bbox` completes that job rather than dead-lettering an in-flight
    scan — but it is the defective behaviour, so it is logged at WARNING with the aoi id. A silent
    fallback would make the bug survive its own fix.
    """
    if scout.aoi_bbox is None:
        log.warning(
            "ScoutResult carries no aoi_bbox; falling back to the scene footprint. "
            "This measures the whole scene, not the AOI — expected only for jobs queued "
            "before this release.",
            extra={"aoi_id": scout.aoi_id, "item": scene.item_id},
        )
        return scene.bbox

    aoi = scout.aoi_bbox
    west = max(aoi.west, scene.bbox.west)
    south = max(aoi.south, scene.bbox.south)
    east = min(aoi.east, scene.bbox.east)
    north = min(aoi.north, scene.bbox.north)

    # A disjoint scene has no intersection, and `BBox` refuses to construct one — it validates
    # `east > west` and `north > south`, correctly, because a degenerate bbox is meaningless as an
    # AOI. So return the AOI unchanged and let the coverage check reject it: `_coverage` returns
    # 0.0 for a window that does not overlap, and the leg declines to measure.
    #
    # Returning the AOI rather than raising keeps the failure in ONE place. A scene that does not
    # intersect its own AOI should not normally exist — Scout searched by that bbox — so this is a
    # guard against a catalogue returning something unexpected, not a routine path.
    if east <= west or north <= south:
        log.warning(
            "scene does not intersect its own AOI; declining to measure",
            extra={"aoi_id": scout.aoi_id, "item": scene.item_id},
        )
        return aoi

    return BBox(west=west, south=south, east=east, north=north)


def _coverage(scout: ScoutResult, scene: SceneRef, window: BBox) -> float:
    """What share of the AOI this window covers. 1.0 when there is no AOI to compare against.

    Area-based rather than a width/height test, because a scene edge cutting diagonally across an
    AOI reduces both dimensions modestly while removing most of the land.

    Takes the SCENE as well as the window, because `_read_window` returns the AOI unchanged when the
    two do not intersect — so the window alone cannot distinguish "fully covered" from "not covered
    at all", and reporting 1.0 for the second would be the worst possible answer.
    """
    if scout.aoi_bbox is None:
        return 1.0

    from app.eo.geometry import area_hectares

    aoi = scout.aoi_bbox
    # No overlap at all. Checked against the scene rather than the window, per the docstring.
    if (
        scene.bbox.east <= aoi.west
        or scene.bbox.west >= aoi.east
        or scene.bbox.north <= aoi.south
        or scene.bbox.south >= aoi.north
    ):
        return 0.0

    aoi_area = area_hectares(aoi)
    if aoi_area <= 0:
        return 1.0
    return min(1.0, area_hectares(window) / aoi_area)


class AnalystAgent(Agent[ScoutResult, AnalystResult]):
    stage = JobStage.ANALYST
    next_stage = JobStage.ORACLE

    async def run(self, payload: ScoutResult) -> AnalystResult:
        scout = payload

        optical_task = self._analyze_optical(scout)
        radar_task = self._analyze_radar(scout)
        optical_result, radar_result = await asyncio.gather(
            optical_task, radar_task, return_exceptions=True
        )

        optical_data = optical_result if isinstance(optical_result, dict) else {}
        radar_data = radar_result if isinstance(radar_result, dict) else {}

        if isinstance(optical_result, Exception):
            self.log.warning("optical leg failed", extra={"error": str(optical_result)})
        if isinstance(radar_result, Exception):
            self.log.warning("radar leg failed", extra={"error": str(radar_result)})

        if optical_data and radar_data:
            source = "fused"
        elif radar_data:
            source = "sar"
        elif optical_data:
            source = "optical"
        else:
            source = "none"

        # Confidence is the *weakest* contributing leg, not the average: a
        # confident flood read shouldn't paper over a blind crop read.
        confidences = [
            c
            for c in (optical_data.get("confidence"), radar_data.get("confidence"))
            if c is not None
        ]
        confidence = min(confidences) if confidences else 0.0

        # No imagery at all means no measurement. Reporting zeros with zero
        # confidence is honest; reporting zeros with high confidence would tell
        # the Oracle everything is fine.
        if source == "none":
            self.log.warning("no usable imagery", extra={"aoi_id": scout.aoi_id})

        return AnalystResult(
            aoi_id=scout.aoi_id,
            indices=optical_data.get("indices", []),
            # Still 0.0 when a leg produced nothing — but `*_measured` below now says so, which
            # is what stops the Oracle reading "not measured" as "no hazard found". See the
            # field docs on `AnalystResult`.
            inundated_fraction=radar_data.get("inundated_fraction", 0.0),
            stressed_crop_fraction=optical_data.get("stressed_fraction", 0.0),
            flood_measured=bool(radar_data),
            stress_measured=bool(optical_data),
            flood_coverage=radar_data.get("coverage"),
            stress_coverage=optical_data.get("coverage"),
            stress_attribution=optical_data.get("stress_attribution", {}),
            # When the pixels were acquired, and by which platform. `computed_at` is always now
            # and says nothing about how current the measurement is.
            flood_observed_at=radar_data.get("observed_at"),
            stress_observed_at=optical_data.get("observed_at"),
            flood_platform=radar_data.get("platform"),
            stress_platform=optical_data.get("platform"),
            confidence=confidence,
            scenes_used=len(scout.optical) + len(scout.radar),
            source=source,
            flood_method=radar_data.get("flood_method", "heuristic"),
            stress_method=optical_data.get("stress_method", "heuristic"),
            flood_diagnostics=radar_data.get("flood_diagnostics", {}),
            stress_diagnostics=optical_data.get("stress_diagnostics", {}),
        )

    # ------------------------------------------------------------------ #
    # Optical leg — vegetation vigour and crop stress
    # ------------------------------------------------------------------ #

    async def _analyze_optical(self, scout: ScoutResult) -> dict:
        scene = self._best_optical(scout.optical)
        if scene is None:
            return {}

        hrefs = {
            band: asset.href
            for band in ("red", "green", "nir", "swir16", "scl")
            if (asset := scene.asset(band)) is not None
        }
        if "red" not in hrefs or "nir" not in hrefs:
            self.log.warning(
                "optical scene missing red/nir", extra={"item": scene.item_id}
            )
            return {}

        # The AOI window, not the scene footprint. See `_read_window`.
        window = _read_window(scout, scene)
        coverage = _coverage(scout, scene, window)
        if coverage < MIN_AOI_COVERAGE:
            self.log.warning(
                "optical scene covers too little of the AOI to measure",
                extra={"aoi_id": scout.aoi_id, "item": scene.item_id,
                       "coverage": round(coverage, 3)},
            )
            return {}

        try:
            bands = await cog.read_bands(hrefs, window)
        except CogReadError as exc:
            self.log.warning("optical read failed", extra={"error": str(exc)})
            return {}

        # Convert to surface reflectance BEFORE any index is computed.
        #
        # A normalised difference is scale-invariant but NOT offset-invariant, and Landsat
        # Collection-2 Level-2 carries an offset (`DN * 0.0000275 - 0.2`) where Sentinel-2 L2A is a
        # pure `DN / 10000`. Measured on a real Ikorodu scene: raw DN gave NDVI 0.119 against a
        # correct 0.246 — below the stress threshold rather than above it, so the whole AOI read as
        # 100% stressed when it was not. A no-op for Sentinel-2, which is why this was never needed
        # before the second optical sensor arrived.
        scaled = {
            band: indices.to_reflectance(array, scene.collection)
            for band, array in bands.items()
            # SCL holds class codes, not reflectance. Scaling it would be meaningless and would
            # destroy the cloud mask.
            if band != "scl"
        }

        red, nir = scaled["red"], scaled["nir"]
        ndvi_arr = indices.ndvi(red, nir)
        ndwi_arr = indices.ndwi(scaled["green"], nir) if "green" in scaled else None
        ndmi_arr = indices.ndmi(nir, scaled["swir16"]) if "swir16" in scaled else None

        # Mask cloud before anything is summarised, or the stats describe cloud.
        if "scl" in bands:
            scl = bands["scl"]
            ndvi_arr = indices.apply_scl_mask(ndvi_arr, scl)
            if ndwi_arr is not None:
                ndwi_arr = indices.apply_scl_mask(ndwi_arr, scl)
            if ndmi_arr is not None:
                ndmi_arr = indices.apply_scl_mask(ndmi_arr, scl)

        # Then mask to the field outline, if one was drawn.
        #
        # AFTER the cloud mask and BEFORE any summarisation. Order matters: both write NaN
        # and both are excluded from every statistic, so the two masks compose — a pixel is
        # counted only if it is inside the field AND cloud-free. Masking after `summarize`
        # would leave the stats describing the envelope, which is the whole error being
        # fixed here.
        # `window`, NOT `scene.bbox`. The mask's transform must describe the extent the
        # PIXELS came from; against the scene footprint the ring would land in the wrong
        # place entirely — and being ~154x too large, it would rasterise to nothing and
        # `_ring_mask` would fail open to all-ones, silently unmasking the field.
        ring = scout.aoi_ring
        if ring:
            ndvi_arr = indices.apply_ring_mask(ndvi_arr, ring, window)
            if ndwi_arr is not None:
                ndwi_arr = indices.apply_ring_mask(ndwi_arr, ring, window)
            if ndmi_arr is not None:
                ndmi_arr = indices.apply_ring_mask(ndmi_arr, ring, window)

        # The seasonal anomaly is computed FIRST, because it is now an INPUT to the model rather
        # than only a post-hoc comparison. `anomaly_arr` is None when the AOI has fewer than
        # `MIN_OBSERVATIONS` of history, and `predict_crop_stress` substitutes zeros — the honest
        # neutral value, so a new AOI degrades to 3-channel behaviour rather than failing.
        anomaly_arr = None
        seasonal = await self._seasonal_fraction(scout.aoi_id, ndvi_arr)
        if seasonal is not None:
            _seasonal_fraction_value, _seasonal_baseline, anomaly_arr = seasonal

        stress_probability, confidence = await inference.predict_crop_stress(
            ndvi_arr, ndmi_arr, ndwi_arr, anomaly_arr
        )

        # WHICH input drove that verdict. Four extra forward passes on a 1,377-parameter network, and
        # it decides whether the advice is "irrigate" or "look at pests" — see
        # `AnalystResult.stress_attribution`. None on the heuristic path, where there is nothing to
        # attribute.
        attribution = await inference.crop_stress_attribution(
            ndvi_arr, ndmi_arr, ndwi_arr, anomaly_arr
        )

        stats = [indices.summarize("ndvi", ndvi_arr)]
        if ndmi_arr is not None:
            stats.append(indices.summarize("ndmi", ndmi_arr))
        if ndwi_arr is not None:
            stats.append(indices.summarize("ndwi", ndwi_arr))

        # A scene that is 90% cloud produces valid-looking stats from 10% of the
        # pixels. Scale confidence by coverage so the Oracle sees that.
        valid_fraction = stats[0].valid_fraction
        confidence *= max(0.2, valid_fraction)

        stressed_fraction = inference.mean_fraction(stress_probability)
        # `predict_crop_stress` returns CONFIDENCE_TRAINED only when weights loaded;
        # `confidence` has since been scaled by coverage, so compare against the
        # same scaling rather than the bare constant.
        method = (
            "trained-model"
            if confidence >= inference.CONFIDENCE_TRAINED * max(0.2, valid_fraction)
            else "heuristic"
        )

        # Step 6 — the seasonal anomaly. Accumulate this reading first, then try to
        # measure against the AOI's own history.
        #
        # `record_index_observation` runs on every cycle regardless of whether a
        # baseline exists yet, which is what makes the baseline arrive at all: with
        # nothing writing, `NDVI < 0.35` would stay the only option forever. It
        # never raises, so a failed insert costs accuracy later rather than a
        # warning now.
        anomaly_stats: dict = {}
        for stat in stats:
            await repository.record_index_observation(
                scout.aoi_id, stat.name, stat.mean, stat.valid_fraction
            )

        # Reuses the baseline fetched before inference — one history read per cycle, not two.
        if seasonal is not None:
            fraction, baseline = _seasonal_fraction_value, _seasonal_baseline
            anomaly_stats = {
                "baseline_observations": baseline.observations,
                "baseline_residual_std": round(baseline.residual_std, 4),
                "threshold_fraction": round(stressed_fraction, 4),
            }
            # The anomaly answers the question the field claims to answer, so it
            # wins when available. The threshold figure is retained in diagnostics
            # so a reviewer can see both and judge the change.
            stressed_fraction = fraction
            method = f"seasonal-anomaly ({baseline.observations} obs)"

        return {
            "indices": stats,
            "stressed_fraction": stressed_fraction,
            "confidence": confidence,
            "coverage": coverage,
            "observed_at": scene.datetime,
            "platform": scene.collection,
            "stress_method": method,
            "stress_attribution": attribution or {},
            "stress_diagnostics": anomaly_stats,
        }

    async def _seasonal_fraction(
        self, aoi_id: str, ndvi_arr: np.ndarray
    ) -> tuple[float, anomaly_mod.HarmonicBaseline, np.ndarray | None] | None:
        """Stressed fraction from the AOI's own seasonal norm, or None.

        None means "no usable baseline" — too few observations, a fit that would not
        converge, or an all-cloud scene. The caller then keeps the documented
        `NDVI < 0.35` fallback, so a fresh deployment behaves exactly as before and
        improves on its own as history accumulates.
        """
        days, means = await repository.index_history(aoi_id, "ndvi")
        if len(days) < anomaly_mod.MIN_OBSERVATIONS:
            return None

        baseline = anomaly_mod.fit_harmonic_baseline(days, means)
        if not baseline.available:
            return None

        today = datetime.now(timezone.utc).timetuple().tm_yday
        fraction = anomaly_mod.stressed_fraction_from_anomaly(ndvi_arr, today, baseline)
        if fraction is None:
            return None

        # The per-pixel z-score, not just the summary fraction.
        #
        # `CropStressNet` takes this as its FOURTH INPUT CHANNEL, and it is the feature that lifted
        # precision off 0.37: the label means "below this location's seasonal norm", and without the
        # norm as an input the model was being asked a question it could not see the terms of. See
        # the class docstring for the measured separation.
        anomaly_arr = anomaly_mod.seasonal_anomaly(ndvi_arr, today, baseline)
        return fraction, baseline, anomaly_arr

    # ------------------------------------------------------------------ #
    # Radar leg — standing water (Track B engine)
    # ------------------------------------------------------------------ #

    async def _analyze_radar(self, scout: ScoutResult) -> dict:
        scene = self._most_recent(scout.radar)
        if scene is None:
            return {}

        vv_asset = scene.asset("vv")
        if vv_asset is None:
            self.log.warning("radar scene missing VV", extra={"item": scene.item_id})
            return {}

        hrefs = {"vv": vv_asset.href}
        if (vh_asset := scene.asset("vh")) is not None:
            hrefs["vh"] = vh_asset.href

        window = _read_window(scout, scene)
        coverage = _coverage(scout, scene, window)
        if coverage < MIN_AOI_COVERAGE:
            self.log.warning(
                "radar scene covers too little of the AOI to measure",
                extra={"aoi_id": scout.aoi_id, "item": scene.item_id,
                       "coverage": round(coverage, 3)},
            )
            return {}

        try:
            bands = await cog.read_bands(hrefs, window)
        except CogReadError as exc:
            self.log.warning("radar read failed", extra={"error": str(exc)})
            return {}

        # GRD products ship linear power; thresholds and the model expect dB.
        vv_db = indices.to_db(bands["vv"])
        vh_db = indices.to_db(bands["vh"]) if "vh" in bands else None

        # Step 1 — the permanent-water baseline. Fetched before inference because
        # both the trained model and the threshold path need the same correction:
        # a river that is always there is not today's flood. Cached per AOI with a
        # 30-day TTL, so this is one read on the first cycle and free afterwards.
        permanent = None
        if settings.permanent_water_masking:
            try:
                # The SAME window as the radar read above. A baseline fetched over a
                # different extent would not align pixel-for-pixel with `vv_db`, so
                # subtracting it would remove the wrong ground.
                permanent = await terrain.permanent_water_mask(window)
            except Exception as exc:
                # Never fail the radar leg over a baseline: absent it, we report
                # what we did before, which is the documented status quo.
                self.log.warning(
                    "permanent-water baseline unavailable", extra={"error": str(exc)}
                )

        water_probability, confidence = await inference.predict_flood(vv_db, vh_db)
        method = "trained-model" if confidence >= inference.CONFIDENCE_TRAINED else "heuristic"
        diagnostics: dict = {}

        # Step 2 — adaptive thresholding, but only on the heuristic path.
        #
        # This ordering is deliberate and is the important decision in this method.
        # A trained U-Net has *learned* its own decision boundary from labelled
        # scenes; overriding its output with a histogram split would discard that
        # and silently make a validated model worse. Otsu replaces the *fixed
        # constant*, not the model — so it fires exactly when `predict_flood` has
        # fallen back to `sar_water_mask`, which is the case the constant governs.
        if settings.adaptive_sar_threshold and method == "heuristic":
            water_probability, diagnostics = indices.adaptive_water_mask(
                vv_db, permanent_water=permanent
            )
            method = f"adaptive-{diagnostics.get('method', 'otsu')}"
        elif permanent is not None:
            # Trained path: still subtract permanent water, since step 1 is a
            # correction to the *question*, not to the classifier.
            water_probability, diagnostics = indices.apply_permanent_water(
                water_probability, permanent
            )

        # Mask to the field outline last, so it applies to whichever path produced the
        # water probability — trained model, adaptive Otsu, or the threshold fallback.
        #
        # This is the measurement the flood severity is computed from, and it is where the
        # envelope error bites hardest: a fully flooded riverside strip reads 35% of its
        # envelope and 100% of itself. `mean_fraction` ignores NaN, so masked pixels leave
        # the denominator rather than counting as dry.
        ring = scout.aoi_ring
        if ring:
            # `window` for the same reason as the optical leg: the transform must match
            # the extent the pixels were read from.
            water_probability = indices.apply_ring_mask(
                water_probability, ring, window
            )

        return {
            "inundated_fraction": inference.mean_fraction(water_probability),
            "confidence": confidence,
            "coverage": coverage,
            "observed_at": scene.datetime,
            "platform": scene.collection,
            "flood_method": method,
            "flood_diagnostics": diagnostics,
        }

    # ------------------------------------------------------------------ #

    @staticmethod
    def _best_optical(scenes: list[SceneRef]) -> SceneRef | None:
        """Least cloudy, tie-broken by recency."""
        if not scenes:
            return None
        return min(
            scenes,
            key=lambda s: (s.cloud_cover if s.cloud_cover is not None else 100.0,
                           -s.datetime.timestamp()),
        )

    @staticmethod
    def _most_recent(scenes: list[SceneRef]) -> SceneRef | None:
        """Newest wins — for flood extent, currency beats every other quality."""
        if not scenes:
            return None
        return max(scenes, key=lambda s: s.datetime)
