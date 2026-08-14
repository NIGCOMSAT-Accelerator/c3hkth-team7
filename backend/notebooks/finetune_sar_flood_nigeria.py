"""Fine-tune `sar_flood.pt` on the Nigeria weak-supervision set from `build_sar_flood_nigeria_dataset.py`.

## Why a re-gate against the SAME held-out Sen1Floods11 Nigeria chips, not a new metric

Weak supervision that improves on its own weakly-labelled set proves nothing — the label was
self-generated, so a model can fit it perfectly and still be wrong. The only trustworthy check is
whether the fine-tuned weights do BETTER OR NO WORSE on the untouched, hand-labelled Sen1Floods11
Nigeria test chips this repo already holds (`/tmp/s1f`, 18 chips) than the Sen1Floods11-only model
already sitting in `sar_flood.pt`. If not, the fine-tune is discarded and the existing weights ship
unchanged — a worse model with a plausible-sounding "trained on real Nigerian floods" story is
exactly the failure this codebase's other ship gates exist to catch.

Only 3 whole-scene examples exist in the Nigeria set (one per documented flood event), so there is
no internal train/val split worth doing — flips and 90-degree rotations are the only augmentation,
matching `train_sar_flood_unet.py`'s own choice, and a LOW learning rate (1e-4, vs 1e-3 for the
from-scratch run) to adapt rather than overwrite what Sen1Floods11 already taught.
"""

from __future__ import annotations

import copy
import pathlib
import sys

import numpy as np
import rasterio
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset

sys.path.insert(0, "/Users/lionelorishane/Documents/GitHub/prodCreditChekB2B/c3hkth-team7/backend")
from app.ml.models import SARFloodUNet  # noqa: E402

NIGERIA_DATA = pathlib.Path("/tmp/sar_flood_nigeria/sar_flood_nigeria.npz")
S1F_ROOT = pathlib.Path("/tmp/s1f")
WEIGHTS_PATH = pathlib.Path(
    "/Users/lionelorishane/Documents/GitHub/prodCreditChekB2B/c3hkth-team7/backend/app/ml/weights/sar_flood.pt"
)

VV_MEAN, VV_STD = -12.0, 5.0
VH_MEAN, VH_STD = -19.0, 5.0
FINETUNE_LR = 1e-4
FINETUNE_EPOCHS = 20


def _standardize_stack(vv_db: np.ndarray, vh_db: np.ndarray) -> np.ndarray:
    vv_n = np.nan_to_num((vv_db - VV_MEAN) / VV_STD, nan=0.0, posinf=0.0, neginf=0.0)
    vh_n = np.nan_to_num((vh_db - VH_MEAN) / VH_STD, nan=0.0, posinf=0.0, neginf=0.0)
    return np.stack([vv_n, vh_n]).astype("float32")


class NigeriaFineTuneSet(Dataset):
    """The 3 weakly-labelled Nigeria chips, standardised with the SAME constants training and
    serving already share (`inference.SAR_DB_MEAN`/`SAR_DB_STD`) — anything else would be exactly
    the training/serving skew that made the Kano dry-Sahel bug possible."""

    def __init__(self, x_db: np.ndarray, y: np.ndarray, v: np.ndarray) -> None:
        self.x_db = x_db  # (N, 2, H, W) — VV/VH in dB, NOT yet standardised
        self.y = y
        self.v = v

    def __len__(self) -> int:
        return self.x_db.shape[0]

    def __getitem__(self, index: int):
        stack = _standardize_stack(self.x_db[index, 0], self.x_db[index, 1])
        # `self.y`/`self.v` are (N, 1, H, W) — stored pre-unsqueezed by the build script — so index
        # the channel out here to get clean (H, W) for the flip/rotate logic below, which restores
        # it via `.unsqueeze(0)` at return. Without the `, 0]` these stayed (1, H, W), which put the
        # second flip on the size-1 channel axis instead of W and left every array negative-strided.
        target = self.y[index, 0]
        valid = self.v[index, 0]

        if np.random.rand() < 0.5:
            stack, target, valid = stack[:, :, ::-1], target[:, ::-1], valid[:, ::-1]
        if np.random.rand() < 0.5:
            stack, target, valid = stack[:, ::-1], target[::-1], valid[::-1]
        k = np.random.randint(4)
        if k:
            stack = np.rot90(stack, k, axes=(1, 2))
            target = np.rot90(target, k, axes=(0, 1))
            valid = np.rot90(valid, k, axes=(0, 1))
        stack = np.ascontiguousarray(stack)
        target = np.ascontiguousarray(target)
        valid = np.ascontiguousarray(valid)

        return (
            torch.from_numpy(stack),
            torch.from_numpy(target).unsqueeze(0),
            torch.from_numpy(valid).unsqueeze(0),
        )


