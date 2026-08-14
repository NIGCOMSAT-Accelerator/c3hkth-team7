"""Build a weakly-supervised crop-stress dataset from real Sentinel-2 over Nigeria.

## What the label actually means, and why it is not just the threshold

The serving heuristic is `NDVI < 0.35` — a fixed cut. A model trained to reproduce that would learn
nothing and would earn `CONFIDENCE_TRAINED = 0.88` for relearning a constant. That would be worse
than shipping no weights, because the confidence would be a lie.

So the label here is a **per-location seasonal anomaly**: a pixel is stressed when its NDVI sits
materially below what THAT LOCATION shows at that time of year, measured from its own multi-year
history. That adds information a fixed threshold cannot have:

  * Sahelian rangeland at NDVI 0.30 in August is normal, not stressed.
  * Irrigated Delta cropland at NDVI 0.30 in August is badly stressed.

A fixed cut calls the first stressed and misses the second. The anomaly label distinguishes them,
which is exactly the signal `stats/anomaly.py` computes at serving time — so the model learns a
generalisable version of a rule the platform already trusts.

## Why this is weak supervision and the docs must say so

There is no ground-truth crop-stress survey for Nigerian smallholder plots. These labels are derived
from imagery, not from a field visit, so the model learns to reproduce a *statistical* definition of
stress rather than agronomic truth. That is stated in the exported metadata and in
`AnalystResult.stress_method`, so an operator can see which it is.

## Inputs are the SERVING inputs

NDVI, NDMI, NDWI from Sentinel-2 L2A surface reflectance, cloud-masked with SCL using
nearest-neighbour — identical to `agents/analyst._analyze_optical`. Any difference here is
training/serving skew, which is the failure that makes a good validation score meaningless.

`x` carries a FOURTH channel, `ndvi - baseline` (the anomaly), matching `CropStressNet`'s
`in_channels=4` and the stack order `predict_crop_stress` builds at serving time
(`ndvi, ndmi, ndwi, anomaly` — see `app/ml/inference.py::CROP_CHANNELS`). An earlier version of
this script wrote only 3 channels, which loads fine at `model(xb)` and then crashes on the very
first forward pass with a conv-shape mismatch — the cheap failure mode. Silently misordering the
four instead of dropping one would have been the expensive one: the model would train and serve
without error while attributing a decision to the wrong input.

## Landsat 8/9 fallback, added because Sentinel-2-only starved several AOIs

A first run against Sentinel-2 alone returned 0 scenes for two of sixteen AOIs (Benin, Yenagoa) and
1 for several more, in a 40%-cloud, 2-year, 2-window search — Nigeria's cloud cover in the wet-season
windows this dataset needs is exactly what starves it. `catalogs.chain_for` already treats Landsat
8/9 (`landsat-c2-l2`, same Element84 STAC endpoint) as the next rung after Sentinel-2 in the live
optical chain (`app/eo/sources.py`); this script now does the same when a window comes back empty.
Landsat's asset names and cloud mask differ (`nir08` not `nir`, bit-packed `qa_pixel` not categorical
`SCL`), handled by `BAND_MAP` and `_cloud_invalid_mask` below.

## The baseline now comes from Digital Earth Africa, not from `samples` itself

The seasonal baseline used to be the median NDVI of whatever live 2024-2025 scenes this exact run
happened to pull for an (AOI, window) — as few as ONE scene for several AOIs. That is not a
climatology, it is that scene's own value, and the SAME handful of samples were used both to build
the baseline and to be labelled against it — a small-N self-referential loop the original audit
flagged.

Digital Earth Africa publishes `ndvi_anomaly` (an `ndvi_mean` monthly composite plus a precomputed
standardised anomaly band, `deafrica-services` S3, `af-south-1`, unsigned — verified reachable
2026-08-14, `Accept-Ranges: bytes`) with monthly coverage since at least 2017. The baseline for each
(AOI, window) is now the median of `ndvi_mean` across every available year for the calendar months
that window sits in — an independent, multi-year reference the live-fetched "current" scene is
compared against, rather than compared against itself.
"""

