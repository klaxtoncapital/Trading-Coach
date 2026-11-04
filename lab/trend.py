"""Position-style backtests for daily/weekly trend strategies (H9: Jordi Visser's 200-day rule).

Unlike lab/engine.py (single trades with a stop and a target), these strategies hold a position
that changes size over time. Each market is sized by volatility so every market carries a similar
risk, and the portfolio gives each market an equal share of the risk budget.

Visser state rule (checked at the close of the last trading day of each week, traded at the next open):
  below a falling 200-day            -> 1/4 size
  above the 200-day, 200-day not up  -> 1/2 size
  above a rising 200-day             -> full size
  below the 200-day but it's rising  -> 1/4 size (a break back below = back to small)
Variant "roc" (H9b): when the 50-day rate of change has been stretched (top 10% of its own history)
and rolls back below half of that level, cut to 1/2 until it resets near zero above a rising 200-day.
Benchmark: always full size (buy and hold, same volatility sizing).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def daily_frame(adj: pd.DataFrame, raw: pd.DataFrame) -> pd.DataFrame:
    """Daily bars: adjusted prices for signals and point moves, raw close for % (notional) terms."""
    d = pd.DataFrame({"o": adj["o"], "c": adj["c"], "raw_c": raw["c"].reindex(adj.index)})
    d.index = pd.to_datetime(d.index).tz_convert("UTC").normalize() if d.index.tz is not None else pd.to_datetime(d.index)
    d = d[~d.index.duplicated(keep="last")]
    return d[d.index.dayofweek < 5].dropna()


def visser_state(c: pd.Series, ma_len=200, slope_days=20) -> pd.Series:
    ma = c.rolling(ma_len, min_periods=ma_len).mean()
    rising = ma > ma.shift(slope_days)
    above = c > ma
    s = pd.Series(np.where(above & rising, 1.0, np.where(above, 0.5, 0.25)), index=c.index)
    return s.where(ma.notna())


def roc_overlay(c: pd.Series, state: pd.Series, n=50, pct=0.90, lookback=750) -> pd.Series:
    """H9b: cut to 1/2 after a stretched 50-day rate of change rolls over; restore on a reset near zero."""
    roc = c / c.shift(n) - 1
    hi = roc.rolling(lookback, min_periods=250).quantile(pct).shift(1)
    out = state.copy()
    cut = False
    peak = 0.0
    for i, (r, h, s) in enumerate(zip(roc.to_numpy(), hi.to_numpy(), state.to_numpy())):
        if not np.isfinite(r) or not np.isfinite(h) or not np.isfinite(s):
            continue
        if r > h:
            peak = max(peak, r)
        if not cut and peak > 0 and r < peak / 2:
            cut, peak = True, 0.0
        if cut and abs(r) < 0.02 and s == 1.0:
            cut = False
        if cut:
            out.iloc[i] = min(s, 0.5)
    return out


def ewmac_forecast(c: pd.Series, raw: pd.Series, pairs=((16, 64), (32, 128), (64, 256)), cap=20.0) -> pd.Series:
    """Carver's EWMAC: (fast EMA - slow EMA) / price volatility, scaled so the average |forecast| is ~10,
    averaged across speeds, capped at +/-20. Positive = long, negative = short."""
    pvol = c.diff().ewm(span=36, min_periods=36).std()                # daily volatility in price points
    fs = []
    for f, sl in pairs:
        raw_f = (c.ewm(span=f, min_periods=sl).mean() - c.ewm(span=sl, min_periods=sl).mean()) / pvol
        scale = 10 / raw_f.abs().expanding(min_periods=250).mean().shift(1)   # scaled on the past only
        fs.append((raw_f * scale).clip(-cap, cap))
    return pd.concat(fs, axis=1).mean(axis=1).clip(-cap, cap)


def _buffer(m: pd.Series, band: float) -> pd.Series:
    """Only change the position when the target moves more than `band` from what's held."""
    held, out = np.nan, []
    for x in m.to_numpy():
        if np.isfinite(x) and (not np.isfinite(held) or abs(x - held) > band):
            held = x
        out.append(held)
    return pd.Series(out, index=m.index)


def backtest_market(d: pd.DataFrame, mode="visser", weekly=True, vol_target=0.20, vol_len=60,
                    cost_per_turn=0.0002, max_lev=3.0, buffer=0.2, mult_override: pd.Series | None = None) -> pd.DataFrame:
    """Daily returns (as a fraction of capital) for one market traded alone at vol_target."""
    c, raw = d["c"], d["raw_c"]
    ret = (d["o"].shift(-1) - d["o"]) / raw.shift(1)          # open-to-open move as % of notional
    if mode == "hold":
        mult = pd.Series(1.0, index=d.index)
    elif mode == "ewmac":
        mult = _buffer(ewmac_forecast(c, raw) / 10.0, buffer)
    elif mode == "ma_ls":
        ma = c.rolling(200, min_periods=200).mean()
        mult = pd.Series(np.where(c > ma, 1.0, -1.0), index=d.index).where(ma.notna())
    else:
        st = visser_state(c)
        mult = roc_overlay(c, st) if mode == "roc" else st
    if mult_override is not None:
        mult = mult_override.reindex(d.index).fillna(0.0)
    if weekly and mode != "ewmac":
        last_of_week = d.index.to_series().groupby(d.index.to_period("W")).transform("max") == d.index.to_series()
        mult = mult.where(last_of_week).ffill()
    vol = (c.diff() / raw.shift(1)).rolling(vol_len, min_periods=vol_len).std() * math.sqrt(252)
    w = (vol_target / vol).clip(upper=max_lev) * mult
    w = w.shift(1)                                            # decided at yesterday's close, held from today's open
    turn = w.diff().abs().fillna(0)
    pnl = (w * ret - turn * cost_per_turn).dropna()
    return pd.DataFrame({"pnl": pnl, "w": w.reindex(pnl.index), "mult": mult.shift(1).reindex(pnl.index)})


def stats(pnl: pd.Series) -> dict:
    pnl = pnl.dropna()
    if len(pnl) < 50:
        return {}
    eq = (1 + pnl).cumprod()
    yrs = len(pnl) / 252
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    vol = pnl.std() * math.sqrt(252)
    dd = (eq / eq.cummax() - 1).min()
    by_year = pnl.groupby(pnl.index.year).apply(lambda x: (1 + x).prod() - 1)
    return {"cagr": cagr, "vol": vol, "sharpe": pnl.mean() / pnl.std() * math.sqrt(252) if pnl.std() > 0 else float("nan"),
            "max_dd": dd, "calmar": cagr / -dd if dd < 0 else float("nan"), "years": yrs,
            "yrs_pos": int((by_year > 0).sum()), "n_years": len(by_year),
            "worst_year": by_year.min(), "t": pnl.mean() / (pnl.std() / math.sqrt(len(pnl)))}


def portfolio(results: dict[str, pd.DataFrame], vol_target=0.20) -> pd.Series:
    """Equal risk per market, rescaled so the whole book runs near vol_target."""
    df = pd.DataFrame({k: v["pnl"] for k, v in results.items()}).fillna(0)
    active = (df != 0).sum(axis=1).clip(lower=1)
    raw = df.sum(axis=1) / active
    scale = vol_target / (raw.rolling(250, min_periods=120).std() * math.sqrt(252)).shift(1)
    return (raw * scale.clip(upper=4)).dropna()
