"""Train SARFloodUNet on Sen1Floods11, Nigeria held out. Runs on Apple MPS.

Mirrors the notebook so the exported weights match what `app/ml/inference.py` will load. The
notebook is the human-facing artefact; this is the same procedure as a script so it can be run
unattended and its numbers quoted.
"""

from __future__ import annotations

import collections
import copy
import pathlib
import re
import sys
import time

import numpy as np
import rasterio
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset

sys.path.insert(0, "/Users/lionelorishane/Documents/GitHub/prodCreditChekB2B/c3hkth-team7/backend")
from app.ml.models import SARFloodUNet  # noqa: E402

ROOT = pathlib.Path("/tmp/s1f")
TEST_REGIONS = {"nigeria"}
VAL_REGIONS = {"bolivia", "somalia"}

# Sen1Floods11 label convention: 1 = water, 0 = not water, -1 = no data / cloud.
# The -1 class must be MASKED, not learned — it is an absence of ground truth, and training on it
# would teach the model to predict "unknown" as a class.
NODATA_LABEL = -1

# Serving standardisation, copied from `app/eo/indices.py`'s expectations: the Analyst hands the
# model VV/VH in DECIBELS. Training on raw linear power would produce a model that sees a completely
# different input distribution at inference — the single most common training/serving skew.
VV_MEAN, VV_STD = -12.0, 5.0
VH_MEAN, VH_STD = -19.0, 5.0


def to_db(linear: np.ndarray) -> np.ndarray:
    """Linear power -> dB, with non-positive values as NaN. Same rule as `indices.to_db`."""
    out = np.full_like(linear, np.nan, dtype="float32")
    positive = linear > 0
    out[positive] = 10.0 * np.log10(linear[positive])
    return out


class Sen1Floods11(Dataset):
    def __init__(self, pairs: list[dict], augment: bool = False) -> None:
        self.pairs = pairs
        self.augment = augment

    def __len__(self) -> int:
        return len(self.pairs)

    def __getitem__(self, index: int):
        pair = self.pairs[index]
        with rasterio.open(pair["chip"]) as src:
            # Sen1Floods11 S1Hand ships VV and VH already in dB.
            vv = src.read(1).astype("float32")
            vh = src.read(2).astype("float32")
        with rasterio.open(pair["label"]) as src:
            label = src.read(1).astype("float32")

        valid = (label != NODATA_LABEL) & np.isfinite(vv) & np.isfinite(vh)
        target = np.where(label == 1, 1.0, 0.0).astype("float32")

        # Standardise, then zero the invalid pixels. NaN would poison the forward pass; the loss
        # masks them out, so their value is irrelevant as long as it is finite.
        vv_n = np.nan_to_num((vv - VV_MEAN) / VV_STD, nan=0.0, posinf=0.0, neginf=0.0)
        vh_n = np.nan_to_num((vh - VH_MEAN) / VH_STD, nan=0.0, posinf=0.0, neginf=0.0)
        x = np.stack([vv_n, vh_n]).astype("float32")

        if self.augment:
            if np.random.rand() < 0.5:
                x, target, valid = x[:, :, ::-1], target[:, ::-1], valid[:, ::-1]
            if np.random.rand() < 0.5:
                x, target, valid = x[:, ::-1], target[::-1], valid[::-1]
            x, target, valid = np.ascontiguousarray(x), np.ascontiguousarray(target), np.ascontiguousarray(valid)

        return (
            torch.from_numpy(x),
            torch.from_numpy(target).unsqueeze(0),
            torch.from_numpy(valid.astype("float32")).unsqueeze(0),
        )


def masked_loss(logits, target, valid, pos_weight: float, dice_w: float = 0.5):
    """Masked BCE + soft Dice. Both restricted to labelled pixels.

    Dice is included because water is a minority class and BCE alone optimises toward predicting
    "dry everywhere" — which scores well on pixel accuracy and is useless as a flood detector.
    """
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
def evaluate(model, loader, device, threshold: float = 0.5) -> dict:
    model.eval()
    tp = fp = fn = tn = 0.0
    for x, y, v in loader:
        x, y, v = x.to(device), y.to(device), v.to(device)
        pred = (torch.sigmoid(model(x)) > threshold).float() * v
        truth = y * v
        tp += float((pred * truth).sum())
        fp += float((pred * (1 - truth) * v).sum())
        fn += float(((1 - pred) * truth * v).sum())
        tn += float(((1 - pred) * (1 - truth) * v).sum())
    iou = tp / max(tp + fp + fn, 1.0)
    precision = tp / max(tp + fp, 1.0)
    recall = tp / max(tp + fn, 1.0)
    f1 = 2 * precision * recall / max(precision + recall, 1e-9)
    return {"iou": iou, "f1": f1, "precision": precision, "recall": recall}


@torch.no_grad()
def evaluate_heuristic(loader, threshold_db: float = -16.0) -> dict:
    """The incumbent: VV < -16 dB. This is what the trained model has to beat to ship."""
    tp = fp = fn = 0.0
    for x, y, v in loader:
        # Undo standardisation to recover dB.
        vv_db = x[:, 0:1] * VV_STD + VV_MEAN
        pred = (vv_db < threshold_db).float() * v
        truth = y * v
        tp += float((pred * truth).sum())
        fp += float((pred * (1 - truth) * v).sum())
        fn += float(((1 - pred) * truth * v).sum())
    iou = tp / max(tp + fp + fn, 1.0)
    precision = tp / max(tp + fp, 1.0)
    recall = tp / max(tp + fn, 1.0)
    f1 = 2 * precision * recall / max(precision + recall, 1e-9)
    return {"iou": iou, "f1": f1, "precision": precision, "recall": recall}


