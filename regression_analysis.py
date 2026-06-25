#!/usr/bin/env python3
"""
Inferential analysis of the model — z-tests, p-values, confidence intervals.

Where factor_calibration.py shows you raw buckets and rates, this script tells
you which factors are STATISTICALLY DISTINGUISHABLE from noise at your current
sample size.

For each factor multiplier, runs a two-proportion z-test comparing actual hit
rate when the factor was a BOOST (mult > 1.05) vs a DRAG (mult < 0.95):
  - p < 0.05 = the factor's effect is real and detectable
  - p > 0.05 = could be a small real effect or pure noise — can't tell yet

Also reports:
  - Brier score and log loss (overall model quality)
  - Overall bias (is the model systematically over- or under-confident?)
  - Point-biserial correlations between continuous factors and outcomes
  - Ranked list of factors by effect size, restricted to significant ones

Pure stdlib — no scipy/sklearn needed.

Run from repo root:
    python regression_analysis.py
or trigger 'Regression Analysis' workflow from the Actions tab.
"""
import json
import math
import os
import sys
from collections import defaultdict

RESULTS_DIR = "results"


# ====================== STATISTICS PRIMITIVES ======================

def normal_cdf(x):
    """Standard-normal cumulative distribution function via math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def two_tailed_p(z):
    """Two-tailed p-value for a standard-normal z-statistic."""
    return 2.0 * (1.0 - normal_cdf(abs(z)))


def two_prop_z_test(wins1, n1, wins2, n2):
    """Two-proportion z-test. Returns dict with lift, CI on the lift, z, p-value.
    Tests H0: p1 = p2 vs H1: p1 != p2. Uses pooled SE for the test, unpooled SE
    for the CI on the difference (standard practice)."""
    if n1 == 0 or n2 == 0:
        return None
    p1 = wins1 / n1
    p2 = wins2 / n2
    lift = p1 - p2
    p_pool = (wins1 + wins2) / (n1 + n2)
    se_pool = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
    z = lift / se_pool if se_pool > 0 else 0.0
    p = two_tailed_p(z)
    se_unpool = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    return {
        "lift": lift, "p1": p1, "p2": p2, "n1": n1, "n2": n2,
        "ci_lo": lift - 1.96 * se_unpool, "ci_hi": lift + 1.96 * se_unpool,
        "z": z, "p_value": p,
    }


def brier_score(picks):
    """Mean squared error between predicted P and binary outcome (0 or 1).
    Perfect = 0, random = 0.25. Always non-negative."""
    n = 0; total = 0.0
    for p in picks:
        pred = p.get("p_over_15")
        if pred is None: continue
        actual = 1 if p.get("over_15") else 0
        total += (pred - actual) ** 2
        n += 1
    return total / n if n else None


def log_loss(picks):
    """Cross-entropy (negative log-likelihood). Penalizes confident wrong picks
    heavily. Random uniform predictions = 0.693 (ln 2). Lower is better."""
    n = 0; total = 0.0
    EPS = 1e-12
    for p in picks:
        pred = p.get("p_over_15")
        if pred is None: continue
        actual = 1 if p.get("over_15") else 0
        pred = max(EPS, min(1 - EPS, pred))
        total += -(actual * math.log(pred) + (1 - actual) * math.log(1 - pred))
        n += 1
    return total / n if n else None


def point_biserial_correlation(picks, key):
    """Correlation between a continuous factor and the binary outcome (over_15).
    Same as Pearson correlation when one variable is dichotomous. Returns r and
    a p-value (testing r != 0 using a t-statistic approximation)."""
    xs = []; ys = []
    for p in picks:
        v = p.get(key)
        if v is None: continue
        xs.append(float(v))
        ys.append(1.0 if p.get("over_15") else 0.0)
    n = len(xs)
    if n < 10:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx2 = sum((x - mx) ** 2 for x in xs)
    sy2 = sum((y - my) ** 2 for y in ys)
    if sx2 == 0 or sy2 == 0:
        return None
    r = cov / math.sqrt(sx2 * sy2)
    if abs(r) >= 1:
        return {"r": r, "n": n, "t": float("inf"), "p_value": 0.0}
    t = r * math.sqrt((n - 2) / max(1e-12, 1 - r * r))
    p = two_tailed_p(t)
    return {"r": r, "n": n, "t": t, "p_value": p}


# ====================== DATA LOADING ======================

def load_started_picks(results_dir=RESULTS_DIR):
    """Returns (picks, n_days). Each pick is the per-row dict from a results
    file's 'results' list, filtered to those where started == True."""
    if not os.path.isdir(results_dir):
        print(f"ERROR: no '{results_dir}/' directory found in {os.getcwd()}", file=sys.stderr)
        return [], 0
    picks = []
    days = 0
    for fn in sorted(os.listdir(results_dir)):
        if not fn.endswith(".json"): continue
        try:
            with open(os.path.join(results_dir, fn)) as f:
                d = json.load(f)
        except Exception as e:
            print(f"  WARN: could not read {fn}: {e}", file=sys.stderr)
            continue
        days += 1
        for r in d.get("results", []):
            if r.get("started"):
                picks.append(r)
    return picks, days


