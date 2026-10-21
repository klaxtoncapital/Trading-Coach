"""COT extremes board: a weekly ranking of commodity positioning.

The per-idea positioning gate (gates.cot) already checks managed money on the
market an idea is in. This board adds the weekly view across every commodity:

  * Managed money (hedge funds / CTAs) net position as a 3-year percentile
  * Commercials (producers and merchants: the physical hedgers) net position as a
    3-year percentile, the "smart money" side of the classic COT read
  * The one-week change in managed money, in % of open interest
  * A setup flag when the two sides sit at opposite extremes:
      squeeze-up risk  = managed money crowded short + commercials heavily long
      flush-down risk  = managed money crowded long  + commercials heavily short

Posted once per report (CFTC publishes Friday 3:30pm ET = Saturday morning
Sydney). Source: CFTC Public Reporting, disaggregated futures-only, keyless.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp

from . import config

log = logging.getLogger("scanner.cot_board")
SYD = ZoneInfo("Australia/Sydney")

COT_BOARD_WEBHOOK = os.getenv("DISCORD_COT_WEBHOOK", "")
COT_BOARD_HOUR_SYD = int(os.getenv("COT_BOARD_HOUR_SYD", "") or 9)   # Saturday, after the 6:30am release
EXTREME = int(os.getenv("COT_EXTREME_PCT", "") or 90)                # top/bottom 10% = extreme
WEEKS = 156
CLASSES = ("energy", "metals", "ags")


def _pick(row: dict, name: str) -> float | None:
    """CFTC field names vary slightly: prefer name_all, then name, then any name* (not _old/_other)."""
    keys = [name + "_all", name] + sorted(k for k in row if k.startswith(name) and not k.endswith(("_old", "_other")))
    for k in keys:
        if k in row:
            try:
                return float(row[k])
            except (TypeError, ValueError):
                return None
    return None


def _pct(values: list[float], x: float) -> int:
    return round(100 * sum(1 for v in values if v < x) / max(len(values) - 1, 1))


def read_market(rows: list[dict]) -> dict | None:
    """rows: newest first, raw CFTC disaggregated records for one market."""
    mm, com, oi = [], [], []
    for r in rows:
        ml, ms = _pick(r, "m_money_positions_long"), _pick(r, "m_money_positions_short")
        pl, ps = _pick(r, "prod_merc_positions_long"), _pick(r, "prod_merc_positions_short")
        o = _pick(r, "open_interest_all")
        if None in (ml, ms, pl, ps, o) or not o:
            continue
        mm.append(ml - ms)
        com.append(pl - ps)
        oi.append(o)
    if len(mm) < 52:
        return None
    return {
        "report_date": str(rows[0].get("report_date_as_yyyy_mm_dd", ""))[:10],
        "mm_net": int(mm[0]), "mm_pct": _pct(mm, mm[0]),
        "com_net": int(com[0]), "com_pct": _pct(com, com[0]),
        "mm_chg_oi": round(100 * (mm[0] - mm[1]) / oi[0], 1),
        "weeks": len(mm),
    }


def flag(x: dict) -> str | None:
    lo, hi = 100 - EXTREME, EXTREME
    if x["mm_pct"] <= lo and x["com_pct"] >= hi:
        return "squeeze-up risk"
    if x["mm_pct"] >= hi and x["com_pct"] <= lo:
        return "flush-down risk"
    if x["mm_pct"] <= lo:
        return "funds crowded short"
    if x["mm_pct"] >= hi:
        return "funds crowded long"
    return None


async def fetch(http: aiohttp.ClientSession) -> dict[str, dict]:
    out = {}
    for m in config.MARKETS:
        if m.asset_class not in CLASSES or m.cot_report != "disagg" or not m.cot_code:
            continue
        params = {"$where": f"cftc_contract_market_code='{m.cot_code}'",
                  "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": str(WEEKS)}
        try:
            async with http.get(f"{config.CFTC_BASE}/{config.CFTC_DISAGG}.json", params=params) as r:
                rows = await r.json(content_type=None)
        except Exception as e:  # noqa: BLE001
            log.warning("COT board fetch failed for %s: %s", m.root, e)
            continue
        x = read_market(rows or [])
        if x:
            out[m.root] = {**x, "name": m.name, "micros": m.micros}
    return out


def board_embed(data: dict[str, dict]) -> dict:
    ranked = sorted(data.items(), key=lambda kv: -abs(kv[1]["mm_pct"] - 50))
    setups = [(r, x) for r, x in ranked if flag(x) in ("squeeze-up risk", "flush-down risk")]
    lines = []
    for root, x in ranked:
        f = flag(x)
        icon = "🟢" if f == "squeeze-up risk" else "🔴" if f == "flush-down risk" else "🟠" if f else "⚪"
        lines.append(f"{icon} **{root}** {x['name']}: funds {_ord(x['mm_pct'])} pct · commercials {_ord(x['com_pct'])} pct"
                     f" · funds {x['mm_chg_oi']:+.1f}% OI w/w" + (f" · _{f}_" if f else ""))
    movers = sorted(data.items(), key=lambda kv: -abs(kv[1]["mm_chg_oi"]))[:3]
    fields = []
    if setups:
        fields.append({"name": "🎯 Opposite extremes (the classic COT setup)",
                       "value": "\n".join(f"**{r}**: {flag(x)}. Funds {_ord(x['mm_pct'])}, commercials {_ord(x['com_pct'])}. "
                                          "Positioning is fuel, not a trigger: wait for price to turn."
                                          for r, x in setups)[:1000], "inline": False})
    fields.append({"name": "⚡ Biggest fund moves this week",
                   "value": " · ".join(f"{r} {x['mm_chg_oi']:+.1f}% OI" for r, x in movers)[:1000], "inline": False})
    rep = max((x["report_date"] for x in data.values()), default="")
    return {
        "title": f"📋 COT extremes · commodities · report {rep}",
        "description": ("Ranked by how stretched hedge funds (managed money) are vs their own 3 years. "
                        "Commercials are the producers and merchants hedging real barrels and bushels.\n\n"
                        + "\n".join(lines))[:4000],
        "color": 0x8C6A1F,
        "fields": fields,
        "footer": {"text": f"CFTC disaggregated, futures only. Extreme = top/bottom {100 - EXTREME}% of 3 years. Not advice."},
    }


def _ord(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def due(now: datetime, posted: str | None) -> bool:
    """Saturday from COT_BOARD_HOUR_SYD, or any later day if the PC/service missed it, once per week."""
    s = now.astimezone(SYD)
    this_sat = (s - timedelta(days=(s.weekday() - 5) % 7)).date()
    if s.weekday() == 5 and s.hour < COT_BOARD_HOUR_SYD:
        return False
    return posted != this_sat.isoformat()


STATE_FILE = os.getenv("COT_BOARD_STATE_FILE", "cot_board_state.json")


def _load() -> dict:
    import json
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(s: dict) -> None:
    import json
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(s, f)
    except OSError as e:
        log.warning("Couldn't save COT board state: %s", e)


async def board_loop(http: aiohttp.ClientSession, post, dry_run: bool):
    """Runs all week (the scanner pauses at weekends, this doesn't). Checks hourly."""
    import asyncio
    while True:
        try:
            now = datetime.now(timezone.utc)
            state = _load()
            if due(now, state.get("cot_board")):
                data = await fetch(http)
                if data:
                    hook = COT_BOARD_WEBHOOK or config.DISCORD_DESK_WEBHOOK or config.DISCORD_SCANNER_WEBHOOK
                    await post(http, hook, [board_embed(data)], dry_run)
                    s = now.astimezone(SYD)
                    state["cot_board"] = (s - timedelta(days=(s.weekday() - 5) % 7)).date().isoformat()
                    _save(state)
        except Exception:  # noqa: BLE001
            log.exception("COT board failed; retrying in an hour")
        await asyncio.sleep(3600)
