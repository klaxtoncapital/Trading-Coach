"""Head of desk: checks every scanner idea before it reaches you.

Code rules only (fast, free, predictable). An idea is VERIFIED only if no
check fails. Every check's result travels with the idea, so you can see why
it passed, and rejected ideas are logged with the reason.

Checks
  1  Data is fresh            last bar inside DATA_STALE_MIN minutes
  2  Levels make sense        stop / entry / target on the right sides, R:R >= 2 and adds up
  3  Stop size is sane        between 0.1 and 1.5 daily ATR
  4  Entry is a real price    inside today's range
  5  Trend regime (Gate 2)    200-day state sets the size ceiling (never fails on its own)
  6  Macro call agrees        fails if today's macro call is the other way
  7  Positioning (Gate 3)     fails if the idea joins a crowded COT position
  8  Catalyst window (Gate 5) fails if a tier-1 event is inside CATALYST_BLOCK_MIN; warns if in the hold
  9  Headlines                warns on a high-priority headline for this market in the last NEWS_BLOCK_MIN
 10  No conflict              fails if the opposite idea on the same market was verified today
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from . import gates as gates_mod
from . import macro as macro_mod
from . import news as news_mod
from . import regime as regime_mod

DATA_STALE_MIN = int(os.getenv("DATA_STALE_MIN", "") or 90)
CATALYST_BLOCK_MIN = int(os.getenv("CATALYST_BLOCK_MIN", "") or 60)
MIN_RR = float(os.getenv("MIN_RR", "") or 2.0)

ICON = {"pass": "✅", "warn": "⚠️", "fail": "❌", "info": "➖"}


def _c(name: str, status: str, text: str) -> dict:
    return {"name": name, "status": status, "text": text}


def verify(r, gates: dict, reg: dict | None, macro_call: dict | None, now: datetime,
           verified_today: dict | None = None) -> dict:
    """Run every check on one idea. `r` is a metrics.Reading with a thesis."""
    t = r.thesis
    d = t["dir"]
    checks: list[dict] = []

    # 1 Fresh data
    last_t = getattr(r, "last_t", None)
    if last_t is None:
        checks.append(_c("Data", "warn", "No bar timestamp: couldn't confirm the data is live."))
    else:
        age = (now - last_t).total_seconds() / 60
        if age > DATA_STALE_MIN:
            checks.append(_c("Data", "fail", f"Last bar is {age:.0f} min old: data looks stale."))
        else:
            checks.append(_c("Data", "pass", f"Last bar {age:.0f} min old."))

    # 2 Levels
    e, s, tg = t["entry"], t["stop"], t["target"]
    if tg is None:
        checks.append(_c("Levels", "fail", "No target."))
    else:
        right_side = (s < e < tg) if d == 1 else (tg < e < s)
        risk = abs(e - s)
        rr = abs(tg - e) / risk if risk else 0
        if not right_side:
            checks.append(_c("Levels", "fail", "Stop or target is on the wrong side of the entry."))
        elif rr < MIN_RR or abs(rr - t["rr"]) > 0.15:
            checks.append(_c("Levels", "fail", f"R:R works out at {rr:.1f}, needs {MIN_RR:.0f}R or more."))
        else:
            checks.append(_c("Levels", "pass", f"Stop, entry and target line up: {rr:.1f}R."))

    # 3 Stop size
    risk = abs(e - s)
    if r.atr and not (0.1 * r.atr <= risk <= 1.5 * r.atr):
        checks.append(_c("Stop size", "fail", f"Stop is {risk / r.atr:.2f}x the daily ATR (allowed 0.1 to 1.5)."))
    else:
        checks.append(_c("Stop size", "pass", f"Stop is {risk / r.atr:.2f}x the daily ATR." if r.atr else "n/a"))

    # 4 Entry inside today's range
    tol = 0.05 * r.atr
    if not (r.day_low - tol <= e <= r.day_high + tol):
        checks.append(_c("Entry", "fail", "Entry isn't inside today's range: the price looks wrong."))
    else:
        checks.append(_c("Entry", "pass", "Entry is a live price inside today's range."))

    # 5 Regime
    step, why = regime_mod.ceiling(reg, d)
    if step is None:
        checks.append(_c("Trend regime", "info", why[0]))
    else:
        status = "warn" if step == "starter" else "pass"
        checks.append(_c("Trend regime", status, " ".join(why)))

    # 6 Macro
    mm = macro_mod.for_market(macro_call, r.root)
    want = "bullish" if d == 1 else "bearish"
    if mm is None:
        checks.append(_c("Macro call", "info", "No macro call for this market today."))
    elif mm["bias"] == want:
        checks.append(_c("Macro call", "pass", f"Macro call is {mm['bias']} (conviction {mm.get('conviction', '?')}/3)."
                         + (f" {mm['why']}" if mm.get("why") else "")))
    elif mm["bias"] == "neutral":
        checks.append(_c("Macro call", "warn", "Macro call is neutral today: no help from the big picture."))
    else:
        checks.append(_c("Macro call", "fail", f"Macro call is {mm['bias']} for {r.root}: the idea fights it."
                         + (f" {mm['why']}" if mm.get("why") else "")))

    # 7 Positioning
    pos = gates["positioning"]
    if pos["status"] == "fail":
        checks.append(_c("Positioning", "fail", pos["text"]))
    else:
        checks.append(_c("Positioning", "pass" if pos["status"] in ("pass", "support") else "info", pos["text"]))

    # 8 Catalyst
    cat = gates["catalyst"]
    soon = [ev for ev in cat.get("events", []) if now <= ev["t"] <= now + timedelta(minutes=CATALYST_BLOCK_MIN)]
    if soon:
        ev = soon[0]
        mins = (ev["t"] - now).total_seconds() / 60
        checks.append(_c("Catalyst", "fail", f"{gates_mod.label(ev)} is {mins:.0f} min away: too close to enter."))
    elif cat["status"] == "warn":
        checks.append(_c("Catalyst", "warn", cat["text"]))
    else:
        checks.append(_c("Catalyst", "pass", cat["text"]))

    # 9 Headlines
    heads = news_mod.recent([r.root], news_mod.headline_check_window(), now)
    if heads:
        h = heads[-1]
        checks.append(_c("Headlines", "warn", f"{h['source']}: \"{h['title'][:120]}\" ({len(heads)} high-priority in "
                         f"the last {news_mod.headline_check_window()} min). Check it before entering."))
    else:
        checks.append(_c("Headlines", "pass", "No high-priority headline for this market in the last "
                         f"{news_mod.headline_check_window()} min."))

    # 10 Conflict
    other = (verified_today or {}).get(f"{r.root}:{-d}")
    if other:
        checks.append(_c("Conflict", "fail", f"The opposite idea on {r.root} was verified earlier today."))
    else:
        checks.append(_c("Conflict", "pass", "No opposite idea on this market today."))

    failed = [c for c in checks if c["status"] == "fail"]
    return {
        "verdict": "rejected" if failed else "verified",
        "reason": "; ".join(f"{c['name']}: {c['text']}" for c in failed),
        "checks": checks,
        "ceiling": step,
        "ceiling_factor": regime_mod.STEP_FACTOR.get(step, 1.0) if step else 1.0,
    }


def checks_text(v: dict) -> str:
    return "\n".join(f"{ICON[c['status']]} **{c['name']}:** {c['text']}" for c in v["checks"])[:1020]
