"""Device resolution and weight portability.

## Why this file exists

The models are TRAINED on Apple Silicon (MPS) and RUN on a CPU-only Ubuntu VPS. Those are different
machines with different accelerators, and the setting that is correct on one used to crash the other:

    TORCH_DEVICE=mps  on Linux  ->  RuntimeError: Storage device not recognized: mps

Reproduced in the real container before the fix. It happened inside
`torch.load(map_location=_device())`, so the weights never loaded, every assessment fell back to the
threshold heuristic at confidence 0.55, and the container still reported healthy. A training-machine
setting must not be able to silently halve the platform's confidence on the deployment target.
"""

from __future__ import annotations

import numpy as np
import pytest
import torch

from app.ml import inference


def _fresh_device(monkeypatch, requested: str) -> torch.device:
    """Resolve with the cache cleared, since `_device` memoises deliberately."""
    monkeypatch.setattr(inference.settings, "torch_device", requested, raising=False)
    monkeypatch.setattr(inference, "_resolved_device", None, raising=False)
    return inference._device()


def test_mps_falls_back_to_cpu_when_unavailable(monkeypatch):
    """**The crash this file is named for.**

    Simulates a Linux host: the `mps` backend exists in torch 2.5 but `is_built()` is False. Both
    conditions have to be checked — a torch built without MPS reports `is_built() == False`, and
    Apple hardware lacking Metal reports `is_available() == False`.
    """

    class _NoMPS:
        @staticmethod
        def is_built() -> bool:
            return False

        @staticmethod
        def is_available() -> bool:
            return False

    monkeypatch.setattr(torch.backends, "mps", _NoMPS, raising=False)
    assert _fresh_device(monkeypatch, "mps").type == "cpu"


def test_cuda_falls_back_to_cpu_when_unavailable(monkeypatch):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    assert _fresh_device(monkeypatch, "cuda:0").type == "cpu"


def test_an_unparseable_device_falls_back_rather_than_raising(monkeypatch):
    """A typo in `.env` must degrade, not take the process down mid-assessment."""
    assert _fresh_device(monkeypatch, "nonsense").type == "cpu"


def test_an_empty_setting_is_cpu(monkeypatch):
    assert _fresh_device(monkeypatch, "").type == "cpu"


def test_the_device_is_proven_usable_not_merely_constructed():
    """A device that constructs but cannot allocate would fail later, inside a forward pass.

    There the fallback is a confidence downgrade rather than a clean degrade to CPU, so the check
    belongs here — one tensor allocation at resolution time.
    """
    import pathlib

    source = pathlib.Path("app/ml/inference.py").read_text()
    start = source.index("def _device(")
    body = source[start : source.index("\ndef _load(", start)]

    assert "torch.zeros(1, device=" in body, (
        "constructing a torch.device does not prove it can allocate"
    )


def test_weights_are_saved_in_a_portable_form():
    """State dicts must be CPU tensors, or a Linux host cannot deserialise them.

    `torch.save` records each tensor's device. An MPS-resident state dict makes `torch.load` try to
    restore onto MPS regardless of `map_location` for some torch versions — which is precisely the
    crash above. The training notebooks must move tensors to CPU before saving.
    """
    import json
    import pathlib

    for notebook in pathlib.Path("notebooks").glob("*.ipynb"):
        text = json.dumps(json.loads(notebook.read_text()))
        if "torch.save" not in text:
            continue
        assert ".cpu()" in text, (
            f"{notebook.name} saves weights without moving them to CPU first — the result will "
            "not load on a CPU-only Linux host"
        )


# --------------------------------------------------------------------------- #
# Training/serving skew — the defect that made a correct model produce a false WARNING
# --------------------------------------------------------------------------- #


