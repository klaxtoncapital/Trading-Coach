"""NKD Asia: sweep + displacement + FVG on 5-minute bars (Dave's entry signature, Layer C).

Inside the Asia window (6PM-2AM New York = 10:00-18:00 Sydney), for a long:
  1. SWEEP: a 5m bar trades below a pivot on the OTE side of the 6PM open: yesterday's low, or
     the session low so far (made at least `min_age` bars earlier). Shorts mirror at highs.
  2. DISPLACEMENT within `window` bars: a bullish candle with body >= 60% of its range and range
     >= `disp_mult` x the normal range for that time of day (20-day average, same 5-minute slot).
  3. FVG: the displacement leaves a gap (the bar before it has a high below the bar after it's low).
  4. ENTRY: limit at the middle of the FVG (consequent encroachment), at or below the 6PM open,
     valid for `window` bars. STOP: `stop_ticks` past the sweep's extreme.
  5. TARGET: "std" = 6PM open + target_std x expected daily move (20-day std of open-to-close),
     or "r2"/"r3" = a fixed 2R/3R from the entry. Out at the end of the trading day otherwise.
vol_filter: the displacement bar must trade above the normal volume for its time slot.
bias: "with" / "against" / "none" (both directions), from the daily-close 50% rule.
One trade a day: the first signature.
"""
from __future__ import annotations

from datetime import datetime, time

import numpy as np
import pandas as pd

from .. import data
from ..engine import Setup


class NKDSignature:
    name = "nkd_signature"
    timeframe = "5m"
    defaults = {"bias": "with", "vol_filter": True, "target": "std", "target_std": 1.0, "stop_ticks": 5,
                "disp_mult": 1.5, "window": 12, "min_age": 3, "asia_end_hour": 2, "tick": None,
                "since": None, "only": ""}
    hypothesis = ("A sweep of a pivot on the OTE side of the 6PM open, followed by displacement that leaves an FVG on "
                  "above-normal volume, marks the turn into the day's expansion; entering at the FVG with the stop "
                  "past the sweep gives a small risk against a large move.")

    def resolve(self, params: dict) -> dict:
        return {**self.defaults, **params}

    def setups(self, bars: pd.DataFrame, bias="with", vol_filter=True, target="std", target_std=1.0, stop_ticks=5,
               disp_mult=1.5, window=12, min_age=3, asia_end_hour=2, tick=None, since=None, only="") -> list[Setup]:
        if bars.empty:
            return []
        tick = float(tick or 5.0)
        ny = bars.index.tz_convert("America/New_York")
        b = bars.assign(day=data.trading_days(bars).values, hour=ny.hour, slot=ny.hour * 60 + ny.minute)
        b = b[pd.to_datetime(b["day"]).dt.weekday < 5]
        b["rng"] = b["h"] - b["l"]
        b["n_rng"] = b.groupby("slot")["rng"].transform(lambda s: s.rolling(20, min_periods=10).mean().shift(1))
        b["n_vol"] = b.groupby("slot")["v"].transform(lambda s: s.rolling(20, min_periods=10).mean().shift(1))
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
            if wanted and ["MON", "TUE", "WED", "THU", "FRI"][day.weekday()] not in wanted:
                continue
            sd = move_sd[i]
            if not np.isfinite(sd) or sd <= 0:
                continue
            mid2 = (dh[i - 2] + dl[i - 2]) / 2
            b_dir = 1 if (dc[i - 1] > do[i - 1] and dc[i - 1] > mid2) else -1 if (dc[i - 1] < do[i - 1] and dc[i - 1] < mid2) else 0
            sides = ([b_dir] if b_dir else []) if bias == "with" else ([-b_dir] if b_dir else []) if bias == "against" else [1, -1]
            if not sides:
                continue
            g = by_day[day]
            asia = g[(g["hour"] >= 18) | (g["hour"] < asia_end_hour)]
            if len(asia) < min_age + 4:
                continue
            op = g["o"].iloc[0]
            exit_by = pd.Timestamp(datetime.combine(day, time(17)), tz="America/New_York").tz_convert("UTC")
            best = None
            for side in sides:
                s = self._find(asia, op, side, dl[i - 1] if side == 1 else dh[i - 1], tick, stop_ticks,
                               disp_mult, window, min_age, vol_filter)
                if not s:
                    continue
                decided, entry, stop, expires = s
                risk = (entry - stop) * side
                if target == "std":
                    tgt = op + side * target_std * sd
                else:
                    tgt = entry + side * {"r2": 2.0, "r3": 3.0}[target] * risk
                if (tgt - entry) * side <= 0:
                    continue
                st = Setup(decided, side, stop=stop, target=tgt, entry=entry, expires=expires, exit_by=exit_by,
                           target_on_fill_bar=False, tag=f"sig {'long' if side == 1 else 'short'}", info={"bias": bias})
                if best is None or st.decided_at < best.decided_at:
                    best = st
            if best:
                out.append(best)
        return out

    @staticmethod
    def _find(asia, op, side, prev_ext, tick, stop_ticks, disp_mult, window, min_age, vol_filter):
        o, h, l, c = (asia[k].to_numpy() for k in ("o", "h", "l", "c"))
        v, n_rng, n_vol = asia["v"].to_numpy(), asia["n_rng"].to_numpy(), asia["n_vol"].to_numpy()
        idx = asia.index
        n = len(asia)
        run_ext = np.minimum.accumulate(l) if side == 1 else np.maximum.accumulate(h)
        for k in range(min_age, n - 3):
            ref = run_ext[k - min_age]                       # session extreme made at least min_age bars ago
            pivot = min(ref, prev_ext) if side == 1 else max(ref, prev_ext)
            if side == 1:
                if not (pivot < op and l[k] < pivot):
                    continue
            else:
                if not (pivot > op and h[k] > pivot):
                    continue
            sweep_ext = l[k] if side == 1 else h[k]
            for j in range(k + 1, min(k + 1 + window, n - 1)):
                sweep_ext = min(sweep_ext, l[j]) if side == 1 else max(sweep_ext, h[j])
                body = (c[j] - o[j]) * side
                r = h[j] - l[j]
                if body <= 0 or r <= 0 or not np.isfinite(n_rng[j]) or body < 0.6 * r or r < disp_mult * n_rng[j]:
                    continue
                if vol_filter and not (np.isfinite(n_vol[j]) and v[j] > n_vol[j]):
                    continue
                if side == 1 and l[j + 1] > h[j - 1]:
                    entry = (h[j - 1] + l[j + 1]) / 2
                elif side == -1 and h[j + 1] < l[j - 1]:
                    entry = (l[j - 1] + h[j + 1]) / 2
                else:
                    continue
                if (entry - op) * side > 0:                 # OTE: longs at or below the open, shorts at or above
                    continue
                if j + 2 >= n:
                    return None
                stop = sweep_ext - side * stop_ticks * tick
                expires = idx[min(j + 2 + window, n - 1)]
                return idx[j + 2], entry, stop, expires
            # no signature after this sweep: keep looking for the next sweep
        return None
