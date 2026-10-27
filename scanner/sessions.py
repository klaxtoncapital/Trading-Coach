"""Trading hours per market, so every idea says when the market is open in Sydney and US time.

Hours are CME Globex hours in Chicago time (CT), per Topstep's product-hours page:
  Most markets (equity index, FX, rates, energy, metals, crypto):
      Sunday 5:00pm to Friday 4:00pm CT, daily break 4:00-5:00pm CT.
  CBOT grains (ZC ZW ZS ZM ZL): 7:00pm-7:45am CT and 8:30am-1:20pm CT, Sun-Fri.
  CME livestock (HE LE): 8:30am-1:05pm CT, Monday to Friday only.
Topstep: every position must be flat by 3:10pm CT, or the product's own close if earlier.
Conversions use zoneinfo, so US and Sydney daylight-saving changes are handled automatically.
CME holiday hours are not modelled."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
ET = ZoneInfo("America/New_York")
SYD = ZoneInfo("Australia/Sydney")

GRAINS = {"ZC", "ZW", "ZS", "ZM", "ZL"}
LIVESTOCK = {"HE", "LE"}
TOPSTEP_FLAT = time(15, 10)


def kind(root: str) -> str:
    return "grains" if root in GRAINS else "livestock" if root in LIVESTOCK else "globex"


def _at(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=CT)


def day_segments(root: str, d: date) -> list[tuple[datetime, datetime]]:
    """Trading windows belonging to CT trade date d (Monday to Friday)."""
    if d.weekday() >= 5:
        return []
    k, prev = kind(root), d - timedelta(days=1)
    if k == "livestock":
        return [(_at(d, time(8, 30)), _at(d, time(13, 5)))]
    if k == "grains":
        return [(_at(prev, time(19, 0)), _at(d, time(7, 45))), (_at(d, time(8, 30)), _at(d, time(13, 20)))]
    return [(_at(prev, time(17, 0)), _at(d, time(16, 0)))]


def segments(root: str, now: datetime, back: int = 3, ahead: int = 8) -> list[tuple[datetime, datetime]]:
    today = now.astimezone(CT).date()
    out = []
    for i in range(-back, ahead + 1):
        out += day_segments(root, today + timedelta(days=i))
    return sorted(out)


def status(root: str, now: datetime | None = None) -> dict:
    """{'open': bool, 'until': close time if open, else 'opens': next open}."""
    now = now or datetime.now(timezone.utc)
    for a, b in segments(root, now):
        if a <= now < b:
            return {"open": True, "until": b}
        if a > now:
            return {"open": False, "opens": a}
    return {"open": False, "opens": None}


def _t(dt: datetime, tz: ZoneInfo, day: bool = False) -> str:
    x = dt.astimezone(tz)
    s = x.strftime("%-I:%M%p").lower().replace(":00", "")
    return f"{x:%a} {s}" if day else s


def _in(delta: timedelta) -> str:
    m = max(int(delta.total_seconds() // 60), 0)
    h, m = divmod(m, 60)
    return f"{h // 24}d {h % 24}h" if h >= 24 else f"{h}h {m:02d}m" if h else f"{m}m"


def _ref_day(now: datetime) -> date:
    """Next Wednesday: a normal mid-week trade date, in the current daylight-saving regime."""
    d = now.astimezone(CT).date()
    return d + timedelta(days=(2 - d.weekday()) % 7)


def schedule_lines(root: str, now: datetime | None = None) -> list[str]:
    """Plain hours in Sydney, New York and Chicago time, for this week's clocks."""
    now = now or datetime.now(timezone.utc)
    k, d = kind(root), _ref_day(now)
    segs = day_segments(root, d)
    if k == "globex":
        mon = d - timedelta(days=d.weekday())
        a, b = day_segments(root, mon)[0][0], day_segments(root, mon + timedelta(days=4))[0][1]
        brk = (segs[0][1], segs[0][1] + timedelta(hours=1))
        return [f"**Sydney:** {_t(a, SYD, True)} → {_t(b, SYD, True)}, daily break {_t(brk[0], SYD)}–{_t(brk[1], SYD)}",
                f"**US:** Sun {_t(a, ET)} → Fri {_t(b, ET)} New York ({_t(a, CT)}–{_t(b, CT)} Chicago), "
                f"break {_t(brk[0], ET)}–{_t(brk[1], ET)} NY"]
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    def days(x: datetime, tz: ZoneInfo) -> str:
        off = (x.astimezone(tz).date() - d).days        # trade dates run Mon-Fri
        return f"{names[off % 7]}–{names[(4 + off) % 7]}"
    def span(tz):
        return " and ".join(f"{_t(a, tz)}–{_t(b, tz)} ({days(a, tz)})" for a, b in segs)
    return [f"**Sydney:** {span(SYD)}",
            f"**US:** {span(ET)} New York · Chicago {', '.join(f'{_t(a, CT)}–{_t(b, CT)}' for a, b in segs)}"]


def short_hours(root: str) -> bool:
    return kind(root) != "globex"


def hours_field(root: str, now: datetime | None = None) -> dict:
    """Discord embed field: open/closed now, the schedule, and Topstep's flat-by time."""
    now = now or datetime.now(timezone.utc)
    st = status(root, now)
    if st["open"]:
        head = f"🟢 **Open now** · closes {_t(st['until'], SYD, True)} Sydney (in {_in(st['until'] - now)})"
    elif st["opens"]:
        head = f"🔴 **Closed now** · opens {_t(st['opens'], SYD, True)} Sydney (in {_in(st['opens'] - now)})"
    else:
        head = "Closed"
    flat = _at(_ref_day(now), TOPSTEP_FLAT)
    lines = [head, *schedule_lines(root, now)]
    if kind(root) == "globex":   # grains and livestock close before 3:10pm CT anyway
        lines.append(f"Topstep: flat by {_t(flat, SYD)} Sydney ({_t(flat, ET)} NY)")
    name = "⏰ Trading hours · SHORT-HOURS MARKET" if short_hours(root) else "⏰ Trading hours"
    return {"name": name, "value": "\n".join(lines)[:1000], "inline": False}


def board_tag(root: str, now: datetime | None = None) -> str:
    """Short note for the movers board, only for markets that keep unusual hours."""
    if not short_hours(root):
        return ""
    now = now or datetime.now(timezone.utc)
    st = status(root, now)
    if st["open"]:
        return f" · ⏰ open, closes {_t(st['until'], SYD)} Syd"
    return f" · ⏰ closed, opens {_t(st['opens'], SYD, True)} Syd" if st["opens"] else ""
