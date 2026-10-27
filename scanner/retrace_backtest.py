"""Retrace Rule backtest on ProjectX history: the weekday continuation study, any market.

  python -m scanner.main --retrace-backtest NKD --rr-mode ticks --since 2025-01-01
  python -m scanner.main --retrace-backtest NKD,ES,NQ,CL,GC,6J --days 2000

A Python copy of Dave's TradingView "6PM Retrace Rule Study":

  * The trading day starts at the 6PM New York open (Sunday evening is Monday).
  * Bull day: yesterday closed above the day before. Bear day: below.
    ("prior close"; "low/high" and "50%" definitions are available too.)
  * Enter at the 6PM open (or `entry` past it, against the trade), stop past
    the open, target from the open. Hourly bars, adverse side checked first, so
    a bar that touches both stop and target counts as a loss. A trade that hits
    neither is closed at the day's close.

Stops and targets can be set three ways:
  ticks  fixed ticks from the open (the Pine study; NKD 50 / 185)
  pct    percent of the day's open price
  atr    multiples of the 20-day average true range before that day (default:
         0.22 / 0.80, roughly NKD's 50 / 185 ticks against its recent daily range)
`pct` and `atr` make different years and different markets comparable.

Each setup (weekday x bull/bear) is reported per year and overall, in R (one R =
the stop distance) and in dollars per contract, with the worst losing streak
and the deepest drawdown. Every trade saves to retrace_trades_<date>.csv and
the summary to retrace_summary_<date>.csv. No costs or slippage.
"""
from __future__ import annotations

import csv
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from . import config
from .metrics import Day, to_days
from .projectx import UNIT_HOUR, pick_front_contracts
from .symbols import point_value

log = logging.getLogger("scanner.retrace_backtest")

DAYS = ["MON", "TUE", "WED", "THU", "FRI"]
MODES = ("ticks", "pct", "atr")
DEFAULTS = {"ticks": (50.0, 185.0), "pct": (0.40, 1.47), "atr": (0.22, 0.80)}
CONT_DEFS = ("close", "lowhigh", "half")


@dataclass
class Settings:
    mode: str = "atr"
    stop: float | None = None        # in `mode` units; None = DEFAULTS[mode]
    target: float | None = None
    entry: float = 0.0               # past the open, against the trade, in `mode` units
    tol_ticks: float = 10.0          # "came back over the open" for the missed-by-waiting count
    cont: str = "close"
    atr_len: int = 20

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if self.cont not in CONT_DEFS:
            raise ValueError(f"cont must be one of {CONT_DEFS}")
        d_stop, d_tgt = DEFAULTS[self.mode]
        self.stop = d_stop if self.stop is None else self.stop
        self.target = d_tgt if self.target is None else self.target

    def label(self) -> str:
        unit = {"ticks": "t", "pct": "%", "atr": " ATR"}[self.mode]
        return f"stop {self.stop:g}{unit} · target {self.target:g}{unit} · entry {self.entry:g}{unit} · {self.cont}"


def direction(c1: float, c2: float, h2: float, l2: float, o1: float, cont: str) -> int:
    """+1 bull, -1 bear, 0 none, from yesterday (o1, c1) and the day before (h2, l2, c2)."""
    if cont == "close":
        return -1 if c1 < c2 else 1 if c1 > c2 else 0
    if cont == "lowhigh":
        return -1 if c1 < l2 else 1 if c1 > h2 else 0
    mid = (h2 + l2) / 2
    return -1 if (c1 < o1 and c1 < mid) else 1 if (c1 > o1 and c1 > mid) else 0


def _atr_before(days: list[Day], i: int, n: int) -> float | None:
    trs = []
    for k in range(max(1, i - n), i):
        p = days[k - 1].c
        trs.append(max(days[k].h - days[k].l, abs(days[k].h - p), abs(days[k].l - p)))
    return sum(trs) / len(trs) if len(trs) >= n else None


