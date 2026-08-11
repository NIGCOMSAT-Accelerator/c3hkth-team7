"""Windowed Cloud-Optimized GeoTIFF reads.

This is the file that makes the whole thing affordable. A Sentinel-2 scene is
~1 GB; an AOI is usually a few square kilometres of it. COGs are internally
tiled and carry an index header, so rasterio can issue HTTP range requests for
exactly the tiles covering our bounding box and never touch the rest.

rasterio is synchronous and releases the GIL during I/O, so every read here is
pushed to a worker thread via `asyncio.to_thread` to keep the event loop free.
"""

from __future__ import annotations

import asyncio
import os

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.errors import RasterioIOError
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds

from app.config import settings
from app.logging_config import get_logger
from app.models.schemas import BBox

log = get_logger(__name__)

# GDAL tuning for remote COG access. Without these every read re-scans the
# directory listing and the whole point of range requests is lost.
_GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.TIF,.tiff,.jp2",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_VERSION": "2",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": "10000000",  # 10 MB per-file read cache
    # An INT, not a string, and this is the one option where that matters.
    #
    # rasterio special-cases `GDAL_CACHEMAX` and passes it to GDAL as a number, so a string
    # raises `TypeError: an integer is required` inside `rasterio.Env(**...)` — before any
    # network call, and therefore for EVERY read. The symptom was total: `band read failed`
    # on every band of every scene, which the Analyst reported honestly as "no usable
    # satellite imagery", so the pipeline looked like it was working and finding nothing.
    #
    # Every other key here is a genuine string config option, which is why this went
    # unnoticed — the dict reads as uniform and is not.
    "GDAL_CACHEMAX": 256,
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
}

# Mirrored into the process environment for any GDAL use that does not go through
# `rasterio.Env` — a direct `rasterio.open` in a notebook, say. `str()` because the environment
# only holds strings, while `GDAL_CACHEMAX` above is deliberately an int for rasterio's sake.
for _key, _value in _GDAL_ENV.items():
    os.environ.setdefault(_key, str(_value))


class CogReadError(RuntimeError):
    """A band could not be read; the caller decides whether that is fatal."""


#: Bands whose values are CATEGORY CODES, not measurements. Read with nearest-neighbour.
#:
#: ## Why this list has to exist
#:
#: Every band went through `Resampling.bilinear`, which is right for reflectance and wrong for a
#: classification raster. Sentinel-2's SCL band holds class codes (4 = vegetation, 8 = cloud-medium,
#: 9 = cloud-high, 10 = cirrus) and `indices.apply_scl_mask` tests membership of
#: `SCL_INVALID = (0,1,3,8,9,10,11)` after an `astype("int16")` truncation. Interpolating codes
#: produces values that are not any class, and the truncation lands them on the wrong side:
#:
#:     vegetation(4) / cloud-high(9)   -> 6.5 -> 6  -> NOT in SCL_INVALID -> passes as clear
#:     vegetation(4) / cloud-medium(8) -> 6.0 -> 6  -> NOT in SCL_INVALID -> passes as clear
#:     vegetation(4) / cirrus(10)      -> 7.0 -> 7  -> NOT in SCL_INVALID -> passes as clear
#:
#: So cloud EDGES went unmasked, and the error compounds: unmasked cloud has low NDVI, which reads
#: as crop stress, while simultaneously inflating `valid_fraction` — the only term by which cloud
#: reduces confidence. The hazard and the certainty rose together. It was live: the container logged
#: `RuntimeWarning: invalid value encountered in cast` from that exact line.
#:
#: `jrc-gsw` occurrence is the same shape of data — a per-pixel percentage used as a threshold mask
#: for permanent water — and is read through this same function by `eo/terrain.permanent_water_mask`.
CATEGORICAL_BANDS: frozenset[str] = frozenset({"scl", "occurrence", "extent", "seasonality"})


def _resampling_for(band: str | None) -> Resampling:
    """Nearest for category codes, bilinear for measurements.

    Defaults to bilinear when the band is unnamed: a caller that does not say what it is reading is
    reading a measurement (`read_band` with a bare href), and bilinear is the better choice there.
    """
    if band and band.lower() in CATEGORICAL_BANDS:
        return Resampling.nearest
    return Resampling.bilinear


