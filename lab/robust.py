"""Tests that catch a backtest lying: Monte Carlo, parameter sweeps, walk-forward, cost stress."""
from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import engine, metrics


# ── Monte Carlo: how bad can the drawdown get with this same edge? ──────

@dataclass
class MonteCarlo:
    sims: int
    trades: int
    dd_p50: float
    dd_p95: float
    dd_p99: float
    streak_p95: int
    total_p05: float
    total_p50: float
    risk_usd: float | None = None
    limit_usd: float | None = None
    p_breach: float | None = None      # chance the drawdown hits the account limit


def monte_carlo(r, risk_usd: float | None = None, limit_usd: float | None = None, n_trades: int | None = None,
                sims: int = 5000, seed: int = 3) -> MonteCarlo:
    """Reshuffle (resample) the trades thousands of times. The backtest's own drawdown is one
    lucky-or-unlucky ordering; this shows the spread of drawdowns the same edge can produce.

    risk_usd / limit_usd: e.g. $275 a trade against Topstep's $4,500 max loss, gives the chance
    of blowing the account inside n_trades trades."""
    r = np.asarray(r, dtype=float)
    n = n_trades or len(r)
    rng = np.random.default_rng(seed)
    paths = rng.choice(r, size=(sims, n), replace=True)
    eq = np.cumsum(paths, axis=1)
    peak = np.maximum.accumulate(np.concatenate([np.zeros((sims, 1)), eq], axis=1), axis=1)[:, 1:]
    dd = (eq - peak).min(axis=1)
    streaks = np.array([metrics.longest_losing_streak(p) for p in paths[: min(sims, 2000)]])
    mc = MonteCarlo(sims, n, float(np.quantile(dd, 0.5)), float(np.quantile(dd, 0.05)),
                    float(np.quantile(dd, 0.01)), int(np.quantile(streaks, 0.95)),
                    float(np.quantile(eq[:, -1], 0.05)), float(np.quantile(eq[:, -1], 0.5)))
    if risk_usd and limit_usd:
        mc.risk_usd, mc.limit_usd = risk_usd, limit_usd
        mc.p_breach = float((dd * risk_usd <= -limit_usd).mean())
    return mc


# ── parameter sweep: is it a plateau or a lucky spike? ───────────────────

def grid(spec: dict[str, list]) -> list[dict]:
    keys = list(spec)
    return [dict(zip(keys, vals)) for vals in itertools.product(*(spec[k] for k in keys))]


def sweep(strategy, bars: pd.DataFrame, costs: engine.Costs, spec: dict[str, list], base: dict | None = None,
          gap_fills: bool = True) -> pd.DataFrame:
    rows = []
    for p in grid(spec):
        params = {**(base or {}), **p}
        t = engine.simulate(bars, strategy.setups(bars, **params), costs, gap_fills)
        s = metrics.summary(t)
        rows.append({**p, "n": s["n"], "expectancy_r": s.get("expectancy_r", np.nan),
                     "total_r": s.get("total_r", np.nan), "max_dd_r": s.get("max_dd_r", np.nan),
                     "win_rate": s.get("win_rate", np.nan)})
    return pd.DataFrame(rows)


def plateau_score(sw: pd.DataFrame, chosen: dict, metric: str = "expectancy_r") -> float | None:
    """Share of the chosen setting's neighbours (one step away on each knob) that are also profitable."""
    keys = [k for k in chosen if k in sw.columns]
    if not keys:
        return None
    levels = {k: sorted(sw[k].unique()) for k in keys}
    try:
        pos = {k: levels[k].index(chosen[k]) for k in keys}
    except ValueError:
        return None
    nb = sw.copy()
    mask = np.ones(len(nb), dtype=bool)
    for k in keys:
        idx = nb[k].map(lambda v, k=k: levels[k].index(v))
        mask &= (idx - pos[k]).abs() <= 1
    nb = nb[mask]
    nb = nb[~np.all([nb[k] == chosen[k] for k in keys], axis=0)]
    if nb.empty:
        return None
    return float((nb[metric] > 0).mean())


# ── walk-forward: choose settings on the past, trade them on the future ───

def walk_forward(strategy, bars: pd.DataFrame, costs: engine.Costs, spec: dict[str, list],
                 base: dict | None = None, train_days: int = 365, test_days: int = 90,
                 min_trades: int = 20, objective: str = "expectancy_r", gap_fills: bool = True):
    """Roll a window through history: pick the best settings on `train_days`, then record what
    those settings did over the next `test_days`, which they never saw. Stitching the test
    windows together gives an honest out-of-sample result for the whole process,
    including the act of choosing settings."""
    base = base or {}
    combos = grid(spec)
    # run every combo once over the full history, then slice by time: same result, much faster
    runs = []
    for p in combos:
        t = engine.simulate(bars, strategy.setups(bars, **{**base, **p}), costs, gap_fills)
        runs.append((p, t))
    start, end = bars.index[0], bars.index[-1]
    t0 = start + pd.Timedelta(days=train_days)
    windows, oos = [], []
    while t0 < end:
        t1 = min(t0 + pd.Timedelta(days=test_days), end + pd.Timedelta(seconds=1))
        best, best_score = None, -np.inf
        for p, t in runs:
            tr = t[(t["entry_time"] >= t0 - pd.Timedelta(days=train_days)) & (t["entry_time"] < t0)]
            if len(tr) < min_trades:
                continue
            score = metrics.summary(tr)[objective]
            if score > best_score:
                best, best_score = (p, t), score
        if best is not None:
            p, t = best
            te = t[(t["entry_time"] >= t0) & (t["entry_time"] < t1)]
            oos.append(te.assign(wf_window=str(t0.date())))
            windows.append({"train_to": t0.date(), "test_to": t1.date(), **p,
                            "train_" + objective: best_score, "test_n": len(te),
                            "test_r": te["r"].sum() if len(te) else 0.0})
        t0 = t1
    oos_t = pd.concat(oos, ignore_index=True) if oos else engine._empty()
    return oos_t, pd.DataFrame(windows)


def cost_stress(strategy, bars: pd.DataFrame, costs: engine.Costs, params: dict, mult: float = 1.5,
                gap_fills: bool = True) -> dict:
    t = engine.simulate(bars, strategy.setups(bars, **params), costs.scaled(mult), gap_fills)
    return metrics.summary(t)
