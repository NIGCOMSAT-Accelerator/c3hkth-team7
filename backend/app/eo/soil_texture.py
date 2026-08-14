"""USDA soil texture class from iSDAsoil, and the water-retention band it implies.

## Why this exists rather than reusing `soil.py`'s SoilGrids clay/sand

`soil.py` answers a coarser question — free/moderate/impeded drainage from SoilGrids clay/sand
alone — which is enough to scale how long a flood persists. Irrigation timing needs a different
question: the wilting-point / field-capacity / saturation bands `SoilMoisture.status` classifies
against, which were a single fixed "loam" default because "a per-texture curve needs parameters
SoilGrids does not serve" (see `soil_moisture.py`). iSDAsoil serves richer soil properties at 30 m,
Africa-only, as unsigned COGs on the AWS Open Data registry — verified reachable 2026-08-14,
`s3://isdasoil`, no signing, no account, `Accept-Ranges: bytes` confirmed on the raw asset.

## Texture CLASS, not a reconstructed pedotransfer function

iSDAsoil publishes clay/sand/silt/bulk-density fractions AND its own USDA texture-class
classification (12 classes). This module reads `texture_class` directly rather than re-deriving a
class from clay/sand ourselves, or re-deriving water-retention values from a multi-term
pedotransfer regression (e.g. Saxton & Rawls) reconstructed from memory — either would risk exactly
the "plausible-looking wrong number" failure this codebase has hit before (the Kano SMAP bug, the
linear EASE-Grid bug, the SCL-averaging bug in `cog.py`). The 12-class -> (wilting point, field
capacity, saturation) table below is the standard soil-physics reference range for each USDA class
(Rawls et al. 1982; USDA-NRCS soil-water-characteristics guidance) — still a band, exactly like the
loam default it replaces, just the RIGHT band for this plot's actual texture instead of one band
assumed for every plot in Nigeria.

Verified live over Kano (2026-08-14): `texture_class` reads codes 6 (Sandy Clay Loam) and 9 (Sandy
Loam) across a small window, consistent with directly-read clay ~21-24%, sand ~53-62%, silt
~19-23% at the same point — a genuine, internally-consistent Sahelian sandy soil, not a guess.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.config import settings
from app.eo import cog
from app.logging_config import describe, get_logger
from app.models.schemas import BBox

log = get_logger(__name__)

#: USDA texture class code -> label, from iSDAsoil's own attribute table
#: (`texture_class_attribute_table.csv`, verified live 2026-08-14).
TEXTURE_LABELS: dict[int, str] = {
    1: "Clay",
    2: "Silty Clay",
    3: "Sandy Clay",
    4: "Clay Loam",
    5: "Silty Clay Loam",
    6: "Sandy Clay Loam",
    7: "Loam",
    8: "Silt Loam",
    9: "Sandy Loam",
    10: "Silt",
    11: "Loamy Sand",
    12: "Sand",
}

#: `(wilting point, field capacity, saturation)` m3/m3, by USDA texture class code.
#:
#: Standard soil-physics reference ranges, not a per-pixel calculation — the same honesty the old
#: single "loam" default in `soil_moisture.py` already carried, extended to all 12 classes instead
#: of assuming loam everywhere. Class 7 (Loam) sits close to but not identical to the old fixed
#: default (WP 0.12 / FC 0.32) — the old constants were a rounded approximation of exactly this row.
_WATER_RETENTION: dict[int, tuple[float, float, float]] = {
    12: (0.05, 0.10, 0.43),  # Sand
    11: (0.06, 0.13, 0.44),  # Loamy Sand
    9: (0.10, 0.18, 0.45),  # Sandy Loam
    6: (0.15, 0.24, 0.42),  # Sandy Clay Loam
    7: (0.12, 0.28, 0.46),  # Loam
    8: (0.13, 0.31, 0.46),  # Silt Loam
    10: (0.09, 0.28, 0.47),  # Silt
    4: (0.20, 0.32, 0.48),  # Clay Loam
    5: (0.21, 0.34, 0.48),  # Silty Clay Loam
    3: (0.24, 0.32, 0.40),  # Sandy Clay
    2: (0.25, 0.37, 0.48),  # Silty Clay
    1: (0.27, 0.39, 0.48),  # Clay
}


@dataclass(frozen=True)
class TextureThresholds:
    """Wilting point / field capacity / saturation implied by a plot's USDA texture class.

    `available=False` means unknown, never "assume loam" — the caller (`oracle.py`) leaves
    `SoilMoisture`'s texture fields unset in that case, and `SoilMoisture.status` falls back to the
    wide loam default exactly as it did before this module existed.
    """

    wilting_point: float | None = None
    field_capacity: float | None = None
    saturation_point: float | None = None
    texture_class: str | None = None
    available: bool = False


async def texture_thresholds(bbox: BBox) -> TextureThresholds:
    """This plot's water-retention band, from iSDAsoil's USDA texture classification.

    Never raises. Degrades to an unavailable result on any failure — unreachable bucket, an
    all-nodata window, an unrecognised class code — so the caller falls back to the wide loam
    default rather than propagating a partial or invented reading.
    """
    try:
        band = await cog.read_band(
            settings.isda_texture_class_url, bbox, out_size=4, band="texture_class"
        )
    except cog.CogReadError as exc:
        log.info("iSDAsoil texture_class unavailable", extra={"error": describe(exc)})
        return TextureThresholds()

    valid = band[np.isfinite(band) & (band > 0)]
    if valid.size == 0:
        log.info("iSDAsoil texture_class window is all nodata for this AOI")
        return TextureThresholds()

    # Modal class over the read window, not the mean — texture_class is a category code, and
    # averaging codes (as with SCL, see `cog.CATEGORICAL_BANDS`) would invent a class that does
    # not exist.
    values, counts = np.unique(valid.astype("int16"), return_counts=True)
    code = int(values[np.argmax(counts)])

    retention = _WATER_RETENTION.get(code)
    if retention is None:
        log.warning("unrecognised iSDAsoil texture class code", extra={"code": code})
        return TextureThresholds()

    wilting_point, field_capacity, saturation = retention
    return TextureThresholds(
        wilting_point=wilting_point,
        field_capacity=field_capacity,
        saturation_point=saturation,
        texture_class=TEXTURE_LABELS.get(code, f"class {code}"),
        available=True,
    )