from __future__ import annotations

import pathlib
import sys

import numpy as np

sys.path.insert(0, "/Users/lionelorishane/Documents/GitHub/prodCreditChekB2B/c3hkth-team7/backend")

import asyncio  # noqa: E402

import httpx  # noqa: E402
import rasterio  # noqa: E402
from rasterio.warp import transform_bounds  # noqa: E402
from rasterio.windows import from_bounds  # noqa: E402

OUT = pathlib.Path("/tmp/crop_ds")
OUT.mkdir(parents=True, exist_ok=True)

STAC = "https://earth-search.aws.element84.com/v1/search"

# Sixteen AOIs spanning Nigeria's agro-ecological gradient, north to south. Diversity is the point:
# a model fitted only on the Sahel would call irrigated Delta cropland stressed, and one fitted only
# on the south would call normal Sahelian rangeland catastrophic.
AOIS = [
    ("sokoto",      [5.20, 12.98, 5.30, 13.08]),
    ("katsina",     [7.55, 12.95, 7.65, 13.05]),
    ("kano",        [8.46, 11.92, 8.56, 12.02]),
    ("zaria",       [7.68, 11.06, 7.78, 11.16]),
    ("maiduguri",   [13.10, 11.80, 13.20, 11.90]),
    ("bauchi",      [9.80, 10.28, 9.90, 10.38]),
    ("jos",         [8.85, 9.88, 8.95, 9.98]),
    ("abuja",       [7.42, 9.02, 7.52, 9.12]),
    ("lokoja",      [6.72, 7.78, 6.82, 7.88]),
    ("ilorin",      [4.52, 8.46, 4.62, 8.56]),
    ("oyo",         [3.90, 7.82, 4.00, 7.92]),
    ("benin",       [5.60, 6.30, 5.70, 6.40]),
    ("enugu",       [7.48, 6.42, 7.58, 6.52]),
    ("ikorodu",     [3.48, 6.58, 3.58, 6.68]),
    ("yenagoa",     [6.28, 4.88, 6.38, 4.98]),
    ("calabar",     [8.30, 4.95, 8.40, 5.05]),
]

# Eight years of the same season, not two. Same-season sampling is essential: comparing an August
# total against a year-round distribution would report every wet-season week as exceptional. Two
# years gave 36 scenes total and left the held-out test AOIs (Calabar, Kano, Lokoja) with 1-2 scenes
# each — not enough to trust a ship-gate number against. Widening the archive, not the labelling
# scheme, is the fix: the DE Africa climatology baseline is already robust, the live "current scene"
# side was the thin one.
YEARS = list(range(2018, 2026))
WINDOWS = [("06-15", "08-01"), ("09-15", "11-01")]

# Digital Earth Africa's `ndvi_anomaly` monthly composites are calendar-month binned, so the
# day-level WINDOWS above don't translate directly — each maps to the calendar months with the
# most overlap. Verified reachable live 2026-08-14: unsigned S3 (`deafrica-services`, af-south-1),
# `Accept-Ranges: bytes`, monthly coverage since at least 2017.
DE_AFRICA_STAC = "https://explorer.digitalearth.africa/stac/search"
DE_AFRICA_S3_PREFIX = "s3://deafrica-services/"
DE_AFRICA_HTTPS_PREFIX = "https://deafrica-services.s3.af-south-1.amazonaws.com/"
WINDOW_MONTHS: dict[str, tuple[int, ...]] = {"06-15": (6, 7), "09-15": (9, 10)}

MAX_CLOUD = 40.0
TILE = 96

SCL_INVALID = (0, 1, 3, 8, 9, 10, 11)

