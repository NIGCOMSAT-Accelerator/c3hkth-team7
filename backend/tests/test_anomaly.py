"""Seasonal baseline fitting — physical bounds and coverage."""

from __future__ import annotations

import numpy as np

# --------------------------------------------------------------------------- #
# Physical bounds and seasonal coverage
#
# A harmonic fit is unconstrained; a normalised difference is not. On a gappy record the two
# disagree, and the fit wins unless something stops it.
# --------------------------------------------------------------------------- #


def test_expected_never_leaves_the_index_range():
    """**NDVI is bounded [-1, 1] by arithmetic. A harmonic fit does not know that.**

    Measured on the backfilled history: Yenagoa's baseline predicted NDVI **+1.66** at day 206 while
    the AOI has only ever been observed between +0.26 and +0.66. Three empty two-month buckets
    spanning May-October — the rainy season, when cloud blocks optical — and the spurious peak landed
    inside that hole.
    """
    from app.stats.anomaly import INDEX_MAX, INDEX_MIN, HarmonicBaseline

    # Coefficients chosen to make the raw fit overshoot badly.
    baseline = HarmonicBaseline(
        coefficients=[0.5, 3.0, 3.0, 2.0, 2.0],
        residual_std=0.05,
        harmonics=2,
        observations=52,
        available=True,
    )
    expected = np.array([float(baseline.expected(d)[0]) for d in range(1, 366, 3)])
    assert expected.max() <= INDEX_MAX
    assert expected.min() >= INDEX_MIN


def test_a_fit_with_seasonal_gaps_is_refused_not_clamped():
    """Clamping bounds the damage; refusing avoids it.

    An expectation pinned at 1.0 still makes every real observation look catastrophically below
    normal, so the clamp alone is not enough. A fit that has never observed August cannot state what
    August looks like, however many January scenes it holds.
    """
    from app.stats.anomaly import SEASONAL_BUCKETS, fit_harmonic_baseline

    rng = np.random.default_rng(0)
    # Yenagoa's real distribution: 52 observations in exactly 3 of the 6 two-month buckets, with
    # May-October empty. Bucket edges are computed rather than hardcoded so the fixture cannot
    # silently straddle a boundary — which is how the first version of this test passed.
    edges = np.linspace(1.0, 366.0, SEASONAL_BUCKETS + 1)
    days = np.concatenate(
        [
            # `ceil` on the lower edge: the edges are fractional (365/6 is not an integer), and
            # truncating put samples in the neighbouring bucket — which is how the first version of
            # this fixture spanned 4 buckets while claiming 3.
            rng.integers(int(np.ceil(edges[0])), int(edges[1]), 28),   # Jan-Feb
            rng.integers(int(np.ceil(edges[1])), int(edges[2]), 10),   # Mar-Apr
            rng.integers(int(np.ceil(edges[5])), int(edges[6]), 14),   # Nov-Dec
        ]
    )
    counts, _ = np.histogram(days, bins=edges)
    assert int(np.count_nonzero(counts)) == 3, f"fixture must span 3 buckets, spans {counts}"

    values = rng.uniform(0.25, 0.65, days.size)

    baseline = fit_harmonic_baseline(days, values)
    assert not baseline.available, (
        "a series with three empty two-month buckets must be refused — it invented a +1.66 peak"
    )


def test_a_well_covered_fit_is_still_accepted():
    """The control. The gate must not reject the fifteen AOIs that are genuinely usable."""
    from app.stats.anomaly import fit_harmonic_baseline

    rng = np.random.default_rng(1)
    days = rng.integers(1, 366, 120)
    # A real seasonal signal, so the fit has something to find.
    values = 0.3 + 0.15 * np.sin(2 * np.pi * days / 365.25) + rng.normal(0, 0.02, days.size)

    baseline = fit_harmonic_baseline(days, values)
    assert baseline.available
    expected = np.array([float(baseline.expected(d)[0]) for d in range(1, 366, 5)])
    assert -1.0 <= expected.min() and expected.max() <= 1.0


def test_the_coverage_threshold_leaves_cloudy_regions_usable():
    """Southern Nigeria genuinely cannot supply cloud-free optical in some months.

    Measured: Ikorodu returned ZERO Sentinel-2 scenes under 40% cloud across a 90-day rainy season.
    Demanding all six buckets would leave the wettest, most flood-exposed AOIs permanently on the
    fixed threshold — so the bar is two thirds of the year, not all of it.
    """
    from app.stats.anomaly import MIN_SEASONAL_BUCKETS, SEASONAL_BUCKETS

    assert MIN_SEASONAL_BUCKETS < SEASONAL_BUCKETS, "requiring full coverage excludes the delta"
    assert MIN_SEASONAL_BUCKETS >= SEASONAL_BUCKETS // 2, "less than half the year is not a season"
