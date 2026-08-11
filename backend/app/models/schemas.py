"""Domain models.

These are the objects that move between the four agents over Redis streams and
out to the API, so every one of them is JSON-round-trippable.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
)

from app.models.enums import (
    Channel,
    DeliveryMode,
    DeliveryStatus,
    HazardType,
    JobStage,
    JobStatus,
    Severity,
    SubscriberKind,
    Verdict,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    """Internal identifier for objects that are never quoted by a human.

    Assessments, alerts, jobs and verifications are referenced by machines and appear
    in logs, so a prefix is useful there — `alert_…` in a log line is self-describing.

    **Subscriber and account ids do not use this.** Those are public-facing references
    that a partner stores, a farmer reads off a printed slip and an agent quotes on a
    support call, so they are 10-character alphanumerics minted by
    `app/iam/identifiers.py` with a uniqueness check. See `_new_identity_id`.
    """
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _new_identity_id() -> str:
    """A 10-character public identifier for a subscriber.

    Imported lazily so `app/models/schemas.py` stays importable without the IAM
    package — the pipeline constructs `Subscriber` objects and must not depend on
    identity code.

    This is the *fallback* path: a subscriber created through the IAM activation flow
    gets an id minted against the uniqueness check. One constructed directly (a test, a
    direct `POST /subscribers`) gets an unchecked candidate, which the unique index
    still catches. Same shape either way, so nothing downstream can tell them apart.
    """
    from app.iam.identifiers import mint

    return mint()


# --------------------------------------------------------------------------- #
# Geography
# --------------------------------------------------------------------------- #


class BBox(BaseModel):
    """Geographic bounding box in EPSG:4326, west/south/east/north."""

    west: float = Field(ge=-180, le=180)
    south: float = Field(ge=-90, le=90)
    east: float = Field(ge=-180, le=180)
    north: float = Field(ge=-90, le=90)

    @field_validator("east")
    @classmethod
    def _east_of_west(cls, v: float, info) -> float:
        west = info.data.get("west")
        if west is not None and v <= west:
            raise ValueError("east must be greater than west")
        return v

    @field_validator("north")
    @classmethod
    def _north_of_south(cls, v: float, info) -> float:
        south = info.data.get("south")
        if south is not None and v <= south:
            raise ValueError("north must be greater than south")
        return v

    def as_list(self) -> list[float]:
        """STAC `bbox` order."""
        return [self.west, self.south, self.east, self.north]

    @property
    def centroid(self) -> tuple[float, float]:
        return ((self.west + self.east) / 2, (self.south + self.north) / 2)

    @property
    def area_deg2(self) -> float:
        return (self.east - self.west) * (self.north - self.south)


class AreaOfInterest(BaseModel):
    """A monitored place — a farm, a ward, a district.

    ## The two geometries, and why both exist

    `bbox` is what the imagery layer consumes: STAC `/search` takes a bbox, and a windowed
    COG read takes a rectangle. Neither interface accepts a polygon, so the envelope is
    unavoidable at that level and is stored first-class rather than derived per read.

    `geometry` is the *true* outline when the subscriber drew one, and it narrows what gets
    COUNTED inside the window. That distinction is the whole point — measured on real
    shapes:

        square 1 km field    polygon 98.5 ha   envelope  98.5 ha   1.0x
        L-shaped field       polygon 68.1 ha   envelope  98.5 ha   1.4x
        riverside strip      polygon 72.9 ha   envelope 218.8 ha   3.0x

    The strip is the shape most flood-exposed smallholdings actually have. Assessing its
    envelope means two-thirds of the pixels feeding `inundated_fraction` are somebody else's
    land, which dilutes a real signal toward the threshold and can turn a WARNING into a
    WATCH. `app/eo/indices.py` applies the polygon as a raster mask for exactly this reason.

    `geometry` is OPTIONAL, and a pin-plus-radius AOI is fully supported: it produces a
    square whose envelope *is* its geometry, so masking is a no-op and nothing is lost.
    """

    id: str = Field(default_factory=lambda: _new_id("aoi"))
    name: str
    bbox: BBox
    #: The exterior ring, GeoJSON order: `[[lon, lat], ...]`, closed, counter-clockwise.
    #:
    #: Validated by `app.eo.geometry.validate_ring` at the API boundary rather than here —
    #: this model is also constructed from the database, where the ring is already known
    #: good, and re-running an O(n^2) self-intersection test on every read would be waste.
    #:
    #: Holes are deliberately unsupported. The pipeline reports one fraction per AOI, so a
    #: field with an excluded pond is better expressed as the field it is; accepting holes
    #: would imply a precision the single-number output cannot carry.
    geometry: list[list[float]] | None = Field(
        default=None,
        description=(
            "Exterior ring as [[lon, lat], ...] — the true field outline. Omit for a "
            "pin-and-radius area, where the bbox is the geometry."
        ),
    )
    country: str = "NG"
    admin1: str | None = Field(default=None, description="State / province")
    admin2: str | None = Field(default=None, description="LGA / district")
    crop: str | None = Field(default=None, description="Dominant crop, e.g. rice")
    #: True area of `geometry` when present, otherwise of `bbox`. What a subscriber
    #: recognises as their farm size, so it must describe the shape they drew.
    hectares: float | None = None
    #: Who contacts the subscriber about this plot — see `DeliveryMode`.
    #:
    #: Per AREA rather than per subscriber because an aggregator may relay for the plots they
    #: onboarded while a farmer's own plot still reaches them directly. Defaults to `direct`, so
    #: an area created by any existing caller keeps behaving exactly as it did.
    delivery_mode: DeliveryMode = DeliveryMode.DIRECT

    @property
    def has_polygon(self) -> bool:
        """Whether masking will do anything.

        A pin-and-radius AOI has no polygon and needs no mask — checking here keeps that
        branch out of the imagery code, which should not care how the area was drawn.
        """
        return bool(self.geometry) and len(self.geometry) >= 4


# --------------------------------------------------------------------------- #
# Subscribers
# --------------------------------------------------------------------------- #


class ChannelBinding(BaseModel):
    """Where one subscriber wants a given channel delivered."""

    channel: Channel
    #: Phone (E.164) for WhatsApp/Signal, chat id for Telegram, address for
    #: email, channel for Slack, URL for webhook, terminal id for broadcast.
    address: str
    #: Which area this applies to. **None means every area** — see `Subscriber.channels_for`.
    #:
    #: Nullable rather than required so every binding that existed before per-area overrides
    #: keeps working, and keeps applying to plots added later. A subscriber with one phone has
    #: one row; only someone who genuinely wants different delivery per plot pays for extra rows.
    aoi_id: str | None = None
    enabled: bool = True
    #: Only deliver on this channel at or above this severity.
    #:
    #: **Defaults to INFO, matching `herald.DISPATCH_FLOOR`.** The two have to agree: a floor of
    #: INFO with a binding default of ADVISORY would mean the platform generates a heartbeat alert
    #: and then every channel silently discards it — the feature would appear built and deliver
    #: nothing, which is worse than not having it.
    #:
    #: Raising this per binding is how a subscriber opts out of the heartbeat for one plot, or one
    #: channel, while keeping real warnings.
    min_severity: Severity = Severity.INFO
    #: Only deliver on this channel at or above this risk score. **None means no score filter.**
    #:
    #: The continuous companion to `min_severity`, and the reason both exist: the severity ladder
    #: has five steps, so between WATCH (0.40) and WARNING (0.60) sits a 0.20-wide band in which
    #: every subscriber is treated identically. That is precisely the band they disagree about — an
    #: irrigated commercial farm wants everything from 0.30 up; a smallholder who loses a day's
    #: labour reacting wants nothing under 0.55. Both are "watch and up".
    #:
    #: **This filters DELIVERY, never the assessment.** The reading is computed and persisted
    #: identically whatever this is set to; raising it opts out of being *messaged*, not out of
    #: being *watched*, and the portal still shows every assessment. It can only ever remove a
    #: delivery, so it cannot manufacture a warning and cannot reach
    #: `CONFIDENCE_ESCALATION_FLOOR` — a subscriber may make themselves harder to reach, never
    #: make an under-confident reading escalate.
    #:
    #: None rather than 0.0 so "no filter" stays distinguishable from "deliberately the lowest
    #: setting" — see migration 017.
    min_score: float | None = Field(default=None, ge=0, le=1)


class Subscriber(BaseModel):
    """A person or agency receiving alerts, and the AOIs they care about."""

    #: 10-character alphanumeric, e.g. `A7K2M9P4QX`. Public-facing: partners store it,
    #: farmers read it aloud, support agents quote it. Excludes I/O/0/1 because those
    #: are confused when read or handwritten, and every such confusion is a support
    #: call about a record that exists under a neighbouring id.
    id: str = Field(default_factory=_new_identity_id)
    name: str
    kind: SubscriberKind = SubscriberKind.FARMER
    language: str = Field(
        default="en",
        description="ISO-639-1. Advisory text is generated in this language.",
    )
    areas: list[AreaOfInterest] = Field(default_factory=list)
    channels: list[ChannelBinding] = Field(default_factory=list)
    active: bool = True
    created_at: datetime = Field(default_factory=_now)

    def channels_for(
        self,
        severity: Severity,
        aoi_id: str | None = None,
        score: float | None = None,
    ) -> list[ChannelBinding]:
        """Bindings that should fire at this severity and score, for this area.

        ## Specific overrides general

        A subscriber may want flood alerts for the rice plot by SMS and crop alerts for the palm
        plantation by email. So resolution is:

          * if any binding names this `aoi_id`, **only those** apply;
          * otherwise the bindings with `aoi_id is None` apply.

        Not a union. A union would mean adding an SMS override for one plot silently leaves the
        general email binding firing as well, so the subscriber gets two alerts and cannot turn
        the first off without losing it everywhere. "Override" has to mean override.

        `aoi_id=None` is the caller saying "no particular area" — used by the manual-dispatch path
        and by tests — and returns the general bindings only. That is the safe default: it can
        never fan out to an override the subscriber set for one specific plot.

        ## The two thresholds are ANDed, and `score=None` disables the second

        A binding fires only if the severity clears `min_severity` *and* the score clears
        `min_score`. Both are floors, so ANDing them is the only reading under which raising either
        one narrows delivery — an OR would mean setting a stricter dial made a subscriber receive
        *more*, which is the opposite of what the control says it does.

        **`score=None` skips the score filter entirely** rather than treating the score as 0. That
        keeps every existing caller correct without change: the manual-dispatch path and the
        per-plot channel preview in `api/routes/iam` ask "which channels would this reach?" with no
        assessment in hand, and applying an unknown score as zero there would report a subscriber's
        channels as silenced when in fact nothing had been measured yet.
        """
        from app.models.enums import SEVERITY_ORDER

        threshold = SEVERITY_ORDER[severity]
        eligible = [
            b
            for b in self.channels
            if b.enabled
            and SEVERITY_ORDER[b.min_severity] <= threshold
            # `>=`, so a dial set exactly to the score still delivers. A subscriber choosing 0.60
            # means "warn me at 0.60", not "above it".
            and (score is None or b.min_score is None or score >= b.min_score)
        ]

        if aoi_id is not None:
            specific = [b for b in eligible if b.aoi_id == aoi_id]
            if specific:
                return specific

        return [b for b in eligible if b.aoi_id is None]


class SubscriberCreate(BaseModel):
    """Request body for POST /subscribers — ids and timestamps are server-side."""

    name: str
    kind: SubscriberKind = SubscriberKind.FARMER
    language: str = "en"
    areas: list[AreaOfInterest]
    channels: list[ChannelBinding]


# --------------------------------------------------------------------------- #
# Agent 1 — Scout: what imagery exists
# --------------------------------------------------------------------------- #


class AssetRef(BaseModel):
    """One COG band, addressable by HTTP range request."""

    band: str
    href: str
    nodata: float | None = None


class SceneRef(BaseModel):
    """A STAC item narrowed to the assets we actually read."""

    item_id: str
    collection: str
    datetime: datetime
    cloud_cover: float | None = None
    bbox: BBox
    assets: list[AssetRef] = Field(default_factory=list)

    def asset(self, band: str) -> AssetRef | None:
        return next((a for a in self.assets if a.band == band), None)


class ScoutResult(BaseModel):
    """Agent 1 output: the scenes Agent 2 should slice."""

    aoi_id: str
    #: The AOI's true outline, carried forward so the Analyst can mask to it.
    #:
    #: Passed through the pipeline rather than re-read from Postgres in the Analyst, for the
    #: same reason `run_id` travels on the envelope: the Analyst is a queue-consuming stage
    #: that should not need a database round trip per job, and re-reading would let the
    #: geometry change between the search and the measurement — so the mask would not match
    #: the scene that was found.
    #:
    #: Optional, and None for a pin-and-radius AOI. A required field would also fail
    #: `model_validate_json` on every ScoutResult already sitting on a stream at deploy
    #: time, dead-lettering in-flight scans.
    aoi_ring: list[list[float]] | None = None
    #: The AOI's bounding box — **the window the Analyst must read.**
    #:
    #: Without this the Analyst had nothing to window with and passed `scene.bbox`, the
    #: satellite footprint, to `cog.read_bands`. Measured over Ikorodu that was 1,208,655 ha
    #: against a 7,825 ha AOI — **154x** — and the footprint contained Lagos Lagoon and the
    #: Atlantic, so a SAR water mask reported "65% of the area is under standing water" from
    #: open ocean. Kano, whose scene holds no coastline, read plausibly. That contrast is what
    #: identified the bug.
    #:
    #: Three existing safeguards were all defeated by the same root cause: `apply_ring_mask` is
    #: a documented no-op for pin-and-radius AOIs because "the bbox IS the geometry" (false when
    #: the bbox is the scene's); a polygon ring rasterises to nothing at the resulting
    #: 215 m/pixel and `indices._ring_mask` fails open to all-ones; and the
    #: "AOI does not intersect scene" guard in `cog.read_window` compared the scene against
    #: itself, so it could never fire.
    #:
    #: Optional for the same reason `aoi_ring` is: a required field would fail
    #: `model_validate_json` on every ScoutResult already on a stream at deploy time. The
    #: Analyst falls back to `scene.bbox` when it is absent and says so in the log, because an
    #: in-flight job from the previous release must still complete rather than dead-letter.
    aoi_bbox: BBox | None = None
    optical: list[SceneRef] = Field(default_factory=list)
    radar: list[SceneRef] = Field(default_factory=list)
    #: Set when optical is unusable (rainstorm cloud) and SAR must carry the
    #: analysis alone — the exact case that blinds optical-only systems.
    optical_blinded: bool = False
    searched_at: datetime = Field(default_factory=_now)


# --------------------------------------------------------------------------- #
# Agent 2 — Analyst: what the pixels say
# --------------------------------------------------------------------------- #


class IndexStats(BaseModel):
    """Summary of one spectral index over an AOI."""

    name: str
    mean: float
    std: float
    p10: float
    p90: float
    valid_fraction: float = Field(ge=0, le=1)


class AnalystResult(BaseModel):
    """Agent 2 output: physical measurements, not yet decisions."""

    aoi_id: str
    indices: list[IndexStats] = Field(default_factory=list)
    #: Fraction of the AOI classified as standing water by the SAR model.
    #:
    #: **Read `flood_measured` before trusting this.** 0.0 means "no standing water" only when
    #: `flood_measured` is True; when the radar leg failed it means "not measured", and the two are
    #: not interchangeable — see below.
    inundated_fraction: float = Field(default=0.0, ge=0, le=1)
    #: Fraction of cropland whose vegetation index sits below its seasonal norm.
    #:
    #: **Read `stress_measured` before trusting this.** Same reasoning as above.
    stressed_crop_fraction: float = Field(default=0.0, ge=0, le=1)

    # --- Was each measurement actually taken? -------------------------------
    #
    # ## Why a float alone cannot say this
    #
    # A failed leg returned `{}` and the result took `.get(field, 0.0)`, so a radar read failure
    # was indistinguishable from a measured absence of water. The consequences were concrete:
    # `oracle._classify` tests `stressed > 0.30 and inundated < 0.05` and so classified a
    # radar-blind cycle during a real flood as CROP_DROUGHT_STRESS — advice that is not merely
    # vague but inverted. And `min()` over the contributing legs cannot pull confidence down for a
    # leg that contributed nothing, so the wrong figure travelled at the *other* sensor's
    # confidence.
    #
    # The codebase already had the right pattern in two places — `stats/anomaly` returns None
    # rather than 0.0, and `eo/terrain` sets `available=False` on a mostly-void DEM window. This
    # brings the Analyst into line with them.
    #
    # Default True so a result built by older code, or in a test that does not care, keeps its
    # existing meaning. The legs set it explicitly.

    #: False when the radar leg produced no reading — failed, absent, or too little AOI coverage.
    flood_measured: bool = True
    #: False when the optical leg produced no reading.
    stress_measured: bool = True
    #: Share of the AOI the imagery actually covered, per leg. Below
    #: `analyst.MIN_AOI_COVERAGE` the leg declines to measure rather than reporting a figure
    #: derived from a sliver of the field.
    flood_coverage: float | None = Field(default=None, ge=0, le=1)
    stress_coverage: float | None = Field(default=None, ge=0, le=1)
    #: Model confidence, propagated into severity so a shaky read can't
    #: escalate to EMERGENCY on its own.
    confidence: float = Field(default=0.0, ge=0, le=1)
    scenes_used: int = 0
    source: str = Field(default="sar", description="'sar', 'optical' or 'fused'")
    computed_at: datetime = Field(default_factory=_now)

    # --- WHEN the pixels were acquired ---------------------------------------
    #
    # `computed_at` is when we did the arithmetic — always now, and therefore useless as
    # provenance. Without the acquisition time a 20-day-old pass and a 6-hour-old one produce
    # byte-identical output, and for a flood warning "as of this morning" versus "as of nearly
    # three weeks ago" is the difference between actionable and misleading.
    #
    # This became load-bearing when `max_scene_age_days` widened from 12 to 20 to accommodate
    # Landsat's 16-day revisit: `_best_optical` sorts by cloud cover with recency only as a
    # tie-break, so a wider window lets a clear-but-stale scene outrank a cloudier fresh one. That
    # is the right trade for measurement quality only if the age is disclosed.
    #
    # Optional because a result built before this field existed, or by a test that does not care,
    # must still validate.
    #: When the radar scene was acquired.
    flood_observed_at: datetime | None = None
    #: When the optical scene was acquired.
    stress_observed_at: datetime | None = None
    #: Which platform each measurement came from — `sentinel-2-l2a` or `landsat-c2-l2` for the
    #: optical leg. Two sensors now feed it at different resolutions (10 m vs 30 m), so "which
    #: one" is part of reading the number.
    stress_platform: str | None = None
    flood_platform: str | None = None

    # --- Provenance for the two measurements above --------------------------
    # Which code path produced each number. Surfaced in `evidence` so an advisory
    # can say the figure excludes permanent water, or that stress is measured
    # against a seasonal norm rather than a fixed cut. That is checkable
    # provenance rather than a number the reader must simply trust.

    #: "trained-model" | "adaptive-otsu" | "adaptive-fixed-threshold" | "heuristic"
    flood_method: str = "heuristic"
    #: "trained-model" | "heuristic" | "seasonal-anomaly (N obs)"
    stress_method: str = "heuristic"
    #: Free-form diagnostics from the flood path — the chosen dB threshold, the
    #: histogram separability, how many pixels permanent-water masking removed.
    flood_diagnostics: dict = Field(default_factory=dict)
    #: Diagnostics from the crop-stress path — baseline size, residual scatter, and
    #: the threshold-based figure alongside the anomaly one for comparison.
    stress_diagnostics: dict = Field(default_factory=dict)

    #: Which input drove the crop-stress verdict, as `{channel: signed contribution}`.
    #:
    #: **Exact, not estimated.** `CropStressNet` is 1x1 convolutions throughout, so it is a per-pixel
    #: function of four numbers with no spatial context — an ablation is therefore the contribution
    #: rather than an approximation of it. See `ml/inference.crop_stress_attribution`.
    #:
    #: This changes the ADVICE, not just the wording. Two pixels can score identically stressed for
    #: opposite reasons: driven by `ndmi` the plant is short of water and should be irrigated; driven
    #: by `anomaly` growth is below the field's own norm while moisture is fine, which points at
    #: pests, nutrients or planting date — and irrigating would waste water.
    #:
    #: Empty when no weights are loaded: the heuristic path has no channels to attribute, and
    #: inventing an attribution for a fixed threshold would be a fabricated explanation.
    stress_attribution: dict = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Agent 3 — Oracle: what it means, and for whom
# --------------------------------------------------------------------------- #


class ForecastPoint(BaseModel):
    """One day of the 7-day outlook."""

    day: int = Field(ge=0, description="Days from now; 0 is today")
    date: datetime
    risk: float = Field(ge=0, le=1)
    rainfall_mm: float = 0.0
    note: str | None = None


class RainfallOutlook(BaseModel):
    """Result of the rainfall source chain.

    Only a genuine forecast source (GEFS via ClimateSERV) can fill `points`
    with future rainfall. Observational sources — CHIRPS, IMERG, ERA5 — supply
    `antecedent_mm` instead: how wet the ground already is, which is a real
    flood-risk signal but is *not* a forecast. Keeping the two apart is what
    stops the Oracle from reporting predicted rain that nobody predicted.
    """

    points: list[ForecastPoint] = Field(default_factory=list)
    #: True only when `points` came from a forecast model.
    forecast_available: bool = False
    #: Observed rainfall over the preceding week, mm. 0 when unknown.
    antecedent_mm: float = 0.0
    #: "climateserv-gefs" | "climateserv-chirps" | "gpm-imerg" | "era5" | "none"
    source: str = "none"

    # --- Derived statistics (app/stats/) -----------------------------------
    # All three are None/0 when the statistic could not be computed, and the
    # Oracle then uses the raw-millimetre path. None means "not measured", never
    # "measured as zero" — the same distinction `ExposureSummary.sources` draws.

    #: Standardized Precipitation Index for the accumulation window. Dimensionless
    #: and comparable across the country, unlike raw mm: 180 mm is an ordinary wet
    #: week in Bayelsa and a rare event in Sokoto. None without ~20 comparable
    #: historical windows to fit against.
    spi: float | None = None
    #: Antecedent Precipitation Index — exponentially-weighted antecedent wetness,
    #: mm-equivalent. Weights yesterday's rain above last week's, which a flat
    #: 7-day sum cannot. 0 when no daily series was available.
    api_mm: float = 0.0
    #: Per-day ensemble members, when the forecast source delivered a spread rather
    #: than one averaged series. `[[day0_member0, day0_member1, ...], [day1...]]`.
    #: Empty for a deterministic forecast — which is the current ClimateSERV case,
    #: so the Oracle must not assume this is populated.
    ensemble_by_day: list[list[float]] = Field(default_factory=list)


class ExposureSummary(BaseModel):
    """Who and what sits inside the hazard footprint.

    Every field here is measured, not estimated. When a source doesn't answer
    the value stays 0 and its name is absent from `sources` — so the Oracle can
    tell "nothing there" apart from "we don't know", which are very different
    inputs to a severity decision.
    """

    population: int = 0
    cropland_hectares: float = 0.0
    cropland_fraction: float = Field(default=0.0, ge=0, le=1)
    water_fraction: float = Field(default=0.0, ge=0, le=1)
    builtup_fraction: float = Field(default=0.0, ge=0, le=1)
    #: Share of the AOI sitting materially below its median elevation — where
    #: water collects first, from the Copernicus DEM.
    lowland_fraction: float = Field(default=0.0, ge=0, le=1)
    settlements: int = 0
    health_facilities: int = 0
    #: Which datasets actually answered, e.g. ["worldpop", "worldcover", "dem"].
    sources: list[str] = Field(default_factory=list)


class SoilProfile(BaseModel):
    """Drainage behaviour from SoilGrids.

    Governs how long standing water persists, which is what separates a flood
    that drains in two days from one that rots roots for three weeks.
    """

    clay_g_kg: float = 0.0
    sand_g_kg: float = 0.0
    #: "free" | "moderate" | "impeded"
    drainage: str = "unknown"
    available: bool = False

    @property
    def waterlogging_multiplier(self) -> float:
        """How much this soil prolongs a waterlogging event."""
        return {"impeded": 1.25, "moderate": 1.0, "free": 0.8}.get(self.drainage, 1.0)


class SoilMoisture(BaseModel):
    """Measured volumetric soil water content from SMAP, m3/m3.

    Distinct from `SoilProfile`, which reports what the soil IS (texture, permanent). This reports
    what is IN it right now, which is the state an irrigation decision turns on — see
    `eo/soil_moisture.py` for why rainfall cannot substitute for it.

    `available=False` means **unknown**, never dry. A zero default that read as "no water in the
    soil" would drive a confident irrigation instruction from no measurement at all.
    """

    #: Volumetric water content, m3 of water per m3 of soil. Physically bounded to [0, 1].
    volumetric: float = Field(default=0.0, ge=0.0, le=1.0)
    #: Date of the overpass this came from, `YYYY-MM-DD`. SMAP publishes with a ~2-day lag, so this
    #: is never today and the advisory must be able to say when it was measured.
    observed_date: str = ""
    #: How far the grid cell actually read was from the AOI centroid, degrees. Carried rather than
    #: discarded because it is the evidence that the EASE-Grid lookup was correct — see
    #: `soil_moisture.MAX_LOCATION_ERROR_DEG`.
    location_error_deg: float = 0.0
    available: bool = False

    # ## Both derived fields are SERIALISED, not left as bare properties
    #
    # `@computed_field` puts them on the wire. Without it a client sees only `volumetric` and has
    # to re-implement the wilting-point and field-capacity thresholds to say "irrigate" — a second
    # copy of an agronomic decision, in a language that cannot import the first. The web card and
    # the email would then be one edit away from disagreeing about whether to water a field.
    #
    # Same reasoning as `intelligence.describe` being shared between the portal and the webhook:
    # the platform must not have two opinions about one measurement.

    @computed_field  # type: ignore[prop-decorator]
    @property
    def status(self) -> str:
        """Agronomic band: `unknown` | `very_dry` | `dry` | `adequate` | `wet` | `saturated`.

        Bands rather than a raw figure because "0.19 m3/m3" is not actionable to a farmer and the
        band is what changes the advice. Thresholds are the wide loam bands in `soil_moisture.py` —
        deliberately coarse, since a per-texture curve needs parameters SoilGrids does not serve.
        """
        if not self.available:
            return "unknown"
        from app.eo.soil_moisture import FIELD_CAPACITY, SATURATION, WILTING_POINT

        if self.volumetric < WILTING_POINT:
            return "very_dry"
        if self.volumetric < WILTING_POINT + (FIELD_CAPACITY - WILTING_POINT) * 0.5:
            return "dry"
        if self.volumetric <= FIELD_CAPACITY:
            return "adequate"
        if self.volumetric < SATURATION:
            return "wet"
        return "saturated"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def irrigation_advice(self) -> str | None:
        """`irrigate` | `hold` | `drain`, or None when unknown.

        None rather than a default is the whole point: no measurement must produce no instruction.
        `drain` fires at saturation because further irrigation there causes root anoxia — the same
        waterlogging damage a flood does, self-inflicted.
        """
        return {
            "very_dry": "irrigate",
            "dry": "irrigate",
            "adequate": "hold",
            "wet": "hold",
            "saturated": "drain",
        }.get(self.status)


class HealthBaseline(BaseModel):
    """Malaria reference data — the malaria arm of the cascade is only credible
    where the parasite already circulates."""

    malaria_pfpr: float = Field(default=0.0, ge=0, le=1)
    endemic: bool = False
    available: bool = False


class SituationChange(BaseModel):
    """How this reading differs from the previous run, and from the seasonal norm.

    ## Why "what changed" belongs on the alert

    A severity on its own does not tell a farmer whether to act *now*. WATCH that has been WATCH
    for a fortnight is a background condition; WATCH that was INFO yesterday is a developing
    event. Same label, opposite urgency — and only the delta separates them.

    ## Why "compared with normal" is separate from the score

    The score already accounts for the seasonal baseline where one exists. But a subscriber cannot
    audit a score, and "drier than this field usually is in August" is a sentence they can check
    against their own memory of the place. It is the same argument as showing a shape on a map
    rather than a hectares figure.

    Every field is optional, and **absent means unknown rather than unchanged.** A first-ever
    assessment has nothing to compare against, and a plot with no fitted baseline has no norm —
    reporting either as "no change" would assert something false.
    """

    #: Severity of the previous assessment, e.g. "info". None on a first run.
    previous_severity: str | None = None
    #: Score of the previous assessment, for the arrow direction.
    previous_score: float | None = None
    #: When that reading was taken, so "since yesterday" can be said honestly.
    previous_assessed_at: datetime | None = None

    #: `up` | `down` | `steady`, derived from the score. Machine token; switch on this.
    direction: str | None = None

    #: "wetter" | "drier" | "greener" | "browner" | "normal" — against this plot's own history.
    #: None when no baseline is fitted, which is the common case on a new area.
    vs_seasonal: str | None = None
    #: The anomaly in standard deviations, signed. Carried so a dashboard can size a bar rather
    #: than only print a word.
    vs_seasonal_z: float | None = None

    @property
    def is_escalating(self) -> bool:
        """Whether this reading is worse than the last.

        Used to decide whether a card leads with "rising" — the one framing that should never be
        applied to a steady condition, because it would make every routine reading read as an
        emergency and cost the next real one its audience.
        """
        return self.direction == "up"


class DataFreshness(BaseModel):
    """When the inputs were observed, and when the next look is due.

    Apple states region availability and provider names explicitly; the equivalent here is saying
    which satellite last saw this plot and when it will pass again. A subscriber who knows the last
    radar pass was six days ago reads a "no flooding detected" very differently from one who
    assumes it is live.

    `next_expected` is an ESTIMATE from the revisit cadence, not a promise. Cloud, orbit gaps and
    upstream outages all move it, which is why the field is named for expectation rather than
    schedule.
    """

    #: Most recent successful observation across all legs.
    observed_at: datetime | None = None
    #: Which platform produced it — "sentinel-1", "sentinel-2".
    platform: str | None = None
    #: Estimated next observation, from the source's own revisit cadence.
    next_expected: datetime | None = None
    #: Why a leg is missing, when one is: "radar unavailable this cycle", "scene 80% cloud".
    #: Stated rather than left blank — an absent measurement is a fact about the reading.
    caveat: str | None = None


class ScoreDriver(BaseModel):
    """One input's exact contribution to a risk score.

    Not a SHAP estimate or a sensitivity probe — the Oracle's score IS a weighted sum, so
    `weight * value` is the contribution, full stop. Reporting it is bookkeeping rather than
    interpretation, which is what makes it safe to put in front of a farmer.
    """

    #: Machine token: `observed`, `forecast`, `exposure`. Switch on this.
    key: str
    #: Human label, safe to display. May be reworded; do not parse.
    label: str
    #: The term's own value, 0-1, before weighting.
    value: float = Field(ge=0, le=1)
    #: The Oracle's fixed weight for this term.
    weight: float = Field(ge=0, le=1)
    #: `value * weight` — this term's share of the final score.
    contribution: float = Field(ge=0, le=1)
    #: What actually fed it, so the number is traceable to a measurement rather than to a stage
    #: name. Empty when the term ran on defaults because its inputs were unavailable.
    inputs: list[str] = Field(default_factory=list)


class RiskAssessment(BaseModel):
    """Agent 3 output: the decision, with its evidence attached."""

    id: str = Field(default_factory=lambda: _new_id("risk"))
    aoi_id: str
    aoi_name: str
    #: Administrative place names, carried for verification.
    #:
    #: **Fahis cannot search on `aoi_name`.** That is the subscriber's own label — "My Irri Palm
    #: Fruit Plantation" — and it appears in no news report anywhere, so a query built from it is
    #: structurally incapable of finding corroboration. Verified live: three queries returned ten
    #: sources, none about the place, and the verdict was `unverified` for the wrong reason.
    #:
    #: "Isoko South, Delta State" is what a report would actually name. Copied onto the assessment
    #: rather than joined at verification time because Fahis runs days later and the area may have
    #: been renamed, moved or removed by then — the verdict must describe the ground as it was
    #: measured, not as it is now.
    admin1: str | None = None
    admin2: str | None = None
    country: str = "NG"
    hazard: HazardType
    severity: Severity
    score: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    forecast: list[ForecastPoint] = Field(default_factory=list)
    #: Whether `forecast`'s rainfall figures came from a forecast model, or describe rain that has
    #: already fallen.
    #:
    #: **Forecast and antecedent are different things and must never be collapsed.** Only
    #: ClimateSERV GEFS predicts; CHIRPS, IMERG and ERA5 report how wet the ground already is.
    #: `RainfallOutlook.forecast_available` draws that line upstream, and this field is what carries
    #: it onto the assessment so the per-track modules and the frontend can word it correctly —
    #: "120 mm expected" and "120 mm already fell" call for opposite actions from a farmer.
    #:
    #: Defaults to **False**, which is the safe reading: describing a real forecast as rain that has
    #: already fallen understates a warning, where the reverse would invent a prediction nobody
    #: made. The Oracle sets it explicitly from the outlook.
    forecast_is_prediction: bool = False
    exposure: ExposureSummary = Field(default_factory=ExposureSummary)
    soil: SoilProfile = Field(default_factory=SoilProfile)
    #: Measured soil wetness. Separate from `soil` (texture) because one is a state and the other a
    #: property — see `SoilMoisture`. Carries the Track A irrigation call.
    soil_moisture: SoilMoisture = Field(default_factory=SoilMoisture)
    health: HealthBaseline = Field(default_factory=HealthBaseline)

    #: How this reading differs from the previous run and from the seasonal norm.
    #:
    #: Empty on a first assessment, which is honest: there is nothing to compare against. The card
    #: renders no change line rather than "no change" — see `SituationChange`.
    change: SituationChange = Field(default_factory=SituationChange)
    #: When the inputs were observed and when the next look is due.
    freshness: DataFreshness = Field(default_factory=DataFreshness)
    #: Which upstream datasets contributed, for provenance on the dashboard.
    data_sources: list[str] = Field(default_factory=list)

    #: Which inputs produced this score, and by how much. **Exact, not inferred.**
    #:
    #: ## Why this is attribution and not interpretation
    #:
    #: `score` is a weighted sum of three terms the Oracle computes explicitly:
    #: `W_OBSERVED * observed + W_FORECAST * forecast + W_EXPOSURE * exposure`. So the contribution
    #: of each input to the final number is not something a model has to guess at — it is
    #: arithmetic we already did and then threw away.
    #:
    #: Keeping it turns "narrate the drivers behind a risk score" from an inference into a
    #: statement of fact. `explain/drivers.py` receives these numbers and can say *rainfall
    #: contributed 0.31 of the 0.67* rather than reasoning from prose about which factor mattered
    #: most. That is the difference between explainable AI and a plausible-sounding guess, and it
    #: costs nothing because the values are already in hand.
    #:
    #: Empty on an assessment built before this field existed, or by a test that does not care.
    #: Consumers must treat absence as "not recorded", never as "contributed zero".
    score_drivers: list[ScoreDriver] = Field(default_factory=list)

    # --- Measurement provenance, carried from AnalystResult -------------------
    #
    # ## Why these are copied rather than looked up
    #
    # `AnalystResult` already records when each leg observed, from which platform, and by which
    # method. But it is a pipeline-internal object: the Oracle consumes it and the assessment is
    # what everything downstream sees — the advisory generator, the three explainers, the webhook,
    # the portal, Fahis. Without these fields the provenance stops at the Oracle, and every
    # consumer that wants to say "measured by radar on 9 August by a trained model" would have to
    # re-read the Analyst's output, which no longer exists by then.
    #
    # `*_method` is the honest part: a farmer is entitled to know whether a figure came from a
    # trained model or from a physical threshold, and `explain/base.provenance_block` states it.
    observed_at_flood: datetime | None = None
    observed_at_stress: datetime | None = None
    platform_flood: str | None = None
    platform_stress: str | None = None
    method_flood: str | None = None
    method_stress: str | None = None
    #: Per-channel attribution for the crop-stress verdict. See `AnalystResult.stress_attribution`
    #: for what it means and why it is exact. Carried here because `AnalystResult` does not survive
    #: the Oracle, and the explainers only ever see the assessment.
    stress_attribution: dict = Field(default_factory=dict)

    # --- The measured fractions, carried for the retraining loop ---------------
    #
    # ## Why these are here and not left on AnalystResult
    #
    # `verification_outcomes` joins a Fahis verdict back to the assessment it judged. That gives
    # `(confidence, outcome)` — enough to CALIBRATE a confidence, and useless for RETRAINING, because
    # a model needs the inputs that produced the prediction rather than the prediction itself.
    #
    # A CONFIRMED flood should teach us that "65% inundated, on impeded clay, with 48 mm forecast"
    # was correct. Without the fraction on the assessment, all it can teach us is "the pipeline was
    # right at 0.88" — a scalar, and the same scalar for every hazard.
    #
    # **None means NOT MEASURED**, never zero. That distinction is the one `flood_measured` exists to
    # preserve, and collapsing it here would reintroduce the defect that made a radar failure
    # classify as drought: a training row asserting "0% water, and that was CONFIRMED" would teach
    # the model that a blind cycle is evidence of dry ground.
    inundated_fraction: float | None = Field(default=None, ge=0, le=1)
    stressed_crop_fraction: float | None = Field(default=None, ge=0, le=1)
    #: Plain-language drivers, e.g. "31% of cropland under standing water".
    #: These are what the advisory generator is allowed to cite.
    evidence: list[str] = Field(default_factory=list)
    #: The cascade: hazards this one is expected to trigger downstream.
    cascade: list[HazardType] = Field(default_factory=list)
    lead_time_days: int = 7
    assessed_at: datetime = Field(default_factory=_now)


# --------------------------------------------------------------------------- #
# Agent 4 — Herald: what we say, and where it goes
# --------------------------------------------------------------------------- #


class Explanations(BaseModel):
    """The three explanation surfaces, carried with the advisory.

    ## Why these travel WITH the alert rather than being fetched later

    An alert is the record of what a subscriber was told. If the explanations were regenerated on
    read, a farmer disputing "you never warned me about the water" would be shown text produced
    today from an assessment measured weeks ago — and the model might word it differently. Storing
    them makes the alert self-contained and the record honest.

    It also means the email and the portal show identical wording, which matters: a subscriber who
    reads one and then the other should not find two accounts of the same finding.

    Every field defaults to empty, so an alert created before this existed — or one where the
    provider was unavailable — deserialises cleanly rather than failing validation on a row
    already in the database.
    """

    #: What the crop is doing, in plain language. From `explain.optical`.
    crop: str = ""
    #: Why the risk score is what it is. From `explain.drivers`.
    drivers: str = ""
    #: Irrigate or hold, with the reason. From `explain.irrigation`.
    irrigation: str = ""


class Advisory(BaseModel):
    """Generated guidance, sized per channel."""

    headline: str = Field(max_length=140)
    body: str
    actions: list[str] = Field(default_factory=list)
    #: Hard-capped for satellite broadcast; see `nigcomsat_max_payload_bytes`.
    broadcast_text: str = Field(default="", max_length=280)
    language: str = "en"
    generated_by: str = "template"
    #: The three explanation surfaces. Empty when no provider is configured, in which case each
    #: carries its deterministic template rather than nothing — see `app/explain/`.
    explanations: Explanations = Field(default_factory=Explanations)


class DeliveryReceipt(BaseModel):
    channel: Channel
    address: str
    status: DeliveryStatus
    provider_message_id: str | None = None
    error: str | None = None
    attempted_at: datetime = Field(default_factory=_now)


class AssessmentTrack(BaseModel):
    """One measured dimension of a plot's situation, as the API serialises it.

    ## Why this mirrors `dispatch.tracks.Track` instead of being it

    `Track` is a frozen dataclass in `app/dispatch/`, and the derivation that builds it imports this
    module — so `Alert` cannot hold a `Track` without a circular import. It is also the wrong
    dependency direction: the schemas are the platform's vocabulary and must not depend on a
    delivery channel's helper.

    So the agronomy — thresholds, wording, concern bands — lives in exactly one place
    (`dispatch/tracks.py`), and this is the wire shape. `api/routes/alerts` converts at the edge, the
    same place and for the same reason `_attach_verdicts` joins the verdict.

    **The frontend must not re-derive any of this.** A TypeScript copy of the thresholds is how the
    email and the portal would come to describe one plot differently, which is precisely the drift
    `card_fields` and `email/layout` were each written to end. `tests/test_tracks.py` asserts the
    frontend ships no threshold constants of its own.
    """

    #: Stable machine key: `flood` | `crop` | `soil_water` | `rainfall` | `malaria`. The frontend
    #: routes icons off this, never off `label`.
    key: str
    label: str
    #: The headline value, already formatted with its unit.
    reading: str
    #: One sentence of plain language.
    meaning: str
    #: Ordering only — see `Track.weight`. Serialised so the frontend can preserve the server's
    #: order without knowing the thresholds that produced it.
    weight: float = 0.0
    sources: list[str] = Field(default_factory=list)
    detail: list[tuple[str, str]] = Field(default_factory=list)


class Alert(BaseModel):
    """The full artefact: assessment + advisory + delivery outcome."""

    id: str = Field(default_factory=lambda: _new_id("alert"))
    subscriber_id: str
    assessment: RiskAssessment
    advisory: Advisory
    receipts: list[DeliveryReceipt] = Field(default_factory=list)
    #: Fahis's verdict, once it has run. `None` until then — see `VerdictSummary`.
    #:
    #: Populated on read rather than stored on the alert row, because a verdict is recorded days
    #: later and an alert is immutable once sent. Joining on read is also what keeps the two
    #: independent: the alert is what we told the subscriber, the verdict is what turned out to be
    #: true, and conflating them would let the second silently rewrite the first.
    verdict: VerdictSummary | None = None
    #: The per-track modules, most relevant first. Derived on read, never stored.
    #:
    #: Empty is a real and common state — a fully clouded cycle with no radar pass measured nothing,
    #: and the honest rendering is "we could not look" rather than five modules reading zero.
    tracks: list[AssessmentTrack] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)

    @property
    def delivered(self) -> bool:
        return any(r.status == DeliveryStatus.SENT for r in self.receipts)


# --------------------------------------------------------------------------- #
# Agent 5 — Fahis: did it actually happen?
# --------------------------------------------------------------------------- #


class SourceCitation(BaseModel):
    """One external source Fahis consulted.

    Stored verbatim so an operator can audit the reasoning rather than trusting
    it. The snippet is what the model actually saw — re-fetching later may return
    changed content.
    """

    url: str
    title: str
    snippet: str
    #: "official" | "media" | "other". Weighted by Fahis; an agency bulletin is
    #: not a blog post.
    tier: str = "other"
    published: str | None = None


class CitedSource(BaseModel):
    """One source behind a verdict, as shown to the subscriber.

    ## Why this is not `SourceCitation`

    `SourceCitation` carries the **snippet** — the raw web prose the model actually read. That is
    provenance for an operator and must not travel to an alert card: putting unattributed outside
    text beside a measured advisory is the adjacency the grounding rule exists to prevent, and it
    is a rule this codebase has broken twice.

    What a subscriber needs in order to *check* a verdict is different and smaller: where it came
    from, who published it, how much weight it carries, and when. Those four are checkable — the
    reader can open the link and judge for themselves. A snippet asks them to trust our excerpt
    instead, which is the opposite of verifiable.
    """

    url: str
    title: str = ""
    #: `official` (a government or agency), `media`, or `low`. Shown because a subscriber weighing
    #: a verdict should see whether it rests on NEMA or on a blog — the same distinction Fahis
    #: itself applies when it downgrades a `confirmed` that has only low-tier support.
    tier: str = "other"
    #: Publication date where the source stated one. Load-bearing for trust rather than decorative:
    #: a 2019 article cannot corroborate a 2026 warning, and showing the date is what lets a reader
    #: catch that themselves instead of taking the verdict on faith.
    published: str | None = None


class VerdictSummary(BaseModel):
    """A Fahis verdict, attached to the alert it judges.

    ## Why the verdict travels ON the alert

    "Were we right?" is only meaningful beside the warning it judges. A separate accuracy page can
    show the aggregate, but a subscriber looking at one alert wants to know about *that* one — and
    an aggregator reconciling a lending decision needs the verdict on the record they acted on.

    ## Why the sources travel too, but not the snippets

    A verdict without its sources asks to be believed. "Independently confirmed" is a claim about
    the outside world, and the only thing that turns it from an assertion into evidence is the
    reader's ability to go and look. So the citations travel: url, title, tier and date — the four
    fields that make a verdict checkable.

    The **snippets stay behind**, for the reason in `CitedSource`. A count alone (which is all this
    carried before) tells a subscriber how many sources exist but not what they were, so it cannot
    be verified and cannot build the trust the accountability agent exists to earn.

    Absent (`None`) means Fahis has not run yet, which is normal: verification is scheduled days
    after the assessment. That is different from `unverified`, which IS a finding.
    """

    verdict: Verdict
    #: How sure Fahis is of the VERDICT, not of the original alert.
    confidence: float = Field(default=0.0, ge=0, le=1)
    rationale: str = ""
    #: How many independent sources were cited. Kept beside `sources` rather than replaced by it:
    #: a caller routing on weight ("two or more agencies") reads the count without walking a list,
    #: and it stays correct for an older client that does not know about `sources`.
    source_count: int = 0
    #: The citations themselves, so the verdict can be checked rather than believed. Ordered as
    #: Fahis ranked them — highest tier first.
    sources: list[CitedSource] = Field(default_factory=list)
    #: True for `confirmed` and `refuted` only — the two that count toward precision.
    trainable: bool = False
    verified_at: datetime | None = None


class Verification(BaseModel):
    """Agent 5 output: whether reality matched the warning.

    This is the only ground truth the system ever gets. `ml/weights/*.pt` are
    absent by default and inference falls back to threshold heuristics, so without
    this table there is no way to know whether any of it works.

    **Never feeds back into an advisory.** It writes here and to agent_memory, and
    that is the whole of its reach — see `agents/fahis.py`.
    """

    id: str = Field(default_factory=lambda: _new_id("ver"))
    assessment_id: str
    aoi_id: str
    alert_id: str | None = None

    #: What we claimed, copied so a verdict is readable without a join.
    claimed_hazard: HazardType
    claimed_severity: Severity
    assessed_at: datetime

    verdict: Verdict = Verdict.UNVERIFIED
    #: How sure Fahis is *of the verdict itself* — not of the original alert.
    #: A CONFIRMED from one blog is low confidence; from two agencies, high.
    confidence: float = Field(default=0.0, ge=0, le=1)
    #: Plain-language justification, citing only the sources below.
    rationale: str = ""
    sources: list[SourceCitation] = Field(default_factory=list)
    #: Queries actually issued. Provenance for why a search found nothing.
    queries: list[str] = Field(default_factory=list)

    verified_at: datetime = Field(default_factory=_now)

    @property
    def is_trainable(self) -> bool:
        """Whether this row is usable as evaluation signal.

        UNVERIFIED and NOT_ATTEMPTED are excluded: counting an unreported real
        flood as a false positive would make the metrics actively misleading.
        """
        from app.models.enums import TRAINABLE_VERDICTS

        return self.verdict in TRAINABLE_VERDICTS


# --------------------------------------------------------------------------- #
# Chat — Herald's conversational surface
# --------------------------------------------------------------------------- #


class ChatMessage(BaseModel):
    """One turn. `role` is "user" or "assistant"; tool traffic is not exposed."""

    role: str
    content: str
    created_at: datetime = Field(default_factory=_now)


class ChatTurn(BaseModel):
    """A reply, with everything needed to audit where it came from."""

    session_id: str
    reply: str
    #: External pages cited. Empty when the answer came only from the
    #: subscriber's own alert data.
    sources: list[SourceCitation] = Field(default_factory=list)
    #: Tools invoked, in order. Cheap provenance for "how did it know that?".
    tools_used: list[str] = Field(default_factory=list)
    #: True when the tool-round bound was hit, so the answer may be incomplete.
    truncated: bool = False
    created_at: datetime = Field(default_factory=_now)


# --------------------------------------------------------------------------- #
# Queue envelope
# --------------------------------------------------------------------------- #


class JobEnvelope(BaseModel):
    """What actually sits on a Redis stream.

    `payload` is the previous stage's result, already serialised. Keeping the
    envelope thin means a stuck stage can be inspected without deserialising
    a whole scene graph.
    """

    model_config = ConfigDict(use_enum_values=False)

    id: str = Field(default_factory=lambda: _new_id("job"))
    stage: JobStage
    status: JobStatus = JobStatus.QUEUED
    subscriber_id: str | None = None
    aoi_id: str | None = None
    payload: dict = Field(default_factory=dict)
    attempts: int = 0
    error: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    #: Correlates one AOI's whole journey — Scout → Analyst → Oracle → Herald, and
    #: the Fahis verification days later. Minted once at enqueue and copied on every
    #: hand-off, so `run_id=...` in a log query returns the entire run across all
    #: five stages and both worker pools.
    #:
    #: `job.id` cannot do this: it is regenerated per stage (each hand-off is a new
    #: envelope) and again on every retry, which is exactly what makes it useful for
    #: identifying *one* queue entry and useless for following a scan.
    #:
    #: **Optional on purpose.** Envelopes already sitting on a Redis stream when
    #: this shipped have no such field, and a required one would make every one of
    #: them fail `model_validate_json` and dead-letter — silently dropping the
    #: in-flight scans of every active subscriber at deploy time.
    run_id: str | None = None