def test_sar_standardisation_uses_fixed_constants_not_per_scene_statistics():
    """**A correct model fed a recentred input produced the platform's first false WARNING.**

    `_standardize` used to compute each SCENE's own mean and standard deviation. That destroys the
    absolute radiometry the decision depends on: water is dark in absolute dB, and per-scene
    recentring maps a bone-dry scene and a fully flooded one onto the same distribution.

    Measured over Kano — Sahel, VV median -4.3 dB, heuristic water fraction 0.000 — the trained model
    reported **52.9%** standing water and escalated to WARNING at confidence 0.88. Probing the
    weights with fixed inputs showed the model was fine and monotonic (-4 dB -> 0.022,
    -25 dB -> 0.913). The input was the lie. After the fix: **0.003**.
    """
    import pathlib

    source = pathlib.Path("app/ml/inference.py").read_text()
    start = source.index("def _standardize(")
    body = source[start : source.index("\ndef _infer_sync(", start)]

    assert "SAR_DB_MEAN" in body and "SAR_DB_STD" in body
    # The per-scene statistics must be gone.
    assert "finite.mean()" not in body, "per-scene mean destroys absolute radiometry"
    assert "finite.std()" not in body


def test_vv_and_vh_get_their_own_constants():
    """VV and VH differ by ~7 dB. Using one channel's constants for the other shifts it wholesale."""
    from app.ml.inference import SAR_DB_MEAN, _standardize

    assert SAR_DB_MEAN[0] != SAR_DB_MEAN[1]

    value = np.full((4, 4), -12.0, dtype="float32")
    # -12 dB is exactly the VV mean, so channel 0 standardises it to zero.
    assert abs(float(_standardize(value, 0).mean())) < 1e-6
    # The same value on the VH channel must NOT be zero.
    assert abs(float(_standardize(value, 1).mean())) > 0.5


def test_dry_land_and_water_standardise_to_different_places():
    """The property per-scene standardisation destroyed.

    Two uniform scenes — one dry, one wet — must land in different parts of the input space. Under
    per-scene statistics both became all-zeros and were indistinguishable.
    """
    from app.ml.inference import _standardize

    dry = _standardize(np.full((8, 8), -4.0, dtype="float32"), 0)
    wet = _standardize(np.full((8, 8), -22.0, dtype="float32"), 0)
    assert float(dry.mean()) > float(wet.mean()) + 2.0, (
        "dry land must standardise well above open water, or the model cannot tell them apart"
    )


def test_nodata_standardises_to_the_mean_not_an_extreme():
    """NaN becomes 0.0 — the standardised mean — so a gap reads as typical, not as water."""
    from app.ml.inference import _standardize

    array = np.array([[-12.0, np.nan], [-12.0, np.nan]], dtype="float32")
    out = _standardize(array, 0)
    assert np.isfinite(out).all()
    assert float(out[0, 1]) == 0.0


# --------------------------------------------------------------------------- #
# CropStressNet's fourth channel — the feature that doubled precision
# --------------------------------------------------------------------------- #


def test_crop_stress_net_takes_four_channels():
    """**The fix for a precision ceiling that more data could not have raised.**

    The label means "below what THIS location shows at THIS time of year". With only the three
    absolute indices as input, the model could not see the baseline it was being compared against:

        NDVI where labelled STRESSED : mean 0.123
        NDVI where labelled NOT      : mean 0.371     <- heavily overlapping

    Identical NDVI carried opposite labels in different AOIs, so precision was capped at 0.37 by
    construction. Adding the deviation-from-baseline separates them:

        anomaly < -0.2  ->  98.2% stressed
        anomaly >  0.0  ->   0.0% stressed

    Held-out AOIs, measured: precision **0.368 -> 0.764**, IoU 0.351 -> 0.727, recall 0.883 -> 0.936.
    """
    from app.ml.models import CropStressNet

    model = CropStressNet()
    first = next(model.net.children())
    assert first.in_channels == 4, "the anomaly channel is required — see the class docstring"


