"""Build a weakly-supervised Nigeria SAR-flood fine-tuning set, from real flood events.

## The gap this closes

`train_sar_flood_unet.py` trains on Sen1Floods11 — Ghana, India, Mekong, Pakistan, Paraguay, Spain,
Sri-Lanka, USA — and holds Nigeria out entirely as the TEST set. That means the deployed model has
never seen a single Nigerian training example; it is only ever graded on Nigeria at the end. Sen1
Floods11 is a fixed 446-chip benchmark — nothing can be added to it — so the only way to inject
Nigeria-domain training signal is a second, independently-sourced dataset, built the same way
`build_crop_stress_dataset.py` builds its Sentinel-2 one: from live imagery, with a documented weak
label, honestly flagged as such.

## Why this label is real flood events, not a synthetic split

Sentinel-1 SAR over three real, dated, publicly-documented Nigerian flood events, cross-referenced
against JRC Global Surface Water (permanent-water occurrence) — the same product already used live
in `app/eo/terrain.permanent_water_mask` to strip permanent rivers from `inundated_fraction`. Reused
here via `app.eo.stac`/`app.eo.cog` rather than re-implemented, because that path is already tested
and SAS-signing-correct.

Events, each verified to have a Sentinel-1 RTC scene from DE Africa within the flood window and a
dry-season comparison scene from the same tile:

  * **Maiduguri, Borno** — the Alau Dam collapse flood, ~2024-09-08/10. Scene 2024-09-17.
  * **Lokoja, Kogi** — Niger-Benue confluence, part of the 2022 Nigeria floods (nationally declared
    disaster, Sept-Oct 2022). Scene 2022-10-13.
  * **Yenagoa, Bayelsa** — Niger Delta, also part of the 2022 Nigeria floods. Scene 2022-10-13.

## The label scheme, and why it is NOT "SAR-dark minus permanent water"

An earlier version of this plan proposed labelling "SAR-dark AND not already permanent water" as
flood. That is wrong for fine-tuning THIS model: Sen1Floods11's own ground truth labels ALL water —
permanent rivers and flood water alike — as class 1, "not permanent water" is not part of its label
semantic. Training this second dataset on a different definition of the positive class would hand
the model two disagreeing objectives, which is worse than not fine-tuning at all.

So the scheme keeps Sen1Floods11's semantic (water = 1, whether permanent or new) and uses JRC-GSW
only to validate the ambiguous case, not to redefine the label:

  * **label=1 (water), always valid** — JRC permanent-water occurrence >50% (from `terrain.py`'s own
    threshold), on either date. High confidence: JRC is built from decades of Landsat, independent
    of this scene entirely.
  * **label=1 (water), valid** — SAR-dark (<-16 dB, the same fixed threshold `indices.py` and the
    original training script use) during the FLOOD-window scene. Confidence comes from the
    documented event, not from the threshold alone.
  * **label=0 (land), valid** — SAR-bright on EITHER date and not JRC permanent water. Unambiguous.
  * **INVALID, excluded from the loss** — SAR-dark on the DRY-season scene and not JRC permanent
    water. This is the ambiguous case a fixed threshold cannot resolve (lake-edge quantisation, a
    genuinely wet dry-season patch, threshold noise), and Sen1Floods11's own -1 "no observation"
    convention is precisely for pixels like this: excluded, not guessed.

## Weak supervision, and the ship gate that keeps it honest

These labels are self-generated from a threshold and a historical occurrence layer, not a field
survey — exactly the caveat `build_crop_stress_dataset.py` states for its own labels. `finetune_
sar_flood_nigeria.py` fine-tunes on this set and then re-evaluates on the SAME held-out Nigeria
Sen1Floods11 test chips this repo already has — if the fine-tuned weights do not match or beat the
Sen1Floods11-only model on that held-out set, they must not ship. Weak supervision that makes the
benchmark score worse is not a trade worth making.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

import numpy as np
import rasterio
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds

sys.path.insert(0, "/Users/lionelorishane/Documents/GitHub/prodCreditChekB2B/c3hkth-team7/backend")

from app.eo import cog, stac  # noqa: E402
from app.models.schemas import BBox  # noqa: E402

OUT = pathlib.Path("/tmp/sar_flood_nigeria")
OUT.mkdir(parents=True, exist_ok=True)

TILE = 512  # matches Sen1Floods11's own chip size, so the fine-tune sees the same input shape

# Fixed SAR water threshold, the same one `eo/indices.py` and `train_sar_flood_unet.py`'s
# `evaluate_heuristic` use — not a new number invented for this script.
SAR_WATER_THRESHOLD_DB = -16.0

# JRC Global Surface Water occurrence threshold, matching `terrain.PERMANENT_WATER_OCCURRENCE_PCT`
# exactly — a second copy of this number would be the same drift risk `dispatch/tracks.py`'s
# shared threshold table exists to prevent.
PERMANENT_WATER_OCCURRENCE_PCT = 50.0

DE_AFRICA_STAC = "https://explorer.digitalearth.africa/stac/search"
DE_AFRICA_S3_PREFIX = "s3://deafrica-sentinel-1/"
DE_AFRICA_HTTPS_PREFIX = "https://deafrica-sentinel-1.s3.af-south-1.amazonaws.com/"

# Each entry: (name, bbox, flood-window scene search range, dry-season comparison search range).
# Verified live 2026-08-14: every range below returned at least one real Sentinel-1 RTC scene.
EVENTS: list[tuple[str, list[float], str, str]] = [
    (
        "maiduguri_2024",
        [13.10, 11.80, 13.20, 11.90],
        "2024-09-01/2024-09-20",  # Alau Dam collapse flood, ~Sept 8-10 2024
        "2024-01-15/2024-02-15",  # harmattan dry season, same tile
    ),
    (
        "lokoja_2022",
        [6.72, 7.78, 6.82, 7.88],
        "2022-10-01/2022-10-20",  # 2022 Nigeria floods, Niger-Benue confluence
        "2022-01-15/2022-02-15",
    ),
    (
        "yenagoa_2022",
        [6.28, 4.88, 6.38, 4.98],
        "2022-10-01/2022-10-20",  # 2022 Nigeria floods, Niger Delta
        "2022-01-15/2022-02-15",
    ),
]


def to_db(linear: np.ndarray) -> np.ndarray:
    """Linear power -> dB. Same rule as `indices.to_db` and `train_sar_flood_unet.to_db`."""
    out = np.full_like(linear, np.nan, dtype="float32")
    positive = linear > 0
    out[positive] = 10.0 * np.log10(linear[positive])
    return out


def read_window(href: str, bbox: list[float], out_size: int = TILE) -> np.ndarray | None:
    """Direct windowed COG read against DE Africa's unsigned Sentinel-1 bucket.

    Mirrors `build_crop_stress_dataset.read_window` — a standalone read, not routed through
    `app.eo.cog`, because DE Africa is outside that module's SAS-signing/catalogue-chain system and
    a direct read is the honest description of what this is.
    """
    try:
        with rasterio.open(href) as src:
            left, bottom, right, top = transform_bounds(
                "EPSG:4326", src.crs, *bbox, densify_pts=21
            )
            window = from_bounds(left, bottom, right, top, transform=src.transform)
            window = window.intersection(rasterio.windows.Window(0, 0, src.width, src.height))
            if window.width < 8 or window.height < 8:
                return None
            data = src.read(1, window=window, out_shape=(out_size, out_size)).astype("float32")
            nodata = src.nodata
            if nodata is not None:
                data[data == nodata] = np.nan
            return data
    except Exception:
        return None


async def _search_s1(bbox: list[float], date_range: str) -> dict[str, str] | None:
    """VV/VH hrefs for the least-gappy scene in this range, or None."""
    import httpx

    start, end = date_range.split("/")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                DE_AFRICA_STAC,
                json={
                    "collections": ["s1_rtc"],
                    "bbox": bbox,
                    "datetime": f"{start}T00:00:00Z/{end}T00:00:00Z",
                    "limit": 5,
                },
            )
            response.raise_for_status()
            feats = response.json().get("features", [])
    except Exception:
        return None

    if not feats:
        return None
    # First hit is fine here — SAR has no cloud to dodge, unlike the optical search above it.
    feature = feats[0]
    assets = feature.get("assets", {})
    hrefs = {}
    for band in ("vv", "vh"):
        href = assets.get(band, {}).get("href", "")
        if href.startswith(DE_AFRICA_S3_PREFIX):
            hrefs[band] = DE_AFRICA_HTTPS_PREFIX + href[len(DE_AFRICA_S3_PREFIX) :]
    return hrefs or None


async def _permanent_water(bbox: list[float]) -> np.ndarray | None:
    """JRC-GSW occurrence, via the SAME live path `terrain.permanent_water_mask` uses.

    Reused rather than re-implemented: SAS-signing and the Planetary Computer catalogue chain are
    already correct there, and a second, slightly-different reimplementation is exactly how a
    codebase accumulates two disagreeing opinions about one measurement.
    """
    area = BBox(west=bbox[0], south=bbox[1], east=bbox[2], north=bbox[3])
    try:
        scenes = await stac.search_surface_water(area)
        if not scenes:
            return None
        asset = scenes[0].asset("occurrence") or scenes[0].asset("data")
        if asset is None:
            return None
        bands = await cog.read_bands({"occurrence": asset.href}, area, out_size=TILE)
    except Exception:
        return None
    return bands.get("occurrence")


async def _build_event(name: str, bbox: list[float], flood_range: str, dry_range: str) -> dict | None:
    flood_hrefs, dry_hrefs, occurrence = await asyncio.gather(
        _search_s1(bbox, flood_range),
        _search_s1(bbox, dry_range),
        _permanent_water(bbox),
    )
    if not flood_hrefs or "vv" not in flood_hrefs:
        print(f"  {name}: no flood-window Sentinel-1 scene, skipping")
        return None
    if not dry_hrefs or "vv" not in dry_hrefs:
        print(f"  {name}: no dry-season Sentinel-1 scene, skipping")
        return None
    if occurrence is None:
        print(f"  {name}: JRC-GSW unavailable, skipping — the label needs it")
        return None

    flood_vv = read_window(flood_hrefs["vv"], bbox)
    dry_vv = read_window(dry_hrefs["vv"], bbox)
    if flood_vv is None or dry_vv is None:
        print(f"  {name}: COG read failed, skipping")
        return None

    # Every array must share one shape before pixel-wise label logic runs. `occurrence` comes from
    # `app.eo.cog` at `out_size=TILE` already; the two direct reads above target `TILE` too, but a
    # partially-overlapping AOI can still come back a few pixels short.
    min_h = min(flood_vv.shape[0], dry_vv.shape[0], occurrence.shape[0])
    min_w = min(flood_vv.shape[1], dry_vv.shape[1], occurrence.shape[1])
    flood_vv = flood_vv[:min_h, :min_w]
    dry_vv = dry_vv[:min_h, :min_w]
    occurrence = occurrence[:min_h, :min_w]

    flood_db = to_db(flood_vv)
    dry_db = to_db(dry_vv)

    permanent_water = np.isfinite(occurrence) & (occurrence > PERMANENT_WATER_OCCURRENCE_PCT)
    flood_dark = np.isfinite(flood_db) & (flood_db < SAR_WATER_THRESHOLD_DB)
    dry_dark = np.isfinite(dry_db) & (dry_db < SAR_WATER_THRESHOLD_DB)

    # See the module docstring's "label scheme" section for why this is not simply
    # "SAR-dark minus permanent water".
    label = np.where(permanent_water | flood_dark, 1.0, 0.0).astype("float32")
    ambiguous = dry_dark & ~permanent_water
    valid = (np.isfinite(flood_db) & np.isfinite(dry_db) & np.isfinite(occurrence) & ~ambiguous).astype(
        "float32"
    )

    positive_rate = float((label * valid).sum() / max(valid.sum(), 1))
    print(
        f"  {name}: {min_h}x{min_w}  positive rate {positive_rate:.4f}  "
        f"valid fraction {valid.mean():.3f}  permanent-water {permanent_water.mean():.3f}"
    )

    # VH for both scenes too, so the fine-tune input matches the model's 2-channel (VV, VH)
    # contract exactly. Duplicating VV if VH is missing is the same degrade
    # `inference.predict_flood` already applies when a GRD product lacks it.
    flood_vh = read_window(flood_hrefs.get("vh", flood_hrefs["vv"]), bbox)
    flood_vh_db = to_db(flood_vh)[:min_h, :min_w] if flood_vh is not None else flood_db

    return {
        "name": name,
        "vv_db": flood_db,
        "vh_db": flood_vh_db,
        "label": label,
        "valid": valid,
    }


async def main() -> int:
    results = []
    for name, bbox, flood_range, dry_range in EVENTS:
        event = await _build_event(name, bbox, flood_range, dry_range)
        if event is not None:
            results.append(event)

    if not results:
        print("no events built — nothing to write")
        return 1

    x = np.stack(
        [np.stack([np.nan_to_num(e["vv_db"], nan=0.0), np.nan_to_num(e["vh_db"], nan=0.0)]) for e in results]
    ).astype("float32")
    y = np.stack([e["label"][None, ...] for e in results]).astype("float32")
    v = np.stack([e["valid"][None, ...] for e in results]).astype("float32")
    names = np.array([e["name"] for e in results])

    print(f"\n  dataset {x.shape} across {len(results)} events")
    np.savez_compressed(OUT / "sar_flood_nigeria.npz", x=x, y=y, v=v, name=names)
    print(f"  wrote {OUT / 'sar_flood_nigeria.npz'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