def main() -> int:
    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    print(f"device: {device}")

    pairs = []
    for chip in sorted((ROOT / "S1Hand").glob("*_S1Hand.tif")):
        stem = chip.name.replace("_S1Hand.tif", "")
        label = ROOT / "LabelHand" / f"{stem}_LabelHand.tif"
        if label.exists():
            pairs.append(
                {
                    "region": re.match(r"([A-Za-z-]+)_", stem).group(1),
                    "chip": chip,
                    "label": label,
                    "id": stem,
                }
            )

    def bucket(region: str) -> str:
        r = region.lower()
        if r in TEST_REGIONS:
            return "test"
        if r in VAL_REGIONS:
            return "val"
        return "train"

    splits = collections.defaultdict(list)
    for p in pairs:
        splits[bucket(p["region"])].append(p)
    for name in ("train", "val", "test"):
        regions = sorted({p["region"] for p in splits[name]})
        print(f"  {name:5} {len(splits[name]):3} chips  {regions}")

    train_dl = DataLoader(Sen1Floods11(splits["train"], augment=True), batch_size=8, shuffle=True)
    val_dl = DataLoader(Sen1Floods11(splits["val"]), batch_size=8)
    test_dl = DataLoader(Sen1Floods11(splits["test"]), batch_size=8)

    # Real positive rate, for the BCE weight.
    positives = total = 0.0
    for _, y, v in DataLoader(Sen1Floods11(splits["train"]), batch_size=8):
        positives += float((y * v).sum())
        total += float(v.sum())
    rate = positives / max(total, 1.0)
    pos_weight = min(max((1 - rate) / max(rate, 1e-6), 1.0), 20.0)
    print(f"  positive rate {rate:.4f} -> pos_weight {pos_weight:.2f}")

    model = SARFloodUNet().to(device)
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=30)

    best_iou, best_state, best_epoch = -1.0, None, -1
    started = time.perf_counter()
    for epoch in range(1, 31):
        model.train()
        running = 0.0
        for x, y, v in train_dl:
            x, y, v = x.to(device), y.to(device), v.to(device)
            opt.zero_grad()
            loss = masked_loss(model(x), y, v, pos_weight)
            loss.backward()
            opt.step()
            running += float(loss)
        sched.step()
        metrics = evaluate(model, val_dl, device)
        flag = ""
        if metrics["iou"] > best_iou:
            best_iou, best_epoch = metrics["iou"], epoch
            best_state = copy.deepcopy({k: v.detach().cpu() for k, v in model.state_dict().items()})
            flag = "  <- best"
        print(
            f"  epoch {epoch:2}  loss {running / max(len(train_dl), 1):.4f}  "
            f"val IoU {metrics['iou']:.4f}  F1 {metrics['f1']:.4f}{flag}"
        )

    print(f"\ntrained in {time.perf_counter() - started:.0f}s; best epoch {best_epoch} IoU {best_iou:.4f}")

    model.load_state_dict(best_state)
    model.to(device)

    # Threshold sweep on VALIDATION only — choosing it on test would leak.
    best_t, best_t_iou = 0.5, -1.0
    print("\nthreshold sweep (val):")
    for t in (0.3, 0.4, 0.5, 0.6, 0.7):
        m = evaluate(model, val_dl, device, threshold=t)
        print(f"  {t:.1f}  IoU {m['iou']:.4f}  F1 {m['f1']:.4f}  P {m['precision']:.4f}  R {m['recall']:.4f}")
        if m["iou"] > best_t_iou:
            best_t, best_t_iou = t, m["iou"]
    print(f"  chosen threshold {best_t}")

    print("\n=== SHIP GATE: Nigeria (held out) — trained vs the -16 dB heuristic ===")
    trained = evaluate(model, test_dl, device, threshold=best_t)
    heuristic = evaluate_heuristic(test_dl)
    print(f"  trained    IoU {trained['iou']:.4f}  F1 {trained['f1']:.4f}  P {trained['precision']:.4f}  R {trained['recall']:.4f}")
    print(f"  heuristic  IoU {heuristic['iou']:.4f}  F1 {heuristic['f1']:.4f}  P {heuristic['precision']:.4f}  R {heuristic['recall']:.4f}")

    ship = trained["iou"] > heuristic["iou"]
    print(f"\n  GATE: {'PASS — trained model beats the heuristic on held-out Nigeria' if ship else 'FAIL — do NOT ship these weights'}")

    if ship:
        out = pathlib.Path(
            "/Users/lionelorishane/Documents/GitHub/prodCreditChekB2B/c3hkth-team7/backend/app/ml/weights/sar_flood.pt"
        )
        torch.save(best_state, out)
        print(f"  wrote {out} ({out.stat().st_size / 1e6:.2f} MB, CPU tensors)")
    return 0 if ship else 1


if __name__ == "__main__":
    raise SystemExit(main())
