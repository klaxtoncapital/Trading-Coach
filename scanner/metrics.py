"""Turn hourly bars into the readings the scanner ranks on.

Everything is normalised by the market's own ATR so a move in gold can be
compared with a move in NQ. Trading days follow the CME Globex day: a new day
starts at 18:00 New York time (the 6pm open), so Sunday evening belongs to
Monday.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@dataclass
class Day:
    d: date
    o: float
    h: float
    l: float
    c: float
    v: float
    bars: list = field(default_factory=list)


def trading_day(t: datetime) -> date:
    """Globex trading day for a bar's UTC start time (rolls at 18:00 New York)."""
    return (t.astimezone(NY) + timedelta(hours=6)).date()


def to_days(hourly: list[dict]) -> list[Day]:
    days: dict[date, Day] = {}
    for b in hourly:
        k = trading_day(b["t"])
        if k.weekday() >= 5:      # stray weekend prints
            continue
        d = days.get(k)
        if d is None:
            days[k] = Day(k, b["o"], b["h"], b["l"], b["c"], b["v"], [b])
        else:
            d.h, d.l, d.c, d.v = max(d.h, b["h"]), min(d.l, b["l"]), b["c"], d.v + b["v"]
            d.bars.append(b)
    return [days[k] for k in sorted(days)]


def week_key(d: date) -> tuple[int, int]:
    iso = d.isocalendar()
    return iso[0], iso[1]


def atr(days: list[Day], n: int = 20) -> float:
    trs = []
    for i in range(1, len(days)):
        p = days[i - 1].c
        trs.append(max(days[i].h - days[i].l, abs(days[i].h - p), abs(days[i].l - p)))
    trs = trs[-n:]
    return mean(trs) if trs else 0.0


