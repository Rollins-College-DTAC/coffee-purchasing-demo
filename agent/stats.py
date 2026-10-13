"""Statistical methods (Milestone 4). Each returns the dict shape the dashboard renders:
method, formula_latex, inputs, result, threshold, interpretation. log_stat() saves it to stats_log."""

import json
import math
import warnings

import numpy as np
import pandas as pd
from scipy import stats as sps

from agent.db import now

CI_FORMULA = r"\bar{x} \pm t_{0.975,\,n-1}\,\frac{s}{\sqrt{n}}"
Z_FORMULA = r"z = \frac{x - \bar{x}}{s}"
PI_FORMULA = r"\hat{y}_{t+h} \pm 1.96\,\hat{\sigma}_h"
MAE_FORMULA = r"\mathrm{MAE} = \frac{1}{n}\sum_t \lvert y_t - \hat{y}_t \rvert"


def mean_ci(values, label):
    """Mean with a 95% confidence interval (t distribution)."""
    x = np.asarray(values, dtype=float)
    n, mean = len(x), float(x.mean())
    s = float(x.std(ddof=1)) if n > 1 else 0.0
    t = float(sps.t.ppf(0.975, n - 1)) if n > 1 else None
    half = t * s / math.sqrt(n) if n > 1 else None
    return {
        "method": f"mean_ci_95:{label}",
        "formula_latex": CI_FORMULA,
        "inputs": {r"\bar{x}": round(mean, 3), "s": round(s, 3), "n": n, "t_{0.975,n-1}": round(t, 3) if t else None},
        "result": round(mean, 3),
        "threshold": None,
        "interpretation": (f"{label}: mean ${mean:.2f}/lb, 95% CI ${mean - half:.2f} to ${mean + half:.2f} "
                           f"(n={n})" if n > 1 else f"{label}: only one lot, no interval"),
        "ci": (round(mean - half, 3), round(mean + half, 3)) if n > 1 else None,
    }


def z_score(x, history, threshold):
    """How many standard deviations this import's mean $/lb is from recent published imports."""
    h = np.asarray(history, dtype=float)
    mean, s = float(h.mean()), float(h.std(ddof=1))
    z = (x - mean) / s if s > 0 else 0.0
    direction = "above" if z > 0 else "below"
    return {
        "method": "z_score",
        "formula_latex": Z_FORMULA,
        "inputs": {"x": round(x, 3), r"\bar{x}": round(mean, 3), "s": round(s, 3), "n": len(h)},
        "result": round(z, 3),
        "threshold": threshold,
        "interpretation": (f"This import's mean ${x:.2f}/lb is {abs(z):.1f} standard deviations {direction} "
                           f"the last {len(h)} published imports (${mean:.2f}/lb)."
                           + (" Above the threshold: hold for review." if abs(z) > threshold else "")),
    }


def _fit(y):
    from statsmodels.tsa.exponential_smoothing.ets import ETSModel
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # convergence chatter on short, noisy series
        return ETSModel(pd.Series(y, dtype=float), error="add", trend="add", damped_trend=True).fit(disp=False)


def forecast(series, horizon=5):
    """Exponential smoothing (additive damped trend) with a 95% prediction interval.
    Returns the next `horizon` points plus a stats dict for the next import."""
    fit = _fit(series)
    pred = fit.get_prediction(start=len(series), end=len(series) + horizon - 1).summary_frame(alpha=0.05)
    points = [{"h": i + 1, "yhat": round(float(r["mean"]), 3), "lo": round(float(r["pi_lower"]), 3),
               "hi": round(float(r["pi_upper"]), 3)} for i, (_, r) in enumerate(pred.iterrows())]
    nxt = points[0]
    sigma = (nxt["hi"] - nxt["yhat"]) / 1.96
    return points, {
        "method": "forecast_ets",
        "formula_latex": PI_FORMULA,
        "inputs": {r"\hat{y}_{t+1}": nxt["yhat"], r"\hat{\sigma}_1": round(sigma, 3), "n": len(series)},
        "result": nxt["yhat"],
        "threshold": None,
        "interpretation": (f"Next import expected at ${nxt['yhat']:.2f}/lb; 95% of imports should fall "
                           f"between ${nxt['lo']:.2f} and ${nxt['hi']:.2f}."),
    }


def backtest(series, holdout=10):
    """Fit on all but the last `holdout` imports, forecast them, and compare with a naive
    'same as the last import' forecast. Lower MAE is better."""
    train, test = np.asarray(series[:-holdout], float), np.asarray(series[-holdout:], float)
    ets = np.asarray(_fit(train).forecast(holdout), float)
    naive = np.full(holdout, train[-1])
    mae_ets, mae_naive = float(np.mean(np.abs(test - ets))), float(np.mean(np.abs(test - naive)))
    mape_ets = float(100 * np.mean(np.abs((test - ets) / test)))
    better = mae_ets < mae_naive
    return {
        "method": "backtest_mae",
        "formula_latex": MAE_FORMULA,
        "inputs": {"MAE_{ETS}": round(mae_ets, 3), "MAE_{naive}": round(mae_naive, 3),
                   "MAPE_{ETS}": round(mape_ets, 2), "holdout": holdout},
        "result": round(mae_ets, 3),
        "threshold": round(mae_naive, 3),
        "interpretation": (f"On the last {holdout} imports the forecast was off by ${mae_ets:.2f}/lb on average "
                           f"({mape_ets:.1f}%), vs ${mae_naive:.2f} for 'same as last import': "
                           + ("the model beats the baseline." if better else "the baseline wins; the model adds nothing yet.")),
    }


def log_stat(db, step, s):
    db.run_sql(
        step, "INSERT INTO stats_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (db.run_id, step, s["method"], s["formula_latex"], json.dumps(s["inputs"]),
         s["result"], s["threshold"], s["interpretation"], now()),
    )