# ====================== REPORT ======================

def stars(p):
    if p < 0.01: return "***"
    if p < 0.05: return "**"
    if p < 0.10: return "*"
    return "   "


def main():
    print("=" * 84)
    print("MLB PROP MODEL — INFERENTIAL REGRESSION ANALYSIS")
    print("=" * 84)

    picks, n_days = load_started_picks()
    n = len(picks)
    print(f"\nLoaded {n_days} day(s) of graded results, {n} starter-picks total")

    if n < 50:
        print("\n*** WARN: very small sample (N<50). Most tests will not be significant.")
    elif n < 200:
        print(f"\nSample N={n}: enough for headline analysis; per-factor tests will only")
        print("detect large effects (~8%+ lifts).")
    elif n < 500:
        print(f"\nSample N={n}: good for headline tests; per-factor tests can detect 5%+")
        print("lifts on factors with broad bucket coverage.")
    else:
        print(f"\nSample N={n}: solid sample. Per-factor tests can detect ~3% effects.")

    n_with_factors = sum(1 for p in picks if p.get("hf") is not None)
    if n_with_factors < n:
        print(f"\nNote: {n - n_with_factors} of {n} picks were saved BEFORE the factor-multiplier")
        print(f"      schema update, so per-factor analysis uses only {n_with_factors} picks.")

    # ============ 1. Overall model quality ============
    print("\n" + "=" * 84)
    print("1. OVERALL MODEL QUALITY")
    print("=" * 84)
    bs = brier_score(picks)
    ll = log_loss(picks)
    wins = sum(1 for p in picks if p.get("over_15"))
    actual = wins / n if n else 0
    pred_mean = sum((p.get("p_over_15") or 0) for p in picks) / n if n else 0
    bias = pred_mean - actual
    bias_se = math.sqrt(actual * (1 - actual) / n) if n > 0 else 0
    bias_z = bias / bias_se if bias_se > 0 else 0
    bias_p = two_tailed_p(bias_z)
    print(f"  Brier score:      {bs:.4f}   (0.0 perfect, 0.25 random — lower is better)")
    print(f"  Log loss:         {ll:.4f}   (0.693 = always-50/50 baseline — lower is better)")
    print(f"  Actual hit rate:    {actual*100:.1f}%   ({wins}/{n})")
    print(f"  Predicted mean:     {pred_mean*100:.1f}%")
    print(f"  Bias (pred-actual): {bias*100:+.1f}%   z={bias_z:.2f}, p={bias_p:.4f}  {stars(bias_p)}")
    if bias_p < 0.05:
        if bias > 0:
            print(f"  → Model is SIGNIFICANTLY OVERCONFIDENT. Predictions should be shrunk")
            print(f"    by ~{bias*100:.1f}% to be calibrated.")
        else:
            print(f"  → Model is SIGNIFICANTLY UNDERCONFIDENT. Real picks are sharper than the model.")
    else:
        print(f"  → Overall calibration is within noise; no significant systematic bias.")

    # ============ 2. Per-factor z-tests ============
    print("\n" + "=" * 84)
    print("2. PER-FACTOR SIGNIFICANCE TESTS  (boost mult > 1.05 vs drag mult < 0.95)")
    print("=" * 84)
    print("Reports lift = boost_rate - drag_rate, with 95% CI and a z-test p-value.")
    print("If lift > 0 and p < 0.05, the factor is helping. If lift ≈ 0 with tight CI,")
    print("the factor is noise. If CI is wide, sample size is just too small to tell.\n")

    factors = [
        ("Handedness (hf)",       "hf",           None,     0),
        ("Park factor",           "park_mult",    None,     0),
        ("Weather",               "weather_mult", None,     0),
        ("Bullpen quality",       "bullpen_mult", None,     0),
        ("BvP (vs pitcher)",      "bvp_mult",     "bvp_pa", 5),
        ("BvT (vs team)",         "bvt_mult",     "bvt_pa", 30),
        ("BvS (at stadium)",      "bvs_mult",     "bvs_pa", 20),
        ("Pitch-arsenal quality", "quality_mult", None,     0),
    ]
    factor_results = []
    for label, mult_key, pa_key, min_pa in factors:
        boost_wins = boost_n = 0
        drag_wins = drag_n = 0
        for p in picks:
            v = p.get(mult_key)
            if v is None: continue
            if pa_key and (p.get(pa_key) or 0) < min_pa: continue
            if v >= 1.05:
                boost_n += 1
                if p.get("over_15"): boost_wins += 1
            elif v <= 0.95:
                drag_n += 1
                if p.get("over_15"): drag_wins += 1
        if boost_n < 10 or drag_n < 10:
            print(f"  {label:<24} insufficient samples (boost N={boost_n}, drag N={drag_n})")
            factor_results.append((label, None))
            continue
        t = two_prop_z_test(boost_wins, boost_n, drag_wins, drag_n)
        verdict = "REAL" if t["p_value"] < 0.05 else "noise so far"
        print(f"  {label:<24}  boost {boost_wins}/{boost_n} ({t['p1']*100:>5.1f}%)  "
              f"drag {drag_wins}/{drag_n} ({t['p2']*100:>5.1f}%)  "
              f"lift {t['lift']*100:+5.1f}%  [{t['ci_lo']*100:+.1f}, {t['ci_hi']*100:+.1f}]  "
              f"p={t['p_value']:.4f} {stars(t['p_value'])} {verdict}")
        factor_results.append((label, t))

    # ============ 3. Ranked-by-effect-size of significant factors ============
    print("\n" + "=" * 84)
    print("3. SIGNIFICANT FACTORS RANKED BY EFFECT SIZE (p < 0.05 only)")
    print("=" * 84)
    sig = [(label, t) for label, t in factor_results if t and t["p_value"] < 0.05]
    sig.sort(key=lambda x: -abs(x[1]["lift"]))
    if not sig:
        print("  No factors are statistically significant at p<0.05 yet.")
        print("  With your current N, undetected real effects could still be ~3-7%.")
        print("  Re-run after another ~30 days of data.")
    else:
        for label, t in sig:
            direction = ("HELPING — boost picks hit more often than drag picks"
                         if t["lift"] > 0 else
                         "INVERTED — boost picks hit LESS often than drag picks (factor may be reversed)")
            print(f"  {label:<24}  lift = {t['lift']*100:+.1f}%   {direction}")

    # ============ 4. Continuous-factor correlations ============
    print("\n" + "=" * 84)
    print("4. CONTINUOUS-FACTOR CORRELATIONS WITH OUTCOME")
    print("=" * 84)
    print("Pearson (point-biserial) correlation between each continuous factor and")
    print("the binary outcome. |r| ≈ 0 = no relationship; |r| ≥ 0.10 with p<0.05 = real signal.\n")
    cont = [
        ("Model P(over 1.5)",    "p_over_15"),
        ("Model edge",           "best_edge"),
        ("Expected PA",          "expected_pa"),
        ("Pitch-arsenal qual",   "quality_mult"),
        ("Handedness",           "hf"),
        ("Park",                 "park_mult"),
        ("Weather",              "weather_mult"),
        ("Bullpen",              "bullpen_mult"),
        ("BvP",                  "bvp_mult"),
        ("BvT",                  "bvt_mult"),
        ("BvS",                  "bvs_mult"),
    ]
    for label, key in cont:
        c = point_biserial_correlation(picks, key)
        if c is None:
            print(f"  {label:<22} insufficient data")
            continue
        print(f"  {label:<22}  r = {c['r']:+.3f}   N={c['n']:>4}   p={c['p_value']:.4f} {stars(c['p_value'])}")

    print("\n" + "=" * 84)
    print("Legend:  ***p<0.01 (very strong)   **p<0.05 (significant)   *p<0.10 (marginal)")
    print("=" * 84)


if __name__ == "__main__":
    main()
