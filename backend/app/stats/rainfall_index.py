"""Step 4 — SPI and the Antecedent Precipitation Index.

**The problem this solves.** `_observed_term` and `_forecast_term` currently divide
raw millimetres by a constant (`PONDING_RAINFALL_MM`). That constant is a single
national number, and rainfall in Nigeria is not one distribution: 180 mm in a week
is an ordinary wet spell in Bayelsa and a once-in-a-decade event in Sokoto. Dividing
both by 25 gives the same risk term for two situations that warrant different
warnings.

**SPI** fixes that by asking a different question — not "how much fell?" but "how
unusual is this, *here*?" It fits the local historical distribution, then reports
where the current total sits on it as a standard normal deviate:

    SPI  0   exactly median for this place and window
    SPI +1   wetter than ~84% of history
    SPI +2   wetter than ~98% of history        <- genuinely rare
    SPI -2   drier than ~98% of history

Because the output is a normal quantile, **SPI +2 means the same thing everywhere**.
That is what makes a national service possible with one threshold instead of 36.

**Why a gamma fit and not a z-score.** Rainfall is right-skewed and zero-inflated —
many dry days, a few very wet ones. A mean/σ z-score assumes symmetry, so it
systematically understates wet extremes, which are exactly the events this service
exists to warn about. The gamma distribution is the WMO-recommended fit for
precipitation accumulations, and the zero-inflation is handled explicitly by mixing
a point mass at zero (the Thom / Guttman treatment) rather than by fudging the fit.

**API** is the second function here and answers a different question: how saturated
is the ground *right now*. It is an exponentially-weighted sum, so rain three days
ago counts less than rain yesterday, which is the physically correct weighting for
soil-moisture memory. One parameter, no training, no history required.

Both are pure functions over arrays. Neither needs a network call, a model file, or
a GPU.
"""

from __future__ import annotations

import numpy as np
from scipy import special, stats

#: Recession constant for the Antecedent Precipitation Index — the fraction of
#: yesterday's stored moisture still present today.
#:
#: 0.90 sits at the wet end of the 0.85–0.95 range in the literature, chosen because
#: the soils this service warns about are the impeded clays of the Niger and Benue
#: floodplains, which hold water longer than a continental average would suggest.
#: `SoilProfile.drainage` refines it per AOI via `api_decay_for_drainage`.
API_DECAY = 0.90

#: Minimum number of historical observations before a gamma fit is trustworthy.
#:
#: Below this, the shape parameter is dominated by sampling noise and SPI becomes
#: confidently wrong — worse than not computing it, because a spurious SPI −2.5
#: would look like a rare drought. The WMO recommends 30 years; we accept 20
#: comparable windows as the floor and return None underneath it.
MIN_HISTORY = 20


def spi(current_mm: float, history_mm: np.ndarray | list[float]) -> float | None:
    """Standardized Precipitation Index for one accumulation window.

    `history_mm` must be totals over the *same* window length and season as
    `current_mm` — comparing a 7-day total against a history of 30-day totals is
    meaningless. The caller owns that alignment.

    Returns `None` rather than a number when there is too little history, or when
    history is degenerate (all zeros — a hyper-arid cell where "unusual" has no
    meaning). Never raises, and never invents a value: an unavailable SPI must
    degrade to the raw-millimetre path, not to a fabricated quantile.
    """
    hist = np.asarray(history_mm, dtype="float64")
    hist = hist[np.isfinite(hist) & (hist >= 0)]

    if hist.size < MIN_HISTORY:
        return None

    # Zero-inflation, handled explicitly (Thom 1958 / Guttman 1999). Fitting a
    # gamma to data containing zeros is invalid — the density diverges at 0 for
    # shape < 1 — so zeros are removed, the gamma is fitted to the positive part,
    # and the resulting CDF is mixed back with the observed dry probability.
    zero_fraction = float(np.count_nonzero(hist == 0) / hist.size)
    positive = hist[hist > 0]

    if positive.size < 4 or float(np.std(positive)) <= 0.0:
        # Effectively no variability to standardise against.
        return None

    try:
        # floc=0 because precipitation has a hard physical lower bound at zero;
        # letting scipy fit a location parameter would allow negative rainfall.
        shape, loc, scale = stats.gamma.fit(positive, floc=0.0)
        if not np.isfinite([shape, scale]).all() or shape <= 0 or scale <= 0:
            return None
        gamma_cdf = float(stats.gamma.cdf(max(current_mm, 0.0), shape, loc=loc, scale=scale))
    except Exception:
        # A fit that will not converge is a missing statistic, not an error worth
        # propagating — the caller falls back to raw millimetres.
        return None

    # Mixed CDF: P(X <= x) = q + (1-q) * G(x), where q is P(dry).
    if current_mm <= 0.0:
        # Anywhere in the dry mass; use its midpoint so a dry week maps to a
        # single well-defined quantile rather than to -inf.
        cumulative = zero_fraction / 2.0
    else:
        cumulative = zero_fraction + (1.0 - zero_fraction) * gamma_cdf

    # Clamp before the inverse normal, or a cumulative of exactly 0 or 1 yields
    # -inf/+inf and poisons every downstream arithmetic operation.
    cumulative = float(np.clip(cumulative, 1e-6, 1.0 - 1e-6))

    value = float(special.ndtri(cumulative))  # inverse standard normal CDF
    # SPI beyond +/-3.5 is not physically meaningful; it means the fit is being
    # extrapolated far past the data it was built from.
    return float(np.clip(value, -3.5, 3.5))