def simulate_day(day: Day, d: int, stop: float, target: float, entry: float, tol: float) -> dict:
    """One day, exactly as the Pine study: price distances, d = +1 long / -1 short."""
    o = day.o
    retr = entered = tgt_hit = missed = False
    res = 0                       # 0 open, 1 win, -1 loss
    max_adv = 0.0
    entry_px = None
    for b in day.bars:
        if tgt_hit:
            break
        adv = (b["h"] - o) if d == -1 else (o - b["l"])
        fav = (o - b["l"]) if d == -1 else (b["h"] - o)
        max_adv = max(max_adv, adv)
        if adv >= tol:
            retr = True
        new_entry = False
        if not entered and adv >= entry:
            entered = new_entry = True
            entry_px = o - d * entry
        if entered and res == 0 and adv >= stop:
            res = -1
        if fav >= target:
            tgt_hit = True
            if not retr:
                missed = True
            if entered and not new_entry and res == 0:
                res = 1
    pnl = None
    if entered:
        risk = stop - entry
        if res == 1:
            pnl = target + entry
        elif res == -1:
            pnl = -risk
        else:
            pnl = (day.c - entry_px) * d
    return {"entered": entered, "result": {1: "target", -1: "stop", 0: "close"}[res],
            "target_hit": tgt_hit, "missed": missed, "max_adv": max_adv, "pnl": pnl}


def backtest_market(root: str, hourly: list[dict], tick: float, pv: float | None,
                    s: Settings, since: date | None = None) -> list[dict]:
    """Every qualifying weekday trade for one market."""
    days = to_days(hourly)
    trades = []
    for i in range(2, len(days)):
        day, y1, y2 = days[i], days[i - 1], days[i - 2]
        if since and day.d < since:
            continue
        wd = day.d.weekday()
        if wd > 4 or not day.bars:
            continue
        d = direction(y1.c, y2.c, y2.h, y2.l, y1.o, s.cont)
        if d == 0:
            continue
        if s.mode == "ticks":
            unit = tick
        elif s.mode == "pct":
            unit = day.o / 100
        else:
            a = _atr_before(days, i, s.atr_len)
            if a is None:
                continue
            unit = a
        stop, target, entry = s.stop * unit, s.target * unit, s.entry * unit
        if stop <= entry:
            continue
        r = simulate_day(day, d, stop, target, entry, s.tol_ticks * tick)
        if not r["entered"]:
            continue
        risk = stop - entry
        trades.append({
            "root": root, "date": day.d.isoformat(), "year": day.d.year, "weekday": DAYS[wd],
            "side": "Bull" if d == 1 else "Bear", "open": round(day.o, 6),
            "stop_dist": round(stop, 6), "target_dist": round(target, 6),
            "result": r["result"], "target_hit": r["target_hit"], "missed_by_waiting": r["missed"],
            "max_adverse": round(r["max_adv"], 6),
            "pnl_pts": round(r["pnl"], 6), "r": round(r["pnl"] / risk, 4),
            "usd": round(r["pnl"] * pv, 2) if pv else None,
        })
    return trades


def _streak_and_dd(rs: list[float]) -> tuple[int, float]:
    worst = cur = 0
    peak = eq = dd = 0.0
    for r in rs:
        cur = cur + 1 if r < 0 else 0
        worst = max(worst, cur)
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return worst, round(dd, 2)


def summarise(trades: list[dict], min_n: int = 10) -> list[dict]:
    """One row per market x setup, with per-year columns and a consistency flag."""
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for t in trades:
        groups[(t["root"], t["weekday"], t["side"])].append(t)
    rows = []
    for (root, wd, side), ts in groups.items():
        ts.sort(key=lambda t: t["date"])
        rs = [t["r"] for t in ts]
        usd = [t["usd"] for t in ts if t["usd"] is not None]
        streak, dd = _streak_and_dd(rs)
        by_year = defaultdict(list)
        for t in ts:
            by_year[t["year"]].append(t["r"])
        years = {y: (len(v), round(sum(v), 2)) for y, v in sorted(by_year.items())}
        big = [tot for n, tot in years.values() if n >= min_n]
        rows.append({
            "root": root, "setup": f"{side} {wd}", "side": side, "weekday": wd,
            "n": len(ts), "target_pct": round(100 * sum(t["target_hit"] for t in ts) / len(ts)),
            "avg_r": round(sum(rs) / len(rs), 3), "total_r": round(sum(rs), 2),
            "avg_usd": round(sum(usd) / len(usd)) if usd else None,
            "total_usd": round(sum(usd)) if usd else None,
            "worst_streak": streak, "max_dd_r": dd,
            "years": years,
            "every_year_positive": bool(big) and len(big) == len(years) and all(x > 0 for x in big),
            "first": ts[0]["date"], "last": ts[-1]["date"],
        })
    order = {w: i for i, w in enumerate(DAYS)}
    rows.sort(key=lambda r: (r["root"], r["side"] != "Bull", order[r["weekday"]]))
    return rows