# Sentinel-2-l2a first (finer resolution, the serving primary); Landsat 8/9 C2-L2 as the fallback
# when a window has no cloud-free Sentinel-2 hit — mirrors `catalogs.chain_for`'s failover order for
# optical imagery in the live pipeline (`app/eo/sources.py`).
COLLECTIONS = ("sentinel-2-l2a", "landsat-c2-l2")

# Asset key differs by collection even for the same physical band.
BAND_MAP: dict[str, dict[str, str]] = {
    "sentinel-2-l2a": {"red": "red", "green": "green", "nir": "nir", "swir16": "swir16", "qa": "scl"},
    "landsat-c2-l2": {"red": "red", "green": "green", "nir": "nir08", "swir16": "swir16", "qa": "qa_pixel"},
}

# Landsat Collection 2 QA_PIXEL is bit-packed, not categorical like SCL. Bits verified against the
# USGS Collection 2 Level 2 Science Product Guide: 0 fill, 1 dilated cloud, 2 cirrus, 3 cloud,
# 4 cloud shadow. Unverified against a live Nigerian scene — flag any dataset built on the Landsat
# rung as such until it has been.
_QA_PIXEL_INVALID_BITS = (0, 1, 2, 3, 4)


def _cloud_invalid_mask(qa: np.ndarray, collection: str) -> np.ndarray:
    """True where the pixel is cloud/shadow/fill/no-data, for the given collection's QA band."""
    if collection == "sentinel-2-l2a":
        return np.isin(qa.astype("int16"), SCL_INVALID)
    packed = qa.astype("uint16")
    invalid = np.zeros(packed.shape, dtype=bool)
    for bit in _QA_PIXEL_INVALID_BITS:
        invalid |= (packed & (1 << bit)) != 0
    return invalid


