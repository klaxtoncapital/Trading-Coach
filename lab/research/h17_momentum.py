"""H17: cross-sectional 12-1 momentum, 36 Topstep markets, daily data (Asness, Moskowitz & Pedersen 2013).

Rules (signed off):
  - Each month end, rank eligible markets by return from 12 months ago to 1 month ago (12-1).
  - Eligible = at least 13 months of history.
  - Long the top third, short the bottom third, flat the middle.
  - Each position sized to 20%/yr vol on its own (60-day vol), held one month.
  - Whole book scaled to 20% vol (lab.trend.portfolio). Costs 0.02% of amount traded.
"""
from __future__ import annotations
import math, sys
import numpy as np, pandas as pd
from lab import data
from lab.trend import daily_frame, backtest_market, portfolio, stats

ROOTS = "ES,NQ,RTY,YM,NKD,BTC,ETH,6A,6B,6C,6E,6J,6S,6M,6N,LE,HE,CL,NG,RB,HO,PL,GC,SI,HG,ZC,ZW,ZS,ZM,ZL,ZT,ZF,ZN,TN,ZB,UB".split(",")
MIN_DAYS = 273          # ~13 months of trading days
LOOK, SKIP = 252, 21    # 12 months back, skip the latest month


def load_frames(roots=ROOTS) -> dict[str, pd.DataFrame]:
    out = {}
    for r in roots:
        raw = data.load(r, "1d"); adj = data.load(r, "1d", adjusted=True)
        out[r] = daily_frame(adj, raw)
    return out


def momentum_signals(frames: dict[str, pd.DataFrame], measure: str = "adjprice") -> pd.DataFrame:
    """+1 / -1 / 0 per market, set at each month-end close and held until the next.

    measure="adjprice" (H17 as logged, Sharpe ~0.47): (adj close 1m ago - adj close 12m ago) / raw close 12m ago.
    measure="compound" (textbook AMP): compounded daily % returns over the same window. Gives Sharpe ~0.38.
    """
    dret = pd.DataFrame({k: d["c"].diff() / d["raw_c"].shift(1) for k, d in frames.items()})
    if measure == "compound":
        logc = np.log1p(dret.fillna(0)).cumsum()
        mom = logc.shift(SKIP) - logc.shift(LOOK)
    else:
        c = pd.DataFrame({k: d["c"] for k, d in frames.items()}); raw = pd.DataFrame({k: d["raw_c"] for k, d in frames.items()})
        mom = (c.shift(SKIP) - c.shift(LOOK)) / raw.shift(LOOK)
    age = dret.notna().cumsum()
    mom = mom.where(age >= MIN_DAYS)
    idx = dret.index
    month_end = idx.to_series().groupby(idx.to_period("M")).transform("max") == idx.to_series()
    sig = pd.DataFrame(np.nan, index=idx, columns=dret.columns)
    for t in idx[month_end.values]:
        m = mom.loc[t].dropna()
        if len(m) < 6:
            continue
        n = len(m) // 3
        rk = m.rank(method="first")
        row = pd.Series(0.0, index=dret.columns)
        row[rk[rk > len(m) - n].index] = 1.0
        row[rk[rk <= n].index] = -1.0
        sig.loc[t] = row
    return sig.ffill().fillna(0.0)


def run_book(frames, sig, vol_target=0.20):
    res = {}
    for k, d in frames.items():
        m = sig[k].reindex(d.index).fillna(0.0)
        res[k] = backtest_market(d, mode="hold", weekly=False, vol_target=vol_target, mult_override=m)
    return res, portfolio(res, vol_target)


if __name__ == "__main__":
    frames = load_frames()
    sig = momentum_signals(frames)
    res, book = run_book(frames, sig)
    book = book[book.index >= "2011-07-01"]
    s = stats(book)
    print({k: round(v, 3) if isinstance(v, float) else v for k, v in s.items()})
    half = book.index < "2019-01-01"
    print("2011-18", round(stats(book[half])["sharpe"], 2), "2019-26", round(stats(book[~half])["sharpe"], 2))
    print((book.groupby(book.index.year).apply(lambda x: (1 + x).prod() - 1) * 100).round(1).to_dict())
    book.to_csv(sys.argv[1] if len(sys.argv) > 1 else "h17_book.csv")