class Sen1Floods11Nigeria(Dataset):
    """The held-out Nigeria Sen1Floods11 test chips — untouched, hand-labelled, the ship gate."""

    def __init__(self, pairs: list[dict]) -> None:
        self.pairs = pairs

    def __len__(self) -> int:
        return len(self.pairs)

    def __getitem__(self, index: int):
        pair = self.pairs[index]
        with rasterio.open(pair["chip"]) as src:
            vv = src.read(1).astype("float32")
            vh = src.read(2).astype("float32")
        with rasterio.open(pair["label"]) as src:
            label = src.read(1).astype("float32")

        valid = (label != -1) & np.isfinite(vv) & np.isfinite(vh)
        target = np.where(label == 1, 1.0, 0.0).astype("float32")
        stack = _standardize_stack(vv, vh)

        return (
            torch.from_numpy(stack),
            torch.from_numpy(target).unsqueeze(0),
            torch.from_numpy(valid.astype("float32")).unsqueeze(0),
        )


def masked_loss(logits, target, valid, pos_weight: float, dice_w: float = 0.5):
    weight = torch.full_like(target, 1.0)
    weight = torch.where(target > 0.5, weight * pos_weight, weight)
    bce = F.binary_cross_entropy_with_logits(logits, target, weight=weight, reduction="none")
    bce = (bce * valid).sum() / valid.sum().clamp(min=1.0)
    probability = torch.sigmoid(logits) * valid
    truth = target * valid
    intersection = (probability * truth).sum()
    dice = 1.0 - (2.0 * intersection + 1.0) / (probability.sum() + truth.sum() + 1.0)
    return bce + dice_w * dice


@torch.no_grad()
def evaluate(model, loader, device, threshold: float = 0.7) -> dict:
    """Threshold 0.7 to match the value `train_sar_flood_unet.py` already chose on the val sweep —
    a different threshold here would not be comparing the two models on the same terms."""
    model.eval()
    tp = fp = fn = 0.0
    for x, y, v in loader:
        x, y, v = x.to(device), y.to(device), v.to(device)
        pred = (torch.sigmoid(model(x)) > threshold).float() * v
        truth = y * v
        tp += float((pred * truth).sum())
        fp += float((pred * (1 - truth) * v).sum())
        fn += float(((1 - pred) * truth * v).sum())
    iou = tp / max(tp + fp + fn, 1.0)
    precision = tp / max(tp + fp, 1.0)
    recall = tp / max(tp + fn, 1.0)
    f1 = 2 * precision * recall / max(precision + recall, 1e-9)
    return {"iou": iou, "f1": f1, "precision": precision, "recall": recall}


def _load_nigeria_test_chips() -> list[dict]:
    import re

    pairs = []
    for chip in sorted((S1F_ROOT / "S1Hand").glob("Nigeria_*_S1Hand.tif")):
        stem = chip.name.replace("_S1Hand.tif", "")
        label = S1F_ROOT / "LabelHand" / f"{stem}_LabelHand.tif"
        if label.exists():
            pairs.append({"chip": chip, "label": label, "id": stem})
    return pairs


