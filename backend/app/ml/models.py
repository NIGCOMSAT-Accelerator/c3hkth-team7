"""PyTorch model definitions.

Two small networks, both deliberately shallow enough to run on CPU inside the
container — a hackathon deployment should not require a GPU to produce a real
alert, and inference latency is dominated by the COG reads anyway.

`SARFloodUNet`  — semantic segmentation of standing water from Sentinel-1.
`CropStressNet` — per-pixel crop-stress probability from optical indices.

Both fall back to a documented physical heuristic when no trained weights are
present, so the pipeline degrades to "defensible threshold science" rather than
to nothing. `AnalystResult.confidence` reports which path ran.
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F


def _conv_block(in_ch: int, out_ch: int) -> nn.Sequential:
    """Conv-BN-ReLU twice. BatchNorm matters here because Sentinel-1 backscatter
    varies by tens of dB between scenes and orbits."""
    return nn.Sequential(
        nn.Conv2d(in_ch, out_ch, kernel_size=3, padding=1, bias=False),
        nn.BatchNorm2d(out_ch),
        nn.ReLU(inplace=True),
        nn.Conv2d(out_ch, out_ch, kernel_size=3, padding=1, bias=False),
        nn.BatchNorm2d(out_ch),
        nn.ReLU(inplace=True),
    )


class SARFloodUNet(nn.Module):
    """Compact U-Net for flood segmentation from Sentinel-1 VV/VH.

    Input : (B, 2, H, W) — VV and VH in decibels, standardised.
    Output: (B, 1, H, W) — water logits. Apply sigmoid for probability.

    Two channels rather than one because VV alone confuses smooth water with
    smooth dry surfaces (tarmac, dry sand); the VV/VH ratio separates them.
    """

    def __init__(self, in_channels: int = 2, base: int = 16) -> None:
        super().__init__()
        self.enc1 = _conv_block(in_channels, base)
        self.enc2 = _conv_block(base, base * 2)
        self.enc3 = _conv_block(base * 2, base * 4)
        self.bottleneck = _conv_block(base * 4, base * 8)

        self.up3 = nn.ConvTranspose2d(base * 8, base * 4, kernel_size=2, stride=2)
        self.dec3 = _conv_block(base * 8, base * 4)
        self.up2 = nn.ConvTranspose2d(base * 4, base * 2, kernel_size=2, stride=2)
        self.dec2 = _conv_block(base * 4, base * 2)
        self.up1 = nn.ConvTranspose2d(base * 2, base, kernel_size=2, stride=2)
        self.dec1 = _conv_block(base * 2, base)

        self.head = nn.Conv2d(base, 1, kernel_size=1)
        self.pool = nn.MaxPool2d(2)

    @staticmethod
    def _pad_to_match(x: torch.Tensor, ref: torch.Tensor) -> torch.Tensor:
        """Odd input dimensions leave the upsampled tensor a pixel short."""
        dh = ref.shape[-2] - x.shape[-2]
        dw = ref.shape[-1] - x.shape[-1]
        if dh or dw:
            x = F.pad(x, [dw // 2, dw - dw // 2, dh // 2, dh - dh // 2])
        return x

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        e1 = self.enc1(x)
        e2 = self.enc2(self.pool(e1))
        e3 = self.enc3(self.pool(e2))
        b = self.bottleneck(self.pool(e3))

        d3 = self.dec3(torch.cat([self._pad_to_match(self.up3(b), e3), e3], dim=1))
        d2 = self.dec2(torch.cat([self._pad_to_match(self.up2(d3), e2), e2], dim=1))
        d1 = self.dec1(torch.cat([self._pad_to_match(self.up1(d2), e1), e1], dim=1))
        return self.head(d1)


class CropStressNet(nn.Module):
    """Per-pixel crop-stress classifier over stacked optical indices.

    Input : (B, C, H, W) — NDVI, NDMI, NDWI, **NDVI anomaly** (C=4).
    Output: (B, 1, H, W) — stress logits.

    1x1 convolutions only: this is a learned per-pixel decision boundary over
    index space, not a spatial-context model. That is the right inductive bias
    here — stress is a property of the pixel's spectral signature, and it keeps
    the parameter count low enough to train on the small labelled sets that
    exist for Nigerian smallholder plots.

    ## Why there is a FOURTH channel, and why the model was capped at 0.37 precision without it

    "Stressed" means *below what this location normally shows at this time of year* — that is what
    `stats/anomaly.py` computes and what the training labels encode. But the first version received
    only the three absolute indices, so it could not see the baseline it was being asked to compare
    against. Measured on the training set:

        NDVI where labelled STRESSED : mean 0.123
        NDVI where labelled NOT      : mean 0.371     <- heavily overlapping

    Two pixels with identical NDVI carry opposite labels in different AOIs, so precision was capped
    by construction at 0.37. More data could not have fixed it. Adding the deviation-from-baseline as
    an input separates the classes almost completely:

        anomaly < -0.2   ->  98.2% stressed
        anomaly >  0.0   ->   0.0% stressed

    The channel is `ndvi - seasonal_baseline(day_of_year)`, and it is **0.0 when no baseline exists**
    — which is the honest neutral value, not a missing-data sentinel. A new AOI with no history
    therefore degrades to roughly the 3-channel behaviour rather than failing, and improves as
    `index_history` accumulates.
    """

    def __init__(self, in_channels: int = 4, hidden: int = 32) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv2d(in_channels, hidden, kernel_size=1),
            nn.BatchNorm2d(hidden),
            nn.ReLU(inplace=True),
            nn.Conv2d(hidden, hidden, kernel_size=1),
            nn.BatchNorm2d(hidden),
            nn.ReLU(inplace=True),
            nn.Conv2d(hidden, 1, kernel_size=1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)