def test_crop_indices_are_not_pushed_through_the_sar_standardiser():
    """A second skew, introduced by the SAR fix and caught before it shipped.

    `_standardize` carries SENTINEL-1 dB constants (mean -12, std 5). NDVI 0.2 through it becomes
    **+2.44** — a 2.4-sigma displacement of a quantity already bounded in -1..1. The crop model is
    trained on RAW indices, so standardising here would be exactly the training/serving skew that
    made the SAR model report 53% flooding on dry Sahel.
    """
    import pathlib

    source = pathlib.Path("app/ml/inference.py").read_text()
    start = source.index("async def predict_crop_stress(")
    body = source[start : source.index("\ndef mean_fraction(", start)]
    code = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )

    assert "_standardize(" not in code, (
        "crop indices must go in raw — _standardize holds Sentinel-1 dB constants"
    )


def test_a_missing_anomaly_degrades_rather_than_failing():
    """A new AOI has no seasonal history, and must still get a reading.

    `stats/anomaly` needs 12 observations before it will fit. Until then the channel is zeros — the
    honest NEUTRAL value, so the model degrades to roughly 3-channel behaviour. Substituting a
    sentinel like -999 would read as catastrophic stress on every fresh subscriber.
    """
    import asyncio

    from app.ml import inference

    ndvi = np.full((16, 16), 0.4, dtype="float32")

    async def run():
        return await inference.predict_crop_stress(ndvi, ndvi, ndvi, None)

    probability, confidence = asyncio.run(run())
    assert np.isfinite(probability).all()
    assert 0.0 <= float(np.nanmean(probability)) <= 1.0
    assert confidence in (0.55, 0.88)


def test_the_analyst_supplies_the_anomaly_before_inference():
    """Order matters: the anomaly is an INPUT now, not a post-hoc comparison.

    It used to be computed after `predict_crop_stress`, which is fine for a summary fraction and
    impossible for a feature. Computed once and reused, so the history read still happens once.
    """
    import pathlib

    source = pathlib.Path("app/agents/analyst.py").read_text()
    start = source.index("async def _analyze_optical(")
    body = source[start : source.index("\n    async def _seasonal_fraction(", start)]

    assert body.index("_seasonal_fraction(") < body.index("predict_crop_stress("), (
        "the anomaly must be computed BEFORE inference — it is an input to it"
    )
    assert "anomaly_arr" in body


# --------------------------------------------------------------------------- #
# Per-channel attribution — exact, because the model has no spatial context
# --------------------------------------------------------------------------- #


def test_attribution_channels_match_the_input_stack_order():
    """**Order is the contract.**

    `CROP_CHANNELS` names the four inputs in the order `predict_crop_stress` stacks them. A mismatch
    would attribute a decision to the wrong cause — a farmer told "your soil is dry" when the model
    actually keyed on canopy greenness gets the wrong advice at full confidence, which is worse than
    no attribution at all.
    """
    import pathlib

    from app.ml.inference import CROP_CHANNELS

    assert [name for name, _ in CROP_CHANNELS] == ["ndvi", "ndmi", "ndwi", "anomaly"]

    source = pathlib.Path("app/ml/inference.py").read_text()
    start = source.index("async def predict_crop_stress(")
    stack = source[start : source.index("\nasync def crop_stress_attribution(", start)]
    # The stack must build in the same order the labels declare.
    order = [
        stack.index("clean(ndvi_arr)"),
        stack.index("clean(ndmi_arr)"),
        stack.index("clean(ndwi_arr)"),
        stack.index("clean(anomaly_arr)"),
    ]
    assert order == sorted(order), "the input stack order must match CROP_CHANNELS"


