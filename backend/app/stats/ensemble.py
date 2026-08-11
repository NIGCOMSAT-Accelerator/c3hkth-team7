"""Step 5 — ensemble exceedance probability and return periods.

**What is currently thrown away.** GEFS is an *ensemble* forecast: it is run many
times from slightly perturbed initial states, and the spread between those runs is
the forecast's own estimate of its uncertainty. `_forecast_term` takes the peak and
the total of a single series, which means the one piece of uncertainty information
the upstream model actually provides is discarded before it reaches the risk score.

**Why that matters for a warning service.** These two forecasts have the same mean
and demand different actions:

    members: 30 31 29 30 31 mm   -> "about 30 mm, high agreement"     (routine)
    members:  0  2 88 60  0 mm   -> "could be nothing, could flood"   (watch closely)

Reporting "30 mm" for the second is not merely lossy, it is misleading: it implies a
confidence the model does not have. Exceedance probability preserves the
distinction, and it is directly actionable — "9 of 21 members put more than 50 mm on
day 4" is a sentence a district officer can act on without knowing what an ensemble
is.

**Return periods** come from a GEV/Gumbel fit rather than counting, because the
events worth warning about are rarer than the record is long. Extreme-value theory
is the standard tool for exactly that extrapolation.

**Degradation.** ClimateSERV's current adapter returns a single averaged series, so
`members` will often be length 1. Every function here handles that honestly:
`available` goes False, the caller keeps the existing deterministic path, and nothing
pretends to have measured a spread that was never delivered.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import stats

#: Minimum ensemble members before a probability is meaningful.
#:
#: With 2 members the only possible probabilities are 0, 0.5 and 1 — the number
#: would be real but the precision implied by reporting it would not be.
MIN_MEMBERS = 3

#: Minimum annual maxima before a GEV fit is trustworthy. Below this the shape
#: parameter is essentially unconstrained and the return period is fiction.
MIN_EXTREMES = 15


@dataclass(frozen=True)
class ExceedanceSummary:
    """Ensemble agreement for one threshold on one day.

    `available` False means the upstream source gave us a single deterministic
    series rather than members — the caller must fall back, not treat
    `probability` as measured.
    """

    threshold_mm: float
    probability: float
    member_count: int
    available: bool
    #: Ensemble median, which is the robust central estimate. Preferred over the
    #: mean for skewed rainfall distributions: one extreme member drags a mean.
    median_mm: float = 0.0
    #: Inter-member spread as p90 - p10, in mm. The honest uncertainty width.
    spread_mm: float = 0.0
    agreement_label: str = "unknown"
    notes: list[str] = field(default_factory=list)


def exceedance_probability(
    members_mm: np.ndarray | list[float], threshold_mm: float
) -> ExceedanceSummary:
    """Fraction of ensemble members exceeding a rainfall threshold.

    This is a *count*, not a model: no fitting, no assumption about the shape of
    the distribution, nothing to train. That is the appeal — it is the cheapest
    possible way to recover uncertainty that the upstream forecast already computed
    and we were discarding.
    """
    values = np.asarray(members_mm, dtype="float64")
    values = values[np.isfinite(values) & (values >= 0)]

    if values.size == 0:
        return ExceedanceSummary(
            threshold_mm=threshold_mm,
            probability=0.0,
            member_count=0,
            available=False,
            notes=["no ensemble members supplied"],
        )

    if values.size < MIN_MEMBERS:
        # A single deterministic series is the common case with the current
        # ClimateSERV adapter. Report the value but mark it unavailable so the
        # caller keeps its own path rather than reading 0.0 or 1.0 as a probability.
        return ExceedanceSummary(
            threshold_mm=threshold_mm,
            probability=float(values[0] > threshold_mm),
            member_count=int(values.size),
            available=False,
            median_mm=float(np.median(values)),
            notes=[
                f"{values.size} member(s) only; deterministic forecast, "
                "not an ensemble"
            ],
        )

    probability = float(np.count_nonzero(values > threshold_mm) / values.size)
    p10, p90 = (float(v) for v in np.percentile(values, [10, 90]))
    spread = p90 - p10

    return ExceedanceSummary(
        threshold_mm=threshold_mm,
        probability=probability,
        member_count=int(values.size),
        available=True,
        median_mm=float(np.median(values)),
        spread_mm=spread,
        agreement_label=_agreement_label(probability),
    )


def _agreement_label(probability: float) -> str:
    """Plain words for how much the members agree.

    Deliberately coarse. A five-band label is honest about a probability estimated
    from ~20 members; a percentage to one decimal place would imply precision the
    sample size cannot support.
    """
    if probability >= 0.90:
        return "near-certain"
    if probability >= 0.66:
        return "likely"
    if probability >= 0.34:
        return "possible"
    if probability >= 0.10:
        return "unlikely"
    return "very unlikely"


def ensemble_risk_term(
    members_by_day: list[list[float]], *, ponding_mm: float
) -> tuple[float, ExceedanceSummary | None]:
    """Forward-looking risk term from ensemble spread, plus the worst day's summary.

    Combines two exceedance levels because they answer different questions:

      * `ponding_mm`        — will fields pond at all? (the common, actionable case)
      * `ponding_mm * 2.5`  — is a damaging flood plausible? (the tail that matters)

    Weighted 0.6/0.4 toward the lower threshold: a high probability of ordinary
    ponding is more often the right thing to warn about than a low probability of
    catastrophe, and over-weighting the tail is how a service starts crying wolf.

    Returns `(0.0, None)` when no day has a usable ensemble, so the caller can tell
    "no spread information" from "spread says low risk".
    """
    best_term = 0.0
    worst_day: ExceedanceSummary | None = None

    for members in members_by_day:
        low = exceedance_probability(members, ponding_mm)
        if not low.available:
            continue
        high = exceedance_probability(members, ponding_mm * 2.5)
        term = float(np.clip(0.6 * low.probability + 0.4 * high.probability, 0.0, 1.0))
        if term > best_term or worst_day is None:
            best_term, worst_day = term, low

    return best_term, worst_day


def return_period(value_mm: float, annual_maxima_mm: np.ndarray | list[float]) -> float | None:
    """Approximate return period in years, from a Gumbel fit to annual maxima.

    Gumbel (GEV type I) rather than a full three-parameter GEV: with the ~20–40
    years of record actually available, the shape parameter of a full GEV is poorly
    constrained and the fitted tail swings wildly with one extra wet year. Gumbel
    fixes shape at zero, which is the standard conservative choice for rainfall and
    far more stable on short records.

    Returns `None` with too few maxima. This is used only for advisory *wording*
    ("wettest week since roughly 2012") — never as a risk-score input, because a
    return period extrapolated past its record is exactly the kind of confident
    number the grounding rule exists to keep out of the score.
    """
    maxima = np.asarray(annual_maxima_mm, dtype="float64")
    maxima = maxima[np.isfinite(maxima) & (maxima > 0)]

    if maxima.size < MIN_EXTREMES:
        return None

    try:
        loc, scale = stats.gumbel_r.fit(maxima)
        if not np.isfinite([loc, scale]).all() or scale <= 0:
            return None
        exceedance = 1.0 - float(stats.gumbel_r.cdf(value_mm, loc=loc, scale=scale))
    except Exception:
        return None

    if exceedance <= 1e-9:
        # Beyond anything the fit can speak to. Cap rather than return a
        # thousand-year claim from a thirty-year record.
        return 500.0
    period = 1.0 / exceedance
    return float(np.clip(period, 1.0, 500.0))
