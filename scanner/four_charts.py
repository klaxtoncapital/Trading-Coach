"""Four Charts: a daily directional bias read for the #general channel.

Nicholas Crown's four-chart dashboard, read by code each morning after the US close:

  1. Crude oil: front month vs the "red Dec" (December of next year). The spread
     rising = near-term oil getting tighter. Front crude's own trend alongside.
  2. US 10-year yield: boxed in (inside its 20-day range) or breaking out.
     Boxed = yields aren't driving, crude leads. A breakout overrides everything.
  3. RSP/SPY: breadth. Rising = the average stock beating the mega-caps (rotation).
  4. MAGS/SOXX: inside AI. Rising = the spenders (Mag 7) beating the chipmakers;
     falling = chipmakers leading. SOXX's own trend gives the AI complex direction.

Each chart's 10-session change is compared with its own last ~6 months of
10-session changes (a z-score), so "rising" means unusually rising, not noise.
The charts then vote on each market. It's a bias for the day, never an entry.

Data (keyless): FRED (10-year, WTI spot fallback), Yahoo Finance daily chart API,
Stooq CSV as a fallback for the ETFs.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from statistics import pstdev
from zoneinfo import ZoneInfo

import aiohttp

from . import config
from .regime import parse_fred_csv

log = logging.getLogger("scanner.four_charts")
SYD = ZoneInfo("Australia/Sydney")

NY_TZ = ZoneInfo("America/New_York")
WEBHOOK = os.getenv("DISCORD_GENERAL_WEBHOOK", "")
POST_HOUR = int(os.getenv("FOUR_CHARTS_HOUR_SYD", "") or 9)
POST_MINUTE = int(os.getenv("FOUR_CHARTS_MINUTE_SYD", "") or 50)
STATE_FILE = os.getenv("FOUR_CHARTS_STATE_FILE", "four_charts_state.json")
LOOKBACK = 10            # sessions for the change
HISTORY = 120            # sessions of 10-day changes to compare against
Z_CLEAR, Z_STRONG = 0.5, 1.25
BOX = 20                 # sessions for the 10-year box
UA = {"User-Agent": "Mozilla/5.0 (KlaxtonDesk four-charts)"}

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
STOOQ = "https://stooq.com/q/d/l/"
STOOQ_SYM = {"RSP": "rsp.us", "SPY": "spy.us", "MAGS": "mags.us", "SOXX": "soxx.us", "CL=F": "cl.f"}

Series = list[tuple[date, float]]


# ─────────────────────────────────────────────
# DATA
# ─────────────────────────────────────────────
def red_dec_symbol(today: date) -> str:
    """December of next year on NYMEX, e.g. CLZ27.NYM in 2026. Override with CURVE_BACK_SYMBOL."""
    return os.getenv("CURVE_BACK_SYMBOL", "") or f"CLZ{str(today.year + 1)[-2:]}.NYM"


def parse_yahoo(data: dict) -> Series:
    try:
        res = data["chart"]["result"][0]
        ts, closes = res["timestamp"], res["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        return []
    out: dict[date, float] = {}
    for t, c in zip(ts, closes):
        if c is not None:
            out[datetime.fromtimestamp(t, timezone.utc).date()] = float(c)
    return sorted(out.items())


def parse_stooq(text: str) -> Series:
    out = []
    for row in list(csv.reader(io.StringIO(text or "")))[1:]:
        try:
            out.append((date.fromisoformat(row[0]), float(row[4])))
        except (IndexError, ValueError):
            continue
    return out


async def yahoo(http: aiohttp.ClientSession, sym: str) -> Series:
    try:
        async with http.get(YAHOO.format(sym=sym), params={"range": "1y", "interval": "1d"}, headers=UA) as r:
            if r.status != 200:
                log.info("Yahoo %s: HTTP %s", sym, r.status)
                return []
            return parse_yahoo(await r.json(content_type=None))
    except Exception as e:  # noqa: BLE001
        log.info("Yahoo %s failed: %s", sym, e)
        return []


async def stooq(http: aiohttp.ClientSession, sym: str) -> Series:
    if sym not in STOOQ_SYM:
        return []
    try:
        async with http.get(STOOQ, params={"s": STOOQ_SYM[sym], "i": "d"}, headers=UA) as r:
            return parse_stooq(await r.text())[-260:]
    except Exception as e:  # noqa: BLE001
        log.info("Stooq %s failed: %s", sym, e)
        return []


async def fred(http: aiohttp.ClientSession, series: str) -> Series:
    start = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y-%m-%d")
    try:
        async with http.get(config.FRED_CSV, params={"id": series, "cosd": start}) as r:
            return parse_fred_csv(await r.text())
    except Exception as e:  # noqa: BLE001
        log.info("FRED %s failed: %s", series, e)
        return []


async def price(http: aiohttp.ClientSession, sym: str) -> Series:
    return await yahoo(http, sym) or await stooq(http, sym)


async def fetch_all(http: aiohttp.ClientSession, today: date) -> dict[str, Series]:
    back = red_dec_symbol(today)
    out = {
        "front": await price(http, "CL=F") or await fred(http, "DCOILWTICO"),
        "back": await yahoo(http, back),
        "us10y": await fred(http, "DGS10") or await yahoo(http, "^TNX"),
    }
    for s in ("RSP", "SPY", "MAGS", "SOXX"):
        out[s] = await price(http, s)
    out["_back_symbol"] = back  # type: ignore[assignment]
    return out


def combine(a: Series, b: Series, how: str) -> Series:
    bd = dict(b)
    out = []
    for d, x in a:
        y = bd.get(d)
        if y is None or (how == "ratio" and not y):
            continue
        out.append((d, x - y if how == "spread" else x / y))
    return out


# ─────────────────────────────────────────────
# READING
# ─────────────────────────────────────────────
def trend(s: Series, pct: bool) -> dict | None:
    """10-session change vs its own history. pct=True for prices/ratios, False for spreads/yields."""
    if len(s) < LOOKBACK + 40:
        return None
    v = [x for _, x in s]
    chg = lambda i: ((v[i] / v[i - LOOKBACK] - 1) * 100) if pct else (v[i] - v[i - LOOKBACK])
    hist = [chg(i) for i in range(max(LOOKBACK, len(v) - HISTORY), len(v))]
    sd = pstdev(hist) or 1e-9
    c = chg(len(v) - 1)
    z = c / sd
    d = 1 if z >= Z_CLEAR else -1 if z <= -Z_CLEAR else 0
    return {"last": v[-1], "chg": c, "z": round(z, 2), "dir": d, "strong": abs(z) >= Z_STRONG, "asof": s[-1][0]}


def box(s: Series) -> dict | None:
    if len(s) < BOX + 1:
        return None
    prior = [x for _, x in s[-BOX - 1:-1]]
    hi, lo, last = max(prior), min(prior), s[-1][1]
    state = "up" if last > hi else "down" if last < lo else "boxed"
    return {"last": last, "hi": hi, "lo": lo, "state": state, "chg": last - s[-1 - LOOKBACK][1] if len(s) > LOOKBACK else 0,
            "asof": s[-1][0]}


def read(data: dict) -> dict:
    curve = combine(data.get("front", []), data.get("back", []), "spread")
    return {
        "curve": trend(curve, pct=False),
        "front": trend(data.get("front", []), pct=True),
        "us10y": box(data.get("us10y", [])),
        "breadth": trend(combine(data.get("RSP", []), data.get("SPY", []), "ratio"), pct=True),
        "ai": trend(combine(data.get("MAGS", []), data.get("SOXX", []), "ratio"), pct=True),
        "soxx": trend(data.get("SOXX", []), pct=True),
        "back_symbol": data.get("_back_symbol", ""),
    }


MARKETS = ("NKD", "NQ", "ES", "YM", "RTY", "CL", "GC")


def votes(r: dict) -> dict[str, list[tuple[int, str]]]:
    """Each chart with a clear read votes +1/-1 on the markets it moves, with the reason."""
    v: dict[str, list[tuple[int, str]]] = {m: [] for m in MARKETS}

    def add(ms, sign, why):
        for m in ms:
            v[m].append((sign, why))

    if r["curve"] and r["curve"]["dir"]:
        add(["CL"], r["curve"]["dir"], "oil curve " + ("tightening" if r["curve"]["dir"] == 1 else "loosening"))
    if r["front"] and r["front"]["dir"]:
        add(["CL"], r["front"]["dir"], "front crude " + ("rising" if r["front"]["dir"] == 1 else "falling"))
    y = r["us10y"]
    if y and y["state"] != "boxed":
        s = -1 if y["state"] == "up" else 1
        add(["NQ", "ES", "RTY", "GC"], s, "10Y breaking " + y["state"])
    b = r["breadth"]
    if b and b["dir"]:
        add(["RTY", "YM"], b["dir"], "breadth " + ("widening" if b["dir"] == 1 else "narrowing"))
        add(["NQ"], -b["dir"], "breadth " + ("widening" if b["dir"] == 1 else "narrowing"))
    a = r["ai"]
    if a and a["dir"]:
        add(["NKD"], -a["dir"], "chips " + ("lagging" if a["dir"] == 1 else "leading") + " the Mag 7")
    sx = r["soxx"]
    if sx and sx["dir"]:
        add(["NQ", "NKD"], sx["dir"], "AI complex " + ("rising" if sx["dir"] == 1 else "falling"))
    return v


def lean(vs: list[tuple[int, str]]) -> dict:
    s = sum(x for x, _ in vs)
    against = sum(1 for x, _ in vs if s and (x > 0) != (s > 0))
    if s == 0:
        conv = "mixed" if vs else "none"
    elif abs(s) >= 2 and against == 0:
        conv = "HIGH"
    elif against == 0:
        conv = "MEDIUM"
    else:
        conv = "LOW"
    return {"score": s, "dir": (s > 0) - (s < 0), "conv": conv, "why": [w for _, w in vs]}


def overall(r: dict, leans: dict[str, dict]) -> str:
    clear = sum([
        bool((r["curve"] and r["curve"]["dir"]) or (r["front"] and r["front"]["dir"])),
        bool(r["us10y"] and r["us10y"]["state"] != "boxed"),
        bool(r["breadth"] and r["breadth"]["dir"]),
        bool((r["ai"] and r["ai"]["dir"]) or (r["soxx"] and r["soxx"]["dir"])),
    ])
    if clear >= 3 and any(x["conv"] == "HIGH" for x in leans.values()):
        return "HIGH"
    return "MEDIUM" if clear >= 2 else "LOW"


# ─────────────────────────────────────────────
# THE POST
# ─────────────────────────────────────────────
# Arrows show direction only; good or bad depends on the market, so colour lives in the leans.
ARROW = {1: "⬆️", -1: "⬇️", 0: "➡️"}


def _strength(t: dict) -> str:
    return "strongly " if t["strong"] else ""


def chart_lines(r: dict) -> list[str]:
    out = []
    c, f = r["curve"], r["front"]
    if c:
        word = "tightening" if c["dir"] == 1 else "loosening" if c["dir"] == -1 else "flat"
        out.append(f"{ARROW[c['dir']]} **1 · Oil curve** (front − {r['back_symbol'][:5]}): ${c['last']:.2f}, "
                   f"{c['chg']:+.2f} in 10 days. {_strength(c).capitalize() if c['dir'] else ''}{word}"
                   + (f"; front crude {f['chg']:+.1f}%." if f else "."))
    elif f:
        out.append(f"{ARROW[f['dir']]} **1 · Front crude** ${f['last']:.2f}, {f['chg']:+.1f}% in 10 days "
                   f"(far contract unavailable today).")
    else:
        out.append("➖ **1 · Oil:** no data today.")
    y = r["us10y"]
    if y:
        if y["state"] == "boxed":
            out.append(f"➡️ **2 · US 10-year** {y['last']:.2f}%: boxed in ({y['lo']:.2f}–{y['hi']:.2f}). "
                       "Yields aren't driving; crude leads.")
        else:
            out.append(f"{'⬆️' if y['state'] == 'up' else '⬇️'} **2 · US 10-year** {y['last']:.2f}%: "
                       f"**breaking {y['state']}** out of {y['lo']:.2f}–{y['hi']:.2f}. This overrides the rest.")
    else:
        out.append("➖ **2 · US 10-year:** no data today.")
    b = r["breadth"]
    if b:
        word = "widening: the average stock beating the mega-caps" if b["dir"] == 1 else \
            "narrowing: mega-caps leading" if b["dir"] == -1 else "flat: no rotation either way"
        out.append(f"{ARROW[b['dir']]} **3 · RSP/SPY** {b['chg']:+.1f}% in 10 days. Breadth {_strength(b) if b['dir'] else ''}{word}.")
    else:
        out.append("➖ **3 · RSP/SPY:** no data today.")
    a, sx = r["ai"], r["soxx"]
    if a:
        word = "spenders (Mag 7) beating the chipmakers" if a["dir"] == 1 else \
            "chipmakers leading the spenders" if a["dir"] == -1 else "spenders and chipmakers level"
        tail = f" SOXX itself {sx['chg']:+.1f}%." if sx else ""
        out.append(f"{ARROW[a['dir']]} **4 · MAGS/SOXX** {a['chg']:+.1f}% in 10 days: {word}.{tail}")
    else:
        out.append("➖ **4 · MAGS/SOXX:** no data today.")
    return out


def headline(leans: dict[str, dict], conv: str) -> str:
    strong = [(m, x) for m, x in leans.items() if x["conv"] == "HIGH"]
    if conv == "LOW" or not any(x["dir"] for x in leans.values()):
        return "Themes are quiet or fighting each other. Trade smaller or skip."
    pick = strong or [(m, x) for m, x in leans.items() if x["conv"] == "MEDIUM"]
    parts = [f"{m} {'long' if x['dir'] == 1 else 'short'}" for m, x in pick[:3]]
    return ("Best aligned: " + ", ".join(parts) + ".") if parts else "No market has the charts lined up."


def embed(r: dict, now: datetime) -> dict:
    leans = {m: lean(vs) for m, vs in votes(r).items()}
    conv = overall(r, leans)
    rows = []
    for m in MARKETS:
        x = leans[m]
        if x["conv"] == "none":
            continue
        word = "Bullish" if x["dir"] == 1 else "Bearish" if x["dir"] == -1 else "Mixed"
        icon = "🟢" if x["dir"] == 1 else "🔴" if x["dir"] == -1 else "⚪"
        rows.append(f"{icon} **{m}** {word}{'' if x['conv'] == 'mixed' else ' · ' + x['conv']} — {', '.join(x['why'])}")
    asof = max((t["asof"] for k in ("curve", "front", "us10y", "breadth", "ai") if (t := r.get(k))), default=None)
    stale = asof and (now.astimezone(SYD).date() - asof).days > 4
    colour = {"HIGH": 0x1F6B52, "MEDIUM": 0xB8860B, "LOW": 0x5D6661}[conv]
    return {
        "title": f"🧭 Four Charts · {now.astimezone(SYD):%a %-d %b} · conviction {conv}",
        "description": (f"**{headline(leans, conv)}**\n\n" + "\n".join(chart_lines(r))
                        + ("\n\n⚠️ Data looks stale; check the charts yourself." if stale else ""))[:4000],
        "color": colour,
        "fields": [{"name": "📌 Leans for your markets", "value": ("\n".join(rows) or "No clear leans today.")[:1024],
                    "inline": False}],
        "footer": {"text": f"Daily closes{f' to {asof}' if asof else ''}. 10-day moves vs their own history. "
                           "Bias, not entry: your 1H/15m model decides. Not advice."},
    }


# ─────────────────────────────────────────────
# SCHEDULE
# ─────────────────────────────────────────────
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
        log.warning("Couldn't save four-charts state: %s", e)


def due(now: datetime, last: str | None) -> bool:
    """Weekdays from 8:40am Sydney, and never before 17:15 New York, so the daily candle
    has closed for the SFP section (matters from November, when New York is on standard time
    and the close is 9am Sydney)."""
    s = now.astimezone(SYD)
    ny = now.astimezone(NY_TZ)
    closed = not (12 <= ny.hour < 17 or (ny.hour == 17 and ny.minute < 15))
    return (s.weekday() < 5 and (s.hour, s.minute) >= (POST_HOUR, POST_MINUTE) and s.hour < 12
            and closed and last != s.date().isoformat())


async def build(http: aiohttp.ClientSession, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    return embed(read(await fetch_all(http, now.astimezone(SYD).date())), now)


async def sfp_section(px, http, now: datetime) -> list[dict]:
    if px is None:
        return []
    from . import daily_setups
    try:
        return daily_setups.bias_embeds(*await daily_setups.scan(px, http, now))
    except Exception:  # noqa: BLE001
        log.exception("Daily SFP section failed; posting Four Charts without it")
        return []


async def loop(http: aiohttp.ClientSession, post, dry_run: bool, px=None):
    import asyncio
    while True:
        try:
            now = datetime.now(timezone.utc)
            st = _load()
            if due(now, st.get("posted")):
                hook = WEBHOOK or config.DISCORD_DESK_WEBHOOK
                await post(http, hook, [await build(http, now)], dry_run)
                # SFP section as its own message(s), one embed each: every market listed,
                # and each message stays under Discord's 6,000-character limit
                for e in await sfp_section(px, http, now):
                    await post(http, hook, [e], dry_run)
                st["posted"] = now.astimezone(SYD).date().isoformat()
                _save(st)
        except Exception:  # noqa: BLE001
            log.exception("Four Charts failed; retrying in 5 min")
        await asyncio.sleep(300)