def main() -> int:
    if not NIGERIA_DATA.exists():
        print(f"missing {NIGERIA_DATA} — run build_sar_flood_nigeria_dataset.py first")
        return 2
    if not WEIGHTS_PATH.exists():
        print(f"missing {WEIGHTS_PATH} — run train_sar_flood_unet.py first")
        return 2

    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    print(f"device: {device}")

    blob = np.load(NIGERIA_DATA, allow_pickle=False)
    x_db, y, v, names = blob["x"], blob["y"], blob["v"], blob["name"]
    print(f"Nigeria fine-tune set: {x_db.shape} — {list(names)}")

    test_pairs = _load_nigeria_test_chips()
    if not test_pairs:
        print(f"no Nigeria Sen1Floods11 test chips found under {S1F_ROOT} — cannot gate")
        return 2
    print(f"ship-gate test set: {len(test_pairs)} held-out Nigeria Sen1Floods11 chips")

    test_dl = DataLoader(Sen1Floods11Nigeria(test_pairs), batch_size=4)

    # Baseline: the model as it ships today, evaluated fresh so the comparison is apples-to-apples
    # under this exact script's evaluate() rather than trusting an earlier run's printed number.
    baseline_model = SARFloodUNet().to(device)
    baseline_state = torch.load(WEIGHTS_PATH, map_location=device, weights_only=True)
    baseline_model.load_state_dict(baseline_state)
    baseline_metrics = evaluate(baseline_model, test_dl, device)
    print(
        f"\nbaseline (Sen1Floods11-only)  IoU {baseline_metrics['iou']:.4f}  "
        f"F1 {baseline_metrics['f1']:.4f}"
    )

    # Fine-tune a FRESH copy — never mutate the model we just used as the baseline measurement.
    model = SARFloodUNet().to(device)
    model.load_state_dict(copy.deepcopy(baseline_state))

    rate = float((y * v).sum() / max(v.sum(), 1.0))
    pos_weight = min(max((1 - rate) / max(rate, 1e-6), 1.0), 20.0)
    print(f"Nigeria set positive rate {rate:.4f} -> pos_weight {pos_weight:.2f}")

    train_dl = DataLoader(NigeriaFineTuneSet(x_db, y, v), batch_size=1, shuffle=True)
    opt = torch.optim.Adam(model.parameters(), lr=FINETUNE_LR)

    for epoch in range(1, FINETUNE_EPOCHS + 1):
        model.train()
        running = 0.0
        for xb, yb, vb in train_dl:
            xb, yb, vb = xb.to(device), yb.to(device), vb.to(device)
            opt.zero_grad()
            loss = masked_loss(model(xb), yb, vb, pos_weight)
            loss.backward()
            opt.step()
            running += float(loss)
        if epoch % 5 == 0 or epoch == 1:
            metrics = evaluate(model, test_dl, device)
            print(
                f"  epoch {epoch:2}  loss {running / max(len(train_dl), 1):.4f}  "
                f"held-out IoU {metrics['iou']:.4f}  F1 {metrics['f1']:.4f}"
            )

    finetuned_metrics = evaluate(model, test_dl, device)
    print(
        f"\nfine-tuned (+ Nigeria weak supervision)  IoU {finetuned_metrics['iou']:.4f}  "
        f"F1 {finetuned_metrics['f1']:.4f}"
    )

    print("\n=== RE-GATE: fine-tuned vs the Sen1Floods11-only baseline, same held-out chips ===")
    print(f"  baseline    IoU {baseline_metrics['iou']:.4f}  F1 {baseline_metrics['f1']:.4f}")
    print(f"  fine-tuned  IoU {finetuned_metrics['iou']:.4f}  F1 {finetuned_metrics['f1']:.4f}")

    ship = finetuned_metrics["iou"] >= baseline_metrics["iou"]
    print(
        f"\n  GATE: {'PASS — fine-tuned matches or beats the baseline, shipping' if ship else 'FAIL — Nigeria fine-tune regressed the held-out score, keeping existing weights'}"
    )

    if ship:
        state = {k: t.detach().cpu() for k, t in model.state_dict().items()}
        torch.save(state, WEIGHTS_PATH)
        print(f"  wrote {WEIGHTS_PATH} ({WEIGHTS_PATH.stat().st_size / 1e6:.2f} MB)")
    else:
        print(f"  {WEIGHTS_PATH} left unchanged")

    return 0 if ship else 1


if __name__ == "__main__":
    raise SystemExit(main())
