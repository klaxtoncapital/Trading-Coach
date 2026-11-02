"""NKD key level sweep and reclaim (Dave's model, v2, from his trade audit of 2 Nov 2026).

Asia window only: 6PM-2AM New York (10:00-18:00 Sydney), 5-minute bars.
Key levels: previous day high (PDH), previous day low (PDL), today's 6PM open.

LONG:  price was above the level, a 5m bar trades below the PDL or the 6PM open, and within
       `reclaim_bars` bars (counting the sweep bar itself) a 5m bar CLOSES back above the level.
       Enter at the next bar's open. Stop `stop_ticks` below the lowest point of the sweep.
       Target: the PDH (target="level"), or the nearer of the PDH and the session-range limit
       (target="capped": session low + average Asia session range).
SHORT: the mirror at the PDH or the 6PM open, target the PDL.
SKIP if the reward to the target is under `min_rr` x the risk, or if there's under `min_rr` x the
risk left inside the average Asia session range (the session has already done its range).
One trade a day (the first valid setup). Out at the end of the trading day otherwise.
Direction comes only from the sweep: no daily-bias filter.
"""
from __future__ import annotations

from datetime import datetime, time

import numpy as np
import pandas as pd

from .. import data
from ..engine import Setup


class NKDSweepReclaim:
    name = "nkd_sweep_reclaim"
    timeframe = "5m"
    defaults = {"reclaim_bars": 6, "min_rr": 1.5, "target": "level", "stop_ticks": 5, "levels": "pdh,pdl,open",
                "asia_end_hour": 2, "asr_days": 20, "tick": None, "since": None, "only": ""}
    hypothesis = ("In NKD's Asia session, a sweep of the previous day's high/low or the 6PM open that closes back "
                  "inside on the 5-minute chart marks a liquidity grab and reversal; entering on the reclaim close "
                  "with the stop past the sweep, targeting the opposite level, has positive expectancy when there "
                  "is at least 1.5R of room to the level and within the normal session range.")

    def resolve(self, params: dict) -> dict:
        return {**self.defaults, **params}

    def setups(self, bars: pd.DataFrame, reclaim_bars=6, min_rr=1.5, target="level", stop_ticks=5,
               levels="pdh,pdl,open", asia_end_hour=2, asr_days=20, tick=None, since=None, only="") -> list[Setup]:
        if bars.empty:
            return []
        tick = float(tick or 5.0)
        use = {x.strip().lower() for x in levels.split(",")}
        ny = bars.index.tz_convert("America/New_York")
        b = bars.assign(day=data.trading_days(bars).values, hour=ny.hour)
        b = b[pd.to_datetime(b["day"]).dt.weekday < 5]
        days = data.daily(b[["o", "h", "l", "c", "v", "contract"]])
        d_idx = list(days.index)
        dh, dl = days["h"].to_numpy(), days["l"].to_numpy()
        asia_all = b[(b["hour"] >= 18) | (b["hour"] < asia_end_hour)]
        asr = asia_all.groupby("day").apply(lambda g: g["h"].max() - g["l"].min(), include_groups=False)
        asr = asr.reindex(d_idx).rolling(asr_days, min_periods=10).mean().shift(1)       # previous days only
        wanted = {w.strip().upper() for w in only.split(",") if w.strip()} if only else None
        since_d = pd.Timestamp(since).date() if since else None
        by_day = {k: g for k, g in asia_all.groupby("day")}
        out = []
        for i in range(1, len(d_idx)):
            day = d_idx[i]
            if since_d and day < since_d:
                continue
            if wanted and ["MON", "TUE", "WED", "THU", "FRI"][day.weekday()] not in wanted:
                continue
            g = by_day.get(day)
            a = asr.iloc[i]
            if g is None or len(g) < 4 or not np.isfinite(a):
                continue
            op = g["o"].iloc[0]
            pdh, pdl = dh[i - 1], dl[i - 1]
            exit_by = pd.Timestamp(datetime.combine(day, time(17)), tz="America/New_York").tz_convert("UTC")
            s = self._first(g, op, pdh, pdl, use, reclaim_bars, min_rr, target, stop_ticks, tick, a)
            if s:
                decided, side, stop, tgt, lvl, entry_ref = s
                out.append(Setup(decided, side, stop=stop, target=tgt, exit_by=exit_by,
                                 tag=f"{lvl} {'long' if side == 1 else 'short'}",
                                 info={"level": lvl, "ref_entry": entry_ref}))
        return out

    @staticmethod
    def _first(g, op, pdh, pdl, use, reclaim_bars, min_rr, target, stop_ticks, tick, asr):
        o, h, l, c = (g[k].to_numpy() for k in ("o", "h", "l", "c"))
        idx = g.index
        n = len(g)
        run_low = np.minimum.accumulate(l)
        run_high = np.maximum.accumulate(h)
        levels = []
        if "pdl" in use:
            levels.append(("PDL", pdl, 1))
        if "open" in use:
            levels += [("6pm open", op, 1), ("6pm open", op, -1)]
        if "pdh" in use:
            levels.append(("PDH", pdh, -1))
        for k in range(1, n - 1):
            for name, lvl, side in levels:
                prev_close = c[k - 1] if k > 0 else o[0]
                if side == 1:
                    if not (prev_close >= lvl and l[k] <= lvl - tick):
                        continue
                else:
                    if not (prev_close <= lvl and h[k] >= lvl + tick):
                        continue
                ext = l[k] if side == 1 else h[k]
                last = n - 1 if not reclaim_bars else min(k + reclaim_bars, n - 1)
                for j in range(k, last):
                    ext = min(ext, l[j]) if side == 1 else max(ext, h[j])
                    if (side == 1 and c[j] > lvl) or (side == -1 and c[j] < lvl):
                        entry = c[j]                                 # reference: the reclaim close
                        stop = ext - side * stop_ticks * tick
                        risk = (entry - stop) * side
                        if risk <= 0:
                            break
                        lvl_tgt = pdh if side == 1 else pdl
                        ceiling = (run_low[j] + asr) if side == 1 else (run_high[j] - asr)
                        room_lvl = (lvl_tgt - entry) * side
                        room_sess = (ceiling - entry) * side
                        if room_lvl < min_rr * risk or room_sess < min_rr * risk:
                            break                                    # not enough room: this sweep is skipped
                        tgt = lvl_tgt if target == "level" else (lvl_tgt if room_lvl <= room_sess else ceiling)
                        return idx[j + 1], side, stop, tgt, name, entry
                # no valid reclaim for this sweep
        return None
