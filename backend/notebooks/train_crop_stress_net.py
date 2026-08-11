"""Train CropStressNet on weakly-supervised Nigerian Sentinel-2 anomaly labels. MPS.

## The gate this must pass

The serving fallback is `NDVI < 0.35` — a fixed cut. A model that merely reproduces that has learned
nothing and would earn `CONFIDENCE_TRAINED = 0.88` for relearning a constant, which makes the
confidence a lie. So the ship gate is not "does it score well"; it is **does it beat the fixed
threshold on held-out AOIs**, evaluated against the same anomaly labels.

Held out by AOI, never at random. Random pixel splits leak: adjacent pixels in one scene are almost
the same observation, so a random split reports a score that says nothing about a new location.
"""

from __future__ import annotations

import copy
import pathlib
import sys
import time

import numpy as np
import torch
import torch.nn.functional as F

sys.path.insert(0, "/Users/lionelorishane/Documents/GitHub/prodCreditChekB2B/c3hkth-team7/backend")
from app.ml.models import CropStressNet  # noqa: E402

DATA = pathlib.Path("/tmp/crop_ds/crop_stress.npz")
OUT = pathlib.Path(
    "/Users/lionelorishane/Documents/GitHub/prodCreditChekB2B/c3hkth-team7/backend/app/ml/weights/crop_stress.pt"
)

# Held out by GEOGRAPHY, spanning the gradient: far-north Sahel, middle belt, and southern delta.
# A model that works on all three is one that generalises across agro-ecological zones.
TEST_AOIS = {"kano", "lokoja", "calabar"}
VAL_AOIS = {"zaria", "ilorin"}

# The incumbent this has to beat. Applied to the NDVI channel, which is x[:, 0].
HEURISTIC_NDVI_CUT = 0.35


def metrics(pred: np.ndarray, truth: np.ndarray, valid: np.ndarray) -> dict:
    p = (pred > 0.5).astype("float32") * valid
    t = truth * valid
    tp = float((p * t).sum())
    fp = float((p * (1 - t) * valid).sum())
    fn = float(((1 - p) * t * valid).sum())
    iou = tp / max(tp + fp + fn, 1.0)
    precision = tp / max(tp + fp, 1.0)
    recall = tp / max(tp + fn, 1.0)
    f1 = 2 * precision * recall / max(precision + recall, 1e-9)
    return {"iou": iou, "f1": f1, "precision": precision, "recall": recall}


def main() -> int:
    if not DATA.exists():
        print(f"missing {DATA} — run build_crop_dataset.py first")
        return 2

    blob = np.load(DATA, allow_pickle=False)
    x, y, v, aoi = blob["x"], blob["y"], blob["v"], blob["aoi"]
    print(f"dataset {x.shape}  scenes={len(aoi)}  AOIs={len(set(aoi.tolist()))}")

    def mask_for(names: set[str]) -> np.ndarray:
        return np.array([a in names for a in aoi])

    test_m = mask_for(TEST_AOIS)
    val_m = mask_for(VAL_AOIS)
    train_m = ~(test_m | val_m)

    for label, m in (("train", train_m), ("val", val_m), ("test", test_m)):
        print(f"  {label:5} {int(m.sum()):3} scenes  {sorted(set(aoi[m].tolist()))}")

    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    print(f"device: {device}")

    def tensors(m):
        return (
            torch.from_numpy(x[m]).to(device),
            torch.from_numpy(y[m]).to(device),
            torch.from_numpy(v[m]).to(device),
        )

    xt, yt, vt = tensors(train_m)
    xv, yv, vv = tensors(val_m)
    xs, ys, vs = tensors(test_m)

    rate = float((yt * vt).sum() / vt.sum().clamp(min=1))
    pos_weight = min(max((1 - rate) / max(rate, 1e-6), 1.0), 20.0)
    print(f"  train positive rate {rate:.4f} -> pos_weight {pos_weight:.2f}")

    model = CropStressNet().to(device)
    opt = torch.optim.Adam(model.parameters(), lr=3e-3)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=60)

    best_iou, best_state, best_epoch = -1.0, None, -1
    started = time.perf_counter()
    batch = 16

    for epoch in range(1, 61):
        model.train()
        order = torch.randperm(xt.shape[0], device=device)
        running = 0.0
        for i in range(0, xt.shape[0], batch):
            idx = order[i : i + batch]
            xb, yb, vb = xt[idx], yt[idx], vt[idx]
            opt.zero_grad()
            logits = model(xb)
            weight = torch.where(yb > 0.5, torch.full_like(yb, pos_weight), torch.ones_like(yb))
            loss = F.binary_cross_entropy_with_logits(
                logits, yb, weight=weight, reduction="none"
            )
            loss = (loss * vb).sum() / vb.sum().clamp(min=1)
            loss.backward()
            opt.step()
            running += float(loss.detach())
        sched.step()

        model.eval()
        with torch.no_grad():
            pv = torch.sigmoid(model(xv)).cpu().numpy()
        m = metrics(pv, yv.cpu().numpy(), vv.cpu().numpy())
        flag = ""
        if m["iou"] > best_iou:
            best_iou, best_epoch = m["iou"], epoch
            best_state = copy.deepcopy(
                {k: t.detach().cpu() for k, t in model.state_dict().items()}
            )
            flag = "  <- best"
        if epoch % 5 == 0 or flag:
            print(
                f"  epoch {epoch:2}  loss {running / max(1, xt.shape[0] // batch):.4f}  "
                f"val IoU {m['iou']:.4f}  F1 {m['f1']:.4f}{flag}"
            )

    print(f"\ntrained in {time.perf_counter() - started:.0f}s; best epoch {best_epoch} IoU {best_iou:.4f}")

    model.load_state_dict(best_state)
    model.to(device).eval()

    print("\n=== SHIP GATE: held-out AOIs — trained vs the NDVI<0.35 heuristic ===")
    with torch.no_grad():
        pt = torch.sigmoid(model(xs)).cpu().numpy()
    truth, validity = ys.cpu().numpy(), vs.cpu().numpy()
    trained = metrics(pt, truth, validity)

    # The incumbent, on exactly the same pixels.
    ndvi_channel = x[test_m][:, 0:1]
    heuristic_pred = (ndvi_channel < HEURISTIC_NDVI_CUT).astype("float32")
    heuristic = metrics(heuristic_pred, truth, validity)

    print(f"  trained    IoU {trained['iou']:.4f}  F1 {trained['f1']:.4f}  P {trained['precision']:.4f}  R {trained['recall']:.4f}")
    print(f"  heuristic  IoU {heuristic['iou']:.4f}  F1 {heuristic['f1']:.4f}  P {heuristic['precision']:.4f}  R {heuristic['recall']:.4f}")

    ship = trained["iou"] > heuristic["iou"]
    print(
        f"\n  GATE: {'PASS — beats the fixed threshold on unseen AOIs' if ship else 'FAIL — do NOT ship; it has only relearned the threshold'}"
    )

    if ship:
        torch.save(best_state, OUT)
        print(f"  wrote {OUT} ({OUT.stat().st_size / 1e3:.1f} KB, CPU tensors)")
    return 0 if ship else 1


if __name__ == "__main__":
    raise SystemExit(main())
