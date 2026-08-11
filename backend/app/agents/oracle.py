"""Agent 3 — Oracle.

Turns measurements into a decision: what hazard, how bad, for whom, and what it
triggers next.

Signals combined:

  observed    what the imagery already shows (inundation, crop stress)
  forecast    rainfall over the next 7 days — where the lead time comes from
  exposure    population, real cropland, and low-lying terrain in the footprint
  soil        how long water will persist once it is there
  wetness     how much water is in the ground right now (SMAP) — the irrigation call
  health      whether malaria is endemic enough for the cascade to be credible

Confidence gates severity. A measurement made from 10% of an AOI's pixels, from
an untrained threshold fallback, or without any rainfall outlook cannot on its
own raise an EMERGENCY — the cost of a false emergency in a farming community
is a wasted harvest.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.agents.base import Agent, clamp
from app.config import settings
from app.eo import exposure as exposure_mod
from app.eo import health as health_mod
from app.eo import rainfall as rainfall_mod
from app.eo import soil as soil_mod
from app.eo import soil_moisture as soil_moisture_mod
from app.eo import terrain as terrain_mod
from app.eo.rainfall import PONDING_RAINFALL_MM
from app.logging_config import get_logger
from app.models.enums import HazardType, JobStage, Severity
from app.models.schemas import (
    AnalystResult,
    AreaOfInterest,
    DataFreshness,
    ExposureSummary,
    ForecastPoint,
    HealthBaseline,
    RainfallOutlook,
    RiskAssessment,
    ScoreDriver,
    SituationChange,
    SoilMoisture,
    SoilProfile,
)
from app.stats import ensemble, rainfall_index
from app.stats.calibration import Calibrator
from app.store import repository

log = get_logger(__name__)

# Score thresholds for each severity band.
SEVERITY_THRESHOLDS: list[tuple[float, Severity]] = [
    (0.80, Severity.EMERGENCY),
    (0.60, Severity.WARNING),
    (0.40, Severity.WATCH),
    (0.20, Severity.ADVISORY),
    (0.00, Severity.INFO),
]

#: Below this confidence, severity is capped at WATCH regardless of score.
CONFIDENCE_ESCALATION_FLOOR = 0.65

#: Weights for the three risk terms. Observed dominates because it is measured
#: rather than predicted; exposure only ever modulates, never originates.
W_OBSERVED = 0.55
W_FORECAST = 0.30
W_EXPOSURE = 0.15

#: Confidence penalty when no genuine rainfall forecast was available.
NO_FORECAST_CONFIDENCE = 0.75

#: Smallest score move worth calling a change.
#:
#: The score is a weighted sum over five measured inputs, so two essentially identical cycles
#: differ in the third decimal. Reporting that as "rising" would make every routine reading look
#: like a developing event and cost the next real escalation its audience.
_MATERIAL_SCORE_CHANGE = 0.05

#: Published revisit cadences, used to estimate the next look. Not promises — see `_freshness`.
_SAR_REVISIT_DAYS = 6
_OPTICAL_REVISIT_DAYS = 5


class OracleAgent(Agent[tuple[AreaOfInterest, AnalystResult], RiskAssessment]):
    stage = JobStage.ORACLE
    next_stage = JobStage.HERALD

    async def run(
        self, payload: tuple[AreaOfInterest, AnalystResult]
    ) -> RiskAssessment:
        aoi, analysis = payload

        # Six independent network calls; none may block the others. Terrain joins
        # the gather rather than running before it because it is cached per AOI with
        # a 30-day TTL — on all but the first cycle it returns without I/O, so
        # placing it here costs nothing and keeps the critical path one round trip.
        (
            outlook,
            exposure,
            soil,
            wetness,
            health,
            terrain_profile,
        ) = await asyncio.gather(
            rainfall_mod.rainfall_outlook(aoi.bbox),
            exposure_mod.exposure_for(aoi.bbox),
            soil_mod.soil_profile(aoi.bbox),
            soil_moisture_mod.soil_moisture(aoi.bbox),
            health_mod.malaria_baseline(aoi.bbox),
            self._terrain(aoi.bbox),
            return_exceptions=True,
        )

        outlook = outlook if isinstance(outlook, RainfallOutlook) else RainfallOutlook()
        exposure = (
            exposure if isinstance(exposure, ExposureSummary) else ExposureSummary()
        )
        soil = soil if isinstance(soil, SoilProfile) else SoilProfile()
        wetness = wetness if isinstance(wetness, SoilMoisture) else SoilMoisture()
        health = health if isinstance(health, HealthBaseline) else HealthBaseline()
        terrain_profile = (
            terrain_profile
            if isinstance(terrain_profile, terrain_mod.TerrainProfile)
            else terrain_mod.TerrainProfile()
        )

        hazard = self._classify(analysis, outlook, exposure, terrain_profile)
        observed_term = self._observed_term(hazard, analysis, soil)
        forecast_term = self._forecast_term(outlook, soil)
        exposure_term = self._exposure_term(exposure, terrain_profile)

        score = clamp(
            W_OBSERVED * observed_term
            + W_FORECAST * forecast_term
            + W_EXPOSURE * exposure_term
        )

        # Record WHAT drove that number, while we still know.
        #
        # The score is a weighted sum of three terms computed immediately above, so each term's
        # contribution is arithmetic rather than inference. Throwing it away and later asking an LLM
        # to reason about "which factor mattered most" from prose would be replacing a fact with a
        # guess — see `ScoreDriver`.
        #
        # `inputs` names the measurements behind each term so the figure traces to data rather than
        # to a stage name. Empty when a term ran on defaults because its inputs were unavailable,
        # which the explainer must render as "not measured" rather than as zero risk.
        score_drivers = self._score_drivers(
            observed_term=observed_term,
            forecast_term=forecast_term,
            exposure_term=exposure_term,
            analysis=analysis,
            outlook=outlook,
            exposure=exposure,
            terrain_profile=terrain_profile,
        )

        # No genuine forecast removes the forward-looking evidence, so the
        # assessment is measurably less certain even if the imagery was clean.
        confidence = clamp(
            analysis.confidence
            * (1.0 if outlook.forecast_available else NO_FORECAST_CONFIDENCE)
        )

        # Step 7 — calibration. Applied last, to the finished confidence, because
        # it is a statement about how often *this pipeline's* confidences have been
        # right, not about any single input.
        #
        # Off by default (`CONFIDENCE_CALIBRATION_ENABLED`) and a no-op until Fahis
        # has accumulated enough trainable verdicts. That ordering is deliberate:
        # calibration moves confidence, confidence gates severity, and a curve
        # fitted to a handful of verdicts could raise an EMERGENCY on noise.
        confidence = await self._calibrate(confidence)

        severity = self._severity(score, confidence)

        # What changed since last time, and how this compares with normal.
        #
        # Computed here rather than in the Herald because it is a property of the ASSESSMENT — the
        # dashboard and the API need it whether or not an alert is ever dispatched, and a
        # suppressed reading that was escalating is exactly what an operator wants to see.
        change = await self._change_since_last(aoi.id, score, severity, analysis)
        freshness = self._freshness(analysis)

        assessment = RiskAssessment(
            aoi_id=aoi.id,
            aoi_name=aoi.name,
            # Carried through for Fahis. The subscriber's own plot name cannot appear in a news
            # report; the administrative names can, and are the only thing verification can search.
            score_drivers=score_drivers,
            # Provenance, carried forward so every downstream consumer can cite it — see the field
            # docs on `RiskAssessment`. The Analyst's result does not survive this stage.
            observed_at_flood=analysis.flood_observed_at,
            observed_at_stress=analysis.stress_observed_at,
            platform_flood=analysis.flood_platform,
            platform_stress=analysis.stress_platform,
            method_flood=analysis.flood_method if analysis.flood_measured else None,
            method_stress=analysis.stress_method if analysis.stress_measured else None,
            stress_attribution=analysis.stress_attribution,
            # None when the leg did not measure — see the field docs. A zero here would tell the
            # retraining loop that a blind cycle observed dry ground.
            inundated_fraction=(
                analysis.inundated_fraction if analysis.flood_measured else None
            ),
            stressed_crop_fraction=(
                analysis.stressed_crop_fraction if analysis.stress_measured else None
            ),
            admin1=aoi.admin1,
            admin2=aoi.admin2,
            country=aoi.country,
            hazard=hazard,
            severity=severity,
            score=score,
            confidence=confidence,
            forecast=self._blend_forecast(outlook.points, observed_term),
            # Carried through so downstream wording can tell a prediction from rain already on the
            # ground. Without it the distinction dies here: `ForecastPoint` has no such flag, and
            # every consumer would have to guess from the source name.
            forecast_is_prediction=outlook.forecast_available,
            exposure=exposure,
            soil=soil,
            soil_moisture=wetness,
            health=health,
            change=change,
            freshness=freshness,
            evidence=self._evidence(
                analysis, outlook, exposure, soil, health, terrain_profile, wetness
            ),
            cascade=self._cascade(hazard, analysis, health),
            data_sources=self._sources(
                analysis, outlook, exposure, soil, health, terrain_profile, wetness
            ),
            lead_time_days=settings.forecast_horizon_days,
        )

        self.log.info(
            "assessment complete",
            extra={
                "aoi_id": aoi.id,
                "hazard": hazard.value,
                "severity": severity.value,
                "score": round(score, 3),
                "confidence": round(confidence, 3),
                "rainfall_source": outlook.source,
                "sources": assessment.data_sources,
            },
        )
        return assessment

    # ------------------------------------------------------------------ #
    # Classification
    # ------------------------------------------------------------------ #

    def _classify(
        self,
        analysis: AnalystResult,
        outlook: RainfallOutlook,
        exposure: ExposureSummary,
        terrain_profile: terrain_mod.TerrainProfile | None = None,
    ) -> HazardType:
        """Pick the dominant hazard.

        Order matters: standing water already on the ground outranks a rainfall
        forecast, and waterlogging outranks a generic vegetation anomaly
        because it carries a specific, actionable remedy.
        """
        inundated = analysis.inundated_fraction
        stressed = analysis.stressed_crop_fraction
        peak_rain = max((p.rainfall_mm for p in outlook.points), default=0.0)

        # An UNMEASURED leg must not be read as a low reading.
        #
        # Both fields default to 0.0 when their leg produced nothing, and the drought branch below
        # keys on `inundated < 0.05` — so a radar failure during a real flood used to classify as
        # CROP_DROUGHT_STRESS, which does not merely under-warn, it tells a farmer to irrigate a
        # flooded field. Substituting None makes "not measured" fail every threshold test instead
        # of passing the low-value ones.
        inundated = inundated if analysis.flood_measured else None
        stressed = stressed if analysis.stress_measured else None

        if inundated is not None and inundated > 0.25:
            return HazardType.FLOOD_INUNDATION
        if (
            inundated is not None
            and stressed is not None
            and inundated > 0.10
            and stressed > 0.20
        ):
            return HazardType.CROP_WATERLOGGING
        if peak_rain > PONDING_RAINFALL_MM * 2:
            return HazardType.FLOOD_FORECAST
        # Saturated ground plus flood-prone terrain is a flood setup even with no
        # forecast. Step 3 supplies the terrain half: HAND answers "is this near a
        # channel?", where `lowland_fraction` only answered "is this below the AOI
        # median?" — true of half of every hillside.
        if terrain_profile is not None and terrain_profile.available:
            terrain_flag = terrain_profile.flood_prone_fraction > 0.25 or (
                terrain_profile.median_hand_m < 3.0
            )
        else:
            terrain_flag = exposure.lowland_fraction > 0.25

        if (
            not outlook.forecast_available
            and outlook.antecedent_mm > PONDING_RAINFALL_MM * 3
            and terrain_flag
        ):
            return HazardType.FLOOD_FORECAST
        # Drought requires BOTH measurements. `inundated < 0.05` is a claim that the ground is
        # dry, and an unmeasured radar leg cannot support it — this is the branch that turned a
        # radar failure during a flood into advice to irrigate.
        if (
            stressed is not None
            and inundated is not None
            and stressed > 0.30
            and inundated < 0.05
        ):
            # Stressed but dry — drought, not waterlogging. The distinction
            # inverts the advice, so getting it wrong is worse than vague.
            return HazardType.CROP_DROUGHT_STRESS
        return HazardType.CROP_VEGETATION_ANOMALY

    # ------------------------------------------------------------------ #
    # Risk terms
    # ------------------------------------------------------------------ #

    @staticmethod
    def _score_drivers(
        *,
        observed_term: float,
        forecast_term: float,
        exposure_term: float,
        analysis: AnalystResult,
        outlook: RainfallOutlook,
        exposure: ExposureSummary,
        terrain_profile: terrain_mod.TerrainProfile | None,
    ) -> list[ScoreDriver]:
        """The three terms, their weights, and what fed each. Ordered by contribution.

        Ordered largest-first because that is the order a reader wants: "what mattered most here"
        is the first question, and an explainer given an arbitrary order would have to sort it
        anyway — or worse, narrate the first item as though it were the biggest.
        """
        # What actually fed each term. Named from the measurements rather than from the stage, so a
        # reader can check the figure against something.
        observed_inputs: list[str] = []
        if analysis.flood_measured:
            observed_inputs.append(
                f"standing water {analysis.inundated_fraction:.0%} ({analysis.flood_method})"
            )
        if analysis.stress_measured:
            observed_inputs.append(
                f"crop stress {analysis.stressed_crop_fraction:.0%} ({analysis.stress_method})"
            )

        forecast_inputs: list[str] = []
        if outlook.forecast_available and outlook.points:
            total = sum(p.rainfall_mm for p in outlook.points)
            forecast_inputs.append(f"{total:.0f} mm forecast over 7 days ({outlook.source})")
        elif outlook.source != "none":
            forecast_inputs.append(
                f"{outlook.antecedent_mm:.0f} mm already fallen ({outlook.source})"
            )

        exposure_inputs: list[str] = []
        if exposure.sources:
            exposure_inputs.append(f"exposure from {', '.join(exposure.sources)}")
        if terrain_profile is not None and terrain_profile.available:
            exposure_inputs.append(
                f"{terrain_profile.flood_prone_fraction:.0%} flood-prone terrain"
            )

        drivers = [
            ScoreDriver(
                key="observed",
                label="What the satellite measured",
                value=observed_term,
                weight=W_OBSERVED,
                contribution=W_OBSERVED * observed_term,
                inputs=observed_inputs,
            ),
            ScoreDriver(
                key="forecast",
                label="Rainfall ahead",
                value=forecast_term,
                weight=W_FORECAST,
                contribution=W_FORECAST * forecast_term,
                inputs=forecast_inputs,
            ),
            ScoreDriver(
                key="exposure",
                label="Who and what is in the way",
                value=exposure_term,
                weight=W_EXPOSURE,
                contribution=W_EXPOSURE * exposure_term,
                inputs=exposure_inputs,
            ),
        ]
        return sorted(drivers, key=lambda d: d.contribution, reverse=True)

    @staticmethod
    def _observed_term(
        hazard: HazardType, analysis: AnalystResult, soil: SoilProfile
    ) -> float:
        """How bad is what we can already see.

        Water hazards are scaled by soil drainage: the same inundation on
        impeded clay stays longer and does more damage than on free-draining
        sand.
        """
        if hazard in (
            HazardType.FLOOD_INUNDATION,
            HazardType.CROP_WATERLOGGING,
            HazardType.FLOOD_FORECAST,
        ):
            # 40% of an AOI under water is already a full-scale event; scaling
            # to 1.0 there rather than at 100% avoids a dead zone at the top.
            # An unmeasured leg contributes NOTHING rather than zero risk. Numerically the same
            # here, but the distinction is carried into `evidence` and confidence below so the
            # advisory can say the measurement is missing instead of implying a dry field.
            if not analysis.flood_measured:
                return 0.0
            base = analysis.inundated_fraction / 0.4
            return clamp(base * soil.waterlogging_multiplier)
        if not analysis.stress_measured:
            return 0.0
        return clamp(analysis.stressed_crop_fraction / 0.5)

    @staticmethod
    def _forecast_term(outlook: RainfallOutlook, soil: SoilProfile | None = None) -> float:
        """Forward-looking rainfall pressure.

        With a real forecast, uses the heaviest single day and the running
        total — 60 mm in one day floods differently from 60 mm over a week, and
        both matter. Without one, falls back to antecedent wetness, which is
        weaker evidence and is scaled down accordingly.

        **Steps 4 and 5 refine both halves, and neither replaces the raw-mm path
        outright** — each is used only when its statistic is actually computable:

        *Step 5, ensemble exceedance.* When GEFS delivers members rather than a
        single averaged series, `ensemble_risk_term` counts how many cross the
        ponding threshold. That recovers the forecast's own uncertainty estimate,
        which taking a peak and a total discards: `[30,31,29,30,31]` and
        `[0,2,88,60,0]` have the same mean and warrant different warnings.

        *Step 4, SPI.* When a rainfall climatology is available, the maximum of the
        raw-mm term and the SPI-derived term is taken. Maximum rather than
        replacement because the two catch different things: raw mm catches an
        absolute deluge anywhere, SPI catches rainfall that is unremarkable
        nationally but rare *here*. Suppressing either would lose warnings.
        """
        if outlook.forecast_available and outlook.points:
            peak = max(p.rainfall_mm for p in outlook.points)
            total = sum(p.rainfall_mm for p in outlook.points)
            raw_term = clamp(
                max(
                    peak / (PONDING_RAINFALL_MM * 2),
                    total / (PONDING_RAINFALL_MM * 5),
                )
            )

            # Step 5 — ensemble spread, when members were delivered.
            if settings.rainfall_statistics_enabled and outlook.ensemble_by_day:
                ensemble_term, _ = ensemble.ensemble_risk_term(
                    outlook.ensemble_by_day, ponding_mm=PONDING_RAINFALL_MM
                )
                if ensemble_term > 0:
                    raw_term = max(raw_term, ensemble_term)

            # Step 4 — SPI, when a local climatology answered.
            if settings.rainfall_statistics_enabled and outlook.spi is not None:
                # SPI +1.5 ("very wet") maps to a full-strength term; below +0.5
                # nothing unusual is happening and SPI contributes nothing.
                spi_term = clamp((outlook.spi - 0.5) / 1.5)
                raw_term = max(raw_term, spi_term)

            return raw_term

        if outlook.antecedent_mm > 0:
            # Saturated ground is real risk, but it is not a prediction — cap
            # its contribution below what a forecast could contribute.
            #
            # Step 4's API replaces the flat 7-day sum when available: rain that
            # fell yesterday leaves more water in the profile than rain from six
            # days ago, and a flat sum cannot tell the two apart.
            if settings.rainfall_statistics_enabled and outlook.api_mm > 0:
                pressure = rainfall_index.ponding_pressure(
                    outlook.api_mm,
                    ponding_mm=PONDING_RAINFALL_MM,
                    drainage_multiplier=(
                        soil.waterlogging_multiplier if soil is not None else 1.0
                    ),
                )
                return clamp(pressure) * 0.7
            return clamp(outlook.antecedent_mm / (PONDING_RAINFALL_MM * 6)) * 0.7
        return 0.0

    @staticmethod
    def _exposure_term(
        exposure: ExposureSummary,
        terrain_profile: terrain_mod.TerrainProfile | None = None,
    ) -> float:
        """How much is at stake.

        Population dominates, then cropland (this is an agricultural service),
        then health infrastructure. Flood-prone terrain amplifies the whole term
        because it concentrates whatever water arrives.

        **Step 3 changes the amplifier, not the base.** Where a terrain profile is
        available, `flood_prone_fraction` (HAND) replaces `lowland_fraction` (the
        share of the AOI below its own median elevation). The latter is a relative
        statistic with no hydrological content — it returns ~50% on a uniform
        floodplain regardless of flood risk, and flags the lower slope of a hillside
        as "lowland" where water never collects. HAND asks the question that
        actually predicts inundation: how far above the nearest channel is this?

        `median_hand_m` is consulted as well, because on smooth planar terrain the
        fraction alone can mislead (documented on `TerrainProfile`). An AOI whose
        median HAND is tens of metres is not a floodplain whatever the fraction says.
        """
        if not exposure.sources:
            return 0.0

        population_term = clamp(exposure.population / 50_000)
        cropland_term = clamp(exposure.cropland_fraction / 0.5)
        facility_term = clamp(exposure.health_facilities / 10)

        base = clamp(
            0.55 * population_term + 0.30 * cropland_term + 0.15 * facility_term
        )

        if terrain_profile is not None and terrain_profile.available:
            # Terrain sitting well above its drainage cannot concentrate water,
            # so the amplifier is damped regardless of the fraction.
            elevation_damping = clamp(
                1.0 - (terrain_profile.median_hand_m / 40.0)
            )
            terrain_pressure = clamp(
                max(
                    terrain_profile.flood_prone_fraction,
                    terrain_profile.wet_index_fraction,
                )
                * elevation_damping
            )
        else:
            terrain_pressure = clamp(exposure.lowland_fraction)

        amplifier = 1.0 + 0.3 * terrain_pressure
        return clamp(base * amplifier)

    # ------------------------------------------------------------------ #
    # Derived-statistics helpers
    # ------------------------------------------------------------------ #

    async def _terrain(self, bbox) -> terrain_mod.TerrainProfile:
        """HAND/TWI profile, or an unavailable one.

        Gated by `TERRAIN_ANALYSIS_ENABLED` and wrapped so a failure here can never
        cost an assessment — it degrades to the elevation-percentile proxy, which is
        the previous behaviour.
        """
        if not settings.terrain_analysis_enabled:
            return terrain_mod.TerrainProfile()
        try:
            return await terrain_mod.terrain_profile(bbox)
        except Exception as exc:
            self.log.warning("terrain analysis failed", extra={"error": str(exc)})
            return terrain_mod.TerrainProfile()

    async def _change_since_last(
        self,
        aoi_id: str,
        score: float,
        severity: Severity,
        analysis: AnalystResult,
    ) -> SituationChange:
        """How this reading differs from the previous one, and from the seasonal norm.

        ## Why the delta matters more than the label

        WATCH that has been WATCH for a fortnight is a background condition; WATCH that was INFO
        yesterday is a developing event. Same severity, opposite urgency — and only the comparison
        separates them. Without it every alert reads with the same weight, which is how a real
        escalation gets skimmed past.

        Never raises. A history read failing must not cost an assessment: the reading itself is
        sound, and an absent comparison renders as no change line rather than a false "steady".
        """
        change = SituationChange()

        # The seasonal comparison comes from the Analyst's own diagnostics, which is where the
        # fitted baseline lives. Absent when no baseline exists — the common case on a new area,
        # and reported as unknown rather than "normal".
        diagnostics = analysis.stress_diagnostics or {}
        residual = diagnostics.get("baseline_residual_std")
        if analysis.stress_method.startswith("seasonal-anomaly") and residual:
            # Signed z-score of the measured stress against the plot's own norm. Positive means
            # MORE stressed than usual, so the vegetation word is "browner".
            threshold_fraction = diagnostics.get("threshold_fraction")
            if threshold_fraction is not None:
                delta = analysis.stressed_crop_fraction - float(threshold_fraction)
                z = delta / float(residual) if float(residual) else 0.0
                change.vs_seasonal_z = round(z, 2)
                change.vs_seasonal = (
                    "browner" if z > 0.5 else "greener" if z < -0.5 else "normal"
                )

        try:
            history = await repository.assessment_history(aoi_id, days=45, limit=2)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "could not read history for the change summary",
                extra={"aoi_id": aoi_id, "error": str(exc)},
            )
            return change

        # `assessment_history` is newest-first and this assessment is not saved yet, so the first
        # row IS the previous run. On a first-ever assessment the list is empty and every
        # comparison field stays None.
        if not history:
            return change

        previous = history[0]
        change.previous_severity = previous.severity.value
        change.previous_score = round(previous.score, 3)
        change.previous_assessed_at = previous.assessed_at

        # A threshold, not equality. Scores are floats over five measured inputs, so two
        # essentially identical cycles differ in the third decimal — calling that "rising" would
        # make every routine reading look like a developing event.
        delta = score - previous.score
        change.direction = (
            "up" if delta > _MATERIAL_SCORE_CHANGE
            else "down" if delta < -_MATERIAL_SCORE_CHANGE
            else "steady"
        )
        return change

    @staticmethod
    def _freshness(analysis: AnalystResult) -> DataFreshness:
        """When the inputs were observed, and when the next look is due.

        ## Why the next pass is an estimate

        Sentinel-1 revisits West Africa about every 6 days and Sentinel-2 about every 5, but cloud,
        orbit gaps and upstream outages all move the useful date. So this is derived from the
        source's own cadence and named for expectation — promising a time the pipeline does not
        control would be the kind of forward commitment the advisory rules already forbid.

        The caveat names an ABSENT leg. A "no flooding detected" from a cycle with no radar is a
        different claim from one with a clean radar pass, and a subscriber cannot tell them apart
        unless it is said.
        """
        # The freshest of the two legs — whichever satellite last actually saw this plot.
        candidates = [
            (analysis.flood_observed_at, analysis.flood_platform, _SAR_REVISIT_DAYS),
            (analysis.stress_observed_at, analysis.stress_platform, _OPTICAL_REVISIT_DAYS),
        ]
        seen = [(when, platform, cadence) for when, platform, cadence in candidates if when]

        freshness = DataFreshness()
        if seen:
            when, platform, cadence = max(seen, key=lambda item: item[0])
            freshness.observed_at = when
            freshness.platform = platform
            freshness.next_expected = when + timedelta(days=cadence)

        # State what was missing, in the subscriber's terms rather than the pipeline's.
        if not analysis.flood_measured and not analysis.stress_measured:
            freshness.caveat = "No usable satellite imagery this cycle"
        elif not analysis.flood_measured:
            freshness.caveat = "No radar pass this cycle, so standing water was not measured"
        elif not analysis.stress_measured:
            freshness.caveat = "Cloud blocked the optical view, so crop condition was not measured"

        return freshness

    async def _calibrate(self, confidence: float) -> float:
        """Apply the fitted calibration map, if one is enabled and available.

        Reads Fahis outcomes and fits on demand rather than caching a fitted object,
        because the fit is two parameters over at most a few hundred rows — cheaper
        than the cache invalidation logic would be, and always current.
        """
        if not settings.confidence_calibration_enabled:
            return confidence
        try:
            from app.store import repository

            confidences, outcomes = await repository.verification_outcomes()
            calibrator = Calibrator.fit(confidences, outcomes)
            if not calibrator.available:
                return confidence
            calibrated = calibrator.apply(confidence)
            self.log.info(
                "confidence calibrated",
                extra={
                    "raw": round(confidence, 3),
                    "calibrated": round(calibrated, 3),
                    "method": calibrator.method,
                    "samples": calibrator.samples,
                },
            )
            return calibrated
        except Exception as exc:
            self.log.warning("calibration unavailable", extra={"error": str(exc)})
            return confidence

    @staticmethod
    def _severity(score: float, confidence: float) -> Severity:
        severity = next(
            level for threshold, level in SEVERITY_THRESHOLDS if score >= threshold
        )
        if confidence < CONFIDENCE_ESCALATION_FLOOR and severity in (
            Severity.WARNING,
            Severity.EMERGENCY,
        ):
            return Severity.WATCH
        return severity

    # ------------------------------------------------------------------ #
    # Narrative inputs
    # ------------------------------------------------------------------ #

    @staticmethod
    def _blend_forecast(
        points: list[ForecastPoint], observed_term: float
    ) -> list[ForecastPoint]:
        """Fold today's observed hazard into the daily risk curve.

        Observed risk decays across the window while rainfall risk accumulates,
        which produces the characteristic dip-then-rise the dashboard shows
        when a storm is inbound.
        """
        blended: list[ForecastPoint] = []
        for point in points:
            decay = max(0.0, 1.0 - point.day / 10.0)
            blended.append(
                point.model_copy(
                    update={"risk": clamp(max(point.risk, observed_term * decay))}
                )
            )
        return blended

    @staticmethod
    def _evidence(
        analysis: AnalystResult,
        outlook: RainfallOutlook,
        exposure: ExposureSummary,
        soil: SoilProfile,
        health: HealthBaseline,
        terrain_profile: terrain_mod.TerrainProfile | None = None,
        wetness: SoilMoisture | None = None,
    ) -> list[str]:
        """Plain-language facts. This is the *only* material the advisory
        generator may cite, which is what keeps generated text grounded."""
        facts: list[str] = []

        if analysis.inundated_fraction > 0.02:
            # State the correction, because "12% flooded" and "12% flooded
            # excluding the permanent river" are different claims and only the
            # second is actionable. This is provenance a reader can check rather
            # than a number they must trust.
            qualifier = (
                ", excluding permanently-wet river channels"
                if analysis.flood_diagnostics.get("permanent_water_removed")
                else ""
            )
            facts.append(
                f"{analysis.inundated_fraction:.0%} of the area is under standing "
                f"water in the latest radar pass{qualifier}"
            )
        if analysis.stressed_crop_fraction > 0.05:
            # The phrase "seasonal norm" was previously asserted while the
            # measurement was a fixed NDVI cut. Only claim it when a fitted
            # baseline actually produced the figure.
            if analysis.stress_method.startswith("seasonal-anomaly"):
                facts.append(
                    f"{analysis.stressed_crop_fraction:.0%} of cropland is "
                    f"significantly below its own seasonal norm for this time of year"
                )
            else:
                facts.append(
                    f"{analysis.stressed_crop_fraction:.0%} of cropland shows "
                    f"vegetation index values below the healthy-canopy threshold"
                )

        ndvi = next((i for i in analysis.indices if i.name == "ndvi"), None)
        if ndvi and ndvi.valid_fraction > 0.1:
            facts.append(f"Mean NDVI is {ndvi.mean:.2f}")

        # --- rainfall ---
        if outlook.forecast_available and outlook.points:
            total = sum(p.rainfall_mm for p in outlook.points)
            peak = max(outlook.points, key=lambda p: p.rainfall_mm)
            facts.append(f"{total:.0f} mm of rain forecast over the next 7 days")
            if peak.rainfall_mm > PONDING_RAINFALL_MM:
                facts.append(
                    f"Heaviest fall expected on day {peak.day} "
                    f"({peak.rainfall_mm:.0f} mm)"
                )
            if outlook.spi is not None:
                facts.append(
                    f"That is {rainfall_index.spi_to_severity_label(outlook.spi)} for "
                    f"this location and time of year (SPI {outlook.spi:+.1f})"
                )
        elif outlook.source != "none":
            # Keyed on the SOURCE, not on the value.
            #
            # This was `elif outlook.antecedent_mm > 0`, which conflated a MEASURED ZERO with no
            # measurement at all — so a genuinely dry week fell through to "Rainfall data was
            # unavailable for this cycle". Dry is a finding, and in the Sahel it is the single most
            # decision-relevant one: a farmer told the data is missing behaves differently from one
            # told no rain fell.
            #
            # Same class of bug as the Analyst's `.get(field, 0.0)`, in a different place. The chain
            # already reports which rung answered, so `source` is the fact to test.
            facts.append(
                f"{outlook.antecedent_mm:.0f} mm of rain fell in the measured window; "
                f"no forward forecast was available"
            )
            if outlook.spi is not None:
                facts.append(
                    f"The past week was "
                    f"{rainfall_index.spi_to_severity_label(outlook.spi)} for this "
                    f"location and season (SPI {outlook.spi:+.1f})"
                )
        else:
            facts.append("Rainfall data was unavailable for this cycle")

        # --- exposure ---
        if exposure.population > 0:
            facts.append(f"About {exposure.population:,} people live in the area")
        if exposure.cropland_hectares > 0:
            facts.append(
                f"{exposure.cropland_hectares:,.0f} ha of cropland "
                f"({exposure.cropland_fraction:.0%} of the area)"
            )
        if terrain_profile is not None and terrain_profile.available:
            # HAND is a hydrological statement; `lowland_fraction` was only a
            # relative-elevation one. Say which was measured.
            if terrain_profile.flood_prone_fraction > 0.15:
                facts.append(
                    f"{terrain_profile.flood_prone_fraction:.0%} of the area sits "
                    f"within {terrain_mod.HAND_FLOOD_THRESHOLD_M:.0f} m of a drainage "
                    f"channel, where river flooding reaches first"
                )
            if terrain_profile.wet_index_fraction > 0.2:
                facts.append(
                    f"{terrain_profile.wet_index_fraction:.0%} of the area is "
                    f"low-lying and slow-draining, so water will collect and linger"
                )
        elif exposure.lowland_fraction > 0.15:
            facts.append(
                f"{exposure.lowland_fraction:.0%} of the area is low-lying, where "
                f"water collects first"
            )
        if exposure.health_facilities > 0:
            facts.append(f"{exposure.health_facilities} health facilities in the area")

        # --- measured soil wetness ---
        #
        # Stated as a band plus the figure plus the date, in that order. The band is what changes the
        # decision, the figure is what makes it checkable, and the date matters because SMAP
        # publishes ~2 days in arrears — a farmer who irrigated yesterday needs to know this reading
        # predates that.
        #
        # No line at all when unavailable, rather than "soil moisture unknown". The caveat block
        # below owns absence; repeating it here would crowd out the measurements that DID land.
        if wetness is not None and wetness.available:
            _WETNESS_PHRASE = {
                "very_dry": "far below the level crops can draw on",
                "dry": "drier than crops need",
                "adequate": "in the range crops draw on comfortably",
                "wet": "wetter than crops need, but still draining",
                "saturated": "saturated, so roots are starved of air",
            }
            phrase = _WETNESS_PHRASE.get(wetness.status)
            if phrase:
                facts.append(
                    f"Soil water measured by satellite radar is {phrase} "
                    f"({wetness.volumetric:.2f} m3/m3 on {wetness.observed_date})"
                )

        # --- soil ---
        if soil.available and soil.drainage == "impeded":
            facts.append(
                "Heavy clay soils here drain slowly, so standing water will persist"
            )
        elif soil.available and soil.drainage == "free":
            facts.append("Free-draining soils here should shed water quickly")

        # --- health ---
        if health.available and health.endemic:
            facts.append(
                f"Malaria is endemic locally (baseline prevalence "
                f"{health.malaria_pfpr:.0%})"
            )

        # --- caveats ---
        #
        # Every sensor state gets a sentence. `"optical"` had none, so a radar failure — the leg
        # that measures flooding — reached the farmer as silence, while `inundated_fraction`
        # defaulted to 0.0 and read as "no water found".
        if analysis.source == "sar":
            facts.append(
                "Optical imagery was blocked by cloud; this reading is radar-only"
            )
        elif analysis.source == "optical":
            facts.append(
                "Radar imagery was unavailable, so standing water was not measured this "
                "cycle; this reading covers crop condition only"
            )
        elif analysis.source == "none":
            facts.append("No usable satellite imagery was available this cycle")

        # How old the pixels are, when that is worth knowing.
        #
        # `max_scene_age_days` is 20 to accommodate Landsat's 16-day revisit, and `_best_optical`
        # sorts by cloud with recency only as a tie-break — so a clear scene from two weeks ago can
        # legitimately outrank a cloudier one from yesterday. That is the right trade for
        # measurement quality, but only if the age is disclosed: "65% under water" means something
        # different as of this morning than as of eighteen days ago.
        #
        # Stated only past a week. Every alert carrying an age note trains the reader to skip it,
        # and inside a week the figure is current enough that the date adds nothing.
        now = datetime.now(timezone.utc)
        for observed_at, what in (
            (analysis.flood_observed_at, "standing water"),
            (analysis.stress_observed_at, "crop condition"),
        ):
            if observed_at is None:
                continue
            moment = (
                observed_at
                if observed_at.tzinfo
                else observed_at.replace(tzinfo=timezone.utc)
            )
            age_days = (now - moment).days
            if age_days >= 7:
                facts.append(
                    f"The {what} reading is from a satellite pass {age_days} days ago "
                    f"({moment:%d %b}); no clearer image has been available since"
                )

        # Partial coverage, stated. A scene clipped to the edge of the AOI measures part of the
        # field and says nothing about the rest, and the reader cannot tell from a percentage
        # alone. Only worth a sentence when it is materially short of the whole.
        for measured, coverage, what in (
            (analysis.flood_measured, analysis.flood_coverage, "standing water"),
            (analysis.stress_measured, analysis.stress_coverage, "crop condition"),
        ):
            if measured and coverage is not None and coverage < 0.95:
                facts.append(
                    f"The {what} reading covers about {coverage:.0%} of this area; "
                    "the satellite pass did not reach the rest"
                )

        return facts

    @staticmethod
    def _cascade(
        hazard: HazardType, analysis: AnalystResult, health: HealthBaseline
    ) -> list[HazardType]:
        """Downstream hazards this one is expected to trigger.

        This is what makes SHELTER a cascade system rather than a flood map:
        standing water today is a lost harvest in weeks and a malaria surge in
        roughly six, and the alert should say so while there is still time to
        act on both.

        The malaria arm is gated on endemicity. Vectors without a reservoir of
        infection don't produce an outbreak, and asserting one anyway is the
        kind of over-alerting that gets a service muted. When the baseline is
        unknown we don't assert it either.
        """
        if hazard in (HazardType.FLOOD_INUNDATION, HazardType.FLOOD_FORECAST):
            downstream = [HazardType.CROP_WATERLOGGING]
            # Water must persist to breed vectors; a brief peak that drains
            # does not carry the same signal.
            if analysis.inundated_fraction > 0.15 and health.endemic:
                downstream.append(HazardType.MALARIA_RISK)
            return downstream
        if hazard == HazardType.CROP_WATERLOGGING:
            return [HazardType.MALARIA_RISK] if health.endemic else []
        return []

    @staticmethod
    def _sources(
        analysis: AnalystResult,
        outlook: RainfallOutlook,
        exposure: ExposureSummary,
        soil: SoilProfile,
        health: HealthBaseline,
        terrain_profile: terrain_mod.TerrainProfile | None = None,
        wetness: SoilMoisture | None = None,
    ) -> list[str]:
        """Provenance — which datasets actually contributed to this assessment."""
        sources: list[str] = []
        if analysis.source in {"optical", "fused"}:
            sources.append("sentinel-2")
        if analysis.source in {"sar", "fused"}:
            sources.append("sentinel-1")
        if outlook.source != "none":
            sources.append(outlook.source)
        sources.extend(exposure.sources)
        if soil.available:
            sources.append("soilgrids")
        if wetness is not None and wetness.available:
            sources.append("smap-l3")
        if health.available:
            sources.append("malaria-atlas")
        if terrain_profile is not None and terrain_profile.available:
            sources.extend(terrain_profile.sources)
        if analysis.flood_diagnostics.get("permanent_water_removed"):
            sources.append("jrc-gsw")

        # Deduplicated, order preserved. `exposure` and `terrain` both read the Copernicus DEM and
        # each honestly reports it, so the raw list showed `copernicus-dem` twice. This is displayed
        # as a provenance list on the dashboard, where a repeated entry reads as a bug in the
        # pipeline rather than as two components sharing one upstream.
        #
        # dict.fromkeys rather than set(): the order is meaningful — imagery first, then rainfall,
        # then context — and a set would scramble it differently on every run.
        return list(dict.fromkeys(sources))
