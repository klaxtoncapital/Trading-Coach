"""The Four Gates, gates 2-4: positioning, options surface, catalyst window.

All sources are public and keyless: CFTC Public Reporting (COT), FRED (CBOE
implied-vol indices) and the Forex Factory weekly calendar feed. Results are
cached so a 15-minute scan loop doesn't hammer them.
"""
from __future__ import annotations

import csv
import io
import logging
import time
from datetime import datetime, timedelta, timezone

import aiohttp

from . import config
from .config import Market

log = logging.getLogger("scanner.gates")

_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, max_age: float):
    hit = _cache.get(key)
    return hit[1] if hit and time.time() - hit[0] < max_age else None


def _store(key: str, value):
    _cache[key] = (time.time(), value)
    return value


# ─────────────────────────────────────────────
# GATE 2 · POSITIONING (CFTC COT)
# ─────────────────────────────────────────────
async def cot(session: aiohttp.ClientSession, m: Market) -> dict | None:
    if not m.cot_report or not m.cot_code:
        return None
    key = f"cot:{m.root}"
    if (hit := _cached(key, 6 * 3600)) is not None:
        return hit or None
    if m.cot_report == "tff":
        ds, lng, sht, who = config.CFTC_TFF, "lev_money_positions_long", "lev_money_positions_short", "Leveraged funds"
    else:
        ds, lng, sht, who = config.CFTC_DISAGG, "m_money_positions_long_all", "m_money_positions_short_all", "Managed money"
    params = {
        "$select": f"report_date_as_yyyy_mm_dd,open_interest_all,{lng},{sht}",
        "$where": f"cftc_contract_market_code='{m.cot_code}'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": "156",
    }
    try:
        async with session.get(f"{config.CFTC_BASE}/{ds}.json", params=params) as r:
            rows = await r.json(content_type=None)
    except Exception as e:  # noqa: BLE001
        log.warning("COT fetch failed for %s: %s", m.root, e)
        return None
    if not rows:
        _store(key, {})
        return None
    nets = [float(x[lng]) - float(x[sht]) for x in rows]
    cur = nets[0]
    pct = round(100 * sum(1 for x in nets if x < cur) / max(len(nets) - 1, 1))
    out = {
        "who": who,
        "report_date": rows[0]["report_date_as_yyyy_mm_dd"][:10],
        "net": int(cur),
        "pct_oi": round(100 * cur / float(rows[0]["open_interest_all"]), 1) if float(rows[0]["open_interest_all"]) else 0,
        "pct3y": pct,
        "chg4w": int(cur - nets[4]) if len(nets) > 4 else None,
    }
    return _store(key, out)


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def positioning_gate(pos: dict | None, direction: int) -> dict:
    if not pos:
        return {"status": "n/a", "text": "No COT data for this market."}
    p = pos["pct3y"]
    trend = ""
    if pos.get("chg4w") is not None:
        trend = ", adding longs" if pos["chg4w"] > 0 else ", adding shorts"
    base = f"{pos['who']} net {pos['net']:+,} ({ordinal(p)} percentile, 3y{trend})"
    if direction == 1 and p >= 90:
        return {"status": "fail", "text": base + ". Longs are crowded: little fuel left upward."}
    if direction == -1 and p <= 10:
        return {"status": "fail", "text": base + ". Shorts are crowded: squeeze risk."}
    if direction == 1 and p <= 25:
        return {"status": "support", "text": base + ". Shorts are crowded: squeeze fuel for a long."}
    if direction == -1 and p >= 75:
        return {"status": "support", "text": base + ". Longs are crowded: fuel for a short."}
    return {"status": "pass", "text": base + ". Not crowded either way."}


# ─────────────────────────────────────────────
# GATE 3 · OPTIONS SURFACE (implied vs realised)
# ─────────────────────────────────────────────
async def implied_vol(session: aiohttp.ClientSession, series: str | None) -> dict | None:
    if not series:
        return None
    key = f"iv:{series}"
    if (hit := _cached(key, 6 * 3600)) is not None:
        return hit or None
    start = (datetime.now(timezone.utc) - timedelta(days=420)).strftime("%Y-%m-%d")
    try:
        async with session.get(config.FRED_CSV, params={"id": series, "cosd": start}) as r:
            text = await r.text()
    except Exception as e:  # noqa: BLE001
        log.warning("FRED fetch failed for %s: %s", series, e)
        return None
    vals = []
    for row in list(csv.reader(io.StringIO(text)))[1:]:
        if len(row) == 2 and row[1] not in (".", ""):
            try:
                vals.append(float(row[1]))
            except ValueError:
                pass
    if not vals:
        _store(key, {})
        return None
    yr, cur = vals[-252:], vals[-1]
    out = {"series": series.replace("CLS", ""), "iv": cur,
           "pct1y": round(100 * sum(1 for x in yr if x < cur) / max(len(yr) - 1, 1))}
    return _store(key, out)


