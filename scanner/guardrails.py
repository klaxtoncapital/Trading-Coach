"""Guardrails built from Dave's own trade history (Oct 2026 autopsy).

The data said:
  * trades closed inside 10 minutes lost money; trades held 30+ minutes made it
  * days with 1-2 trades made money; 4+ trade days lost
  * re-entering within 10 minutes of a loss, or adding size after one, lost
  * London session (3pm-9pm Sydney) won 1 in 5

So the scanner:
  * posts no Watch ideas in the London window and caps ideas per day
  * watches the account (read-only) and posts a cool-down after every losing trade,
    a "that's the day" note after the trade cap or the personal daily stop,
    a reminder when a trade is closed inside 10 minutes, and a warning when
    size goes up after a loss
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SYD = ZoneInfo("Australia/Sydney")
NY = ZoneInfo("America/New_York")

MAX_IDEAS_PER_DAY = int(os.getenv("MAX_IDEAS_PER_DAY", "2"))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "2"))
COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", "30"))
QUICK_EXIT_MIN = int(os.getenv("QUICK_EXIT_MIN", "10"))
# Personal daily stop in dollars (0 = off). Set it below the firm's limit.
PERSONAL_DAILY_STOP = float(os.getenv("PERSONAL_DAILY_STOP", "0"))
# Alert when the day's profit reaches this (dollars, 0 = off): keeps the best day under the consistency cap.
DAILY_PROFIT_CAP = float(os.getenv("DAILY_PROFIT_CAP", "0"))
# London window in Sydney time, [start, end) hours
NO_IDEA_WINDOW = tuple(int(x) for x in os.getenv("NO_IDEA_WINDOW_SYD", "15,21").split(","))

RED, AMBER = 0xA33A2A, 0xB7791F


def in_no_idea_window(now: datetime) -> bool:
    h = now.astimezone(SYD).hour
    start, end = NO_IDEA_WINDOW
    return start <= h < end


def trading_day_start(now: datetime) -> datetime:
    """Start of the current CME trading day (18:00 New York) in UTC."""
    ny = now.astimezone(NY)
    start = ny.replace(hour=18, minute=0, second=0, microsecond=0)
    if ny.hour < 18:
        start -= timedelta(days=1)
    return start.astimezone(timezone.utc)


def _round_trips(fills: list[dict]) -> list[dict]:
    """Pair closing fills (profitAndLoss set) with the most recent opening fill on the same contract."""
    trips, last_open = [], {}
    for f in fills:
        if f.get("profitAndLoss") is None:
            last_open[f["contractId"]] = f
            continue
        o = last_open.get(f["contractId"])
        trips.append({
            "contract": f["contractId"], "close_t": f["t"], "close_id": f["id"],
            "pnl": float(f["profitAndLoss"]) - float(f.get("fees") or 0),
            "size": int(f.get("size") or 0),
            "open_t": o["t"] if o else None, "open_size": int(o.get("size") or 0) if o else None,
            "open_id": o["id"] if o else None,
        })
    return trips


def review_account(name: str, fills: list[dict], seen: set, now: datetime) -> list[dict]:
    """Return Discord embeds for anything new on this account today. Mutates `seen`."""
    embeds = []
    trips = _round_trips(fills)
    day_pnl = sum(t["pnl"] for t in trips)
    syd = lambda t: t.astimezone(SYD).strftime("%-I:%M%p").lower()
    contract = lambda c: str(c).split(".")[-2] if "." in str(c) else str(c)

    # Size up after a loss: an opening fill bigger than the last losing trade's size
    last_loss = None
    for f in fills:
        if f.get("profitAndLoss") is not None and float(f["profitAndLoss"]) < 0:
            last_loss = f
        elif f.get("profitAndLoss") is None and last_loss and int(f.get("size") or 0) > int(last_loss.get("size") or 0):
            key = f"sizeup:{f['id']}"
            if key not in seen:
                seen.add(key)
                embeds.append({"title": f"⚠️ Size went UP after a loss · {name}",
                               "description": (f"{contract(f['contractId'])}: {f.get('size')} contracts at {syd(f['t'])} after losing on "
                                               f"{last_loss.get('size')}. In your history this cost $1,657 across 13 trades. "
                                               "Cut back to your normal size."),
                               "color": RED})

    for i, t in enumerate(trips, 1):
        key = f"trip:{t['close_id']}"
        if key in seen:
            continue
        seen.add(key)
        held = (t["close_t"] - t["open_t"]).total_seconds() / 60 if t["open_t"] else None
        if t["pnl"] < 0:
            until = t["close_t"] + timedelta(minutes=COOLDOWN_MIN)
            embeds.append({"title": f"🛑 {COOLDOWN_MIN}-minute cool-down · {name}",
                           "description": (f"{contract(t['contract'])} closed {t['pnl']:+,.0f} at {syd(t['close_t'])}. "
                                           f"No new trades until **{syd(until)} Sydney**, and no extra size when you come back.\n"
                                           "Re-entries within 10 minutes of a loss won 27% of the time in your history."),
                           "color": RED})
        if held is not None and held < QUICK_EXIT_MIN:
            embeds.append({"title": f"⏱️ Closed after {held:.0f} min · {name}",
                           "description": ("Your trades closed inside 10 minutes lost $10,816 on Topstep. "
                                           "Unless that was your stop, set stop and target at entry and let it run."),
                           "color": AMBER})
        if i == MAX_TRADES_PER_DAY:
            embeds.append({"title": f"✅ {MAX_TRADES_PER_DAY} trades done · {name}",
                           "description": (f"That's the day (P&L today {day_pnl:+,.0f}). "
                                           "Days with 1-2 trades made money in your history; 4+ trade days lost. Close the charts."),
                           "color": AMBER})

    if DAILY_PROFIT_CAP and day_pnl >= DAILY_PROFIT_CAP:
        key = f"profitcap:{trading_day_start(now).date()}"
        if key not in seen:
            seen.add(key)
            embeds.append({"title": f"🏁 Consistency cap reached · {name}",
                           "description": (f"Up {day_pnl:+,.0f} today. Stop here: more profit today only raises the best-day "
                                           "figure for the consistency rule. Bank it and come back tomorrow."),
                           "color": 0x1F6B52})
    if PERSONAL_DAILY_STOP and day_pnl <= -PERSONAL_DAILY_STOP:
        key = f"dailystop:{trading_day_start(now).date()}"
        if key not in seen:
            seen.add(key)
            embeds.append({"title": f"⛔ Personal daily stop hit · {name}",
                           "description": f"Down {day_pnl:,.0f} today. Done until tomorrow's session. Nothing good happens after this point.",
                           "color": RED})
    return embeds
