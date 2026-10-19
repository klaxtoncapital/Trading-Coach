"""Technicals agent: the daily trend regime (Jordi Visser's 200-day rule).

For each market it reads daily closes and works out:
  * the 200-day state: price above/below the 200-day SMA, and whether the SMA
    is rising or falling (today vs 20 sessions ago)
  * ROC 50: the % move over the last 50 daily closes, and its percentile
    against the market's own history (up to 5 years)
  * flags: exhausted (ROC 50 in the top/bottom 10%), flattening (a big run
    cooling off while price holds), reset (ROC 50 back near zero), fresh cross
  * the size ceiling for longs and shorts: full, half or starter

It never kills an idea. It writes the ceiling the head of desk sizes under.

Daily data comes from FRED (long, clean cash-index / spot history, keyless)
where a series exists and is fresh, otherwise from ProjectX daily bars on the
front contract.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from statistics import mean

import aiohttp

from . import config

log = logging.getLogger("scanner.regime")

SMA_LEN = 200
SLOPE_LOOKBACK = 20
ROC_LEN = 50
PCT_LOOKBACK = 1260          # ~5 years of sessions
MIN_SESSIONS = SMA_LEN + SLOPE_LOOKBACK
EXHAUSTED_PCT = int(os.getenv("REGIME_EXHAUSTED_PCT", "") or 90)
RESET_BAND = float(os.getenv("REGIME_RESET_BAND", "") or 2.0)     # ROC 50 within +/- this %
FRESH_CROSS_DAYS = 5
FRED_MAX_AGE_DAYS = 7

# FRED daily series per root, with how to turn the series into a price-like
# number: None = as is, "inv" = 1/x (quote is the other way round),
# "yield" = 100 - x (bond prices move against yields).
FRED_DAILY: dict[str, tuple[str, str | None]] = {
    "ES": ("SP500", None), "NQ": ("NASDAQ100", None), "YM": ("DJIA", None), "NKD": ("NIKKEI225", None),
    "6E": ("DEXUSEU", None), "6B": ("DEXUSUK", None), "6J": ("DEXJPUS", "inv"), "6A": ("DEXUSAL", None),
    "6C": ("DEXCAUS", "inv"), "6S": ("DEXSZUS", "inv"), "6M": ("DEXMXUS", "inv"), "6N": ("DEXUSNZ", None),
    "ZT": ("DGS2", "yield"), "ZF": ("DGS5", "yield"), "ZN": ("DGS10", "yield"), "TN": ("DGS10", "yield"),
    "ZB": ("DGS30", "yield"), "UB": ("DGS30", "yield"),
    "CL": ("DCOILWTICO", None), "NG": ("DHHNGSP", None), "RB": ("DGASNYH", None), "HO": ("DHOILNYH", None),
    "MBT": ("CBBTCUSD", None), "MET": ("CBETHUSD", None),
}

STATE_TEXT = {
    "above_rising": "above a rising 200-day",
    "above_falling": "back above a falling 200-day",
    "below_falling": "below a falling 200-day",
    "below_rising": "below a rising 200-day",
}

# Size ceiling per state: (longs, shorts)
CEILING = {
    "above_rising": ("full", "half"),
    "above_falling": ("half", "half"),
    "below_falling": ("starter", "full"),
    "below_rising": ("half", "half"),
}
STEPS = ["starter", "half", "full"]
STEP_TEXT = {"full": "full size", "half": "half size", "starter": "starter only"}
STEP_FACTOR = {"full": 1.0, "half": 0.5, "starter": 0.25}

_cache: dict[str, tuple[float, object]] = {}


def _step_down(step: str) -> str:
    return STEPS[max(STEPS.index(step) - 1, 0)]


def _cap(step: str, cap: str) -> str:
    return STEPS[min(STEPS.index(step), STEPS.index(cap))]


def _percentile(values: list[float], x: float) -> int:
    """Mid-rank percentile of x in values (ties count half), 0-100."""
    if len(values) < 2:
        return 50
    below = sum(1 for v in values if v < x)
    equal = sum(1 for v in values if abs(v - x) < 1e-9)
    return round(100 * (below + 0.5 * equal) / len(values))


def compute_regime(closes: list[tuple[date, float]], source: str = "") -> dict | None:
    """closes: oldest first. Returns the regime reading, or None if history is too short."""
    closes = [(d, c) for d, c in closes if c is not None and c > 0]
    if len(closes) < MIN_SESSIONS:
        return None
    px = [c for _, c in closes]
    last = px[-1]
    sma = mean(px[-SMA_LEN:])
    sma_prev = mean(px[-SMA_LEN - SLOPE_LOOKBACK:-SLOPE_LOOKBACK])
    rising = sma > sma_prev
    above = last > sma
    state = ("above_" if above else "below_") + ("rising" if rising else "falling")

    # Days since price last crossed the 200-day (counted on closes)
    since = 0
    for i in range(len(px) - 1, SMA_LEN - 1, -1):
        s = mean(px[i - SMA_LEN + 1:i + 1])
        if (px[i] > s) != above:
            break
        since += 1

    rocs = [100 * (px[i] / px[i - ROC_LEN] - 1) for i in range(ROC_LEN, len(px))]
    roc = rocs[-1]
    hist = rocs[-PCT_LOOKBACK:]
    pct = _percentile(hist, roc)
    sorted_hist = sorted(hist)
    p80 = sorted_hist[int(0.8 * (len(sorted_hist) - 1))]
    p20 = sorted_hist[int(0.2 * (len(sorted_hist) - 1))]
    recent = rocs[-20:]
    hi20, lo20 = max(px[-20:]), min(px[-20:])

    flags = []
    # Stretched only counts in the direction of the move (a slowing uptrend isn't "stretched down")
    if pct >= EXHAUSTED_PCT and roc > 0:
        flags.append("exhausted_up")
    elif pct <= 100 - EXHAUSTED_PCT and roc < 0:
        flags.append("exhausted_down")
    if max(recent) >= p80 and roc <= max(recent) / 2 and last >= 0.97 * hi20 and max(recent) > 0:
        flags.append("flattening_up")
    if min(recent) <= p20 and roc >= min(recent) / 2 and last <= 1.03 * lo20 and min(recent) < 0:
        flags.append("flattening_down")
    if abs(roc) <= RESET_BAND:
        flags.append("reset")
    if since < FRESH_CROSS_DAYS:
        flags.append("fresh_cross")

    return {
        "state": state, "state_text": STATE_TEXT[state], "above": above, "rising": rising,
        "close": last, "sma200": sma, "dist_pct": round(100 * (last / sma - 1), 1),
        "days_since_cross": since, "roc50": round(roc, 1), "roc_pct": pct, "roc_history": len(hist),
        "flags": flags, "asof": closes[-1][0].isoformat(), "source": source,
    }


def ceiling(reg: dict | None, direction: int) -> tuple[str | None, list[str]]:
    """Size ceiling for an idea in `direction` (+1 long, -1 short), with the reasons."""
    if not reg:
        return None, ["No 200-day reading for this market: size by your normal plan."]
    longs, shorts = CEILING[reg["state"]]
    step = longs if direction == 1 else shorts
    why = [f"Price {reg['state_text']} ({reg['dist_pct']:+.1f}% from it): "
           f"{STEP_TEXT[step]} for {'longs' if direction == 1 else 'shorts'}."]
    if "fresh_cross" in reg["flags"]:
        step = _cap(step, "half")
        why.append(f"Crossed the 200-day {reg['days_since_cross']} session(s) ago: treat as transition, max half.")
    if (direction == 1 and "exhausted_up" in reg["flags"]) or (direction == -1 and "exhausted_down" in reg["flags"]):
        step = _step_down(step)
        why.append(f"ROC 50 {reg['roc50']:+.1f}% is in the {'top' if direction == 1 else 'bottom'} "
                   f"{100 - EXHAUSTED_PCT}% of its history: too far, too fast, one size step down.")
    return step, why


def _ord(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def summary(reg: dict | None) -> str:
    """One line for Discord."""
    if not reg:
        return "No 200-day reading (not enough daily history)."
    notes = []
    f = reg["flags"]
    if "exhausted_up" in f:
        notes.append("stretched up")
    if "exhausted_down" in f:
        notes.append("stretched down")
    if "flattening_up" in f or "flattening_down" in f:
        notes.append("run flattening")
    if "reset" in f:
        notes.append("ROC reset near zero")
    if "fresh_cross" in f:
        notes.append(f"crossed {reg['days_since_cross']}d ago")
    longs, shorts = CEILING[reg["state"]]
    tail = f" · {', '.join(notes)}" if notes else ""
    return (f"{reg['state_text'].capitalize()} · ROC 50 {reg['roc50']:+.1f}% ({_ord(reg['roc_pct'])} pct)"
            f"{tail} · longs {STEP_TEXT[longs]}, shorts {STEP_TEXT[shorts]}")


# ─────────────────────────────────────────────
# DATA
# ─────────────────────────────────────────────
def _transform(v: float, how: str | None) -> float | None:
    if how == "inv":
        return 1 / v if v else None
    if how == "yield":
        return 100 - v
    return v


def parse_fred_csv(text: str, how: str | None = None) -> list[tuple[date, float]]:
    out = []
    for row in list(csv.reader(io.StringIO(text)))[1:]:
        if len(row) != 2 or row[1] in (".", ""):
            continue
        try:
            d = date.fromisoformat(row[0][:10])
            v = _transform(float(row[1]), how)
        except ValueError:
            continue
        if v is not None:
            out.append((d, v))
    return out


async def fred_closes(http: aiohttp.ClientSession, root: str) -> list[tuple[date, float]]:
    if root not in FRED_DAILY:
        return []
    series, how = FRED_DAILY[root]
    start = (datetime.now(timezone.utc) - timedelta(days=int(365 * 7.5))).strftime("%Y-%m-%d")
    try:
        async with http.get(config.FRED_CSV, params={"id": series, "cosd": start}) as r:
            text = await r.text()
    except Exception as e:  # noqa: BLE001
        log.warning("FRED daily fetch failed for %s (%s): %s", root, series, e)
        return []
    return parse_fred_csv(text, how)


def daily_from_bars(bars: list[dict]) -> list[tuple[date, float]]:
    """ProjectX daily bars -> (date, close), one per date."""
    by_day: dict[date, float] = {}
    for b in bars:
        by_day[b["t"].date()] = b["c"]
    return sorted(by_day.items())


async def regime_for(http: aiohttp.ClientSession, px, root: str, contract: dict | None) -> dict | None:
    """Regime reading for one market, cached for 6 hours."""
    key = f"regime:{root}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]  # type: ignore[return-value]
    reading = None
    closes = await fred_closes(http, root)
    if closes and (datetime.now(timezone.utc).date() - closes[-1][0]).days <= FRED_MAX_AGE_DAYS:
        reading = compute_regime(closes, source=f"FRED {FRED_DAILY[root][0]}")
    if reading is None and px is not None and contract:
        try:
            from .projectx import UNIT_DAY
            bars = await px.bars(contract["id"], UNIT_DAY, 1, days_back=800, include_partial=False)
            reading = compute_regime(daily_from_bars(bars), source=f"ProjectX {contract.get('name', root)}")
        except Exception as e:  # noqa: BLE001
            log.warning("ProjectX daily bars failed for %s: %s", root, e)
    if reading is None:
        log.info("No regime reading for %s (not enough daily history)", root)
    _cache[key] = (time.time(), reading)
    return reading
