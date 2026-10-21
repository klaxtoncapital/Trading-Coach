"""ACD agent: Mark Fisher's opening-range method (The Logical Trader).

For each market, on its own pit/cash open:
  * Opening range (OR): high and low of the first ACD_OR_MIN minutes (default 15).
  * A value: ACD_A_ATR x the 20-day ATR (default 0.10). A up = OR high + A,
    A down = OR low - A.
  * A up / A down CONFIRMED: price closes beyond the A level for half the OR
    time in a row (Fisher's hold rule).
  * FAILED A: price reaches the A level, then closes back inside the OR. A trap:
    the session's first push was a liquidity grab (ICT: sweep / turtle soup).
  * C: after a failed A, price reaches the opposite A level and holds it.
    Fisher's strongest reversal signal.
  * Pivot range from the prior trading day: (H+L)/2 and (H+L+C)/3. Price above
    it leans bullish, below bearish; a narrow pivot range warns of a trend day.

Posts new events to Discord, batched into one message per scan. Read-only:
never places orders. The same evaluate_day() drives the live posts and the
backtest, so what you test is what you get.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from statistics import mean
from zoneinfo import ZoneInfo

log = logging.getLogger("scanner.acd")

OR_MIN = int(os.getenv("ACD_OR_MIN", "") or 15)
A_ATR = float(os.getenv("ACD_A_ATR", "") or 0.10)
# Comma-separated roots to run ACD on; empty = every market with an open below.
ACD_ROOTS = {s.strip().upper() for s in os.getenv("ACD_ROOTS", "").split(",") if s.strip()}
DISCORD_ACD_WEBHOOK = os.getenv("DISCORD_ACD_WEBHOOK", "")

NY, CHI, TOK = ZoneInfo("America/New_York"), ZoneInfo("America/Chicago"), ZoneInfo("Asia/Tokyo")
SYD = ZoneInfo("Australia/Sydney")


@dataclass(frozen=True)
class Session:
    tz: ZoneInfo
    open: time       # opening range starts
    flat: time       # end of the session for ACD (Topstep flat time or the market's close)


# Fisher used each market's own pit open. Topstep flattens at 15:10 CT, so the
# US session ends at 15:05 CT; grains and meats close earlier.
_TOPSTEP_FLAT = time(15, 5)
SESSIONS: dict[str, Session] = {
    **{r: Session(NY, time(9, 30), _TOPSTEP_FLAT) for r in ("ES", "NQ", "YM", "RTY")},
    "NKD": Session(TOK, time(9, 0), time(15, 30)),
    **{r: Session(NY, time(9, 0), _TOPSTEP_FLAT) for r in ("CL", "NG", "RB", "HO")},
    "GC": Session(NY, time(8, 20), _TOPSTEP_FLAT),
    "SI": Session(NY, time(8, 25), _TOPSTEP_FLAT),
    "HG": Session(NY, time(8, 10), _TOPSTEP_FLAT),
    "PL": Session(NY, time(8, 20), _TOPSTEP_FLAT),
    **{r: Session(NY, time(8, 20), _TOPSTEP_FLAT) for r in ("ZT", "ZF", "ZN", "TN", "ZB", "UB")},
    **{r: Session(NY, time(8, 20), _TOPSTEP_FLAT) for r in ("6E", "6B", "6J", "6A", "6C", "6S", "6M", "6N")},
    **{r: Session(CHI, time(8, 30), time(13, 15)) for r in ("ZC", "ZW", "ZS", "ZM", "ZL")},
    **{r: Session(CHI, time(8, 30), time(13, 0)) for r in ("HE", "LE")},
}
# Fisher's open times are in exchange time; for the CT-flat sessions the flat
# time is Chicago time.
_FLAT_TZ = {r: (CHI if s.flat == _TOPSTEP_FLAT else s.tz) for r, s in SESSIONS.items()}


def enabled(root: str) -> bool:
    return root in SESSIONS and (not ACD_ROOTS or root in ACD_ROOTS)


def session_window(root: str, d: date) -> tuple[datetime, datetime, datetime]:
    """(OR start, OR end, session flat) in UTC for local session date d."""
    s = SESSIONS[root]
    start = datetime.combine(d, s.open, tzinfo=s.tz)
    flat = datetime.combine(d, s.flat, tzinfo=_FLAT_TZ[root])
    return (start.astimezone(timezone.utc), (start + timedelta(minutes=OR_MIN)).astimezone(timezone.utc),
            flat.astimezone(timezone.utc))


def session_date(root: str, now: datetime) -> date:
    return now.astimezone(SESSIONS[root].tz).date()


def pivot_range(h: float, l: float, c: float) -> tuple[float, float]:
    """Fisher's pivot range from the prior day: between (H+L)/2 and (H+L+C)/3."""
    a, b = (h + l) / 2, (h + l + c) / 3
    return min(a, b), max(a, b)


