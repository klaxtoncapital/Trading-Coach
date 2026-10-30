"""The Lab's backtest engine.

A strategy never places trades itself. It looks at the bars and returns a list
of Setups: "at this moment, with only the data up to here, I want to go long/
short, with this entry, stop, target and time exit". The engine then walks the
bars that come AFTER that moment and works out what really happened, the same
way for every strategy:

  * entry: market (next bar's open, plus slippage) or limit (filled when price
    trades to the level before it expires)
  * on a limit order's fill bar only the stop can count (the target may have
    printed before the fill)
  * stop is checked before target inside a bar, because a bar's high and low
    don't say which came first, so a bar that touches both is a loss
  * a trade that hits neither is closed at the time exit
  * costs: commission per round trip and slippage in ticks on market entries,
    stops and time exits (limit targets get no slippage)

Results are in points, R (1R = the planned risk) and dollars per contract,
before and after costs.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class Setup:
    decided_at: pd.Timestamp          # the decision uses only bars that START before this time
    side: int                         # +1 long, -1 short
    stop: float | None = None         # absolute price, or None and use stop_dist from the fill
    target: float | None = None       # absolute price, or None and use target_dist (None + None = no target)
    stop_dist: float | None = None
    target_dist: float | None = None
    entry: float | None = None        # limit price; None = market at the first bar's open
    expires: pd.Timestamp | None = None   # unfilled limit is cancelled at this time
    exit_by: pd.Timestamp | None = None   # flat at the close of the last bar starting before this
    target_on_fill_bar: bool = True   # can the target count on the bar the trade was filled?
    cancel_if_target_first: bool = False  # limit entry: drop the trade if price reaches the target before filling
    tag: str = ""                     # e.g. "Bull WED", used to group results
    info: dict = field(default_factory=dict)

    def key(self) -> tuple:
        return (self.decided_at, self.side, self.stop, self.target, self.stop_dist, self.target_dist,
                self.entry, self.tag)


@dataclass
class Costs:
    commission_rt: float = 4.0        # $ per contract, round trip (estimate; set your real fees)
    slip_ticks: float = 1.0           # ticks per side on market fills, stops and time exits
    tick: float = 0.25
    point_value: float = 1.0          # $ per 1.0 point

    def scaled(self, slip_mult: float) -> "Costs":
        return Costs(self.commission_rt, self.slip_ticks * slip_mult, self.tick, self.point_value)


def simulate(bars: pd.DataFrame, setups: list[Setup], costs: Costs, gap_fills: bool = True) -> pd.DataFrame:
    """Every setup run through the bars. One row per filled trade.

    gap_fills: a bar that opens beyond the stop fills at that open (realistic). Off = always
    exactly at the stop, which is how TradingView studies usually count it."""
    if not setups:
        return _empty()
    t = bars.index
    o, h, l, c = (bars[k].to_numpy() for k in ("o", "h", "l", "c"))
    slip = costs.slip_ticks * costs.tick
    rows = []
    for s in sorted(setups, key=lambda s: s.decided_at):
        i0 = t.searchsorted(s.decided_at, side="left")
        if i0 >= len(t):
            continue
        i_end = t.searchsorted(s.exit_by, side="left") - 1 if s.exit_by is not None else len(t) - 1
        if i_end < i0:
            continue
        d = s.side
        # ── entry ──
        fill_i = fill_px = None
        market = s.entry is None
        if market:
            fill_i, fill_px = i0, o[i0]
        else:
            i_exp = t.searchsorted(s.expires, side="left") - 1 if s.expires is not None else i_end
            for i in range(i0, min(i_exp, i_end) + 1):
                if (d == 1 and l[i] <= s.entry) or (d == -1 and h[i] >= s.entry):
                    fill_i = i
                    # a gap through the limit fills at the open, which is better than the limit
                    fill_px = (min(o[i], s.entry) if d == 1 else max(o[i], s.entry)) if gap_fills else s.entry
                    break
                if s.cancel_if_target_first and s.target is not None and \
                        ((d == 1 and h[i] >= s.target) or (d == -1 and l[i] <= s.target)):
                    break             # the move happened without us: setup over
        if fill_i is None:
            continue
        stop = s.stop if s.stop is not None else fill_px - d * s.stop_dist
        target = s.target if s.target is not None else (
            fill_px + d * s.target_dist if s.target_dist is not None else None)
        risk = (fill_px - stop) * d
        if risk <= 0:
            continue
        # ── manage ──
        result, exit_i, exit_px = "time", i_end, c[i_end]
        mae = mfe = 0.0
        for i in range(fill_i, i_end + 1):
            adv = (fill_px - l[i]) if d == 1 else (h[i] - fill_px)
            fav = (h[i] - fill_px) if d == 1 else (fill_px - l[i])
            if (d == 1 and l[i] <= stop) or (d == -1 and h[i] >= stop):
                # a gap through the stop fills at the open, which is worse than the stop
                if gap_fills and i > fill_i:
                    exit_px = min(o[i], stop) if d == 1 else max(o[i], stop)
                else:
                    exit_px = stop
                result, exit_i = "stop", i
                mae = max(mae, risk)
                break
            mae, mfe = max(mae, adv), max(mfe, fav)
            # a limit fills somewhere inside its bar, so that bar's high/low may have come before the
            # fill: never count the target on a limit's fill bar
            if target is not None and (i > fill_i or (s.target_on_fill_bar and market)):
                if (d == 1 and h[i] >= target) or (d == -1 and l[i] <= target):
                    result, exit_i, exit_px = "target", i, target
                    break
        gross_pts = (exit_px - fill_px) * d
        slip_pts = (slip if market else 0.0) + (0.0 if result == "target" else slip)
        net_pts = gross_pts - slip_pts
        cost_usd = slip_pts * costs.point_value + costs.commission_rt
        risk_usd = risk * costs.point_value
        rows.append({
            "tag": s.tag, "side": "Long" if d == 1 else "Short",
            "decided_at": s.decided_at, "entry_time": t[fill_i], "exit_time": t[exit_i],
            "entry": fill_px, "stop": stop, "target": target, "exit": exit_px, "result": result,
            "risk_pts": risk, "gross_pts": gross_pts, "gross_r": gross_pts / risk,
            "gross_usd": gross_pts * costs.point_value, "cost_usd": cost_usd,
            "net_usd": gross_pts * costs.point_value - cost_usd,
            "r": (gross_pts * costs.point_value - cost_usd) / risk_usd,
            "mae_r": mae / risk, "mfe_r": mfe / risk, "bars_held": exit_i - fill_i + 1,
            **{f"info_{k}": v for k, v in s.info.items()},
        })
    if not rows:
        return _empty()
    return pd.DataFrame(rows)


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=["tag", "side", "decided_at", "entry_time", "exit_time", "entry", "stop",
                                 "target", "exit", "result", "risk_pts", "gross_pts", "gross_r", "gross_usd",
                                 "cost_usd", "net_usd", "r", "mae_r", "mfe_r", "bars_held"])


def lookahead_check(strategy, bars: pd.DataFrame, params: dict | None = None, samples: int = 25,
                    seed: int = 7) -> tuple[bool, list[str]]:
    """Does the strategy peek at the future?

    Run it on the full history, then again on history cut off at each decision time.
    A strategy that only uses the past must give the same setup either way."""
    params = params or {}
    full = strategy.setups(bars, **params)
    if not full:
        return True, ["no setups to check"]
    rng = np.random.default_rng(seed)
    pick = rng.choice(len(full), size=min(samples, len(full)), replace=False)
    problems = []
    for k in sorted(pick):
        s = full[k]
        # keep the bar that starts at decided_at too: market entries need its open, and a strategy
        # that used its high/low/close would still be caught because those bars come after
        cut = bars[bars.index <= s.decided_at]
        cut = cut.copy()
        if len(cut) and cut.index[-1] == s.decided_at:
            last = cut.index[-1]
            # blank out everything about the decision bar except its open
            cut.loc[last, ["h", "l", "c"]] = cut.loc[last, "o"]
            cut.loc[last, "v"] = 0.0
        again = {x.decided_at: x for x in strategy.setups(cut, **params)}
        twin = again.get(s.decided_at)
        if twin is None:
            problems.append(f"{s.decided_at}: setup disappears when the future is hidden")
        elif _core(twin) != _core(s):
            problems.append(f"{s.decided_at}: setup changes when the future is hidden ({_core(s)} vs {_core(twin)})")
    return not problems, problems


def _core(s: Setup) -> tuple:
    r = lambda x: None if x is None else round(float(x), 8)
    return (s.side, r(s.stop), r(s.target), r(s.stop_dist), r(s.target_dist), r(s.entry), s.tag)