def realised_vol(days: list[Day], n: int = 20) -> float | None:
    import math
    closes = [d.c for d in days[-(n + 1):]]
    if len(closes) < n // 2:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 3:
        return None
    m = mean(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return round(100 * math.sqrt(var * 252), 1)


@dataclass
class Reading:
    root: str
    last: float
    atr: float
    trading_day: date
    weekday: str
    range_ratio: float            # today's range / ATR20
    week_range_ratio: float       # this week's range / average weekly range
    displacement: float           # largest recent 1h body / average 1h body
    displacement_dir: int         # +1 up, -1 down, 0 none
    relvol: float                 # today's volume so far / same-hours average
    stretch: float                # (last - SMA20) / ATR
    rv20: float | None
    day_high: float
    day_low: float
    levels: dict                  # PDH, PDL, PWH, PWL, MonH, MonL
    events: list                  # liquidity events [{type, level, price, dir, weight}]
    naked_above: list             # untouched daily highs above price (nearest first)
    naked_below: list             # untouched daily lows below price (nearest first)
    profile: dict                 # weekly profile state
    thesis: dict | None = None    # {dir, kind, why, entry, stop, target, target_name, rr}


def _naked_levels(days: list[Day], last: float, lookback: int = 20):
    """Daily highs/lows from the last `lookback` days that price hasn't traded back through."""
    hist = days[-(lookback + 1):-1]
    above, below = [], []
    for i, d in enumerate(hist):
        later = hist[i + 1:] + [days[-1]]
        if d.h > last and all(x.h < d.h for x in later):
            above.append((d.h, f"{d.d:%a %-d %b} high"))
        if d.l < last and all(x.l > d.l for x in later):
            below.append((d.l, f"{d.d:%a %-d %b} low"))
    above.sort(key=lambda x: x[0])
    below.sort(key=lambda x: -x[0])
    return above, below


def _weekly_profile(days: list[Day], a: float) -> dict:
    """Your weekly-profile rules on the current week's daily candles."""
    wk = week_key(days[-1].d)
    week = [d for d in days if week_key(d.d) == wk]
    by_wd = {d.d.weekday(): d for d in week}
    out = {"day": WEEKDAYS[days[-1].d.weekday()], "notes": [], "c2": None, "continuation": None}
    mon, tue, wed = by_wd.get(0), by_wd.get(1), by_wd.get(2)
    today = days[-1]
    if mon and a:
        small = (mon.h - mon.l) < 0.8 * a
        out["monday"] = "small" if small else "large"
        if small:
            out["notes"].append("Monday small: Classic Expansion possible.")
        else:
            out["notes"].append("Monday expanded: watch for a Thursday counter or late-week reversal.")
    # C2 on Tuesday: sweep Monday's high/low and close back inside Monday's range.
    if mon and tue:
        state = "forming" if tue is today else "confirmed"
        if tue.h > mon.h and tue.c < mon.h:
            out["c2"] = {"day": "Tuesday", "dir": -1, "state": state,
                         "text": f"Tuesday swept Monday's high and is back inside (bearish C2, {state})."}
        elif tue.l < mon.l and tue.c > mon.l:
            out["c2"] = {"day": "Tuesday", "dir": 1, "state": state,
                         "text": f"Tuesday swept Monday's low and is back inside (bullish C2, {state})."}
    # Continuation rule: Wednesday closes beyond 50% of Tuesday's range.
    if tue and wed and today.d.weekday() >= 3:
        mid = (tue.h + tue.l) / 2
        if wed.c < mid and wed.c < wed.o:
            out["continuation"] = {"dir": -1, "text": "Wednesday closed bearish below 50% of Tuesday: Thursday bearish continuation."}
        elif wed.c > mid and wed.c > wed.o:
            out["continuation"] = {"dir": 1, "text": "Wednesday closed bullish above 50% of Tuesday: Thursday bullish continuation."}
    return out


def compute(root: str, hourly: list[dict]) -> Reading | None:
    days = to_days(hourly)
    if len(days) < 22:
        return None
    today, prev = days[-1], days[-2]
    a = atr(days[:-1], 20)  # completed days only
    if a <= 0:
        return None
    last = today.c

    # Range expansion
    range_ratio = (today.h - today.l) / a
    wk = week_key(today.d)
    weeks: dict = {}
    for d in days:
        weeks.setdefault(week_key(d.d), []).append(d)
    keys = sorted(weeks)
    cur = weeks[wk]
    prev_weeks = [weeks[k] for k in keys if k < wk][-8:]
    wr = lambda ds: max(x.h for x in ds) - min(x.l for x in ds)
    avg_wr = mean(wr(w) for w in prev_weeks) if prev_weeks else 0
    week_range_ratio = wr(cur) / avg_wr if avg_wr else 0.0

    # Displacement: biggest 1h body in the last 6 bars vs the 20-day average body
    hb = [b for d in days[-21:-1] for b in d.bars]
    avg_body = mean(abs(b["c"] - b["o"]) for b in hb) if hb else 0
    recent = [b for d in days[-2:] for b in d.bars][-6:]
    big = max(recent, key=lambda b: abs(b["c"] - b["o"])) if recent else None
    displacement = abs(big["c"] - big["o"]) / avg_body if big and avg_body else 0.0
    displacement_dir = (1 if big["c"] > big["o"] else -1) if big and displacement >= 2 else 0

    # Relative volume: today's bars so far vs the same number of bars on the last 20 days
    n = len(today.bars)
    past = [sum(b["v"] for b in d.bars[:n]) for d in days[-21:-1] if len(d.bars) >= n]
    relvol = (today.v / mean(past)) if past and mean(past) > 0 else 0.0

    # Stretch from the 20-day mean
    sma20 = mean(d.c for d in days[-20:])
    stretch = (last - sma20) / a

    # Key levels
    prev_week = weeks[keys[keys.index(wk) - 1]] if keys.index(wk) > 0 else None
    levels = {"PDH": prev.h, "PDL": prev.l}
    if prev_week:
        levels["PWH"] = max(x.h for x in prev_week)
        levels["PWL"] = min(x.l for x in prev_week)
    mon = next((d for d in cur if d.d.weekday() == 0), None)
    if mon and mon is not today:
        levels["MonH"], levels["MonL"] = mon.h, mon.l

    # Liquidity events on today's / this week's price
    events = []
    wk_hi, wk_lo = max(x.h for x in cur), min(x.l for x in cur)
    checks = [("PDH", today.h, -1, 12), ("PDL", today.l, 1, 12)]
    if "PWH" in levels:
        checks += [("PWH", wk_hi, -1, 20), ("PWL", wk_lo, 1, 20)]
    if "MonH" in levels:
        checks += [("MonH", wk_hi, -1, 10), ("MonL", wk_lo, 1, 10)]
    for name, extreme, rev_dir, weight in checks:
        lvl = levels[name]
        if rev_dir == -1 and extreme > lvl:          # traded above a high
            if last < lvl:
                events.append({"type": "sweep", "level": name, "price": lvl, "dir": -1, "weight": weight,
                               "extreme": extreme, "text": f"Swept {name} and closed back below"})
            elif last > lvl + 0.25 * a:
                events.append({"type": "break", "level": name, "price": lvl, "dir": 1, "weight": weight // 2,
                               "extreme": extreme, "text": f"Broke and is holding above {name}"})
        if rev_dir == 1 and extreme < lvl:           # traded below a low
            if last > lvl:
                events.append({"type": "sweep", "level": name, "price": lvl, "dir": 1, "weight": weight,
                               "extreme": extreme, "text": f"Swept {name} and closed back above"})
            elif last < lvl - 0.25 * a:
                events.append({"type": "break", "level": name, "price": lvl, "dir": -1, "weight": weight // 2,
                               "extreme": extreme, "text": f"Broke and is holding below {name}"})

    above, below = _naked_levels(days, last)
    for name in ("PWH", "PDH", "MonH"):
        if name in levels and levels[name] > last:
            above.append((levels[name], name))
    for name in ("PWL", "PDL", "MonL"):
        if name in levels and levels[name] < last:
            below.append((levels[name], name))
    above.sort(key=lambda x: x[0])
    below.sort(key=lambda x: -x[0])

    return Reading(
        root=root, last=last, atr=a, trading_day=today.d, weekday=WEEKDAYS[today.d.weekday()],
        range_ratio=round(range_ratio, 2), week_range_ratio=round(week_range_ratio, 2),
        displacement=round(displacement, 1), displacement_dir=displacement_dir,
        relvol=round(relvol, 2), stretch=round(stretch, 2), rv20=realised_vol(days[:-1]),
        day_high=today.h, day_low=today.l, levels=levels, events=events, naked_above=above, naked_below=below,
        profile=_weekly_profile(days, a),
    )


def build_thesis(r: Reading) -> dict | None:
    """Direction from liquidity first (sweeps), then displacement. Levels from structure."""
    sweeps = sorted([e for e in r.events if e["type"] == "sweep"], key=lambda e: -e["weight"])
    breaks = sorted([e for e in r.events if e["type"] == "break"], key=lambda e: -e["weight"])
    if sweeps:
        e = sweeps[0]
        direction, kind, why = e["dir"], "reversal", e["text"]
        # Stop beyond the sweep's extreme
        stop = e["extreme"] - 0.1 * r.atr if direction == 1 else e["extreme"] + 0.1 * r.atr
    elif breaks and r.displacement_dir == breaks[0]["dir"]:
        e = breaks[0]
        direction, kind, why = e["dir"], "continuation", e["text"] + " with displacement"
        stop = e["price"] - 0.25 * r.atr if direction == 1 else e["price"] + 0.25 * r.atr
    elif r.displacement_dir and r.displacement >= 3 and r.range_ratio >= 1.2:
        direction, kind = r.displacement_dir, "continuation"
        why = f"Displacement {r.displacement}x normal with the day already {r.range_ratio}x its ATR"
        stop = r.last - 0.5 * r.atr if direction == 1 else r.last + 0.5 * r.atr
    else:
        return None

    targets = r.naked_above if direction == 1 else r.naked_below
    # First liquidity at least half an ATR away
    tgt = next(((p, n) for p, n in targets if abs(p - r.last) >= 0.5 * r.atr), None)
    risk = abs(r.last - stop)
    if not tgt:
        # No untouched liquidity in range: fall back to the day's ATR projection
        proj = r.day_low + r.atr if direction == 1 else r.day_high - r.atr
        if abs(proj - r.last) >= 0.5 * r.atr and (proj - r.last) * direction > 0:
            tgt = (proj, "daily ATR projection")
    if tgt:
        reward = abs(tgt[0] - r.last)
        rr = round(reward / risk, 1) if risk else 0
        target, target_name = tgt
    else:
        target, target_name, rr = None, "no clear liquidity", 0
    return {"dir": direction, "kind": kind, "why": why, "entry": r.last, "stop": stop,
            "target": target, "target_name": target_name, "rr": rr}
