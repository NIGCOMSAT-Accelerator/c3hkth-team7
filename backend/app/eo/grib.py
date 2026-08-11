"""GRIB decoding — isolated here so `eo/rainfall.py` stays free of the geospatial stack.

## Why this is its own module

`eo/rainfall.py` must be importable without GDAL. That is not a style preference: it is what keeps
`tests/test_oracle.py` runnable with no geospatial stack installed, and it is the same reason
`eo/exposure.py` imports `cog`/`stac` inside its two raster functions rather than at module scope
(see CLAUDE.md). A GRIB reader at the top of `rainfall.py` would drag rasterio into the risk layer
and break that property for every consumer.

So the rainfall chain imports this **lazily, inside the one function that needs it**, and a
deployment without GDAL sees the ERA5 rung decline exactly as it does today rather than failing to
import.

## Why no new dependency was needed

The obvious answer was `cfgrib`/`eccodes`, which is a heavyweight addition to an image that already
takes minutes to build. Measured instead: **GDAL 3.6.2, already present via rasterio 1.4.3, ships the
GRIB driver**. Verified end to end on a real ERA5 response — `driver=GRIB`, 3 bands (one per
requested day), values in metres.

Two things had to be handled to make that work:

  * **GDAL cannot stream it.** `/vsicurl/` on the CDS href raises `RasterioIOError`, because the URL
    carries no `.grib` extension and the driver is never selected. The bytes are fetched with httpx
    and written to a temp file with the right suffix.
  * **The payload is tiny.** A single-cell 3-day request is **330 bytes**, so buffering it is not the
    memory concern it would be for a scene. A continental request would be, which is why the caller
    bounds the area to the AOI.
"""

from __future__ import annotations

import os
import tempfile

from app.logging_config import get_logger

log = get_logger(__name__)


def total_from_grib(blob: bytes) -> float | None:
    """Sum every band's mean, in the file's own units. None when nothing is readable.

    ## What one band is

    ERA5 returns one band per requested time step, so summing the per-band means gives the total
    over the window — which is what an antecedent figure is. Averaging the bands instead would give a
    daily mean and understate a week by a factor of seven.

    ## NaN is excluded, never counted as zero

    The same rule `eo/indices.py` enforces for optical rasters, and for the same reason: a masked or
    absent cell must leave the denominator rather than reading as a measured zero. A band with no
    finite cells contributes nothing rather than contributing 0.0.

    Returns None when no band had any finite value — "not measured", which the caller must not
    confuse with a measured dry window.
    """
    import numpy as np
    import rasterio

    path: str | None = None
    try:
        # The suffix is load-bearing: GDAL selects its driver by extension here, and without
        # `.grib` the open fails even though the magic bytes are correct.
        with tempfile.NamedTemporaryFile(suffix=".grib", delete=False) as handle:
            handle.write(blob)
            path = handle.name

        with rasterio.open(path) as src:
            if src.count == 0:
                return None

            total = 0.0
            measured = 0
            for band in range(1, src.count + 1):
                values = src.read(band).astype("float32")
                finite = values[np.isfinite(values)]
                if finite.size == 0:
                    continue
                total += float(finite.mean())
                measured += 1

            if measured == 0:
                return None
            log.debug(
                "GRIB decoded", extra={"bands": src.count, "bands_with_data": measured}
            )
            return total
    except Exception as exc:  # noqa: BLE001 — a decode failure must degrade, never raise
        log.warning("GRIB decode failed", extra={"error": f"{type(exc).__name__}: {exc}"})
        return None
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                # A leaked temp file is a tidiness problem, not a correctness one, and it must not
                # mask a successful decode.
                pass


def available() -> bool:
    """Whether GRIB decoding is possible in this process.

    Checked by the caller so the ERA5 rung declines cleanly on a deployment without GDAL rather than
    raising an ImportError mid-assessment — the same shape as `objects.available()` and
    `client.available()` elsewhere.
    """
    from importlib.util import find_spec

    return find_spec("rasterio") is not None