def print_table(rows: list[dict], label: str) -> None:
    years = sorted({y for r in rows for y in r["years"]})
    head = (f"{'mkt':4} {'setup':9} {'n':>4} {'tgt%':>4} {'avgR':>6} {'totR':>7} {'$/tr':>7} "
            f"{'streak':>6} {'maxDD':>6}  " + "  ".join(f"{y:>12}" for y in years) + "  every yr +")
    print(f"\nRetrace Rule backtest · {label}")
    print(head)
    print("-" * len(head))
    last = None
    for r in rows:
        if last and r["root"] != last:
            print()
        last = r["root"]
        yc = "  ".join(f"{(str(r['years'][y][1]) + 'R (' + str(r['years'][y][0]) + ')') if y in r['years'] else '—':>12}"
                       for y in years)
        usd = f"{r['avg_usd']:>7}" if r["avg_usd"] is not None else f"{'—':>7}"
        print(f"{r['root']:4} {r['setup']:9} {r['n']:>4} {r['target_pct']:>4} {r['avg_r']:>6} {r['total_r']:>7} {usd} "
              f"{r['worst_streak']:>6} {r['max_dd_r']:>6}  {yc}  {'YES' if r['every_year_positive'] else ''}")
    print("\nR = the stop distance. Years with fewer than 10 trades don't count towards 'every yr +'."
          "\nNo costs or slippage: treat an average under ~0.15R as no edge.")


def save_csv(trades: list[dict], rows: list[dict]) -> tuple[str, str]:
    today = date.today().isoformat()
    tname, sname = f"retrace_trades_{today}.csv", f"retrace_summary_{today}.csv"
    if trades:
        with open(tname, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
            w.writeheader()
            w.writerows(trades)
    years = sorted({y for r in rows for y in r["years"]})
    keys = ["root", "setup", "n", "target_pct", "avg_r", "total_r", "avg_usd", "total_usd",
            "worst_streak", "max_dd_r", "every_year_positive", "first", "last"]
    with open(sname, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(keys + [f"{y}_n" for y in years] + [f"{y}_r" for y in years])
        for r in rows:
            w.writerow([r[k] for k in keys] + [r["years"].get(y, (0, 0))[0] for y in years]
                       + [r["years"].get(y, (0, 0))[1] for y in years])
    return tname, sname


async def run(px, roots: list[str], days: int, s: Settings, since: date | None = None) -> tuple[list[dict], list[dict]]:
    from .acd_backtest import stitched
    contracts = pick_front_contracts(await px.available_contracts(), set(roots))
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    if since:
        start = min(start, datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc))
    start -= timedelta(days=45)       # room for the ATR and the first two prior days
    trades: list[dict] = []
    for root in roots:
        if root not in config.MARKETS_BY_ROOT:
            print(f"{root}: not in the desk's market list, skipped")
            continue
        c = contracts.get(root)
        if not c:
            print(f"{root}: not offered by ProjectX right now, skipped")
            continue
        print(f"{root}: pulling hourly history back to {start.date()} (stitching contract months)...")
        hourly = await stitched(px, root, c["id"], start, end, UNIT_HOUR)
        if len(hourly) < 200:
            print(f"{root}: not enough history from ProjectX ({len(hourly)} hourly bars)")
            continue
        print(f"{root}: history runs {hourly[0]['t'].date()} to {hourly[-1]['t'].date()} ({len(hourly)} bars)")
        tick = float(c.get("tickSize") or 0) or 0.01
        ts = backtest_market(root, hourly, tick, point_value(c), s, since)
        print(f"{root}: {len(ts)} trades")
        trades += ts
    return trades, summarise(trades)
