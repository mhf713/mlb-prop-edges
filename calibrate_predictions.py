#!/usr/bin/env python3
"""
Fit a Platt-scaling calibration layer from accumulated graded results.

Why: the regression analysis showed the model's raw predicted P(over 1.5) is
~27 points more optimistic than reality (Brier score 0.32, worse than random
guessing). This script learns a one-time transform that pulls each future raw
prediction back toward the observed hit rate at that prediction level.

The transform is:
    calibrated_logit = a + b * raw_logit
    calibrated_p     = sigmoid(calibrated_logit)
where (a, b) are fit by maximum likelihood (Newton-Raphson) against the historical
(raw_p_over_15, actual_over_15) pairs from results/*.json.

Output: writes calibration_params.json to repo root with:
    a, b           — fitted coefficients
    raw_brier      — Brier score on raw predictions
    cal_brier      — Brier score after calibration (lower = better)
    raw_log_loss   — log loss before
    cal_log_loss   — log loss after
    n_train        — number of (pred, outcome) pairs used to fit
    fitted_at_utc  — timestamp

daily_email.py loads this file on each run and applies the calibration to every
projected P(O1.5) and P(O2.5) before ranking and email composition. Re-run this
script weekly (or after big slate days) via the 'Calibrate Predictions' workflow.

Run from repo root:
    python calibrate_predictions.py
"""
import datetime as dt
import json
import math
import os
import sys

RESULTS_DIR = "results"
OUT_FILE = "calibration_params.json"
EPS = 1e-9


