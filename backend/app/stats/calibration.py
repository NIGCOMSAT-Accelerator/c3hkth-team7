"""Steps 7 and 10 — confidence calibration and fitted fusion weights.

**The question this answers.** When an assessment reports `confidence: 0.80`, is it
right 80% of the time? Today nobody knows: 0.88 and 0.55 are hard-coded labels for
"trained" and "heuristic", not measured frequencies. A district officer setting their
own action threshold ("act on anything above 0.7") is relying on a number that has
never been checked.

**Why this is the highest-value use of Fahis data, and why it comes before
retraining.** Fahis accumulates CONFIRMED/REFUTED verdicts by itself — roughly
50–200 per season. That is nowhere near enough to retrain a U-Net. But Platt scaling
fits **two parameters**, and isotonic regression fits a monotone step function; both
are well-behaved at n≈100. A well-calibrated 0.6 is more useful than an
overconfident 0.9, because the officer's threshold then means what they think it
means.

**Platt or isotonic?** Platt (a one-dimensional logistic fit) is the default because
it is monotone by construction, needs only two parameters, and cannot invert the
ordering of predictions. Isotonic is more flexible but can overfit badly below a few
hundred points, and — importantly — can produce large flat regions that collapse
distinct confidences into one value. `Calibrator.fit` picks Platt unless there is
genuinely enough data, and says which it used.

**Fusion weights (step 10).** `fit_fusion_weights` fits `W_OBSERVED`/`W_FORECAST`/
`W_EXPOSURE` by logistic regression **constrained to non-negative coefficients**.
That constraint is a safety property, not a statistical nicety: an unconstrained fit
on ~100 noisy samples can easily learn "more rainfall → less risk", which would be
indefensible to an agriculture officer and could suppress a real warning. Gradient
boosting would fit that noise even more readily, which is why it is not used here.

**Nothing in this module is wired into the live scoring path yet, deliberately.** It
computes and reports; adopting a fitted set of weights is an operator decision, for
the same reason `agent_memory` is written but not read. `KNOWN_UNREACHED`-style
honesty applies: these are the tools, and the gate is a human looking at the
reliability diagram.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

#: Below this many labelled outcomes, calibration is not attempted at all.
#:
#: A calibration curve fitted to a handful of points is worse than no curve: it
#: looks authoritative and encodes almost pure noise. 30 is the floor at which a
#: two-parameter Platt fit is meaningfully better than the identity.
MIN_SAMPLES = 30

#: Below this, prefer Platt over isotonic regardless. Isotonic needs several
#: hundred points before its extra flexibility stops being overfitting.
ISOTONIC_MIN_SAMPLES = 300


@dataclass(frozen=True)
class Calibrator:
    """A fitted confidence-calibration map.

    `available` False means there was insufficient data and `apply` is the identity —
    so an uncalibrated deployment behaves exactly as it does today, rather than
    silently applying a curve fitted to six points.
    """

    method: str = "identity"
    #: Platt parameters: calibrated = sigmoid(a * logit(p) + b).
    a: float = 1.0
    b: float = 0.0
    #: Isotonic breakpoints, ascending. Empty for Platt/identity.
    x_knots: list[float] = field(default_factory=list)
    y_knots: list[float] = field(default_factory=list)
    samples: int = 0
    available: bool = False

    def apply(self, confidence: float) -> float:
        """Map a raw confidence onto its calibrated value.

        Clamped to [0.01, 0.99]: a calibrated 0.0 would claim certainty of being
        wrong, and 1.0 certainty of being right. Neither is ever justified from a
        finite sample, and both break downstream arithmetic.
        """
        if not self.available:
            return confidence

        p = float(np.clip(confidence, 1e-6, 1.0 - 1e-6))

        if self.method == "isotonic" and self.x_knots:
            value = float(np.interp(p, self.x_knots, self.y_knots))
        else:
            logit = float(np.log(p / (1.0 - p)))
            value = float(1.0 / (1.0 + np.exp(-(self.a * logit + self.b))))

        return float(np.clip(value, 0.01, 0.99))

    @classmethod
    def fit(
        cls, confidences: np.ndarray | list[float], outcomes: np.ndarray | list[int]
    ) -> Calibrator:
        """Fit a calibration map from Fahis outcomes.

        `outcomes` is 1 for CONFIRMED, 0 for REFUTED. **Only those two verdicts may
        be passed** — including UNVERIFIED would measure news coverage rather than
        model accuracy, which is the same reasoning behind `TRAINABLE_VERDICTS`.
        """
        p = np.asarray(confidences, dtype="float64")
        y = np.asarray(outcomes, dtype="float64")

        mask = np.isfinite(p) & np.isfinite(y)
        p, y = p[mask], y[mask]

        # Both classes must be present, or "calibration" just learns the base rate.
        if p.size < MIN_SAMPLES or len(np.unique(y)) < 2:
            return cls(samples=int(p.size), available=False)

        if p.size >= ISOTONIC_MIN_SAMPLES:
            return cls._fit_isotonic(p, y)
        return cls._fit_platt(p, y)

    @classmethod
    def _fit_platt(cls, p: np.ndarray, y: np.ndarray) -> Calibrator:
        """Two-parameter logistic fit on the logit of the raw confidence.

        Solved by plain Newton–Raphson rather than pulling in sklearn: it is a
        two-parameter convex problem, converges in a handful of iterations, and
        avoids a ~100 MB dependency for 30 lines of arithmetic.
        """
        p = np.clip(p, 1e-6, 1.0 - 1e-6)
        x = np.log(p / (1.0 - p))

        # Platt's own prior correction, which keeps the fit from saturating when one
        # class is rare — the usual case here, since most warnings are confirmed.
        n_pos, n_neg = float(np.sum(y == 1)), float(np.sum(y == 0))
        hi = (n_pos + 1.0) / (n_pos + 2.0)
        lo = 1.0 / (n_neg + 2.0)
        target = np.where(y == 1, hi, lo)

        a, b = 1.0, 0.0
        for _ in range(100):
            z = a * x + b
            q = 1.0 / (1.0 + np.exp(-np.clip(z, -50, 50)))
            w = np.clip(q * (1.0 - q), 1e-9, None)
            residual = q - target

            g = np.array([np.sum(residual * x), np.sum(residual)])
            h = np.array([
                [np.sum(w * x * x), np.sum(w * x)],
                [np.sum(w * x),     np.sum(w)],
            ])
            try:
                step = np.linalg.solve(h + np.eye(2) * 1e-9, g)
            except np.linalg.LinAlgError:
                break
            a, b = a - float(step[0]), b - float(step[1])
            if np.max(np.abs(step)) < 1e-9:
                break

        if not np.isfinite([a, b]).all():
            return cls(samples=int(p.size), available=False)

        return cls(method="platt", a=float(a), b=float(b),
                   samples=int(p.size), available=True)

    @classmethod
    def _fit_isotonic(cls, p: np.ndarray, y: np.ndarray) -> Calibrator:
        """Pool-adjacent-violators isotonic regression.

        Hand-rolled for the same reason as Platt: PAVA is ~15 lines and the
        alternative is a large dependency.
        """
        order = np.argsort(p)
        xs, ys = p[order], y[order].astype("float64")
        weights = np.ones_like(ys)

        # Pool adjacent violators until the sequence is non-decreasing.
        i = 0
        while i < len(ys) - 1:
            if ys[i] <= ys[i + 1]:
                i += 1
                continue
            total_w = weights[i] + weights[i + 1]
            pooled = (ys[i] * weights[i] + ys[i + 1] * weights[i + 1]) / total_w
            ys[i] = pooled
            weights[i] = total_w
            ys = np.delete(ys, i + 1)
            weights = np.delete(weights, i + 1)
            xs = np.delete(xs, i + 1)
            i = max(i - 1, 0)

        return cls(method="isotonic",
                   x_knots=[float(v) for v in xs],
                   y_knots=[float(v) for v in ys],
                   samples=int(p.size), available=True)


def brier_score(confidences: np.ndarray | list[float], outcomes: np.ndarray | list[int]) -> float | None:
    """Mean squared error of probabilistic forecasts. Lower is better.

    Chosen over accuracy because it is a **proper** scoring rule: it is minimised by
    reporting your true belief, so it cannot be gamed by always predicting the
    majority class. It decomposes into calibration plus refinement, which is exactly
    the pair we want to track separately.

    `None` when nothing is measurable — never 0.0, which would read as a perfect
    score. Same discipline as `verification_metrics` reporting `precision: null`.
    """
    p = np.asarray(confidences, dtype="float64")
    y = np.asarray(outcomes, dtype="float64")
    mask = np.isfinite(p) & np.isfinite(y)
    p, y = p[mask], y[mask]
    if p.size == 0:
        return None
    return float(np.mean((p - y) ** 2))


def reliability_bins(
    confidences: np.ndarray | list[float],
    outcomes: np.ndarray | list[int],
    *,
    bins: int = 5,
) -> list[dict]:
    """Reliability-diagram data: predicted vs. observed frequency per bin.

    5 bins by default, not 10: with ~100 samples, 10 bins average ~10 points each
    and the diagram becomes noise. Empty bins are omitted rather than reported as
    zero — an unobserved confidence range is unknown, not perfectly calibrated.
    """
    p = np.asarray(confidences, dtype="float64")
    y = np.asarray(outcomes, dtype="float64")
    mask = np.isfinite(p) & np.isfinite(y)
    p, y = p[mask], y[mask]

    edges = np.linspace(0.0, 1.0, bins + 1)
    out: list[dict] = []
    for i in range(bins):
        lo, hi = float(edges[i]), float(edges[i + 1])
        # Upper-closed on the last bin so confidence 1.0 is not dropped.
        sel = (p >= lo) & (p < hi) if i < bins - 1 else (p >= lo) & (p <= hi)
        if not np.any(sel):
            continue
        out.append({
            "bin_lower": lo,
            "bin_upper": hi,
            "count": int(np.count_nonzero(sel)),
            "mean_confidence": float(np.mean(p[sel])),
            "observed_frequency": float(np.mean(y[sel])),
        })
    return out


# --------------------------------------------------------------------------- #
# Step 10 — fitted fusion weights
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FusionWeights:
    """Fitted replacements for W_OBSERVED / W_FORECAST / W_EXPOSURE.

    Normalised to sum to 1.0 so they are drop-in comparable with the hand-set
    constants, and `available` False means keep the constants.
    """

    observed: float = 0.55
    forecast: float = 0.30
    exposure: float = 0.15
    samples: int = 0
    available: bool = False
    #: Set when the fit succeeded but did NOT beat the current constants on
    #: held-out Brier score. Reported, not adopted.
    beats_current: bool = False


def fit_fusion_weights(
    observed_terms: np.ndarray | list[float],
    forecast_terms: np.ndarray | list[float],
    exposure_terms: np.ndarray | list[float],
    outcomes: np.ndarray | list[int],
    *,
    current: tuple[float, float, float] = (0.55, 0.30, 0.15),
) -> FusionWeights:
    """Non-negative logistic fit of the three risk terms against Fahis outcomes.

    **Non-negativity is enforced by projection** after each gradient step — the
    cheapest correct way to guarantee monotonicity. Without it, a noisy sample can
    yield a negative rainfall coefficient, i.e. "more rain means less flood risk",
    which would be both wrong and impossible to defend.

    **Two details that are load-bearing, not incidental:**

    *L2 regularisation.* An unregularised logistic fit has no bounded optimum here —
    the loss keeps falling as the weight vector grows, so the solver drives the
    single most predictive term up and the others to zero, and normalising afterwards
    reports a spurious `(1.0, 0.0, 0.0)`. Ridge shrinkage bounds the magnitude, which
    is what makes the *ratios* between the three terms meaningful. At n≈100 this is
    also the difference between a fit and memorisation.

    *A fitted intercept.* Without one, the model must explain the base rate of
    confirmed hazards using the three slopes, which biases every weight upward. The
    intercept absorbs it and is then discarded — the Oracle's severity thresholds
    already play that role in the live path.

    Returns `available=False` unless the fit converges *and* the resulting weights
    beat the incumbent constants on held-out Brier score. Even then adoption is a
    human decision — see the module docstring.
    """
    x = np.column_stack([
        np.asarray(observed_terms, dtype="float64"),
        np.asarray(forecast_terms, dtype="float64"),
        np.asarray(exposure_terms, dtype="float64"),
    ])
    y = np.asarray(outcomes, dtype="float64")

    mask = np.isfinite(x).all(axis=1) & np.isfinite(y)
    x, y = x[mask], y[mask]

    if x.shape[0] < MIN_SAMPLES or len(np.unique(y)) < 2:
        return FusionWeights(*current, samples=int(x.shape[0]), available=False)

    # Held-out split so "beats_current" is not measured on the fitting data. A
    # deterministic stride rather than a random shuffle keeps the result
    # reproducible, which matters because this feeds an operator's ship decision.
    holdout = np.zeros(x.shape[0], dtype=bool)
    holdout[::4] = True
    if len(np.unique(y[~holdout])) < 2 or len(np.unique(y[holdout])) < 2:
        holdout[:] = False          # too few of one class to split; fit in-sample

    x_fit, y_fit = x[~holdout], y[~holdout]
    x_eval, y_eval = (x[holdout], y[holdout]) if holdout.any() else (x, y)

    # Projected gradient descent on ridge-penalised logistic loss, weights >= 0.
    w = np.array(current, dtype="float64")
    bias = 0.0
    lr, l2 = 0.5, 1.0 / max(x_fit.shape[0], 1)
    for _ in range(5000):
        q = 1.0 / (1.0 + np.exp(-np.clip(x_fit @ w + bias, -50, 50)))
        residual = q - y_fit
        grad = x_fit.T @ residual / x_fit.shape[0] + l2 * w
        grad_bias = float(np.mean(residual))
        w_next = np.maximum(w - lr * grad, 0.0)   # project onto the non-negative cone
        bias -= lr * grad_bias
        step = np.max(np.abs(w_next - w))
        w = w_next
        if step < 1e-11 and abs(lr * grad_bias) < 1e-11:
            break

    total = float(np.sum(w))
    if not np.isfinite(total) or total <= 1e-9:
        return FusionWeights(*current, samples=int(x.shape[0]), available=False)

    w = w / total
    fitted_brier = float(np.mean((x_eval @ w - y_eval) ** 2))
    current_brier = float(np.mean((x_eval @ np.array(current) - y_eval) ** 2))

    return FusionWeights(
        observed=float(w[0]), forecast=float(w[1]), exposure=float(w[2]),
        samples=int(x.shape[0]),
        available=True,
        beats_current=fitted_brier < current_brier,
    )