def evaluate_day(bars: list[dict], or_start: datetime, or_end: datetime, flat: datetime,
                 a_value: float, hold: int | None = None) -> dict | None:
    """Run the ACD state machine over one session's 1-minute bars (oldest first).

    Returns the OR, A levels and the events in order, each
    {kind, dir, t, price, level, extreme}. kinds: "A" (confirmed), "failed A", "C".
    """
    hold = hold or max(1, OR_MIN // 2)
    orb = [b for b in bars if or_start <= b["t"] < or_end]
    if not orb or a_value <= 0:
        return None
    orh, orl = max(b["h"] for b in orb), min(b["l"] for b in orb)
    a_up, a_dn = orh + a_value, orl - a_value
    after = [b for b in bars if or_end <= b["t"] < flat]
    events: list[dict] = []
    touched = {1: None, -1: None}      # first touch time of each A level
    run = {1: 0, -1: 0}                 # consecutive closes beyond each A level
    confirmed = {1: False, -1: False}
    failed = {1: False, -1: False}
    extreme = {1: orh, -1: orl}
    for b in after:
        for d, lvl in ((1, a_up), (-1, a_dn)):
            beyond_hi = b["h"] >= lvl if d == 1 else b["l"] <= lvl
            if beyond_hi and touched[d] is None:
                touched[d] = b["t"]
            if touched[d] is not None and not failed[d]:
                extreme[d] = max(extreme[d], b["h"]) if d == 1 else min(extreme[d], b["l"])
            closed_beyond = b["c"] >= lvl if d == 1 else b["c"] <= lvl
            run[d] = run[d] + 1 if closed_beyond else 0
            # C: the opposite side failed earlier and this side now holds
            if run[d] >= hold and not confirmed[d]:
                confirmed[d] = True
                kind = "C" if failed[-d] else "A"
                events.append({"kind": kind, "dir": d, "t": b["t"], "price": b["c"], "level": lvl,
                               "extreme": extreme[d]})
            # Failed A: touched the A level, then closed back inside the OR
            back_inside = b["c"] < orh if d == 1 else b["c"] > orl
            if touched[d] is not None and not failed[d] and back_inside:
                failed[d] = True
                events.append({"kind": "failed A", "dir": d, "t": b["t"], "price": b["c"], "level": lvl,
                               "extreme": extreme[d], "had_confirmed": confirmed[d]})
    return {"orh": orh, "orl": orl, "a_up": a_up, "a_dn": a_dn, "a": a_value, "events": events,
            "last": after[-1]["c"] if after else orb[-1]["c"]}


# ─────────────────────────────────────────────
# LIVE: one pass per scan
# ─────────────────────────────────────────────
def _fmt(x: float, a: float) -> str:
    dp = 0 if a >= 20 else 1 if a >= 2 else 2 if a >= 0.2 else 3 if a >= 0.02 else 5
    return f"{x:,.{dp}f}"


def event_line(root: str, ev: dict, res: dict, piv: tuple[float, float] | None) -> str:
    a = res["a"]
    side = "up" if ev["dir"] == 1 else "down"
    t = ev["t"].astimezone(SYD).strftime("%-I:%M%p").lower()
    if ev["kind"] == "A":
        text = (f"**{root} A {side} confirmed** at {_fmt(ev['level'], a)} ({t} Syd). "
                f"Opening-range break held: bias {'long' if ev['dir'] == 1 else 'short'} while it holds the A level.")
    elif ev["kind"] == "C":
        text = (f"**{root} C {side}** at {_fmt(ev['level'], a)} ({t} Syd). The other side failed first and price "
                f"has now held the opposite A level: Fisher's strongest reversal.")
    else:
        lost = " after confirming" if ev.get("had_confirmed") else ""
        text = (f"**{root} failed A {side}**{lost} ({t} Syd). Reached {_fmt(ev['extreme'], a)} then closed back "
                f"inside the OR: a trap / liquidity sweep. Leans {'short' if ev['dir'] == 1 else 'long'}; "
                f"invalid beyond {_fmt(ev['extreme'], a)}.")
    if piv:
        lo, hi = piv
        where = "above" if ev["price"] > hi else "below" if ev["price"] < lo else "inside"
        agrees = (where == "above" and _lean(ev) == 1) or (where == "below" and _lean(ev) == -1)
        text += f" Price {where} the pivot range{' (agrees)' if agrees else ' (against it)' if where != 'inside' else ''}."
    return text


def _lean(ev: dict) -> int:
    return -ev["dir"] if ev["kind"] == "failed A" else ev["dir"]


def pivot_note(piv: tuple[float, float] | None, widths: list[float], a: float) -> str:
    if not piv or len(widths) < 10:
        return ""
    w = piv[1] - piv[0]
    rank = sum(1 for x in widths if x < w) / len(widths)
    return "narrow pivot range: trend-day risk" if rank <= 0.2 else "wide pivot range: range day more likely" if rank >= 0.8 else ""


async def run_live(px, http, contracts: dict, readings: dict, state: dict, now: datetime, post, dry_run: bool):
    """Evaluate today's ACD for every enabled market; post events not posted yet.

    readings: root -> (Reading, list[Day]) from the hourly scan, for ATR and the pivot range.
    """
    from .projectx import UNIT_MINUTE
    seen = state.setdefault("acd", {})
    lines, summary = [], {}
    for root, c in contracts.items():
        if not enabled(root) or root not in readings:
            continue
        r, days = readings[root]
        d = session_date(root, now)
        start, end, flat = session_window(root, d)
        if now < end + timedelta(minutes=1) or now > flat + timedelta(minutes=30):
            continue
        try:
            bars = await px.bars(c["id"], UNIT_MINUTE, 1, days_back=1)
        except Exception as e:  # noqa: BLE001
            log.warning("ACD bars failed for %s: %s", root, e)
            continue
        res = evaluate_day(bars, start, end, flat, A_ATR * r.atr)
        if not res:
            continue
        prior = [x for x in days if x.d < r.trading_day]
        piv = pivot_range(prior[-1].h, prior[-1].l, prior[-1].c) if prior else None
        widths = [abs((x.h + x.l) / 2 - (x.h + x.l + x.c) / 3) for x in prior[-21:-1]]
        summary[root] = {"res": res, "piv": piv}
        note = pivot_note(piv, widths, res["a"])
        for ev in res["events"]:
            key = f"{root}:{d}:{ev['kind']}:{ev['dir']}"
            if key in seen:
                continue
            seen[key] = now.isoformat()
            lines.append(event_line(root, ev, res, piv) + (f" _{note}._" if note else ""))
    cutoff = (now - timedelta(days=5)).isoformat()
    state["acd"] = {k: v for k, v in seen.items() if v >= cutoff}
    if lines:
        hook = DISCORD_ACD_WEBHOOK or os.getenv("DISCORD_SCANNER_WEBHOOK", "")
        emb = {"title": "📐 ACD · opening range signals", "description": "\n\n".join(lines)[:4000],
               "color": 0x2B4C8C,
               "footer": {"text": f"Fisher ACD: OR {OR_MIN} min, A = {A_ATR:g} x ATR, hold {max(1, OR_MIN // 2)} min. "
                                  "Context, not entries: your entry signature decides."}}
        await post(http, hook, [emb], dry_run)
    return summary


def watch_field(summary: dict, root: str) -> str | None:
    """One line for a Watch idea: where this market stands on today's ACD."""
    s = summary.get(root)
    if not s:
        return None
    res, piv = s["res"], s["piv"]
    if res["events"]:
        ev = res["events"][-1]
        side = "up" if ev["dir"] == 1 else "down"
        state = f"{ev['kind']} {side}"
    else:
        state = "inside the A levels (no signal yet)"
    a = res["a"]
    out = f"Today: {state}. OR {_fmt(res['orl'], a)}–{_fmt(res['orh'], a)}, A up {_fmt(res['a_up'], a)}, A down {_fmt(res['a_dn'], a)}."
    if piv:
        out += f" Pivot range {_fmt(piv[0], a)}–{_fmt(piv[1], a)}."
    return out


# ─────────────────────────────────────────────
# BACKTEST
# ─────────────────────────────────────────────
def trade_from_event(ev: dict, res: dict, bars_after: list[dict], rr: float = 2.0) -> dict | None:
    """Simulate one trade from an event.

    A / C: enter at the confirming close in the event direction, stop at the
    other side of the OR (Fisher), target rr x risk, else exit at the session flat.
    Failed A: enter at the failing close against the A direction, stop just beyond
    the extreme the A attempt reached, target rr x risk, else exit at the flat.
    Same-bar stop and target counts as a stop (conservative).
    """
    d = _lean(ev)
    entry = ev["price"]
    if ev["kind"] == "failed A":
        stop = ev["extreme"] + (0.1 * res["a"]) * ev["dir"]
    else:
        stop = res["orl"] if d == 1 else res["orh"]
    risk = (entry - stop) * d
    if risk <= 0:
        return None
    target = entry + rr * risk * d
    for b in bars_after:
        hit_stop = b["l"] <= stop if d == 1 else b["h"] >= stop
        hit_tgt = b["h"] >= target if d == 1 else b["l"] <= target
        if hit_stop:
            return {"r": -1.0, "exit": "stop"}
        if hit_tgt:
            return {"r": rr, "exit": "target"}
    if not bars_after:
        return None
    return {"r": round((bars_after[-1]["c"] - entry) * d / risk, 2), "exit": "flat"}


def backtest_market(root: str, bars: list[dict], daily: list, rr: float = 2.0, a_atr: float | None = None) -> dict:
    """bars: 1-minute bars over many sessions; daily: list[Day] (oldest first) for ATR."""
    from .metrics import atr as atr_fn
    a_atr = a_atr or A_ATR
    by_date: dict[date, list[dict]] = {}
    for b in bars:
        by_date.setdefault(session_date(root, b["t"]), []).append(b)
    trades, sessions, or_widths = [], 0, []
    for d in sorted(by_date):
        if d.weekday() >= 5:
            continue
        hist = [x for x in daily if x.d < d]
        if len(hist) < 21:
            continue
        a = atr_fn(hist, 20)
        start, end, flat = session_window(root, d)
        res = evaluate_day(by_date[d], start, end, flat, a_atr * a)
        if not res:
            continue
        sessions += 1
        or_widths.append((res["orh"] - res["orl"]) / a)
        day_bars = [b for b in by_date[d] if end <= b["t"] < flat]
        for ev in res["events"]:
            after = [b for b in day_bars if b["t"] > ev["t"]]
            t = trade_from_event(ev, res, after, rr)
            if t:
                trades.append({"date": d.isoformat(), "kind": ev["kind"], "dir": _lean(ev), **t})
    return {"root": root, "sessions": sessions, "trades": trades,
            "or_atr": round(mean(or_widths), 2) if or_widths else None}


def summarise(bt: dict) -> list[dict]:
    rows = []
    for kind in ("A", "failed A", "C", "all"):
        ts = bt["trades"] if kind == "all" else [t for t in bt["trades"] if t["kind"] == kind]
        if not ts:
            rows.append({"root": bt["root"], "kind": kind, "n": 0})
            continue
        wins = [t for t in ts if t["r"] > 0]
        rows.append({"root": bt["root"], "kind": kind, "n": len(ts),
                     "per_week": round(len(ts) / max(bt["sessions"] / 5, 1), 1),
                     "win_pct": round(100 * len(wins) / len(ts)),
                     "avg_r": round(mean(t["r"] for t in ts), 2),
                     "total_r": round(sum(t["r"] for t in ts), 1),
                     "targets": sum(1 for t in ts if t["exit"] == "target")})
    return rows