def _read_window_sync(
    href: str, bbox: BBox, out_size: int, band: str | None = None
) -> np.ndarray:
    """Blocking windowed read. Runs on a worker thread.

    Returns float32 with nodata as NaN so downstream index maths propagates
    invalid pixels instead of silently averaging them in as zeros.

    `band` selects the resampling method — see `CATEGORICAL_BANDS`. Getting it wrong on a
    classification raster silently corrupts the cloud mask rather than raising.
    """
    with rasterio.Env(**_GDAL_ENV):
        with rasterio.open(href) as src:
            # Our bbox is WGS84; the scene is usually UTM.
            left, bottom, right, top = transform_bounds(
                "EPSG:4326", src.crs, *bbox.as_list(), densify_pts=21
            )
            window = from_bounds(left, bottom, right, top, transform=src.transform)

            # Clip to the scene, otherwise a partially-overlapping AOI throws.
            window = window.intersection(
                rasterio.windows.Window(0, 0, src.width, src.height)
            )
            if window.width < 1 or window.height < 1:
                raise CogReadError(f"AOI does not intersect scene: {href}")

            # Decimated read: GDAL picks an overview level rather than pulling
            # full-resolution tiles we would immediately downsample anyway.
            height = min(out_size, max(1, int(window.height)))
            width = min(out_size, max(1, int(window.width)))

            data = src.read(
                1,
                window=window,
                out_shape=(height, width),
                resampling=_resampling_for(band),
                boundless=False,
            ).astype("float32")

            nodata = src.nodata
            if nodata is not None:
                data[data == nodata] = np.nan

            return data


async def read_band(
    href: str, bbox: BBox, *, out_size: int | None = None, band: str | None = None
) -> np.ndarray:
    """Read one band over an AOI. Raises `CogReadError` on failure.

    Pass `band` when the raster holds category codes rather than measurements — see
    `CATEGORICAL_BANDS`. `read_bands` does this automatically from its dict keys.
    """
    size = out_size or settings.tile_size
    try:
        return await asyncio.to_thread(_read_window_sync, href, bbox, size, band)
    except (RasterioIOError, CogReadError) as exc:
        raise CogReadError(f"read failed for {href}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — GDAL raises a wide variety
        raise CogReadError(f"unexpected read failure for {href}: {exc}") from exc


async def read_bands(
    hrefs: dict[str, str], bbox: BBox, *, out_size: int | None = None
) -> dict[str, np.ndarray]:
    """Read several bands concurrently.

    Bands that fail are omitted rather than failing the whole read — a missing
    SWIR band should not cost us an NDVI we could still compute.
    """
    names = list(hrefs)
    results = await asyncio.gather(
        # The dict key IS the band name, so categorical bands get nearest-neighbour without the
        # caller having to know which are which. That is the whole reason the name is threaded
        # through: the alternative was every call site remembering, and the one that forgot would
        # corrupt a cloud mask silently.
        *(read_band(hrefs[n], bbox, out_size=out_size, band=n) for n in names),
        return_exceptions=True,
    )

    bands: dict[str, np.ndarray] = {}
    for name, result in zip(names, results, strict=True):
        if isinstance(result, Exception):
            log.warning("band read failed", extra={"band": name, "error": str(result)})
            continue
        bands[name] = result

    if not bands:
        raise CogReadError("no bands could be read for this AOI")

    return align(bands)


def align(bands: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Crop every band to the smallest common shape.

    Sentinel-2 bands sit at 10 m and 20 m, so a windowed read of B04 and B11
    over the same bbox comes back at different pixel dimensions. Cropping to the
    intersection is sufficient here because all bands share an origin.
    """
    if len(bands) < 2:
        return bands
    min_rows = min(a.shape[0] for a in bands.values())
    min_cols = min(a.shape[1] for a in bands.values())
    return {k: v[:min_rows, :min_cols] for k, v in bands.items()}
