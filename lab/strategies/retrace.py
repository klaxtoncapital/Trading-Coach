"""NKD Retrace Rule, rebuilt in the Lab (the reference strategy for Stage 0).

Same rules as scanner/retrace_backtest.py and Dave's TradingView study:
  * trading day starts at the 6PM New York open
  * bull day: yesterday closed above the day before (bear: below); other
    definitions via cont = "lowhigh" or "half"
  * enter at the 6PM open (or `entry` units past it, against the trade, as a limit)
  * stop `stop` units past the open, target `target` units from the open
  * whatever hasn't hit stop or target is closed at the day's close
Units: ticks, percent of the open, or multiples of the 20-day ATR before the day.

pine_compat=True copies two TradingView-study habits so results can be checked
against the old backtest: a target touched on the entry bar doesn't count, and
a limit entry is dropped if price reaches the target first.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

import pandas as pd

from .. import data
from ..engine import Setup

DAYS = ["MON", "TUE", "WED", "THU", "FRI"]
DEFAULT_UNITS = {"ticks": (50.0, 185.0), "pct": (0.40, 1.47), "atr": (0.22, 0.80)}


def direction(c1: float, c2: float, h2: float, l2: float, o1: float, cont: str) -> int:
    if cont == "close":
        return -1 if c1 < c2 else 1 if c1 > c2 else 0
    if cont == "lowhigh":
        return -1 if c1 < l2 else 1 if c1 > h2 else 0
    mid = (h2 + l2) / 2
    return -1 if (c1 < o1 and c1 < mid) else 1 if (c1 > o1 and c1 > mid) else 0


class Retrace:
    name = "retrace"
    timeframe = "1h"
    defaults = {"mode": "ticks", "stop": None, "target": None, "entry": 0.0, "cont": "close",
                "atr_len": 20, "only": "", "since": None, "tick": None, "pine_compat": False}
    hypothesis = ("After a directional day, the next session tends to continue that way from the 6PM open. "
                  "Why it might exist: overnight continuation of the prior day's flow (Asia follows the US lead) "
                  "on specific weekdays. Kill it if: out-of-sample expectancy turns negative or one year carries "
                  "all the profit.")

    def resolve(self, params: dict) -> dict:
        """The settings with the mode's default stop/target filled in."""
        p = {**self.defaults, **params}
        d_stop, d_tgt = DEFAULT_UNITS[p["mode"]]
        p["stop"] = d_stop if p["stop"] is None else p["stop"]
        p["target"] = d_tgt if p["target"] is None else p["target"]
        return p

    def setups(self, bars: pd.DataFrame, mode="ticks", stop=None, target=None, entry=0.0, cont="close",
               atr_len=20, only="", since=None, tick=None, pine_compat=False) -> list[Setup]:
        if bars.empty:
            return []
        d_stop, d_tgt = DEFAULT_UNITS[mode]
        stop = d_stop if stop is None else float(stop)
        target = d_tgt if target is None else float(target)
        entry = float(entry or 0.0)
        if mode == "ticks" and not tick:
            raise ValueError("ticks mode needs the market's tick size (tick=...)")
        wanted = {w.strip().upper() for w in only.split(",") if w.strip()} if only else None
        since_d = pd.Timestamp(since).date() if since else None

        days = data.daily(bars)
        idx = list(days.index)
        o, h, l, c = (days[k].to_numpy() for k in ("o", "h", "l", "c"))
        out = []
        for i in range(2, len(idx)):
            day = idx[i]
            if since_d and day < since_d:
                continue
            d = direction(c[i - 1], c[i - 2], h[i - 2], l[i - 2], o[i - 1], cont)
            if d == 0:
                continue
            tag = f"{'Bull' if d == 1 else 'Bear'} {DAYS[day.weekday()]}"
            if wanted and tag.upper() not in wanted:
                continue
            if mode == "ticks":
                unit = float(tick)
            elif mode == "pct":
                unit = o[i] / 100
            else:
                if i - max(1, i - atr_len) < atr_len:
                    continue
                trs = [max(h[k] - l[k], abs(h[k] - c[k - 1]), abs(l[k] - c[k - 1])) for k in range(i - atr_len, i)]
                unit = sum(trs) / len(trs)
            s_d, t_d, e_d = stop * unit, target * unit, entry * unit
            if s_d <= e_d:          # stop inside the entry: no trade
                continue
            op = o[i]
            exit_by = pd.Timestamp(datetime.combine(day, time(18)), tz="America/New_York").tz_convert("UTC")
            limit = None if e_d == 0 else op - d * e_d
            out.append(Setup(
                decided_at=days["first_bar"].iloc[i], side=d,
                stop=op - d * s_d, target=op + d * t_d, entry=limit,
                expires=exit_by, exit_by=exit_by,
                target_on_fill_bar=not pine_compat and limit is None,
                cancel_if_target_first=pine_compat,
                tag=tag, info={"unit": unit},
            ))
        return out
