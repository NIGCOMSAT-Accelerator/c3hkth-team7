"""Classical statistics for the risk layer — steps 2, 4, 5, 7 and 10.

Deliberately dependency-light: `numpy` and `scipy` only. No rasterio, no torch,
no HTTP client. That keeps the Oracle — and therefore the whole risk layer —
importable and unit-testable with no geospatial stack installed, the same reason
`eo/geometry.py` exists.

Four modules, one job each:

| Module | Answers | Replaces |
|---|---|---|
| `rainfall_index` | "how unusual is this rainfall, in units comparable across the country?" | raw mm |
| `ensemble` | "how many forecast members cross the threshold?" | the ensemble mean |
| `anomaly` | "is this pixel below where it usually is *at this time of year*?" | a fixed NDVI cut |
| `calibration` | "does confidence 0.8 mean right 80% of the time?" | an unvalidated number |

Every function here is **pure**: same input, same output, no I/O, no clock.
That is what makes `score` reproducible and what lets the tests assert real
numerical properties rather than mocking.

**The shared contract:** every function returns `None` (or a `*_available=False`
result) when it has too little data to answer honestly, and no function ever
invents a value. A caller that gets `None` must degrade to the documented
fallback, not substitute a guess — the same rule `rainfall._flat_series` follows.
"""

from __future__ import annotations

from app.stats.anomaly import (
    HarmonicBaseline,
    seasonal_anomaly,
    stressed_fraction_from_anomaly,
)
from app.stats.calibration import Calibrator, brier_score, reliability_bins
from app.stats.ensemble import ExceedanceSummary, exceedance_probability, return_period
from app.stats.rainfall_index import (
    antecedent_precipitation_index,
    spi,
    spi_to_severity_label,
)

__all__ = [
    "Calibrator",
    "ExceedanceSummary",
    "HarmonicBaseline",
    "antecedent_precipitation_index",
    "brier_score",
    "exceedance_probability",
    "reliability_bins",
    "return_period",
    "seasonal_anomaly",
    "spi",
    "spi_to_severity_label",
    "stressed_fraction_from_anomaly",
]