def options_gate(iv: dict | None, rv20: float | None, direction: int) -> dict:
    call_put = "call" if direction == 1 else "put"
    if not iv or rv20 is None:
        rv_txt = f"realised vol {rv20}%" if rv20 is not None else "no vol data"
        return {"status": "n/a", "regime": "No IV feed",
                "text": f"No implied-vol index for this market ({rv_txt}). Check the options chain before an options trade.",
                "structure": f"{call_put.title()} debit spread (check the chain first)"}
    v, p = iv["iv"], iv["pct1y"]
    if p < 30 and v <= rv20:
        regime, structure = "Low", f"Buy {call_put}s or a {call_put} debit spread"
    elif p > 70 and v > rv20:
        regime = "High"
        structure = "Sell a put credit spread" if direction == 1 else "Sell a call credit spread"
    else:
        regime, structure = "Middle", f"{call_put.title()} debit spread"
    return {"status": "pass", "regime": regime, "structure": structure,
            "text": f"{iv['series']} {v:.1f} ({ordinal(p)} percentile, 1y) vs realised {rv20}%: {regime.lower()} IV."}


# ─────────────────────────────────────────────
# GATE 4 · CATALYST WINDOW
# ─────────────────────────────────────────────
async def macro_calendar(session: aiohttp.ClientSession) -> list[dict]:
    if (hit := _cached("ff", 3600)) is not None:
        return hit
    try:
        async with session.get(config.FF_CALENDAR) as r:
            data = await r.json(content_type=None)
    except Exception as e:  # noqa: BLE001
        log.warning("Calendar fetch failed: %s", e)
        return _store("ff", [])
    out = []
    for ev in data or []:
        if str(ev.get("impact", "")).lower() != "high":
            continue
        try:
            t = datetime.fromisoformat(ev["date"]).astimezone(timezone.utc)
        except (KeyError, ValueError):
            continue
        out.append({"t": t, "name": ev.get("title", ""), "currency": ev.get("country", "")})
    return _store("ff", out)


def _recurring(m: Market, now: datetime, hours: int) -> list[dict]:
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    out = []
    for rule in config.RECURRING_EVENTS:
        if m.root not in rule["roots"]:
            continue
        for k in range(0, 8):
            day = (now.astimezone(ny) + timedelta(days=k)).date()
            if day.weekday() != rule["weekday"]:
                continue
            t = datetime(day.year, day.month, day.day, rule["hour"], rule["minute"], tzinfo=ny).astimezone(timezone.utc)
            if now <= t <= now + timedelta(hours=hours):
                out.append({"t": t, "name": rule["name"], "currency": "USD"})
    return out


def label(e: dict) -> str:
    """'META earnings (after the close)' or 'Unemployment Claims (USD)'."""
    return f"{e['name']} ({e['when']})" if e.get("roots") else f"{e['name']} ({e['currency']})"


def catalyst_gate(m: Market, cal: list[dict], now: datetime, hours: int) -> dict:
    def _hits(e):  # earnings carry the roots they move; macro events go by currency
        return m.root in e["roots"] if e.get("roots") else e["currency"] in m.currencies
    window = [e for e in cal if _hits(e) and now <= e["t"] <= now + timedelta(hours=hours)]
    window += _recurring(m, now, hours)
    window.sort(key=lambda e: e["t"])
    if not window:
        return {"status": "pass", "events": [], "text": f"Nothing high-impact in the next {hours}h."}
    from zoneinfo import ZoneInfo
    syd = ZoneInfo("Australia/Sydney")
    names = [f"{label(e)} {e['t'].astimezone(syd):%a %-I:%M%p} Syd" for e in window[:4]]
    return {"status": "warn", "events": window,
            "text": "Inside the window: " + "; ".join(names) + ". Plan to flatten or cut size before it."}
