"""Daily setups: daily SFPs and weekly expansion frameworks, once a day after the close.

The intraday scanner looks for things moving now. This agent looks at the daily
candle that just closed, on every market, and asks Dave's daily questions:

  * Daily SFP: did the day trade through an untouched daily swing high / low (or
    the previous week's high / low) and close back on the other side of it?
    The swept level is the swing failure point. The target is the opposite end
    of the leg that made it ("the high connected to it" for a bullish SFP),
    then the next untouched liquidity beyond.
  * Weekly framework: where the day sits in the week. A Tuesday SFP of
    Monday's high is the classic expansion raid, a Wednesday SFP of the week's
    extreme is the midweek reversal, a Thursday SFP is the Thursday reversal
    with Friday continuation. Also flags continuation days (Wednesday closes
    beyond 50% of Tuesday; small Monday and Tuesday closes outside it) and
    consolidation weeks (Thursday / Friday expansion expected).

Each setup gets a 0-100 quality score from the candle itself (how far back it
closed, body, size, sweep depth, how significant the level was, room to target).
Macro call, COT positioning and the 200-day regime are shown as confluence and
move the ranking, but nothing here is rejected: these are setups to investigate
on the hourly, not trades.

Shown as a "Daily SFP bias" section in the #general morning post (four_charts,
8:40am Sydney, never before 17:15 New York so the daily candle has closed). With
DISCORD_DAILY_WEBHOOK set, a full-detail post also goes to its own channel at
DAILY_SETUPS_NY (17:15 New York).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from statistics import mean
from zoneinfo import ZoneInfo

from . import config, gates, macro, regime, sessions
from .metrics import WEEKDAYS, Day, atr, realised_vol, to_days, trading_day, week_key

log = logging.getLogger("scanner.daily_setups")
NY = ZoneInfo("America/New_York")
SYD = ZoneInfo("Australia/Sydney")

WEBHOOK = os.getenv("DISCORD_DAILY_WEBHOOK", "")
POST_AT = os.getenv("DAILY_SETUPS_NY", "") or "17:15"          # New York time, Mon-Fri closes
MIN_SCORE = int(os.getenv("DAILY_SETUP_MIN_SCORE", "") or 60)   # detail post at or above this
MAX_DETAIL = int(os.getenv("DAILY_SETUP_MAX", "") or 4)         # detailed embeds per post
SWING_LOOKBACK = 30          # trading days searched for untouched swing points
HOLD_DAYS = 5                # backtest: trading days a setup is given to work
STATE_FILE = os.getenv("DAILY_SETUPS_STATE_FILE", "daily_setups_state.json")
GREEN, RED, GREY = 0x1F6B52, 0xA33A2A, 0x5D6661


# ─────────────────────────────────────────────
# DAYS AND LEVELS
# ─────────────────────────────────────────────
def completed_days(days: list[Day], now: datetime) -> list[Day]:
    """Drop the trading day still in progress (a day is done at the 17:00 NY close)."""
    cur = trading_day(now + timedelta(hours=1))
    return [d for d in days if d.d < cur]


def swing_levels(days: list[Day], lookback: int = SWING_LOOKBACK) -> tuple[list[dict], list[dict]]:
    """Untouched daily swing highs / lows before the last day, plus the previous week's high / low.

    A swing high beats the two days before it and isn't beaten by the day after.
    Untouched means no later day (before the last one) traded through it, so the
    last day's candle is the first to reach it.
    """
    hist, last = days[:-1], days[-1]
    start = max(2, len(hist) - lookback)
    highs, lows = [], []
    for i in range(start, len(hist)):
        d = hist[i]
        nxt = hist[i + 1] if i + 1 < len(hist) else None
        later = hist[i + 1:]
        if d.h > hist[i - 1].h and d.h > hist[i - 2].h and (nxt is None or d.h >= nxt.h) \
                and all(x.h < d.h for x in later):
            highs.append({"price": d.h, "idx": i, "age": len(hist) - i, "name": f"{d.d:%a %-d %b} swing high"})
        if d.l < hist[i - 1].l and d.l < hist[i - 2].l and (nxt is None or d.l <= nxt.l) \
                and all(x.l > d.l for x in later):
            lows.append({"price": d.l, "idx": i, "age": len(hist) - i, "name": f"{d.d:%a %-d %b} swing low"})
    # Previous week's high / low (as at the last day), if untouched since
    wk = week_key(last.d)
    prev_keys = sorted({week_key(d.d) for d in hist if week_key(d.d) < wk})
    if prev_keys:
        pw = [d for d in hist if week_key(d.d) == prev_keys[-1]]
        after = [d for d in hist if week_key(d.d) == wk]
        ph = max(pw, key=lambda d: d.h)
        pl = min(pw, key=lambda d: d.l)
        if all(x.h < ph.h for x in after):
            highs.append({"price": ph.h, "idx": hist.index(ph), "age": len(hist) - hist.index(ph),
                          "name": "previous week high", "weekly": True})
        if all(x.l > pl.l for x in after):
            lows.append({"price": pl.l, "idx": hist.index(pl), "age": len(hist) - hist.index(pl),
                         "name": "previous week low", "weekly": True})
    return highs, lows


def _naked_beyond(days: list[Day], price: float, direction: int, exclude: float | None = None) -> list[tuple[float, str]]:
    """Untouched daily highs (direction +1) above `price` or lows (-1) below it, nearest first."""
    hist = days[:-1]
    out = []
    for i, d in enumerate(hist[-SWING_LOOKBACK:]):
        later = hist[-SWING_LOOKBACK:][i + 1:] + [days[-1]]
        if direction == 1 and d.h > price and all(x.h < d.h for x in later):
            out.append((d.h, f"{d.d:%a %-d %b} high"))
        if direction == -1 and d.l < price and all(x.l > d.l for x in later):
            out.append((d.l, f"{d.d:%a %-d %b} low"))
    out = [x for x in out if exclude is None or abs(x[0] - exclude) > 1e-9]
    out.sort(key=lambda x: x[0] if direction == 1 else -x[0])
    return out


# ─────────────────────────────────────────────
# DAILY SFP
# ─────────────────────────────────────────────
def find_sfp(days: list[Day], a: float) -> dict | None:
    """Daily SFP on the last (completed) day, or None."""
    if len(days) < 8 or a <= 0:
        return None
    c = days[-1]
    highs, lows = swing_levels(days)
    cands = []
    swept_h = [x for x in highs if c.h > x["price"] and c.c < x["price"]]
    swept_l = [x for x in lows if c.l < x["price"] and c.c > x["price"]]
    if swept_h:
        cands.append((-1, max(swept_h, key=lambda x: x["price"])))
    if swept_l:
        cands.append((1, min(swept_l, key=lambda x: x["price"])))
    if not cands:
        return None
    best = None
    for direction, lvl in cands:
        s = _build_sfp(days, a, direction, lvl)
        if s and (best is None or s["score"] > best["score"]):
            best = s
    return best


def _build_sfp(days: list[Day], a: float, direction: int, lvl: dict) -> dict | None:
    c = days[-1]
    hist = days[:-1]
    rng = c.h - c.l
    if rng <= 0:
        return None
    entry = c.c                                    # next 6pm open, near enough
    stop = (c.h + 0.1 * a) if direction == -1 else (c.l - 0.1 * a)
    risk = abs(entry - stop)
    # Target: the other end of the leg that made the swept level
    leg = hist[lvl["idx"] + 1:] if lvl["idx"] + 1 < len(hist) else []
    t1 = None
    if leg:
        if direction == -1:
            low_day = min(leg, key=lambda d: d.l)
            if low_day.l < entry:
                t1 = (low_day.l, f"leg low {low_day.d:%a %-d %b}")
        else:
            high_day = max(leg, key=lambda d: d.h)
            if high_day.h > entry:
                t1 = (high_day.h, f"leg high {high_day.d:%a %-d %b}")
    nxt = _naked_beyond(days, entry, direction)
    if t1 is None or abs(t1[0] - entry) < 0.5 * a:
        far = [x for x in nxt if abs(x[0] - entry) >= 0.5 * a]
        t1 = far[0] if far else ((entry + direction * a), "1 daily ATR")
    t2 = next((x for x in nxt if (x[0] - t1[0]) * direction > 0.25 * a), None)
    rr = round(abs(t1[0] - entry) / risk, 1) if risk else 0.0
    # OTE reference: halfway up the SFP wick (above the body on a bearish SFP, below it on a bullish one)
    ote = (max(c.o, c.c) + c.h) / 2 if direction == -1 else (min(c.o, c.c) + c.l) / 2
    ote_risk = abs(stop - ote)
    ote_rr = round(abs(t1[0] - ote) / ote_risk, 1) if ote_risk else 0.0

    # Quality score, 0-100
    close_back = (c.h - c.c) / rng if direction == -1 else (c.c - c.l) / rng
    depth = (c.h - lvl["price"]) / a if direction == -1 else (lvl["price"] - c.l) / a
    body_ok = (c.c < c.o) if direction == -1 else (c.c > c.o)
    parts = {
        "close": 25 if close_back >= 0.66 else 15 if close_back >= 0.5 else 5,
        "body": 10 if body_ok else 0,
        "size": 15 if rng >= a else 8 if rng >= 0.7 * a else 0,
        "depth": 15 if 0.05 <= depth <= 0.6 else 8 if depth < 0.05 else 5,
        "level": 15 if lvl.get("weekly") or lvl["age"] >= 10 else 10 if lvl["age"] >= 5 else 5,
        "room": 20 if rr >= 3 else 12 if rr >= 2 else 5 if rr >= 1.5 else 0,
    }
    why = [f"Swept the {lvl['name']} ({lvl['price']:.6g}) by {depth:.2f} ATR and closed back "
           f"{'below' if direction == -1 else 'above'} it",
           f"Close in the {'bottom' if direction == -1 else 'top'} {round(100 * (1 - close_back))}% of the candle"
           + ("" if body_ok else ", but the body closed the wrong colour"),
           f"Range {rng / a:.1f}x ATR"]
    return {"kind": "sfp", "dir": direction, "level": lvl, "entry": entry, "stop": stop, "risk": risk,
            "target": t1, "target2": t2, "rr": rr, "ote": ote, "ote_rr": ote_rr, "score": sum(parts.values()), "parts": parts, "why": why,
            "close_back": round(close_back, 2), "depth": round(depth, 2), "day": c.d}


# ─────────────────────────────────────────────
# SWING POINTS IN PLAY
# ─────────────────────────────────────────────
IN_PLAY_ATR = float(os.getenv("DAILY_IN_PLAY_ATR", "") or 0.75)   # how close (in daily ATR) counts as in play


def in_play(root: str, days: list[Day], a: float) -> list[dict]:
    """Untouched daily swing highs / lows (and last week's high / low) within IN_PLAY_ATR of the
    last close: where a daily SFP could form next session. Nearest high and nearest low only."""
    if len(days) < 8 or a <= 0:
        return []
    c = days[-1]
    # Treat every completed day as history, so "untouched" includes the last day too
    highs, lows = swing_levels(days + [Day(c.d, c.c, c.c, c.c, c.c, 0, [])])
    out = []
    up = sorted((x for x in highs if c.c < x["price"] <= c.c + IN_PLAY_ATR * a), key=lambda x: x["price"])
    dn = sorted((x for x in lows if c.c - IN_PLAY_ATR * a <= x["price"] < c.c), key=lambda x: -x["price"])
    for lvl, side in ((up[0] if up else None, "high"), (dn[0] if dn else None, "low")):
        if not lvl:
            continue
        dist = abs(lvl["price"] - c.c) / a
        out.append({"kind": "swing", "root": root, "side": side, "level": lvl, "dist_atr": round(dist, 2),
                    "dir": -1 if side == "high" else 1, "score": 0, "atr": a, "day": c.d, "entry": c.c})
    return out


# ─────────────────────────────────────────────
# WEEKLY FRAMEWORKS
# ─────────────────────────────────────────────
def week_context(days: list[Day], a: float) -> dict:
    """Where the last day sits in its week, by Dave's weekly expansion rules."""
    c = days[-1]
    wk = [d for d in days if week_key(d.d) == week_key(c.d)]
    by = {d.d.weekday(): d for d in wk}
    mon, tue, wed = by.get(0), by.get(1), by.get(2)
    before = [d for d in wk if d.d < c.d]
    out = {"weekday": WEEKDAYS[c.d.weekday()], "notes": [], "frameworks": []}
    if mon and a:
        out["monday_small"] = (mon.h - mon.l) < 0.8 * a
    if before:
        out["week_high"] = max(d.h for d in before)
        out["week_low"] = min(d.l for d in before)
    wd = c.d.weekday()

    # Continuation day after a quiet Monday: Tuesday closes outside Monday's range
    if wd == 1 and mon and out.get("monday_small"):
        if c.c > mon.h and c.c > c.o:
            out["frameworks"].append({"kind": "continuation", "dir": 1, "name": "Wednesday continuation day",
                                      "text": "Monday didn't range and Tuesday closed above Monday's high: Wednesday bullish continuation."})
        elif c.c < mon.l and c.c < c.o:
            out["frameworks"].append({"kind": "continuation", "dir": -1, "name": "Wednesday continuation day",
                                      "text": "Monday didn't range and Tuesday closed below Monday's low: Wednesday bearish continuation."})
    # Continuation rule: Wednesday closes beyond 50% of Tuesday in its direction
    if wd == 2 and tue:
        mid = (tue.h + tue.l) / 2
        if c.c < mid and c.c < c.o:
            out["frameworks"].append({"kind": "continuation", "dir": -1, "name": "Thursday continuation day",
                                      "text": "Wednesday closed bearish below 50% of Tuesday: Thursday bearish continuation."})
        elif c.c > mid and c.c > c.o:
            out["frameworks"].append({"kind": "continuation", "dir": 1, "name": "Thursday continuation day",
                                      "text": "Wednesday closed bullish above 50% of Tuesday: Thursday bullish continuation."})
    # Consolidation week: Mon-Wed range small, expect Thursday / Friday to move
    if wd == 2 and mon and tue:
        r3 = max(d.h for d in (mon, tue, c)) - min(d.l for d in (mon, tue, c))
        if r3 < 1.3 * a:
            out["frameworks"].append({"kind": "consolidation", "dir": 0, "name": "Consolidation week",
                                      "text": f"Monday to Wednesday spans only {r3 / a:.1f} ATR: Thursday and Friday are the "
                                              f"likely expansion. Watch for the raid of the Mon-Wed high or low."})
    return out


def sfp_framework(sfp: dict, ctx: dict, days: list[Day]) -> str | None:
    """Name the weekly framework an SFP belongs to, from the day it formed and what it swept."""
    c = days[-1]
    wd = c.d.weekday()
    wk = [d for d in days if week_key(d.d) == week_key(c.d) and d.d < c.d]
    swept_week = False
    if wk:
        if sfp["dir"] == -1:
            swept_week = c.h > max(d.h for d in wk) and c.c < max(d.h for d in wk)
        else:
            swept_week = c.l < min(d.l for d in wk) and c.c > min(d.l for d in wk)
    side = "high" if sfp["dir"] == -1 else "low"
    if wd == 0:
        return "Monday SFP: Monday is let pass, so wait for Tuesday to confirm"
    if wd == 1 and swept_week:
        return f"Classic expansion raid: Tuesday took Monday's {side} and closed back" + \
               (" (Monday was small)" if ctx.get("monday_small") else "")
    if wd == 2 and swept_week:
        return f"Midweek reversal: Wednesday took the week's {side} and closed back"
    if wd == 3 and swept_week:
        return f"Thursday reversal, Friday continuation: Thursday took the week's {side} and closed back"
    if wd == 4:
        return "Friday SFP: next week's prep (weekly candle context below)"
    return None


def weekly_sfp(days: list[Day]) -> dict | None:
    """On a Friday close: did this week take the previous week's high / low and close back?"""
    c = days[-1]
    if c.d.weekday() != 4:
        return None
    keys = sorted({week_key(d.d) for d in days})
    if len(keys) < 2:
        return None
    cur = [d for d in days if week_key(d.d) == keys[-1]]
    prev = [d for d in days if week_key(d.d) == keys[-2]]
    ph, pl = max(d.h for d in prev), min(d.l for d in prev)
    wh, wl = max(d.h for d in cur), min(d.l for d in cur)
    if wh > ph and c.c < ph:
        return {"dir": -1, "text": "Weekly SFP: this week took last week's high and closed back below it."}
    if wl < pl and c.c > pl:
        return {"dir": 1, "text": "Weekly SFP: this week took last week's low and closed back above it."}
    return None


# ─────────────────────────────────────────────
# ONE MARKET
# ─────────────────────────────────────────────
def analyse(root: str, days: list[Day]) -> list[dict]:
    """All setups on the last completed day of `days` (completed days only)."""
    if len(days) < 22:
        return []
    a = atr(days, 20)
    if a <= 0:
        return []
    ctx = week_context(days, a)
    out = []
    sfp = find_sfp(days, a)
    if sfp:
        sfp["framework"] = sfp_framework(sfp, ctx, days)
        if sfp["framework"] and not sfp["framework"].startswith(("Monday", "Friday")):
            sfp["score"] = min(100, sfp["score"] + 5)
            sfp["parts"]["week"] = 5
        w = weekly_sfp(days)
        if w and w["dir"] == sfp["dir"]:
            sfp["weekly"] = w["text"]
            sfp["score"] = min(100, sfp["score"] + 5)
        out.append({**sfp, "root": root, "atr": a, "ctx": ctx})
    for f in ctx["frameworks"]:
        if sfp and f.get("dir") == sfp["dir"]:
            out[0].setdefault("also", []).append(f["text"])
            continue
        c = days[-1]
        rng = c.h - c.l
        strength = ((c.c - c.l) / rng if f["dir"] == 1 else (c.h - c.c) / rng) if rng and f["dir"] else 0.5
        sc = 40 + (20 if strength >= 0.66 else 10 if strength >= 0.5 else 0) + (10 if rng >= a else 0)
        if f["kind"] == "consolidation":
            sc = 45
        out.append({**f, "root": root, "atr": a, "ctx": ctx, "score": sc, "day": c.d, "entry": c.c})
    return out


def confluence(setup: dict, macro_call: dict | None, pos: dict | None, reg: dict | None) -> dict:
    """Macro, positioning and regime lines for a directional setup, and a ranking adjustment."""
    d = setup.get("dir") or 0
    lines, adj = [], 0
    if not d:
        return {"lines": [], "adj": 0}
    mc = macro.for_market(macro_call, setup["root"])
    if mc:
        want = "bullish" if d == 1 else "bearish"
        if mc["bias"] == want:
            lines.append(f"✅ Macro call {mc['bias']} (conviction {mc.get('conviction', '?')})")
            adj += 10
        elif mc["bias"] == "neutral":
            lines.append("➖ Macro call neutral")
        else:
            lines.append(f"❌ Macro call {mc['bias']}: against this setup")
            adj -= 10
    else:
        lines.append("➖ No macro call for this market today")
    g = gates.positioning_gate(pos, d)
    icon = {"support": "✅", "pass": "➖", "fail": "❌", "n/a": "➖"}[g["status"]]
    adj += {"support": 5, "fail": -10}.get(g["status"], 0)
    lines.append(f"{icon} {g['text']}")
    step, why = regime.ceiling(reg, d)
    if step:
        lines.append(f"📏 200-day: {regime.STEP_TEXT[step]} ({why[0]})")
    return {"lines": lines, "adj": adj, "size_step": step}


# ─────────────────────────────────────────────
# DISCORD
# ─────────────────────────────────────────────
def _fmt(x: float | None, a: float) -> str:
    if x is None:
        return "n/a"
    dp = 0 if a >= 50 else 1 if a >= 5 else 2 if a >= 0.5 else 3 if a >= 0.05 else 5
    return f"{x:,.{dp}f}"


COMMODITY_CLASSES = ("energy", "metals", "ags")


def expected_move(close: float, iv: float | None = None, rv20: float | None = None) -> dict | None:
    """1 standard deviation daily move = price x vol / 100 / 16 (16 ~ square root of 252 trading days).
    Uses the implied vol index (OVX for crude, GVZ for gold) when there is one, else 20-day realised vol."""
    vol, src = (iv, "IV") if iv else (rv20, "realised") if rv20 else (None, None)
    if not vol or not close:
        return None
    return {"pts": close * vol / 100 / 16, "vol": vol, "src": src, "pct": vol / 16}


def _em(e: dict | None, a: float) -> str:
    if not e:
        return ""
    return f"1 STD ±{_fmt(e['pts'], a)} ({e['src']})"


def _arrow(d: int) -> str:
    return "🟢 Bullish" if d == 1 else "🔴 Bearish" if d == -1 else "⚪ Neutral"


HOURLY_PLAN = {
    1: ("On the hourly: let price come back below the 6PM open (discount) into the SFP wick, then your entry "
        "signature: sweep of an hourly range low, displacement, FVG / IFVG or CISD. Stop beyond the SFP low. "
        "Never long above the 6PM open."),
    -1: ("On the hourly: let price come back above the 6PM open (premium) into the SFP wick, then your entry "
         "signature: sweep of an hourly range high, displacement, FVG / IFVG or CISD. Stop beyond the SFP high. "
         "Never short below the 6PM open."),
}


def detail_embed(s: dict, m) -> dict:
    a = s["atr"]
    name = f"{m.root} · {m.name}"
    if s["kind"] == "sfp":
        lvl = s["level"]
        t1, t2 = s["target"], s.get("target2")
        desc = [f"**{_arrow(s['dir'])} daily SFP** · score **{s['score']}**/100 · {s['ctx']['weekday']} close"]
        if s.get("framework"):
            desc.append(f"📅 {s['framework']}")
        if s.get("weekly"):
            desc.append(f"🗓️ {s['weekly']}")
        for x in s.get("also", []):
            desc.append(f"➕ {x}")
        fields = [
            {"name": "Why", "value": "\n".join(f"• {w}" for w in s["why"])[:1000], "inline": False},
            {"name": "Levels", "value": (
                f"SFP level: **{_fmt(lvl['price'], a)}** ({lvl['name']})\n"
                f"6PM open (≈ close): {_fmt(s['entry'], a)}\n"
                f"Stop beyond the SFP: {_fmt(s['stop'], a)}\n"
                f"OTE (50% of the SFP wick): {_fmt(s['ote'], a)}\n"
                f"Target 1: **{_fmt(t1[0], a)}** ({t1[1]}) · **{s['rr']}R** from the open, **{s['ote_rr']}R** from OTE\n"
                + (f"Target 2: {_fmt(t2[0], a)} ({t2[1]})" if t2 else "Target 2: none in range")), "inline": False},
            {"name": "Plan", "value": HOURLY_PLAN[s["dir"]], "inline": False},
        ]
    else:
        desc = [f"**{_arrow(s.get('dir', 0))} · {s['name']}** · score **{s['score']}**/100", s["text"]]
        fields = []
        if s.get("dir"):
            fields.append({"name": "Plan", "value": (
                "Continuation day: Asia expands, London may retrace (worst case to the 6PM open). Entry in OTE "
                + ("below" if s["dir"] == 1 else "above") + " the 6PM open with an entry signature, hold toward the "
                "expected daily move."), "inline": False})
    conf = s.get("conf", {}).get("lines")
    if conf:
        fields.append({"name": "Confluence (shown, not filtered)", "value": "\n".join(conf)[:1000], "inline": False})
    color = GREEN if s.get("dir") == 1 else RED if s.get("dir") == -1 else GREY
    return {"title": f"📅 {name}", "description": "\n".join(desc)[:4000], "color": color, "fields": fields}


def summary_embed(rows: list[tuple], day: date, scanned: int) -> dict:
    lines = []
    for s, m in rows:
        tag = "SFP" if s["kind"] == "sfp" else s["name"]
        extra = f" · {s['rr']}R open / {s['ote_rr']}R OTE" if s["kind"] == "sfp" else ""
        lines.append(f"`{s['rank']:>3}` **{m.root}** {_arrow(s.get('dir', 0))} · {tag}{extra}")
    nxt = WEEKDAYS[(day.weekday() + 1) % 7] if day.weekday() < 4 else "Monday (next week)"
    head = (f"Daily candles closed {day:%a %-d %b}. {scanned} markets scanned. "
            f"Setups for **{nxt}**, best first. Rank = quality score plus confluence.")
    return {"title": "📅 Daily setups: investigate on the hourly",
            "description": (head + "\n\n" + ("\n".join(lines) if lines else "No daily setups today. "
                            "No SFPs and no weekly framework signals."))[:4000],
            "color": GREY,
            "footer": {"text": "Detail below for the top setups. A setup is a reason to look, not a trade."}}


# ─────────────────────────────────────────────
# LIVE
# ─────────────────────────────────────────────
async def scan(px, http, now: datetime | None = None) -> tuple[date | None, list[tuple], int]:
    """Every market's last completed daily candle. Returns (day, [(setup, market)] best first, markets scanned)."""
    from .projectx import UNIT_HOUR, pick_front_contracts
    now = now or datetime.now(timezone.utc)
    wanted = {m.root for m in config.MARKETS if m.root not in config.EXCLUDE_ROOTS}
    contracts = pick_front_contracts(await px.available_contracts(), wanted)
    macro_call = await macro.latest(http)
    found, last_day, scanned = [], None, 0
    for root, c in contracts.items():
        m = config.MARKETS_BY_ROOT[root]
        try:
            hourly = await px.bars(c["id"], UNIT_HOUR, 1, days_back=75)
        except Exception as e:  # noqa: BLE001
            log.warning("Daily setups: bars failed for %s: %s", root, e)
            continue
        days = completed_days(to_days(hourly), now)
        if len(days) < 22:
            continue
        scanned += 1
        last_day = max(last_day or days[-1].d, days[-1].d)
        a = atr(days, 20)
        iv = await gates.implied_vol(http, m.iv_series) if m.iv_series else None
        em = expected_move(days[-1].c, (iv or {}).get("iv"), realised_vol(days))
        if em and m.asset_class in COMMODITY_CLASSES:
            found.append(({"kind": "move", "root": root, "day": days[-1].d, "score": -1, "rank": -999,
                           "atr": a, "close": days[-1].c, "em": em}, m))
        for sw in in_play(root, days, a):
            sw["rank"] = -sw["dist_atr"]
            sw["em"] = em
            found.append((sw, m))
        for st in analyse(root, days):
            st["em"] = em
            if st.get("dir"):
                pos = await gates.cot(http, m)
                reg = await regime.regime_for(http, px, root, c)
                st["conf"] = confluence(st, macro_call, pos, reg)
            st["rank"] = st["score"] + st.get("conf", {}).get("adj", 0)
            found.append((st, m))
    # Only setups on the latest close (a market with a stale feed would show an old day)
    found = [x for x in found if x[0]["day"] == last_day]
    found.sort(key=lambda x: -x[0]["rank"])
    return last_day, found, scanned


async def run_once(px, http, now: datetime | None = None) -> tuple[date | None, list[dict]]:
    """The standalone post (summary + detail embeds). Returns (day, embeds)."""
    last_day, found, scanned = await scan(px, http, now)
    if last_day is None:
        return None, []
    found = [x for x in found if x[0]["kind"] != "move"]
    listed = [x for x in found if x[0]["kind"] != "swing" and x[0]["score"] >= MIN_SCORE - 15][:12]
    detail = [x for x in found if x[0]["score"] >= MIN_SCORE and x[0].get("dir") and x[0]["kind"] != "swing"][:MAX_DETAIL]
    return last_day, [summary_embed(listed, last_day, scanned)] + [detail_embed(st, m) for st, m in detail]


def _chunk(title: str, lines: list[str], color: int, footer: str | None = None, limit: int = 3800) -> list[dict]:
    """Lines into as many embeds as needed, each under Discord's description limit."""
    out, cur = [], ""
    for ln in lines:
        if cur and len(cur) + len(ln) + 1 > limit:
            out.append(cur.strip())
            cur = ""
        cur = f"{cur}\n{ln}" if cur else ln
    if cur:
        out.append(cur.strip())
    embeds = [{"title": title if i == 0 else f"{title} (cont.)", "color": color, "description": d}
              for i, d in enumerate(out)]
    if embeds and footer:
        embeds[-1]["footer"] = {"text": footer}
    return embeds


def bias_embeds(last_day: date | None, found: list[tuple], scanned: int) -> list[dict]:
    """The #general SFP section: EVERY daily SFP on every market, any score, no cap.

    Three groups, best first, each sorted by score:
      ⭐ macro call agrees and nothing against
      ✅ clean SFP, macro neutral or no call
      ⚠️ macro call disagrees or positioning is crowded against: still listed and labelled,
         because a squeeze against the macro call can be the best trade of the week
    Then weekly frameworks and swing points in play. Split over several messages when long."""
    title = "📅 Daily SFP bias: seek the entry on the hourly"
    if last_day is None:
        return [{"title": title, "color": GREY, "description": "No daily data from ProjectX this morning, so no SFP read."}]
    cl = lambda st: st.get("conf", {}).get("lines", [])
    macro_bad = lambda st: any(l.startswith("❌ Macro call") for l in cl(st))
    pos_bad = lambda st: any(l.startswith("❌") and not l.startswith("❌ Macro call") for l in cl(st))
    macro_ok = lambda st: any(l.startswith("✅ Macro call") for l in cl(st))
    by_score = lambda xs: sorted(xs, key=lambda x: -x[0]["score"])
    all_sfp = [x for x in found if x[0]["kind"] == "sfp"]
    star = by_score(x for x in all_sfp if macro_ok(x[0]) and not pos_bad(x[0]))
    clean = by_score(x for x in all_sfp if not macro_ok(x[0]) and not macro_bad(x[0]) and not pos_bad(x[0]))
    flagged = by_score(x for x in all_sfp if macro_bad(x[0]) or pos_bad(x[0]))
    frames = [x for x in found if x[0]["kind"] not in ("sfp", "swing", "move")]
    moves = [x for x in found if x[0]["kind"] == "move"]
    swings = sorted((x for x in found if x[0]["kind"] == "swing"), key=lambda x: x[0]["dist_atr"])
    sfp_roots = {m.root for _, m in all_sfp}
    swings = [x for x in swings if x[1].root not in sfp_roots]

    def line(st, m, flag=""):
        a = st["atr"]
        fw = f" · {st['framework'].split(':')[0]}" if st.get("framework") else ""
        ok = sum(1 for l in cl(st) if l.startswith("✅"))
        return (f"**{m.root}** {_arrow(st['dir'])} · score {st['score']}{fw}{flag}{sessions.board_tag(m.root)}\n"
                f"  Swept {_fmt(st['level']['price'], a)} ({st['level']['name']}) · "
                f"OTE {_fmt(st['ote'], a)} · stop {_fmt(st['stop'], a)} · "
                f"T1 {_fmt(st['target'][0], a)} ({st['ote_rr']}R from OTE)"
                + (f" · {ok} confluence ✅" if ok else "")
                + (f" · {_em(st.get('em'), a)}" if st.get("em") else ""))

    def flag(st):
        tags = []
        if macro_bad(st):
            tags.append("⚠️ **macro doesn't align: could be a squeeze**")
        if pos_bad(st):
            tags.append("⚠️ **positioning crowded against**")
        return " · " + " · ".join(tags)

    n = len(all_sfp)
    lines = [f"Daily candles closed {last_day:%a %-d %b} · all {scanned} markets checked · "
             f"**{n} daily SFP{'s' if n != 1 else ''}**, all listed below."]
    lines.append("\n**⭐ Daily SFP + macro call agree**: watch these first")
    lines += [line(*x) for x in star] or ["None today."]
    lines.append("\n**✅ Daily SFP, macro neutral or no call**")
    lines += [line(*x) for x in clean] or ["None today."]
    lines.append("\n**⚠️ Daily SFP against the macro call or positioning**")
    lines += [line(st, m, flag(st)) for st, m in flagged] or ["None today."]
    if frames:
        lines.append("\n**Weekly frameworks**")
        lines += [f"**{m.root}** {_arrow(st.get('dir', 0))} · {st['name']}" for st, m in frames]
    if swings:
        lines.append(f"\n**Swing points in play** (untouched, within {IN_PLAY_ATR:g} ATR of the close)")
        lines += [f"**{m.root}** {'⬆️' if st['side'] == 'high' else '⬇️'} {_fmt(st['level']['price'], st['atr'])} "
                  f"{st['level']['name']} · {st['dist_atr']} ATR away · SFP {'short' if st['side'] == 'high' else 'long'} "
                  f"if it's taken and closes back"
                  + (f" · {_em(st.get('em'), st['atr'])}" if st.get("em") else "") for st, m in swings]
    if moves:
        order = {c: i for i, c in enumerate(COMMODITY_CLASSES)}
        moves.sort(key=lambda x: (order[x[1].asset_class], config.MARKETS.index(x[1])))
        lines.append("\n**Expected daily move, commodities** (1 STD: about 2 days in 3 stay inside this)")
        lines += [f"**{m.root}** {m.name} · {_fmt(st['close'], st['atr'])} · **±{_fmt(st['em']['pts'], st['atr'])}** "
                  f"({st['em']['pct']:.1f}%, {st['em']['src']} vol {st['em']['vol']:.1f}){sessions.board_tag(m.root)}"
                  for st, m in moves]
        syd = lambda r: sessions.schedule_lines(r)[0].replace("**Sydney:** ", "")
        lines.append(f"⏰ = short-hours market. Grains (ZC ZW ZS ZM ZL): {syd('ZC')} Sydney. "
                     f"Hogs and cattle (HE LE): {syd('HE')} Sydney.")
    color = GREY
    top = star or clean or flagged
    if top:
        color = GREEN if top[0][0]["dir"] == 1 else RED
    return _chunk(title, lines, color,
                  "Every daily SFP is listed; the score is the candle's quality. A setup is a reason to look, "
                  "not a trade. OTE above the 6PM open for shorts, below for longs.")


def bias_embed(last_day: date | None, found: list[tuple], scanned: int) -> dict:
    """First embed only (kept for older callers)."""
    return bias_embeds(last_day, found, scanned)[0]


def _load() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(s: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(s, f)
    except OSError as e:
        log.warning("Couldn't save daily setups state: %s", e)


def due(now: datetime, last_posted: str | None) -> date | None:
    """The trading day to post for, or None. Posts after POST_AT on a weekday close,
    catches up for up to 12 hours if the service was down at post time."""
    hh, mm = (int(x) for x in POST_AT.split(":"))
    t = now.astimezone(NY)
    for back in (0, 1):
        d = (t - timedelta(days=back)).date()
        if d.weekday() >= 5:
            continue
        slot = datetime(d.year, d.month, d.day, hh, mm, tzinfo=NY)
        if slot <= t < slot + timedelta(hours=12) and last_posted != d.isoformat():
            return d
    return None


async def loop(px, http, post, dry_run: bool):
    """Standalone post with full detail, only if DISCORD_DAILY_WEBHOOK is set.
    Otherwise the SFP bias rides in the #general morning post (four_charts)."""
    import asyncio
    if not WEBHOOK:
        log.info("Daily setups: no DISCORD_DAILY_WEBHOOK, so the SFP bias goes in the #general morning post only")
        return
    while True:
        try:
            now = datetime.now(timezone.utc)
            st = _load()
            d = due(now, st.get("posted"))
            if d:
                day, embeds = await run_once(px, http, now)
                if embeds:
                    hook = WEBHOOK
                    for i in range(0, len(embeds), 3):     # stay under Discord's 6,000 characters a message
                        await post(http, hook, embeds[i:i + 3], dry_run)
                st["posted"] = d.isoformat()
                _save(st)
                log.info("Daily setups posted for %s (%d embeds)", day, len(embeds))
        except Exception:  # noqa: BLE001
            log.exception("Daily setups failed; retrying in 5 minutes")
        await asyncio.sleep(300)


# ─────────────────────────────────────────────
# BACKTEST
# ─────────────────────────────────────────────
def simulate(setup: dict, after: list[Day], hold: int = HOLD_DAYS, mode: str = "open") -> dict:
    """Trade a setup on the days after it.

    mode "open": enter at the next day's open (the 6PM open).
    mode "ote":  limit order at the OTE (50% of the SFP wick), live for the next trading
                 day only; no fill, no trade.
    Then walk hourly bars until the stop or target 1, else out at the close of day `hold`.
    A bar that touches both counts as the stop, and so does the fill bar if it reaches the stop."""
    if not after:
        return {}
    d, stop, tgt = setup["dir"], setup["stop"], setup["target"][0]
    bars = [(i, b) for i, day in enumerate(after[:hold]) for b in day.bars]
    if mode == "ote":
        entry = setup["ote"]
        fill = next((k for k, (i, b) in enumerate(bars) if i == 0 and (b["h"] >= entry if d == -1 else b["l"] <= entry)), None)
        if fill is None:
            return {"out": "no fill"}
    else:
        entry, fill = after[0].o, 0
    risk = (entry - stop) * d
    if risk <= 0 or (tgt - entry) * d <= 0:
        return {}
    planned = round((tgt - entry) * d / risk, 2)
    mfe = 0.0
    for k, (i, b) in enumerate(bars[fill:]):
        hit_stop = b["l"] <= stop if d == 1 else b["h"] >= stop
        hit_tgt = (b["h"] >= tgt if d == 1 else b["l"] <= tgt) and not (mode == "ote" and k == 0)
        mfe = max(mfe, ((b["h"] - entry) if d == 1 else (entry - b["l"])) / risk)
        if hit_stop:
            return {"r": -1.0, "out": "stop", "mfe": round(mfe, 2), "planned_r": planned}
        if hit_tgt:
            return {"r": planned, "out": "target", "mfe": round(mfe, 2), "planned_r": planned}
    last = after[:hold][-1].c
    return {"r": round((last - entry) * d / risk, 2), "out": "time", "mfe": round(mfe, 2), "planned_r": planned}


def backtest_market(root: str, days: list[Day]) -> list[dict]:
    """Every daily SFP in `days`, scored and traded with the simple rules above."""
    trades = []
    for i in range(25, len(days) - 1):
        hist = days[:i + 1]
        if hist[-1].d.weekday() >= 5:
            continue
        for s in analyse(root, hist):
            if s["kind"] != "sfp":
                continue
            fw = s.get("framework") or "no weekly framework"
            for mode in ("open", "ote"):
                res = simulate(s, days[i + 1:], mode=mode)
                if not res:
                    continue
                trades.append({"root": root, "day": s["day"].isoformat(), "weekday": s["ctx"]["weekday"][:3],
                               "dir": "long" if s["dir"] == 1 else "short", "score": s["score"], "mode": mode,
                               "framework": fw.split(":")[0], "rr": s["rr"], "ote_rr": s["ote_rr"], **res})
    return trades


def summarise(trades: list[dict], key=None) -> list[dict]:
    groups: dict = {}
    for t in trades:
        if t.get("out") == "no fill":
            continue
        k = key(t) if key else "all"
        groups.setdefault(k, []).append(t)
    rows = []
    for k, ts in sorted(groups.items(), key=lambda x: str(x[0])):
        rs = [t["r"] for t in ts]
        rows.append({"group": k, "n": len(ts), "win_pct": round(100 * sum(1 for t in ts if t["r"] > 0) / len(ts)),
                     "target_pct": round(100 * sum(1 for t in ts if t["out"] == "target") / len(ts)),
                     "avg_r": round(mean(rs), 2), "total_r": round(sum(rs), 1),
                     "avg_mfe": round(mean(t["mfe"] for t in ts), 2)})
    return rows


async def run_backtest(px, roots: list[str], days_back: int) -> list[dict]:
    from .acd_backtest import stitched
    from .projectx import UNIT_HOUR, pick_front_contracts
    contracts = pick_front_contracts(await px.available_contracts(), set(roots))
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days_back + 45)
    trades = []
    for root in roots:
        c = contracts.get(root)
        if root not in config.MARKETS_BY_ROOT or not c:
            print(f"{root}: not offered by ProjectX right now, skipped")
            continue
        print(f"{root}: pulling {days_back} days of hourly history (stitching monthly contracts)...")
        hourly = await stitched(px, root, c["id"], start, end, UNIT_HOUR)
        days = completed_days(to_days(hourly), end)
        if len(days) < 30:
            print(f"{root}: only {len(days)} days of history, skipped")
            continue
        trades += backtest_market(root, days)
    return trades


def print_report(trades: list[dict]) -> None:
    if not trades:
        print("No daily SFPs found.")
        return

    def table(title, rows):
        print(f"\n{title}")
        print(f"{'':28} {'n':>4} {'win%':>5} {'tgt%':>5} {'avgR':>6} {'totR':>6} {'MFE':>5}")
        for r in rows:
            print(f"{str(r['group'])[:28]:28} {r['n']:>4} {r['win_pct']:>5} {r['target_pct']:>5} "
                  f"{r['avg_r']:>6} {r['total_r']:>6} {r['avg_mfe']:>5}")

    for mode, label in (("open", "ENTRY AT THE 6PM OPEN"), ("ote", "LIMIT AT OTE (50% of the SFP wick, next day only)")):
        ts = [t for t in trades if t["mode"] == mode]
        if mode == "ote":
            fills = sum(1 for t in ts if t["out"] != "no fill")
            print(f"\n===== {label}: filled {fills} of {len(ts)} =====")
        else:
            print(f"\n===== {label} =====")
        posted = [t for t in ts if t["score"] >= MIN_SCORE]
        table("All daily SFPs", summarise(ts))
        table(f"Score {MIN_SCORE}+ (what gets a detail post)", summarise(posted))
        table("By score band", summarise(ts, lambda t: f"{t['score'] // 10 * 10}-{t['score'] // 10 * 10 + 9}"))
        table("By weekly framework", summarise(ts, lambda t: t["framework"]))
        table(f"By market (score {MIN_SCORE}+)", summarise(posted, lambda t: t["root"]))
        table("By weekday", summarise(ts, lambda t: t["weekday"]))
    print(f"\nStop beyond the SFP, target 1, or out after {HOLD_DAYS} days. avgR is expectancy per trade, "
          "no costs. Stitched contracts can jump at rolls, so treat small edges as no edge.")


def save_csv(trades: list[dict]) -> str:
    import csv
    name = f"daily_sfp_backtest_{date.today().isoformat()}.csv"
    keys = ["root", "day", "weekday", "dir", "mode", "score", "framework", "rr", "ote_rr", "r", "out", "mfe", "planned_r"]
    with open(name, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(trades)
    return name