def spi_to_severity_label(value: float) -> str:
    """The WMO drought/wetness classification for an SPI value.

    Used for `evidence` strings, so a farmer reads "exceptionally wet" rather than
    "SPI 2.1". The bands are the published WMO ones — not tuned here, because the
    whole point of SPI is comparability with everyone else's SPI.
    """
    if value >= 2.0:
        return "exceptionally wet"
    if value >= 1.5:
        return "very wet"
    if value >= 1.0:
        return "moderately wet"
    if value > -1.0:
        return "near normal"
    if value > -1.5:
        return "moderately dry"
    if value > -2.0:
        return "severely dry"
    return "extremely dry"


def api_decay_for_drainage(drainage: str) -> float:
    """Recession constant adjusted for how fast this soil actually sheds water.

    Free-draining sand loses stored moisture faster than impeded clay, so the same
    rainfall history leaves a different amount of water in the profile. This is the
    same physical reasoning as `SoilProfile.waterlogging_multiplier`, applied to the
    time axis instead of the magnitude axis.
    """
    return {"free": 0.85, "moderate": 0.90, "impeded": 0.94}.get(drainage, API_DECAY)


def antecedent_precipitation_index(
    daily_mm: np.ndarray | list[float], *, decay: float = API_DECAY
) -> float:
    """Exponentially-weighted antecedent wetness, in mm-equivalent.

    `daily_mm` is ordered **oldest first**, so the last element is yesterday. Each
    day's contribution is discounted by `decay ** age`, which is the standard API
    recursion and the physically right shape: soil moisture decays roughly
    exponentially through drainage and evapotranspiration.

    Compare with the current `outlook.antecedent_mm`, a flat 7-day sum that weights
    rain from six days ago identically to yesterday's. Two AOIs with the same total
    can have very different amounts of water still in the ground, and the flat sum
    cannot tell them apart.

    Returns 0.0 for empty or all-invalid input — an honest "no accumulated water
    measured", which is what the caller's fallback expects.
    """
    values = np.asarray(daily_mm, dtype="float64")
    values = np.where(np.isfinite(values) & (values >= 0), values, 0.0)
    if values.size == 0:
        return 0.0

    # age 0 for the most recent day, increasing backwards.
    ages = np.arange(values.size - 1, -1, -1, dtype="float64")
    weights = np.power(float(np.clip(decay, 0.5, 0.99)), ages)
    return float(np.sum(values * weights))


def ponding_pressure(api_mm: float, *, ponding_mm: float, drainage_multiplier: float = 1.0) -> float:
    """API expressed as a 0–1 pressure term, scaled by soil drainage.

    Kept here rather than in the Oracle so the whole rainfall→risk transformation is
    in one place and testable without constructing an assessment. The Oracle still
    owns the *weighting* of this term against the others — the separation the
    architecture depends on.
    """
    if ponding_mm <= 0:
        return 0.0
    return float(np.clip(api_mm * drainage_multiplier / (ponding_mm * 4.0), 0.0, 1.0))