def sigmoid(x):
    """Numerically stable sigmoid."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def logit(p):
    p = max(EPS, min(1 - EPS, p))
    return math.log(p / (1 - p))


def fit_platt(raw_ps, outcomes, max_iter=200, tol=1e-7):
    """Fit calibrated_p = sigmoid(a + b * logit(raw_p)) via Newton-Raphson MLE.

    Returns dict with a, b, converged, n_iters, neg_log_likelihood.
    Initialization a=0, b=1 corresponds to the identity transform.
    """
    xs = [logit(p) for p in raw_ps]
    ys = [1.0 if o else 0.0 for o in outcomes]
    n = len(xs)
    if n == 0:
        return None
    # Quick sanity: if all outcomes are identical, the MLE diverges. Bail safely.
    sum_y = sum(ys)
    if sum_y == 0 or sum_y == n:
        return {"a": 0.0, "b": 1.0, "converged": False,
                "reason": "degenerate outcomes (all 0 or all 1)", "n_iters": 0}

    a, b = 0.0, 1.0
    for it in range(max_iter):
        g_a = g_b = 0.0
        h_aa = h_ab = h_bb = 0.0
        for x, y in zip(xs, ys):
            p = sigmoid(a + b * x)
            err = y - p
            w = p * (1 - p)
            g_a += err
            g_b += err * x
            h_aa -= w
            h_ab -= w * x
            h_bb -= w * x * x
        # 2x2 inverse of (negative-definite) Hessian
        det = h_aa * h_bb - h_ab * h_ab
        if abs(det) < 1e-15:
            return {"a": a, "b": b, "converged": False,
                    "reason": "Hessian near-singular", "n_iters": it}
        inv_aa = h_bb / det
        inv_ab = -h_ab / det
        inv_bb = h_aa / det
        # Newton step for maximization: theta_new = theta - H^{-1} g
        da = -(inv_aa * g_a + inv_ab * g_b)
        db = -(inv_ab * g_a + inv_bb * g_b)
        a += da
        b += db
        if abs(da) < tol and abs(db) < tol:
            return {"a": a, "b": b, "converged": True, "n_iters": it + 1}
    return {"a": a, "b": b, "converged": False,
            "reason": f"hit max_iter ({max_iter})", "n_iters": max_iter}


def brier(ps, ys):
    if not ps: return None
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps)


def log_loss(ps, ys):
    if not ps: return None
    total = 0.0
    for p, y in zip(ps, ys):
        p = max(EPS, min(1 - EPS, p))
        total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return total / len(ps)


def load_pairs(results_dir=RESULTS_DIR):
    """Walk results/*.json and yield (raw_p, outcome) for each started pick that
    has a p_over_15 value."""
    if not os.path.isdir(results_dir):
        return [], []
    raws, outs = [], []
    for fn in sorted(os.listdir(results_dir)):
        if not fn.endswith(".json"): continue
        try:
            with open(os.path.join(results_dir, fn)) as f:
                d = json.load(f)
        except Exception:
            continue
        for r in d.get("results", []):
            if not r.get("started"):
                continue
            raw = r.get("p_over_15_raw") or r.get("p_over_15")
            outcome = r.get("over_15")
            if raw is None or outcome is None:
                continue
            raws.append(float(raw))
            outs.append(bool(outcome))
    return raws, outs


def main():
    print("=" * 78)
    print("PLATT-SCALING CALIBRATION FIT")
    print("=" * 78)
    raws, outs = load_pairs()
    n = len(raws)
    if n == 0:
        print(f"ERROR: no graded picks with p_over_15 found in {RESULTS_DIR}/", file=sys.stderr)
        sys.exit(2)
    if n < 100:
        print(f"WARN: only {n} training pairs. Fit will be noisy; consider waiting for more data.")

    print(f"\nLoaded {n} (predicted_P, actual_outcome) pairs from results/")

    # Pre-fit diagnostics
    ys = [1.0 if o else 0.0 for o in outs]
    raw_brier = brier(raws, ys)
    raw_ll = log_loss(raws, ys)
    actual_rate = sum(ys) / n
    pred_mean = sum(raws) / n
    print(f"\nBEFORE calibration:")
    print(f"  Mean predicted P: {pred_mean*100:.1f}%")
    print(f"  Actual hit rate:  {actual_rate*100:.1f}%")
    print(f"  Bias:             {(pred_mean-actual_rate)*100:+.1f}%")
    print(f"  Brier score:      {raw_brier:.4f}  (lower better; 0.25 = random)")
    print(f"  Log loss:         {raw_ll:.4f}  (0.693 = always-50/50 baseline)")

    # Fit
    print(f"\nFitting Platt scaling (Newton-Raphson)...")
    result = fit_platt(raws, outs)
    if result is None:
        print("ERROR: fit returned None", file=sys.stderr)
        sys.exit(2)
    a = result["a"]
    b = result["b"]
    print(f"  a = {a:+.4f}")
    print(f"  b = {b:+.4f}")
    print(f"  converged: {result.get('converged')}  ({result.get('n_iters')} iterations)")
    if not result.get("converged"):
        print(f"  note: {result.get('reason', 'did not converge')}")

    # Post-fit diagnostics
    cal_ps = [sigmoid(a + b * logit(p)) for p in raws]
    cal_brier = brier(cal_ps, ys)
    cal_ll = log_loss(cal_ps, ys)
    cal_pred_mean = sum(cal_ps) / n
    print(f"\nAFTER calibration:")
    print(f"  Mean predicted P: {cal_pred_mean*100:.1f}%  (target: {actual_rate*100:.1f}%)")
    print(f"  Brier score:      {cal_brier:.4f}  (was {raw_brier:.4f}, delta {(cal_brier-raw_brier):+.4f})")
    print(f"  Log loss:         {cal_ll:.4f}  (was {raw_ll:.4f}, delta {(cal_ll-raw_ll):+.4f})")
    improved = cal_brier < raw_brier and cal_ll < raw_ll
    if improved:
        print(f"  ✓ improvement on both metrics — calibration is helping")
    else:
        print(f"  ⚠ no improvement — either fit failed or data was already well-calibrated")

    # Show what the transform does at a few example raw probabilities
    print(f"\nExample transformations:")
    print(f"  raw P → calibrated P")
    for raw in [0.30, 0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]:
        cal = sigmoid(a + b * logit(raw))
        arrow = "↓" if cal < raw else "↑" if cal > raw else "="
        print(f"  {raw*100:>5.1f}%  →  {cal*100:>5.1f}%  {arrow}")

    # Write params
    out = {
        "a": a,
        "b": b,
        "converged": result.get("converged", False),
        "n_iters": result.get("n_iters"),
        "n_train": n,
        "raw_brier": raw_brier,
        "cal_brier": cal_brier,
        "raw_log_loss": raw_ll,
        "cal_log_loss": cal_ll,
        "raw_predicted_mean": pred_mean,
        "actual_rate": actual_rate,
        "cal_predicted_mean": cal_pred_mean,
        "fitted_at_utc": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote {OUT_FILE}")
    print(f"\nNext daily run will apply this calibration to every P(O1.5) and P(O2.5)\n"
          f"before computing edge and ranking picks.")


if __name__ == "__main__":
    main()