def test_attribution_ablates_to_a_meaningful_neutral():
    """Zero is neutral for the anomaly and NOT neutral for an index.

    Anomaly 0.0 means "exactly at this field's seasonal norm" — genuinely neutral. Index 0.0 means
    **bare soil**, so ablating an index to zero would measure the distance from bare soil rather
    than the channel's influence. The scene's own mean is the right counterfactual.
    """
    import pathlib

    source = pathlib.Path("app/ml/inference.py").read_text()
    start = source.index("async def crop_stress_attribution(")
    body = source[start : source.index("\ndef dominant_crop_driver(", start)]

    from app.ml.inference import _CROP_NEUTRALS

    # NOT the scene mean. On a UNIFORM window every pixel already equals the mean, so the ablation
    # is a no-op and every index attributes exactly 0.000 — measured: a flat input reported
    # `{ndvi: 0.0, ndmi: 0.0, ndwi: 0.0}` while probing the weights showed `ndwi` swinging the
    # output by 0.54. The attribution was reporting "did not matter" about the dominant channel.
    assert "_CROP_NEUTRALS" in body
    assert "finite.mean()" not in body, (
        "a scene-mean counterfactual is a no-op on a uniform window"
    )
    # NDVI's reference is the documented healthy-canopy threshold, so the comparison is against the
    # same value the heuristic path treats as "not stressed".
    assert _CROP_NEUTRALS["ndvi"] == 0.35
    # A normalised difference is neutral at zero; so is "exactly at the seasonal norm".
    assert _CROP_NEUTRALS["ndmi"] == 0.0
    assert _CROP_NEUTRALS["anomaly"] == 0.0


def test_no_attribution_without_weights():
    """The heuristic path has no channels to attribute.

    Inventing an attribution for a fixed `NDVI < 0.35` threshold would be a fabricated explanation —
    the exact failure the grounding rule exists to prevent, arriving via the XAI layer.
    """
    import asyncio

    from app.ml import inference

    # Force the "no weights" branch without touching the filesystem.
    saved = inference._models.get("crop_stress", "missing")
    inference._models["crop_stress"] = None
    try:
        result = asyncio.run(
            inference.crop_stress_attribution(np.zeros((8, 8), dtype="float32"))
        )
    finally:
        if saved == "missing":
            inference._models.pop("crop_stress", None)
        else:
            inference._models["crop_stress"] = saved

    assert result is None


def test_no_dominant_driver_when_nothing_pushed_toward_stress():
    """None is a real answer.

    If every contribution is negative or zero, no input argued for stress. Naming a "main driver"
    there would invent one, and the caller must say nothing instead.
    """
    from app.ml.inference import dominant_crop_driver

    assert dominant_crop_driver(None) is None
    assert dominant_crop_driver({}) is None
    assert dominant_crop_driver({"ndvi": -0.1, "anomaly": 0.0}) is None
    assert dominant_crop_driver({"ndvi": 0.05, "anomaly": 0.40}) == (
        "anomaly",
        "compared with this field's own history",
    )


def test_the_irrigation_surface_receives_the_attribution():
    """**This is the surface where attribution changes the ADVICE, not just the wording.**

    Moisture-driven stress means irrigate. History-driven stress with moisture fine points at pests,
    nutrients or planting date — and irrigating wastes water the farmer paid for. The prompt must
    say so explicitly, or the model will default to "water it".
    """
    import pathlib

    source = pathlib.Path("app/explain/irrigation.py").read_text()

    assert "attribution_block(assessment)" in source
    assert "wastes water" in source, (
        "the prompt must state the consequence of irrigating the wrong problem"
    )


def test_attribution_is_non_zero_on_a_uniform_window():
    """**The bug the scene-mean counterfactual produced.**

    A flat window is not an edge case — a small plot read at 512 px is often near-uniform. With the
    scene mean as the reference, every index attributed exactly 0.000 there, which reads as "none of
    these mattered" when in fact `ndwi` was swinging the output by 0.54.
    """
    import asyncio

    from app.ml import inference

    if inference._load(
        "crop_stress", inference.CropStressNet, inference.settings.crop_stress_weights
    ) is None:
        pytest.skip("no weights present; attribution is correctly unavailable")

    uniform = np.full((16, 16), 0.30, dtype="float32")
    result = asyncio.run(
        inference.crop_stress_attribution(
            uniform,
            np.full_like(uniform, -0.35),
            uniform,
            np.zeros_like(uniform),
        )
    )
    assert result is not None
    assert any(abs(v) > 1e-6 for v in result.values()), (
        "a uniform window must still attribute — see the counterfactual note"
    )