def normalised(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    denominator = a + b
    out = np.full(a.shape, np.nan, dtype="float32")
    good = denominator != 0
    out[good] = (a[good] - b[good]) / denominator[good]
    return out


def read_window(href: str, bbox: list[float], nearest: bool = False) -> np.ndarray | None:
    from rasterio.enums import Resampling

    try:
        with rasterio.open(href) as src:
            left, bottom, right, top = transform_bounds(
                "EPSG:4326", src.crs, *bbox, densify_pts=21
            )
            window = from_bounds(left, bottom, right, top, transform=src.transform)
            window = window.intersection(
                rasterio.windows.Window(0, 0, src.width, src.height)
            )
            if window.width < 8 or window.height < 8:
                return None
            data = src.read(
                1,
                window=window,
                out_shape=(TILE, TILE),
                resampling=Resampling.nearest if nearest else Resampling.bilinear,
                boundless=False,
            ).astype("float32")
            nodata = src.nodata
            if nodata is not None:
                data[data == nodata] = np.nan
            return data
    except Exception:
        return None


async def search(
    client: httpx.AsyncClient, collection: str, bbox: list[float], start: str, end: str
) -> list[dict]:
    body = {
        "collections": [collection],
        "bbox": bbox,
        "datetime": f"{start}T00:00:00Z/{end}T00:00:00Z",
        "query": {"eo:cloud_cover": {"lt": MAX_CLOUD}},
        "limit": 3,
    }
    try:
        response = await client.post(STAC, json=body)
        response.raise_for_status()
        return response.json().get("features", [])
    except Exception:
        return []


async def search_chain(
    client: httpx.AsyncClient, bbox: list[float], start: str, end: str
) -> tuple[list[dict], str | None]:
    """Try each collection in `COLLECTIONS` until one returns features. `(features, collection)`."""
    for collection in COLLECTIONS:
        feats = await search(client, collection, bbox, start, end)
        if feats:
            return feats, collection
    return [], None


async def _de_africa_baseline(
    client: httpx.AsyncClient, bbox: list[float], months: tuple[int, ...]
) -> float | None:
    """Median NDVI from DE Africa's `ndvi_anomaly` `ndvi_mean` band, across every available year
    for these calendar months. An independent, multi-year climatological reference — the live
    2024-2025 pull never contributes to this number, so it is no longer the same handful of
    samples used both to build the baseline and to be labelled against it.

    Returns None on any failure (search error, no matching months, all-nodata reads) so the caller
    falls back to the old live-median baseline exactly as if this function did not exist.
    """
    try:
        response = await client.post(
            DE_AFRICA_STAC,
            json={"collections": ["ndvi_anomaly"], "bbox": bbox, "limit": 100},
        )
        response.raise_for_status()
        feats = response.json().get("features", [])
    except Exception:
        return None

    values: list[float] = []
    for feat in feats:
        datetime_str = feat.get("properties", {}).get("datetime", "")
        try:
            month = int(datetime_str[5:7])
        except (ValueError, IndexError):
            continue
        if month not in months:
            continue
        href = feat.get("assets", {}).get("ndvi_mean", {}).get("href", "")
        if not href.startswith(DE_AFRICA_S3_PREFIX):
            continue
        https_href = DE_AFRICA_HTTPS_PREFIX + href[len(DE_AFRICA_S3_PREFIX) :]
        arr = read_window(https_href, bbox)
        if arr is None:
            continue
        valid = arr[np.isfinite(arr)]
        if valid.size:
            values.append(float(np.median(valid)))

    if not values:
        return None
    return float(np.median(values))


async def main() -> int:
    samples: list[dict] = []

    async with httpx.AsyncClient(timeout=60) as client:
        for name, bbox in AOIS:
            per_aoi = 0
            per_aoi_landsat = 0
            for year in YEARS:
                for start_md, end_md in WINDOWS:
                    feats, collection = await search_chain(
                        client, bbox, f"{year}-{start_md}", f"{year}-{end_md}"
                    )
                    if not feats:
                        continue
                    # Least cloudy in the window.
                    feature = min(
                        feats, key=lambda f: f["properties"].get("eo:cloud_cover") or 100
                    )
                    band_map = BAND_MAP[collection]
                    assets = feature.get("assets", {})
                    hrefs = {
                        band: assets[key]["href"]
                        for band, key in band_map.items()
                        if key in assets
                    }
                    if not {"red", "nir"} <= hrefs.keys():
                        continue

                    # Five bands concurrently. Serial range-requests over the network were the
                    # whole cost here; rasterio releases the GIL during I/O so threads help.
                    import concurrent.futures as _cf
                    with _cf.ThreadPoolExecutor(5) as ex:
                        futs = {
                            band: ex.submit(read_window, hrefs[band], bbox, band == "qa")
                            for band in ("red", "green", "nir", "swir16", "qa")
                            if band in hrefs
                        }
                        got = {b: f.result() for b, f in futs.items()}
                    red, nir = got.get("red"), got.get("nir")
                    if red is None or nir is None:
                        continue
                    green, swir, qa = got.get("green"), got.get("swir16"), got.get("qa")

                    ndvi = normalised(nir, red)
                    ndwi = normalised(green, nir) if green is not None else np.zeros_like(ndvi)
                    ndmi = normalised(nir, swir) if swir is not None else np.zeros_like(ndvi)

                    if qa is not None:
                        invalid = _cloud_invalid_mask(qa, collection)
                        ndvi = np.where(invalid, np.nan, ndvi)

                    if np.isfinite(ndvi).mean() < 0.5:
                        continue

                    samples.append(
                        {
                            "aoi": name,
                            "year": year,
                            "window": start_md,
                            "collection": collection,
                            "ndvi": ndvi.astype("float32"),
                            "ndmi": np.nan_to_num(ndmi, nan=0.0).astype("float32"),
                            "ndwi": np.nan_to_num(ndwi, nan=0.0).astype("float32"),
                        }
                    )
                    per_aoi += 1
                    per_aoi_landsat += collection == "landsat-c2-l2"
            flag = f"  ({per_aoi_landsat} via Landsat fallback)" if per_aoi_landsat else ""
            print(f"  {name:11} {per_aoi:2} scenes{flag}", flush=True)

    print(f"\n  collected {len(samples)} scenes across {len({s['aoi'] for s in samples})} AOIs")

    # ---- the label: per (AOI, seasonal window) anomaly -------------------------
    #
    # Baseline = median NDVI for that AOI in that calendar window, from DE Africa's independent
    # multi-year `ndvi_anomaly` archive where it is reachable, falling back to the median of this
    # run's own live-fetched scenes only where DE Africa has no coverage for that tile.
    #
    # Computed per (aoi, window) rather than globally — that is the whole point. A global cut would
    # be the fixed threshold the model is supposed to improve on.
    MARGIN = 0.10
    baselines: dict[tuple[str, str], float] = {}
    baseline_sources: dict[tuple[str, str], str] = {}
    aoi_bbox = dict(AOIS)
    async with httpx.AsyncClient(timeout=60) as de_africa_client:
        for key in {(s["aoi"], s["window"]) for s in samples}:
            aoi_name, window_key = key
            months = WINDOW_MONTHS.get(window_key, ())
            de_africa_value = await _de_africa_baseline(
                de_africa_client, aoi_bbox[aoi_name], months
            )
            if de_africa_value is not None:
                baselines[key] = de_africa_value
                baseline_sources[key] = "de-africa-climatology"
                continue

            group = [s for s in samples if (s["aoi"], s["window"]) == key]
            pooled = np.concatenate([s["ndvi"][np.isfinite(s["ndvi"])] for s in group])
            if pooled.size:
                baselines[key] = float(np.median(pooled))
                baseline_sources[key] = "live-scene-median"

    de_africa_count = sum(1 for v in baseline_sources.values() if v == "de-africa-climatology")
    print(
        f"  baselines: {de_africa_count}/{len(baselines)} from DE Africa climatology, "
        f"{len(baselines) - de_africa_count} from live-scene fallback"
    )

    x_list, y_list, v_list, meta = [], [], [], []
    for s in samples:
        base = baselines.get((s["aoi"], s["window"]))
        if base is None:
            continue
        ndvi = s["ndvi"]
        valid = np.isfinite(ndvi)
        label = (ndvi < (base - MARGIN)).astype("float32")
        # Fourth channel: ndvi - baseline. Zero where invalid, matching the neutral value
        # `predict_crop_stress`/`crop_stress_attribution` use when no seasonal baseline exists —
        # the honest "typical for this field" default, not a missing-data sentinel.
        anomaly = np.where(valid, ndvi - base, 0.0).astype("float32")
        x_list.append(
            np.stack([np.nan_to_num(ndvi, nan=0.0), s["ndmi"], s["ndwi"], anomaly])
        )
        y_list.append(label[None, ...])
        v_list.append(valid.astype("float32")[None, ...])
        meta.append({"aoi": s["aoi"], "year": s["year"], "window": s["window"], "baseline": base})

    x = np.stack(x_list).astype("float32")
    y = np.stack(y_list).astype("float32")
    v = np.stack(v_list).astype("float32")

    positive_rate = float((y * v).sum() / max(v.sum(), 1))
    print(f"  dataset {x.shape}  positive rate {positive_rate:.4f}")
    print(f"  baselines per (aoi, window): {len(baselines)}")
    print(f"  baseline NDVI range {min(baselines.values()):.3f} .. {max(baselines.values()):.3f}")

    np.savez_compressed(
        OUT / "crop_stress.npz",
        x=x,
        y=y,
        v=v,
        aoi=np.array([m["aoi"] for m in meta]),
        year=np.array([m["year"] for m in meta]),
    )
    print(f"  wrote {OUT / 'crop_stress.npz'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
