"""Earnings agent: company results that move the index futures you trade.

Forex Factory carries no company earnings, so without this a Meta-sized
report inside the hold window passed the catalyst gate silently.

Sources:
  * US: Nasdaq's public earnings calendar (keyless JSON, one request per
    New York date). Only the watchlist below is kept: names heavy enough to
    move NQ/ES on their own.
  * Japan: Nasdaq doesn't list Tokyo reports, so the Nikkei movers come from
    earnings_jp.json in the repo root (edit it as dates are confirmed on the
    companies' IR pages).

Each event carries the futures roots it moves, so the catalyst gate warns
only those markets, and the head of desk blocks entries just before it.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

log = logging.getLogger("scanner.earnings")
NY = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")

NASDAQ_URL = os.getenv("EARNINGS_US_URL", "https://api.nasdaq.com/api/calendar/earnings")
# Nasdaq refuses requests without browser-like headers.
NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}
JP_FILE = Path(os.getenv("EARNINGS_JP_FILE", str(Path(__file__).resolve().parent.parent / "earnings_jp.json")))
# How many New York dates ahead to pull (today included).
LOOKAHEAD_DAYS = int(os.getenv("EARNINGS_LOOKAHEAD_DAYS", "") or 7)

_DEFAULT_US = "AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,AVGO,NFLX,AMD,SPCX,ORCL,COST,TSM"
US_WATCH = {s.strip().upper() for s in (os.getenv("EARNINGS_WATCH_US", "") or _DEFAULT_US).split(",") if s.strip()}
US_ROOTS = tuple(s.strip().upper() for s in (os.getenv("EARNINGS_US_ROOTS", "") or "NQ,ES").split(",") if s.strip())

# Nasdaq's "time" field -> nominal New York release time and a plain label.
US_TIMES = {
    "time-pre-market": ((7, 0), "before the open"),
    "time-after-hours": ((16, 5), "after the close"),
}
US_TIME_UNKNOWN = ((16, 5), "time not confirmed, assumed after the close")
# Tokyo reports usually land after the 15:30 JST cash close.
JP_TIMES = {
    "after-close": ((15, 30), "after the Tokyo close"),
    "midday": ((12, 0), "at the Tokyo lunch break"),
    "before-open": ((8, 0), "before the Tokyo open"),
}

_cache: dict[str, tuple[datetime, list[dict]]] = {}
CACHE_SEC = 6 * 3600


def _event(t: datetime, symbol: str, name: str, roots: tuple[str, ...], when: str) -> dict:
    return {"t": t.astimezone(timezone.utc), "name": f"{symbol} earnings", "symbol": symbol,
            "company": name, "currency": "earnings", "roots": roots, "when": when}


def parse_nasdaq(payload: dict, day: date, watch: set[str] = US_WATCH, roots: tuple[str, ...] = US_ROOTS) -> list[dict]:
    rows = ((payload or {}).get("data") or {}).get("rows") or []
    out = []
    for r in rows:
        sym = str(r.get("symbol", "")).upper().strip()
        if sym not in watch:
            continue
        (hh, mm), when = US_TIMES.get(str(r.get("time", "")), US_TIME_UNKNOWN)
        t = datetime(day.year, day.month, day.day, hh, mm, tzinfo=NY)
        out.append(_event(t, sym, str(r.get("name", sym)), roots, when))
    return out


async def us_events(http: aiohttp.ClientSession, today: date) -> list[dict]:
    out = []
    for k in range(LOOKAHEAD_DAYS):
        d = today + timedelta(days=k)
        if d.weekday() >= 5:
            continue
        try:
            async with http.get(NASDAQ_URL, params={"date": d.isoformat()}, headers=NASDAQ_HEADERS,
                                timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    log.warning("Nasdaq earnings %s: HTTP %s", d, r.status)
                    continue
                payload = await r.json(content_type=None)
        except Exception as e:  # noqa: BLE001
            log.warning("Nasdaq earnings %s failed: %s", d, e)
            continue
        out += parse_nasdaq(payload, d)
    return out


def jp_events(path: Path = JP_FILE) -> list[dict]:
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except ValueError as e:
        log.warning("%s is not valid JSON: %s", path, e)
        return []
    out = []
    for r in rows:
        try:
            d = date.fromisoformat(r["date"])
        except (KeyError, ValueError):
            continue
        (hh, mm), when = JP_TIMES.get(r.get("session", "after-close"), JP_TIMES["after-close"])
        if r.get("time"):            # explicit "HH:MM" JST overrides the session default
            hh, mm = (int(x) for x in r["time"].split(":"))
            when = "Tokyo time confirmed"
        t = datetime(d.year, d.month, d.day, hh, mm, tzinfo=TOKYO)
        roots = tuple(r.get("roots") or ("NKD",))
        out.append(_event(t, str(r.get("symbol", "")), str(r.get("name", "")), roots, when))
    return out


async def upcoming(http: aiohttp.ClientSession, now: datetime | None = None) -> list[dict]:
    """All watched earnings from now on, cached for six hours."""
    now = now or datetime.now(timezone.utc)
    hit = _cache.get("all")
    if hit and (now - hit[0]).total_seconds() < CACHE_SEC:
        evs = hit[1]
    else:
        evs = await us_events(http, now.astimezone(NY).date()) + jp_events()
        evs.sort(key=lambda e: e["t"])
        _cache["all"] = (now, evs)
    return [e for e in evs if e["t"] >= now - timedelta(hours=1)]


def for_market(evs: list[dict], root: str) -> list[dict]:
    return [e for e in evs if root in e["roots"]]
