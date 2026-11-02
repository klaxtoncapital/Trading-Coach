"""NKD Asia daily expansion: Dave's model, hourly version (Layers B and C).

Bias (daily close, "50% rule"): yesterday closed in its direction AND beyond the middle of
the day before's range. bias="with" trades it, "against" trades the opposite (the control),
"none" takes both directions every day (the second control).

Two entry types, both inside the Asia window (6PM-2AM New York = 10:00-18:00 Sydney):

  sweep   (retrace day) an hourly bar sweeps a pivot on the OTE side of the 6PM open
          (longs: the session low so far or yesterday's low, below the open) and closes
          back above it. Enter at the next bar's open. Stop past the sweep wick.
  strong  (no-retrace day) one of the first three hourly bars (6, 7, 8PM NY) is a strong
          candle in the bias direction (body >= 60% of its range, range >= 1.2x the average
          range of that hour). Limit entry `pullback` of the way back into its body.
          Stop past the candle's far end.

Target: the 6PM open +/- target_std x the expected daily move (standard deviation of the
last 20 daily open-to-close moves). Out at the end of the trading day otherwise.
One trade a day: the first entry that triggers.
vol_filter: the sweep / strong bar must trade above its hour's 20-day average volume.
"""
from __future__ import annotations

from datetime import datetime, time

import numpy as np
import pandas as pd

from .. import data
from ..engine import Setup


