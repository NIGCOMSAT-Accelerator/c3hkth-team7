"""Step 6 — seasonal anomaly detection for vegetation indices.

**The bug this fixes.** `stressed_crop_fraction` currently comes from `NDVI < 0.35`.
That threshold cannot distinguish:

    a rice field drowning in August          <- warn, urgently
    the same field bare in January           <- normal, say nothing
    a fallow plot the farmer chose not to sow <- not our business

All three read below 0.35. So a fixed cut on a seasonal quantity generates false
alarms in the dry season and, worse, sets a floor on what counts as stress — a field
that has crashed from 0.75 to 0.45 is in serious trouble and this test says nothing
at all.

Note that `AnalystResult.stressed_crop_fraction` already *documents* itself as
"below its seasonal norm", and `_evidence` already emits the phrase "below its
seasonal norm". Neither was true: there was no norm. This module supplies one, which
makes the existing claim honest rather than aspirational.

**The fix.** Ask a different question: *is this pixel below where it usually is on
this day of year?* Fit a harmonic (Fourier) series to the pixel's own history, then
measure the residual in units of its own historical scatter:

    z = (observed - seasonal_expectation) / seasonal_std

Two or three harmonics capture a single growing season plus its asymmetry, which is
what West African cropland actually looks like. More harmonics fit noise.

**Why harmonic regression rather than a monthly mean climatology.**

1. It is a **least-squares fit**, so it works with irregular observation dates —
   which is what cloud gaps produce. A monthly-mean approach needs binning and
   silently loses months that were entirely clouded.
2. It **interpolates**, so it produces an expectation for a day of year never
   observed.
3. It is **~10 coefficients per pixel**, cheap to store and evaluate, and terrain
   does not move — so it is computed once per AOI and cached, exactly like HAND.

**Degradation.** Without history this returns `available=False` and the caller keeps
the `NDVI < 0.35` heuristic. That is the correct default until a real baseline
exists, and it is why the fixed threshold stays in the code rather than being
deleted.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: Harmonics in the seasonal fit. 2 = annual + semi-annual, which captures one
#: growing season and its asymmetric green-up/senescence. 3 allows a bimodal
#: season (southern Nigeria's two rainy periods).
DEFAULT_HARMONICS = 2

#: Minimum observations before a harmonic fit is attempted.
#:
#: The design matrix has 1 + 2*harmonics columns (5 at 2 harmonics), so 12 gives
#: a comfortable margin over the 5 needed to avoid an exactly-determined system.
#: Fewer than this and the fit interpolates its own noise.
MIN_OBSERVATIONS = 12

#: Physical bounds of a normalised difference index. NDVI, NDMI and NDWI are all `(a-b)/(a+b)`.
#:
#: Not a tuning parameter — this is what the arithmetic permits. A harmonic fit is unconstrained and
#: will leave the range on a gappy series; see `HarmonicBaseline.expected`.
INDEX_MIN = -1.0
INDEX_MAX = 1.0

#: Minimum share of the year that must actually be observed before a seasonal fit is trusted.
#:
#: ## Why observation COUNT is not enough
#:
#: `MIN_OBSERVATIONS = 12` guards against an under-determined system — the design matrix has
#: `1 + 2*harmonics` columns, so 12 gives margin over the 5 needed. It says nothing about *where*
#: those observations fall.
#:
#: Yenagoa had **52 observations** and still produced an impossible fit, because they clustered in
#: three of six two-month buckets: the rainy season is cloud-blocked, so the optical record has a
#: May-October hole and the harmonic simply invented a peak there. A fit that has never seen August
#: cannot state what August looks like, however many January scenes it has.
#:
#: 4 of 6 buckets is two thirds of the year. Deliberately not all six: southern Nigeria genuinely
#: cannot supply a cloud-free optical scene in some months (measured — Ikorodu returned zero scenes
#: under 40% cloud across a 90-day rainy season), so demanding complete coverage would leave the
#: wettest, most flood-exposed AOIs permanently on the fixed threshold.
MIN_SEASONAL_BUCKETS = 4
SEASONAL_BUCKETS = 6

#: z-score below which a pixel counts as stressed. -1.5 is roughly the 7th
#: percentile of its own history: unusual enough to act on, common enough that a
#: genuine event covers a meaningful area rather than a handful of pixels.
STRESS_Z_THRESHOLD = -1.5


@dataclass(frozen=True)
class HarmonicBaseline:
    """A fitted seasonal expectation for one index over one AOI.

    Serialisable as plain floats so it caches to MinIO/Postgres without pickling.
    `available` False means the fit was not attempted or did not converge, and the
    caller must use its documented threshold fallback.
    """

    #: Fourier coefficients, [intercept, cos1, sin1, cos2, sin2, ...].
    coefficients: list[float]
    #: Residual standard deviation — the denominator of the z-score.
    residual_std: float
    harmonics: int
    observations: int
    available: bool = True

    def expected(self, day_of_year: int | np.ndarray) -> np.ndarray:
        """Seasonal expectation for a day (or array of days) of year, clamped to the index range.

        ## Why the clamp exists

        A normalised difference is mathematically bounded in [-1, 1] — that is what "normalised"
        means. A harmonic fit has no such constraint, so on a series with gaps it will happily
        extrapolate outside the range that the quantity can physically take.

        Measured on the backfilled history: Yenagoa's baseline predicted NDVI **+1.66** at day 206
        while the AOI has only ever been observed between +0.26 and +0.66. The cause is visible in
        the day-of-year coverage — **three empty two-month buckets spanning May to October**, which
        is the rainy season when cloud blocks optical entirely, and the spurious peak lands in the
        middle of that hole. 1 of 16 AOIs was affected.

        Clamping does not repair the fit; it bounds the damage. An expectation pinned at 1.0 still
        makes every real observation look catastrophically below normal, which is why
        `_has_seasonal_coverage` refuses such a fit outright and this is the second line of defence.
        Both are needed: the coverage gate stops the bad fit being used, and the clamp stops any
        future fit from returning a number the caller could not otherwise recognise as impossible.
        """
        design = _design_matrix(np.atleast_1d(np.asarray(day_of_year, dtype="float64")),
                                self.harmonics)
        raw = design @ np.asarray(self.coefficients, dtype="float64")
        return np.clip(raw, INDEX_MIN, INDEX_MAX)

    @classmethod
    def unavailable(cls) -> HarmonicBaseline:
        return cls(coefficients=[], residual_std=0.0, harmonics=0,
                   observations=0, available=False)


def _design_matrix(days: np.ndarray, harmonics: int) -> np.ndarray:
    """Fourier design matrix over the annual cycle.

    Period is fixed at 365.25 days — the seasonal cycle is astronomical, so this is
    a known constant rather than something to fit.
    """
    omega = 2.0 * np.pi * days / 365.25
    columns = [np.ones_like(days)]
    for k in range(1, harmonics + 1):
        columns.append(np.cos(k * omega))
        columns.append(np.sin(k * omega))
    return np.column_stack(columns)


def _has_seasonal_coverage(days: np.ndarray) -> bool:
    """Whether the record spans enough of the year for a seasonal fit to be constrained.

    Buckets the observations into `SEASONAL_BUCKETS` equal parts of the year and requires at least
    `MIN_SEASONAL_BUCKETS` of them to be non-empty. An empty bucket is a stretch of the calendar the
    fit has never observed, and a harmonic will extrapolate through it without complaint.

    Counting buckets rather than, say, the span between first and last observation, because a series
    running January to December with a six-month hole in the middle has a full span and no
    information about the middle.
    """
    if days.size == 0:
        return False
    edges = np.linspace(1.0, 366.0, SEASONAL_BUCKETS + 1)
    counts, _ = np.histogram(days, bins=edges)
    return int(np.count_nonzero(counts)) >= MIN_SEASONAL_BUCKETS


def fit_harmonic_baseline(
    days_of_year: np.ndarray | list[int],
    values: np.ndarray | list[float],
    *,
    harmonics: int = DEFAULT_HARMONICS,
) -> HarmonicBaseline:
    """Least-squares harmonic fit of one index against day of year.

    Intended for an AOI-mean series (one value per date), which is what
    `assessment_history` can supply — not per-pixel, which would need a raster time
    series we do not store. An AOI-level baseline still removes the seasonal
    confound, which is the dominant error; per-pixel refinement is a later step.

    Never raises: a singular system or non-finite result yields `unavailable()`.
    """
    days = np.asarray(days_of_year, dtype="float64")
    obs = np.asarray(values, dtype="float64")

    mask = np.isfinite(days) & np.isfinite(obs)
    days, obs = days[mask], obs[mask]

    if days.size < MIN_OBSERVATIONS:
        return HarmonicBaseline.unavailable()

    # Refuse a fit that has not SEEN enough of the year.
    #
    # Observation count guards against an under-determined system; it says nothing about where those
    # observations fall. Yenagoa had 52 of them clustered in three of six two-month buckets — the
    # rainy season is cloud-blocked, so the optical record has a May-October hole — and the harmonic
    # invented a +1.66 NDVI peak inside that hole. A fit that has never seen August cannot state what
    # August looks like, however many January scenes it has.
    #
    # Declining is the honest outcome: the caller keeps the documented `NDVI < 0.35` fallback and
    # `stress_method` says so, which is strictly better than a confident z-score computed against a
    # fabricated expectation.
    if not _has_seasonal_coverage(days):
        return HarmonicBaseline.unavailable()

    # Cap harmonics so the system stays over-determined even on a short record.
    usable = max(1, min(harmonics, (days.size - 2) // 2))
    design = _design_matrix(days, usable)

    try:
        coefficients, *_ = np.linalg.lstsq(design, obs, rcond=None)
        if not np.isfinite(coefficients).all():
            return HarmonicBaseline.unavailable()
        residuals = obs - design @ coefficients
        # ddof accounts for the parameters consumed by the fit; without it the
        # residual std is biased low and every z-score comes out too extreme.
        dof = max(1, days.size - design.shape[1])
        residual_std = float(np.sqrt(np.sum(residuals**2) / dof))
    except Exception:
        return HarmonicBaseline.unavailable()

    if not np.isfinite(residual_std) or residual_std <= 1e-6:
        # No scatter to standardise against — every z-score would be enormous.
        return HarmonicBaseline.unavailable()

    return HarmonicBaseline(
        coefficients=[float(c) for c in coefficients],
        residual_std=residual_std,
        harmonics=usable,
        observations=int(days.size),
    )


def seasonal_anomaly(
    observed: float | np.ndarray, day_of_year: int, baseline: HarmonicBaseline
) -> float | np.ndarray | None:
    """z-score of an observation against its seasonal expectation.

    Negative means below normal for the time of year — the direction that indicates
    stress for NDVI and NDMI. Returns `None` when no baseline is available, which
    the caller must treat as "use the threshold fallback".
    """
    if not baseline.available or baseline.residual_std <= 0:
        return None

    expected = float(baseline.expected(day_of_year)[0])
    if isinstance(observed, np.ndarray):
        # NaN in, NaN out — a clouded pixel must not become an anomaly of 0.
        return ((observed - expected) / baseline.residual_std).astype("float32")
    return float((observed - expected) / baseline.residual_std)


def stressed_fraction_from_anomaly(
    index_array: np.ndarray,
    day_of_year: int,
    baseline: HarmonicBaseline,
    *,
    z_threshold: float = STRESS_Z_THRESHOLD,
) -> float | None:
    """Share of valid pixels whose index is anomalously low for the season.

    NaN handling matches `indices.fraction_below` exactly and for the same reason: a
    fully clouded scene must report *no measurement*, never 0% stress. 0% reads
    downstream as "healthy", which is the most dangerous silent failure available
    here — so an all-NaN input returns `None`, not `0.0`.
    """
    z = seasonal_anomaly(index_array, day_of_year, baseline)
    if z is None:
        return None

    finite = z[np.isfinite(z)]
    if finite.size == 0:
        return None
    return float(np.count_nonzero(finite < z_threshold) / finite.size)