def test_different_causes_give_different_dominant_drivers():
    """The whole point: identical stress scores can have opposite causes and opposite advice.

    Moisture-driven -> irrigate. History-driven with moisture fine -> look at pests or nutrients,
    because irrigating wastes water the farmer paid for.
    """
    import asyncio

    from app.ml import inference

    if inference._load(
        "crop_stress", inference.CropStressNet, inference.settings.crop_stress_weights
    ) is None:
        pytest.skip("no weights present")

    flat = np.full((16, 16), 0.30, dtype="float32")

    async def attribute(ndmi, anomaly):
        return await inference.crop_stress_attribution(flat, ndmi, flat, anomaly)

    moisture = asyncio.run(attribute(np.full_like(flat, -0.35), np.zeros_like(flat)))
    history = asyncio.run(attribute(flat, np.full_like(flat, -2.5)))

    assert inference.dominant_crop_driver(history) == (
        "anomaly",
        "compared with this field's own history",
    )
    # And the moisture case must NOT be attributed to history.
    moisture_driver = inference.dominant_crop_driver(moisture)
    assert moisture_driver is None or moisture_driver[0] != "anomaly"


# --------------------------------------------------------------------------- #
# NNPACK — a warning, not a computation
# --------------------------------------------------------------------------- #


def test_cpu_resolution_disables_the_nnpack_backend(monkeypatch):
    """`worker-analyst-1` logged `Could not initialize NNPACK! Reason: Unsupported hardware.`

    NNPACK is one convolution backend among several; it needs CPU features a hypervisor may mask.
    torch falls back to its own kernels, so the message is benign — but it is emitted from C++ on
    every worker start, so it lands in the runtime console as an unstructured line among structured
    JSON, indistinguishable at a glance from a real degradation like `worldpop lookup failed`.

    Asserted on the CPU path only: on CUDA/MPS the backend is irrelevant and touching a global flag
    would be gratuitous.
    """
    calls: list[bool] = []

    class _NNPack:
        @staticmethod
        def set_flags(value):
            calls.append(value)

    monkeypatch.setattr(torch.backends, "nnpack", _NNPack(), raising=False)
    device = _fresh_device(monkeypatch, "cpu")

    assert device.type == "cpu"
    assert calls == [False], "the CPU path must disable NNPACK exactly once"


def test_disabling_nnpack_does_not_change_a_single_number():
    """**The claim that makes suppressing it acceptable**, checked rather than assumed.

    If NNPACK selection changed results, silencing the warning would be hiding a divergence in the
    numbers the flood and crop-stress models produce. It does not: the same convolution is bitwise
    identical with the backend enabled and disabled. Verified in the built image too, where the
    warning actually fires — 2.2 on the training Mac has no `backends.nnpack` namespace at all.
    """
    torch.manual_seed(0)
    x = torch.randn(1, 2, 64, 64)
    conv = torch.nn.Conv2d(2, 8, 3, padding=1).eval()

    nnpack = getattr(torch.backends, "nnpack", None)
    if not hasattr(nnpack, "set_flags"):
        pytest.skip("this torch has no NNPACK backend to toggle")

    with torch.no_grad():
        nnpack.set_flags(True)
        enabled = conv(x)
        nnpack.set_flags(False)
        disabled = conv(x)

    assert torch.equal(enabled, disabled)


def test_a_torch_without_the_nnpack_namespace_still_resolves(monkeypatch):
    """torch 2.2 — the version on the training Mac — has no `backends.nnpack`.

    A cosmetic tweak must never be the thing that stops a device resolving, so absence of the
    namespace, and a `set_flags` that raises, both have to be non-events.
    """
    monkeypatch.delattr(torch.backends, "nnpack", raising=False)
    assert _fresh_device(monkeypatch, "cpu").type == "cpu"

    class _Angry:
        @staticmethod
        def set_flags(value):
            raise RuntimeError("no")

    monkeypatch.setattr(torch.backends, "nnpack", _Angry(), raising=False)
    assert _fresh_device(monkeypatch, "cpu").type == "cpu"