class NKDExpansion:
    name = "nkd_expansion"
    timeframe = "1h"
    defaults = {"entry_type": "both", "bias": "with", "vol_filter": False, "target_std": 1.0,
                "stop_ticks": 5, "pullback": 0.25, "asia_end_hour": 2, "tick": None, "since": None, "only": ""}
    hypothesis = ("In NKD's Asia session, entering at a pivot sweep (retrace days) or on the first pullback into a "
                  "strong early candle (no-retrace days), in the direction of the daily-close bias, with a small stop "
                  "and a 1 STD expected-move target, has positive expectancy. Edge is from entry location and "
                  "reward-to-risk, not the direction call alone.")

    def resolve(self, params: dict) -> dict:
        return {**self.defaults, **params}

    def setups(self, bars: pd.DataFrame, entry_type="both", bias="with", vol_filter=False, target_std=1.0,
               stop_ticks=5, pullback=0.25, asia_end_hour=2, tick=None, since=None, only="") -> list[Setup]:
        if bars.empty:
            return []
        tick = float(tick or 5.0)
        ny = bars.index.tz_convert("America/New_York")
        b = bars.assign(day=data.trading_days(bars).values, hour=ny.hour)
        b = b[pd.to_datetime(b["day"]).dt.weekday < 5]
        rng = b["h"] - b["l"]
        # average range and volume of each clock hour over the previous 20 sessions (no look-ahead: shifted)
        b = b.assign(rng=rng)
        b["avg_rng"] = b.groupby("hour")["rng"].transform(lambda s: s.rolling(20, min_periods=10).mean().shift(1))
        b["avg_vol"] = b.groupby("hour")["v"].transform(lambda s: s.rolling(20, min_periods=10).mean().shift(1))
        days = data.daily(b[["o", "h", "l", "c", "v", "contract"]])
        d_idx = list(days.index)
        do, dh, dl, dc = (days[k].to_numpy() for k in ("o", "h", "l", "c"))
        move_sd = pd.Series(dc - do).rolling(20, min_periods=15).std().shift(1).to_numpy()
        wanted = {w.strip().upper() for w in only.split(",") if w.strip()} if only else None
        since_d = pd.Timestamp(since).date() if since else None
        by_day = {k: g for k, g in b.groupby("day")}
        out = []
        for i in range(2, len(d_idx)):
            day = d_idx[i]
            if since_d and day < since_d:
                continue
            wd = ["MON", "TUE", "WED", "THU", "FRI"][day.weekday()]
            if wanted and wd not in wanted:
                continue
            sd = move_sd[i]
            if not np.isfinite(sd) or sd <= 0:
                continue
            mid2 = (dh[i - 2] + dl[i - 2]) / 2
            b_dir = 1 if (dc[i - 1] > do[i - 1] and dc[i - 1] > mid2) else -1 if (dc[i - 1] < do[i - 1] and dc[i - 1] < mid2) else 0
            if bias == "with":
                sides = [b_dir] if b_dir else []
            elif bias == "against":
                sides = [-b_dir] if b_dir else []
            else:
                sides = [1, -1]
            if not sides:
                continue
            g = by_day[day]
            op = g["o"].iloc[0]
            exit_by = pd.Timestamp(datetime.combine(day, time(17)), tz="America/New_York").tz_convert("UTC")
            asia = g[(g["hour"] >= 18) | (g["hour"] < asia_end_hour)]
            cands = []
            for side in sides:
                target = op + side * target_std * sd
                if entry_type in ("sweep", "both"):
                    s = self._sweep(asia, op, side, dl[i - 1] if side == 1 else dh[i - 1], tick, stop_ticks, vol_filter)
                    if s:
                        cands.append(Setup(s[0], side, stop=s[1], target=target, exit_by=exit_by,
                                           tag=f"sweep {'long' if side == 1 else 'short'}", info={"bias": bias}))
                if entry_type in ("strong", "both"):
                    s = self._strong(asia, op, side, tick, stop_ticks, pullback, vol_filter)
                    if s:
                        cands.append(Setup(s[0], side, stop=s[1], target=target, entry=s[2], expires=s[3],
                                           exit_by=exit_by, target_on_fill_bar=False,
                                           tag=f"strong {'long' if side == 1 else 'short'}", info={"bias": bias}))
            cands = [c for c in cands if (c.target - (c.entry if c.entry is not None else op)) * c.side > 0]
            if cands:
                out.append(min(cands, key=lambda c: c.decided_at))      # one trade a day: the first trigger
        return out

    @staticmethod
    def _sweep(asia, op, side, prev_extreme, tick, stop_ticks, vol_filter):
        """First bar that sweeps a pivot on the OTE side of the open and closes back inside."""
        idx = asia.index
        for k in range(1, len(asia) - 1):
            bar = asia.iloc[k]
            prior = asia.iloc[:k]
            if side == 1:
                pivot = min(prior["l"].min(), prev_extreme) if prev_extreme < op else prior["l"].min()
                if pivot >= op:
                    continue
                if bar["l"] < pivot and bar["c"] > pivot and bar["c"] <= op:
                    if vol_filter and not (bar["v"] > bar["avg_vol"]):
                        continue
                    return idx[k + 1], bar["l"] - stop_ticks * tick
            else:
                pivot = max(prior["h"].max(), prev_extreme) if prev_extreme > op else prior["h"].max()
                if pivot <= op:
                    continue
                if bar["h"] > pivot and bar["c"] < pivot and bar["c"] >= op:
                    if vol_filter and not (bar["v"] > bar["avg_vol"]):
                        continue
                    return idx[k + 1], bar["h"] + stop_ticks * tick
        return None

    @staticmethod
    def _strong(asia, op, side, tick, stop_ticks, pullback, vol_filter):
        """A strong candle among the first three hours; limit entry back into its body."""
        idx = asia.index
        for k in range(min(3, len(asia) - 1)):
            bar = asia.iloc[k]
            body = (bar["c"] - bar["o"]) * side
            r = bar["h"] - bar["l"]
            if body <= 0 or r <= 0 or not np.isfinite(bar["avg_rng"]):
                continue
            if body / r >= 0.6 and r >= 1.2 * bar["avg_rng"] and (bar["c"] - op) * side > 0:
                if vol_filter and not (bar["v"] > bar["avg_vol"]):
                    continue
                entry = bar["c"] - side * pullback * abs(bar["c"] - bar["o"])
                stop = (bar["l"] - stop_ticks * tick) if side == 1 else (bar["h"] + stop_ticks * tick)
                return idx[k + 1], stop, entry, idx[-1] + pd.Timedelta(hours=1)
        return None
