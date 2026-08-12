"""Model loading and inference.

Weights are loaded once per process and cached. When a weights file is missing
we log it loudly and fall back to the physical threshold in `eo/indices.py`;
`confidence` on the result tells the risk model which path ran so an untrained
deployment can't escalate an alert to EMERGENCY on heuristics alone.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import torch

from app.config import settings
from app.eo import indices
from app.logging_config import get_logger
from app.ml.models import CropStressNet, SARFloodUNet

log = get_logger(__name__)

# Confidence assigned to each inference path. The heuristic is defensible
# science but it is not a trained model, and the gap should be visible.
CONFIDENCE_TRAINED = 0.88
CONFIDENCE_HEURISTIC = 0.55

_models: dict[str, torch.nn.Module | None] = {}

#: Resolved once — see `_device`. None until first use.
_resolved_device: torch.device | None = None


def _device() -> torch.device:
    """The device to run inference on. **Always returns something usable.**

    ## Why every accelerator is checked, not just CUDA

    This handled `cuda` and passed anything else straight to `torch.device()`. So
    `TORCH_DEVICE=mps` — the natural setting on the Apple Silicon machine where the models are
    TRAINED — reached a Linux VPS unchanged and raised on the first inference. The weights would
    load, the container would report healthy, and every assessment would fall back to the threshold
    heuristic while logging a load failure. A training-machine setting must not be able to break the
    deployment target.

    `mps` is Apple-only and `cuda` needs a GPU with a matching driver, so both are checked the same
    way and both degrade to CPU with a warning rather than raising. CPU inference is genuinely
    adequate here: the SAR U-Net is 483K parameters over a 512px tile, which is milliseconds.

    Resolved once and cached, because this is called per model load and per forward pass, and the
    availability checks are not free.
    """
    global _resolved_device
    if _resolved_device is not None:
        return _resolved_device

    requested = (settings.torch_device or "cpu").strip().lower()

    if requested.startswith("cuda") and not torch.cuda.is_available():
        log.warning(
            "CUDA requested but unavailable on this host; using CPU",
            extra={"requested": requested},
        )
        requested = "cpu"
    elif requested.startswith("mps") and not (
        # `is_built()` is False on a non-Apple build; `is_available()` is False on Apple hardware
        # without Metal support. Both must hold, and `getattr` guards a torch too old to have the
        # backend at all.
        getattr(torch.backends, "mps", None)
        and torch.backends.mps.is_built()
        and torch.backends.mps.is_available()
    ):
        log.warning(
            "MPS requested but unavailable on this host (expected on Linux); using CPU. "
            "This is the correct setting to leave in place — weights trained on MPS run fine on CPU.",
            extra={"requested": requested},
        )
        requested = "cpu"

    try:
        _resolved_device = torch.device(requested)
        # Construct a tensor to prove the device really works. A device that constructs but cannot
        # allocate would otherwise fail later, inside a forward pass, where the fallback is a
        # confidence downgrade rather than a clean degrade to CPU.
        torch.zeros(1, device=_resolved_device)
    except Exception as exc:  # noqa: BLE001 — an unusable device must degrade, never raise
        log.warning(
            "torch device unusable; using CPU",
            extra={"requested": requested, "error": f"{type(exc).__name__}: {exc}"},
        )
        _resolved_device = torch.device("cpu")

    log.info("torch device resolved", extra={"device": str(_resolved_device)})
    if _resolved_device.type == "cpu":
        _quiet_nnpack()
    return _resolved_device


def _quiet_nnpack() -> None:
    """Stop NNPACK from re-announcing that it cannot run on this CPU.

    ## What the warning is

    `[W812 12:31:28.310] NNPACK.cpp:61 Could not initialize NNPACK! Reason: Unsupported hardware.`
    on `worker-analyst-1`. NNPACK is one of several convolution backends torch may pick on CPU;
    it needs AVX2 (x86) or specific NEON support, and a VPS whose CPU flags are masked by the
    hypervisor does not qualify. torch then falls back to its own kernels — which is what makes
    this **benign**, and why it is only a `W`.

    Verified in the built image rather than assumed: the same convolution with NNPACK attempted
    and with `set_flags(False)` returns **bitwise identical** output (`torch.equal` True, max abs
    diff 0.0). So this suppresses a message, never a computation. The flood and crop-stress
    predictions, and therefore `CONFIDENCE_TRAINED`, are unchanged.

    ## Why it is worth silencing at all

    It is emitted from C++ on every worker start, not through `logging`, so it lands in the
    runtime console as an unstructured line among structured JSON — indistinguishable at a glance
    from `worldpop lookup failed`, which is a real degradation. An operator reading these logs to
    decide whether a scan is trustworthy should not have to learn which warnings to ignore.

    Guarded three ways because this is cosmetic and must never be the thing that breaks
    inference: the namespace is absent on torch < 2.5 (2.2 on the training Mac has no
    `backends.nnpack`), `set_flags` is not part of the documented API surface, and CPU-only —
    on CUDA or MPS the backend is irrelevant and touching global flags would be gratuitous.
    """
    nnpack = getattr(torch.backends, "nnpack", None)
    setter = getattr(nnpack, "set_flags", None)
    if setter is None:
        return
    try:
        setter(False)
    except Exception:  # noqa: BLE001 — a cosmetic tweak must never break inference
        log.debug("could not disable the NNPACK backend; its warning will persist")


def _load(name: str, factory, weights_path: str) -> torch.nn.Module | None:
    """Load once, cache forever. Returns None when weights are absent."""
    if name in _models:
        return _models[name]

    path = Path(weights_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / weights_path

    if not path.exists():
        log.warning(
            "model weights missing; falling back to threshold heuristic",
            extra={"model": name, "path": str(path)},
        )
        _models[name] = None
        return None

    try:
        model = factory()
        state = torch.load(path, map_location=_device(), weights_only=True)
        model.load_state_dict(state)
        model.eval().to(_device())
        _models[name] = model
        log.info("model loaded", extra={"model": name, "path": str(path)})
        return model
    except Exception:
        log.exception("model load failed; using heuristic", extra={"model": name})
        _models[name] = None
        return None


#: FIXED standardisation constants for Sentinel-1 dB, shared by training and serving.
#:
#: ## Why these are constants and not per-scene statistics
#:
#: This function used to compute the mean and standard deviation **of each scene** and standardise
#: against those. That is a reasonable-looking choice and it is catastrophically wrong for a
#: segmentation model, because it destroys the absolute radiometry the decision depends on: water is
#: dark *in absolute dB terms*, and per-scene recentring maps a bone-dry scene and a fully flooded
#: scene onto the same distribution.
#:
#: Measured, and this is what caught it. Over Kano — Sahel, VV median **-4.3 dB**, heuristic water
#: fraction **0.000** — the trained model reported **52.9%** standing water and, at
#: `CONFIDENCE_TRAINED = 0.88`, escalated to the first WARNING the platform had ever issued. Probing
#: the weights directly with fixed inputs showed the model itself was fine and monotonic:
#:
#:     VV  -4 dB -> P(water) 0.022      VV -20 dB -> P(water) 0.778
#:     VV  -8 dB -> P(water) 0.034      VV -25 dB -> P(water) 0.913
#:
#: The model was right; the input it received was a lie. Per-scene standardisation had shifted dry
#: land onto water's part of the distribution.
#:
#: So the constants live here, are used by BOTH the training notebooks and this serving path, and a
#: test asserts they match. Any divergence is training/serving skew — the failure that makes a good
#: validation score meaningless.
SAR_DB_MEAN = (-12.0, -19.0)   # VV, VH
SAR_DB_STD = (5.0, 5.0)


def _standardize(array: np.ndarray, channel: int = 0) -> np.ndarray:
    """Standardise Sentinel-1 dB with FIXED constants. NaN filled post-standardisation with 0.

    `channel` selects VV (0) or VH (1) — they have different means, and using VV's constants for VH
    would shift it by 7 dB.

    NaN becomes 0.0, which is the standardised MEAN, so a no-data pixel reads as "typical" rather
    than as an extreme. It is excluded from every reported fraction anyway (`mean_fraction` ignores
    NaN), but it still passes through the convolution and must not drag its neighbours.
    """
    mean = SAR_DB_MEAN[channel]
    std = SAR_DB_STD[channel] or 1.0
    out = (array - mean) / std
    return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0).astype("float32")


def _infer_sync(model: torch.nn.Module, stack: np.ndarray) -> np.ndarray:
    """Run one forward pass. `stack` is (C, H, W)."""
    tensor = torch.from_numpy(stack).unsqueeze(0).to(_device())
    with torch.inference_mode():
        logits = model(tensor)
        probs = torch.sigmoid(logits)
    return probs.squeeze().cpu().numpy().astype("float32")


# --------------------------------------------------------------------------- #
# Flood
# --------------------------------------------------------------------------- #


async def predict_flood(vv_db: np.ndarray, vh_db: np.ndarray | None) -> tuple[np.ndarray, float]:
    """Water probability from Sentinel-1. Returns `(probability, confidence)`."""
    model = _load("sar_flood", SARFloodUNet, settings.sar_flood_weights)

    if model is None:
        return indices.sar_water_mask(vv_db), CONFIDENCE_HEURISTIC

    # VH is frequently absent on older GRD products; duplicating VV keeps the
    # two-channel input shape valid and the model simply loses the ratio cue.
    vh_source = vh_db if vh_db is not None else vv_db
    # Channel-specific constants: VV and VH have different means.
    stack = np.stack([_standardize(vv_db, 0), _standardize(vh_source, 1)], axis=0)

    try:
        probability = await asyncio.to_thread(_infer_sync, model, stack)
    except Exception:
        log.exception("flood inference failed; using heuristic")
        return indices.sar_water_mask(vv_db), CONFIDENCE_HEURISTIC

    # Re-apply the input mask: the network fills everywhere, including where we
    # had no data, and those pixels must not count toward the flooded fraction.
    probability = np.where(np.isfinite(vv_db), probability, np.nan)
    return probability.astype("float32"), CONFIDENCE_TRAINED


# --------------------------------------------------------------------------- #
# Crop stress
# --------------------------------------------------------------------------- #


async def predict_crop_stress(
    ndvi_arr: np.ndarray,
    ndmi_arr: np.ndarray | None = None,
    ndwi_arr: np.ndarray | None = None,
    anomaly_arr: np.ndarray | None = None,
) -> tuple[np.ndarray, float]:
    """Crop-stress probability. Returns `(probability, confidence)`.

    Heuristic fallback keys on NDVI < 0.35, which is below the canopy vigour a
    cereal crop should show at any point after establishment.
    """
    model = _load("crop_stress", CropStressNet, settings.crop_stress_weights)

    if model is None:
        stressed = np.where(np.isnan(ndvi_arr), np.nan, (ndvi_arr < 0.35).astype("float32"))
        return stressed.astype("float32"), CONFIDENCE_HEURISTIC

    # Indices go in RAW, not standardised.
    #
    # ## Two skews, both caught by measurement
    #
    # `_standardize` carries SENTINEL-1 dB constants (mean -12, std 5). Pushing NDVI through it
    # shifts 0.2 to +2.44 — a 2.4-sigma displacement of a quantity that already lives in -1..1.
    # The crop model is trained on raw indices, so standardising here would be exactly the
    # training/serving skew that made the SAR model report 53% flooding on dry Sahel.
    #
    # NDVI/NDMI/NDWI are already bounded and comparable across scenes, which is *why* they are
    # normalised differences — there is nothing for a standardiser to fix.
    #
    # ## The fourth channel
    #
    # `anomaly` is `ndvi - seasonal_baseline(day_of_year)`, supplied by the caller from
    # `stats/anomaly.py`. **0.0 when no baseline exists** — the honest neutral value: a new AOI with
    # no history degrades to roughly 3-channel behaviour rather than failing, and improves as
    # `index_history` accumulates. See `CropStressNet` for why this channel raised precision from
    # 0.37.
    zeros = np.zeros_like(ndvi_arr, dtype="float32")

    def clean(array: np.ndarray | None) -> np.ndarray:
        # NaN becomes 0.0 — a neutral index value. The pixel is excluded from every reported
        # fraction anyway (`mean_fraction` ignores NaN), but it still passes through the
        # convolution and must not drag its neighbours.
        source = zeros if array is None else array
        return np.nan_to_num(source, nan=0.0, posinf=0.0, neginf=0.0).astype("float32")

    stack = np.stack(
        [
            clean(ndvi_arr),
            clean(ndmi_arr),
            clean(ndwi_arr),
            clean(anomaly_arr),
        ],
        axis=0,
    )

    try:
        probability = await asyncio.to_thread(_infer_sync, model, stack)
    except Exception:
        log.exception("crop stress inference failed; using heuristic")
        stressed = np.where(np.isnan(ndvi_arr), np.nan, (ndvi_arr < 0.35).astype("float32"))
        return stressed.astype("float32"), CONFIDENCE_HEURISTIC

    probability = np.where(np.isfinite(ndvi_arr), probability, np.nan)
    return probability.astype("float32"), CONFIDENCE_TRAINED


#: The four input channels of `CropStressNet`, in order, with reader-facing labels.
#:
#: Order is the contract: it must match the stack built in `predict_crop_stress` and the dataset the
#: notebook writes. A mismatch would attribute a decision to the wrong cause, which is worse than no
#: attribution at all — a farmer told "your soil is dry" when the model actually keyed on canopy
#: greenness receives the wrong advice with full confidence.
CROP_CHANNELS: tuple[tuple[str, str], ...] = (
    ("ndvi", "canopy greenness"),
    ("ndmi", "plant moisture"),
    ("ndwi", "surface water"),
    ("anomaly", "compared with this field's own history"),
)


#: The counterfactual value per channel — "what if this field were healthy and typical?".
#:
#: NOT the scene mean. See the note inside `crop_stress_attribution` for the measured failure that
#: choice produced on a uniform window. 0.35 is the documented healthy-canopy NDVI threshold the
#: heuristic path uses, so the comparison is against the same reference the rest of the codebase
#: treats as "not stressed".
_CROP_NEUTRALS: dict[str, float] = {
    "ndvi": 0.35,
    "ndmi": 0.0,
    "ndwi": 0.0,
    "anomaly": 0.0,
}


async def crop_stress_attribution(
    ndvi_arr: np.ndarray,
    ndmi_arr: np.ndarray | None = None,
    ndwi_arr: np.ndarray | None = None,
    anomaly_arr: np.ndarray | None = None,
) -> dict[str, float] | None:
    """Which input drove the crop-stress verdict. `{channel: signed contribution}` or None.

    ## Why this is exact rather than a SHAP estimate

    `CropStressNet` is 1x1 convolutions throughout, so it is a per-pixel function of four numbers
    with no spatial context. That makes an **ablation** exact rather than approximate: hold three
    channels at their observed values, replace the fourth with its neutral value, and the change in
    mean predicted probability IS that channel's contribution. No sampling, no kernel, no surrogate
    model — four extra forward passes on a 1,377-parameter network.
    
    SHAP would be the right tool for a model with interactions across pixels. Here it would be
    approximating something we can compute directly, and an approximation that a farmer acts on is a
    worse trade than a slightly slower exact answer.

    ## Why it matters for the advice, not just the explanation

    Two pixels can score identically stressed for opposite reasons:

      * driven by `ndmi` — the plant is short of water, so **irrigate**;
      * driven by `anomaly` — growth is below this field's own norm while moisture is fine, which
        points at pests, nutrients or planting date, and irrigating would waste water.

    `explain/irrigation.advise` turns a number into "irrigate or hold", and this is the input that
    makes that a reasoned choice rather than a threshold. Returned as SIGNED values: positive means
    the channel pushed toward stress, negative means it argued against it.

    Returns None when no weights are loaded — the heuristic path has no channels to attribute, and
    inventing an attribution for a fixed threshold would be a fabricated explanation.
    """
    model = _load("crop_stress", CropStressNet, settings.crop_stress_weights)
    if model is None:
        return None

    zeros = np.zeros_like(ndvi_arr, dtype="float32")

    def clean(array: np.ndarray | None) -> np.ndarray:
        source = zeros if array is None else array
        return np.nan_to_num(source, nan=0.0, posinf=0.0, neginf=0.0).astype("float32")

    channels = [clean(ndvi_arr), clean(ndmi_arr), clean(ndwi_arr), clean(anomaly_arr)]

    # Neutral value per channel, and the choice is load-bearing.
    #
    # 0.0 for the anomaly is genuinely neutral: it means "exactly at this field's seasonal norm".
    # 0.0 for an INDEX is not neutral — it is bare soil — so ablating to zero would measure the
    # difference between the observed value and bare soil rather than the channel's influence. The
    # scene's own mean is the right counterfactual: "what if this pixel were typical for this
    # scene?"
    # ## Why the index counterfactual is a CONSTANT, not the scene mean
    #
    # The scene mean looks like the natural neutral, and it has a failure mode that is easy to miss:
    # on a UNIFORM window every pixel already equals the mean, so the ablation is a no-op and every
    # index attributes exactly 0.000. Measured — a flat test input reported
    # `{ndvi: 0.0, ndmi: 0.0, ndwi: 0.0}` while probing the weights directly showed `ndwi` swinging
    # the output by 0.54. The attribution was reporting "this channel did not matter" about a channel
    # that dominated the decision.
    #
    # A fixed reference value fixes it: the question becomes "what if this field looked like a
    # typical healthy plot?" rather than "what if this pixel looked like its neighbours?", which is
    # the counterfactual a farmer actually cares about and is well defined on a uniform window.
    #
    # 0.35 for NDVI is the documented healthy-canopy threshold the heuristic path uses, so the
    # comparison is against the same reference the rest of the codebase treats as "not stressed".
    # NDMI and NDWI use 0.0, the neutral point of a normalised difference. The anomaly uses 0.0
    # because that genuinely means "exactly at this field's seasonal norm".
    neutrals: list[float] = []
    for name, _label in CROP_CHANNELS:
        neutrals.append(_CROP_NEUTRALS[name])

    try:
        baseline_stack = np.stack(channels, axis=0)
        baseline_probability = await asyncio.to_thread(_infer_sync, model, baseline_stack)
        baseline_mean = float(np.nanmean(baseline_probability))

        contributions: dict[str, float] = {}
        for index, (name, _label) in enumerate(CROP_CHANNELS):
            ablated = list(channels)
            ablated[index] = np.full_like(channels[index], neutrals[index])
            probability = await asyncio.to_thread(
                _infer_sync, model, np.stack(ablated, axis=0)
            )
            # Positive => removing the channel LOWERED predicted stress => it pushed toward stress.
            contributions[name] = baseline_mean - float(np.nanmean(probability))
    except Exception:
        log.exception("crop stress attribution failed; omitting it")
        return None

    return contributions


def dominant_crop_driver(contributions: dict[str, float] | None) -> tuple[str, str] | None:
    """The channel that pushed hardest toward stress, as `(name, label)`. None when nothing did.

    None is a real answer: if every contribution is negative or zero, no input argued for stress and
    naming a "main driver" would invent one. The caller then says nothing rather than guessing.
    """
    if not contributions:
        return None
    name, value = max(contributions.items(), key=lambda item: item[1])
    if value <= 0:
        return None
    label = dict(CROP_CHANNELS).get(name, name)
    return name, label


def mean_fraction(probability: np.ndarray, threshold: float = 0.5) -> float:
    """Share of valid pixels the model calls positive."""
    finite = probability[np.isfinite(probability)]
    if finite.size == 0:
        return 0.0
    return float(np.count_nonzero(finite > threshold) / finite.size)
