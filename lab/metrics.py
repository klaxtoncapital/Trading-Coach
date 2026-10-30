"""The numbers on a tear sheet, from a table of trades.

Everything is measured in R after costs (column `r`), because R is what you can
size: risk 1R per trade and these numbers are your results. Dollars per
contract are shown alongside.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def summary(trades: pd.DataFrame) -> dict:
    n = len(trades)
    if n == 0:
        return {"n": 0}
    r = trades["r"].astype(float).to_numpy()
    wins, losses = r[r > 0], r[r <= 0]
    mean = r.mean()
    sd = r.std(ddof=1) if n > 1 else float("nan")
    eq = np.cumsum(r)
    dd = eq - np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:]
    days = _daily_r(trades)
    years = max((trades["exit_time"].max() - trades["entry_time"].min()).days / 365.25, 1 / 365.25)
    sharpe = days.mean() / days.std(ddof=1) * math.sqrt(252) if len(days) > 2 and days.std(ddof=1) > 0 else float("nan")
    downside = days[days < 0]
    sortino = (days.mean() / math.sqrt((downside ** 2).sum() / len(days)) * math.sqrt(252)
               if len(downside) else float("nan"))
    lo, hi = bootstrap_ci(r)
    total = eq[-1]
    max_dd = dd.min()
    return {
        "n": n,
        "trades_per_year": n / years,
        "win_rate": len(wins) / n,
        "avg_win_r": wins.mean() if len(wins) else 0.0,
        "avg_loss_r": losses.mean() if len(losses) else 0.0,
        "payoff": (wins.mean() / -losses.mean()) if len(wins) and len(losses) and losses.mean() < 0 else float("nan"),
        "expectancy_r": mean,
        "expectancy_ci": (lo, hi),
        "gross_expectancy_r": trades["gross_r"].astype(float).mean(),
        "cost_per_trade_r": trades["gross_r"].astype(float).mean() - mean,
        "profit_factor": wins.sum() / -losses.sum() if losses.sum() < 0 else float("inf"),
        "total_r": total,
        "r_per_year": total / years,
        "max_dd_r": max_dd,
        "recovery": total / -max_dd if max_dd < 0 else float("inf"),
        "worst_streak": longest_losing_streak(r),
        "sharpe": sharpe,
        "sortino": sortino,
        "t_stat": mean / (sd / math.sqrt(n)) if n > 1 and sd > 0 else float("nan"),
        "total_usd": trades["net_usd"].astype(float).sum(),
        "avg_usd": trades["net_usd"].astype(float).mean(),
        "first": trades["entry_time"].min(),
        "last": trades["exit_time"].max(),
        "years": years,
    }


def _daily_r(trades: pd.DataFrame) -> pd.Series:
    """R per weekday, including days with no trade (0), for Sharpe and Sortino."""
    day = pd.to_datetime(trades["exit_time"]).dt.tz_convert("America/New_York").dt.tz_localize(None).dt.normalize()
    d = trades["r"].astype(float).groupby(day).sum()
    if d.empty:
        return d
    idx = pd.bdate_range(d.index.min(), d.index.max())
    full = d.reindex(idx.union(d.index), fill_value=0.0)
    assert abs(full.sum() - d.sum()) < 1e-9
    return full


def longest_losing_streak(r) -> int:
    worst = cur = 0
    for x in r:
        cur = cur + 1 if x <= 0 else 0
        worst = max(worst, cur)
    return worst


def bootstrap_ci(r, n: int = 2000, level: float = 0.95, seed: int = 11) -> tuple[float, float]:
    """Range the true expectancy probably sits in, from resampling the trades."""
    r = np.asarray(r, dtype=float)
    if len(r) < 5:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    means = rng.choice(r, size=(n, len(r)), replace=True).mean(axis=1)
    a = (1 - level) / 2
    return float(np.quantile(means, a)), float(np.quantile(means, 1 - a))


def equity(trades: pd.DataFrame) -> pd.DataFrame:
    t = trades.sort_values("exit_time")
    eq = t["r"].astype(float).cumsum()
    peak = np.maximum.accumulate(np.concatenate([[0.0], eq.to_numpy()]))[1:]
    return pd.DataFrame({"time": t["exit_time"].to_numpy(), "equity_r": eq.to_numpy(),
                         "dd_r": eq.to_numpy() - peak})


def by(trades: pd.DataFrame, col: str) -> pd.DataFrame:
    """Results split by a column: 'tag', 'side', 'year', 'weekday' or 'month'."""
    t = trades.copy()
    et = t["entry_time"].dt.tz_convert("America/New_York")
    if col == "year":
        t["year"] = et.dt.year
    elif col == "month":
        t["month"] = et.dt.strftime("%Y-%m")
    elif col == "weekday":
        t["weekday"] = et.dt.day_name().str[:3]
    rows = []
    for k, g in t.groupby(col):
        r = g["r"].astype(float)
        rows.append({col: k, "n": len(g), "win_rate": (r > 0).mean(), "expectancy_r": r.mean(),
                     "total_r": r.sum(), "total_usd": g["net_usd"].sum(),
                     "max_dd_r": equity(g)["dd_r"].min()})
    return pd.DataFrame(rows)


def split(trades: pd.DataFrame, oos_frac: float = 0.3) -> tuple[pd.DataFrame, pd.DataFrame]:
    """In-sample (older) and out-of-sample (most recent oos_frac of trades, by time)."""
    t = trades.sort_values("entry_time")
    k = int(round(len(t) * (1 - oos_frac)))
    return t.iloc[:k], t.iloc[k:]
