"""Discord webhook posts: Watch ideas and the leaderboard."""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

from . import symbols
from .config import Market
from .metrics import Reading

log = logging.getLogger("scanner.discord")
SYD = ZoneInfo("Australia/Sydney")
GREEN, RED, GREY = 0x1F6B52, 0xA33A2A, 0x5D6661


def _px(x: float | None, r: Reading) -> str:
    if x is None:
        return "n/a"
    # Enough decimals for FX (0.0001) and rates, none for indices
    a = r.atr
    dp = 0 if a >= 50 else 1 if a >= 5 else 2 if a >= 0.5 else 3 if a >= 0.05 else 5
    return f"{x:,.{dp}f}"


_GATE_ICON = {"pass": "✅", "support": "✅", "warn": "⚠️", "fail": "❌", "n/a": "➖"}


LOOK_FOR = {
    "reversal": ("Your entry signature after the sweep: displacement back through the level, then MSS/CISD on the 1-5 min, "
                 "entry from the FVG / IFVG / OB it leaves. No signature, no trade."),
    "continuation": ("Don't chase. Wait for the pullback into the displacement candle's FVG or the broken level, "
                     "then your entry signature in the direction of the move."),
}


def _symbols_field(m: Market, r: Reading) -> str:
    c = getattr(r, "contract", None) or {}
    name = c.get("name") or m.root
    cont, exact = symbols.tradingview(m.root, c.get("name"))
    lines = [f"**TopstepX:** `{name}`", f"**TradingView:** `{cont}`" + (f"  (this contract `{exact}`)" if exact else "")]
    for mi, mc in getattr(r, "micro_contracts", []) or []:
        mcont, _ = symbols.tradingview(mi, (mc or {}).get("name"))
        lines.append(f"Micro: TopstepX `{(mc or {}).get('name') or mi}` · TradingView `{mcont}`")
    return "\n".join(lines)


def _size_field(m: Market, r: Reading, t: dict) -> str:
    dist = abs(t["entry"] - t["stop"])
    out = [f"Stop distance {_px(dist, r)} points · risk budget ${symbols.RISK_PER_TRADE:,.0f}"]
    full = symbols.size_for_risk(dist, symbols.point_value(getattr(r, "contract", None)))
    if full:
        n, per = full
        out.append(f"**{m.root}:** {n} contract{'s' if n != 1 else ''} (${per:,.0f} risk each)" if n
                   else f"**{m.root}:** too big, 1 contract = ${per:,.0f} risk")
    for mi, mc in getattr(r, "micro_contracts", []) or []:
        sz = symbols.size_for_risk(dist, symbols.point_value(mc))
        if sz:
            n, per = sz
            out.append(f"**{mi}:** {n} contract{'s' if n != 1 else ''} (${per:,.0f} risk each)" if n
                       else f"**{mi}:** too big, 1 contract = ${per:,.0f} risk")
    return "\n".join(out)


def _levels_field(r: Reading) -> str:
    names = {"PWH": "Prev week high", "PDH": "Prev day high", "MonH": "Monday high",
             "MonL": "Monday low", "PDL": "Prev day low", "PWL": "Prev week low"}
    rows = [(v, names.get(k, k)) for k, v in r.levels.items()] + [(r.last, "**Price now**")]
    rows.sort(key=lambda x: -x[0])
    return "\n".join(f"{_px(v, r)}  {lab}" for v, lab in rows)


def watch_embed(m: Market, r: Reading, sc: int, reasons: list[str], gates: dict) -> dict:
    t = r.thesis
    side = "LONG" if t["dir"] == 1 else "SHORT"
    lean = "bullish" if t["dir"] == 1 else "bearish"
    reward = abs((t["target"] or t["entry"]) - t["entry"])
    lines = [
        f"**{t['why']}.** The scanner reads this as a **{lean} {t['kind']}** with room to "
        f"{t['target_name']} ({t['rr']}R).",
        "",
        f"🔎 **Look for:** {LOOK_FOR.get(t['kind'], '')}",
    ]
    prof = r.profile.get("continuation") or r.profile.get("c2")
    fields = [
        {"name": "📍 Symbols", "value": _symbols_field(m, r)[:1000], "inline": False},
        {"name": "🎯 Plan", "value": (f"**Entry zone** {_px(t['entry'], r)}\n"
                                     f"**Stop** {_px(t['stop'], r)} (beyond the sweep / structure)\n"
                                     f"**Target** {_px(t['target'], r)}: {t['target_name']} ({_px(reward, r)} pts, {t['rr']}R)"),
         "inline": True},
        {"name": "📏 Size", "value": _size_field(m, r, t)[:1000], "inline": True},
        {"name": "🧭 Key levels", "value": _levels_field(r)[:1000], "inline": True},
        {"name": "📈 Context", "value": (f"Daily ATR {_px(r.atr, r)} pts · today {r.range_ratio}x ATR\n"
                                        f"Volume {r.relvol}x normal · biggest 1h candle {r.displacement}x normal\n"
                                        f"Week range {r.week_range_ratio}x normal · {r.weekday}"
                                        + (f"\nWeekly profile: {prof['text']}" if prof else "")), "inline": True},
        {"name": f"{_GATE_ICON[gates['positioning']['status']]} Positioning (COT)",
         "value": gates["positioning"]["text"][:1000], "inline": False},
        {"name": f"{_GATE_ICON[gates['options']['status']]} Options surface",
         "value": (gates["options"]["text"] + f"\n**Options structure:** {gates['options']['structure']}")[:1000], "inline": False},
        {"name": f"{_GATE_ICON[gates['catalyst']['status']]} Catalysts in your hold",
         "value": gates["catalyst"]["text"][:1000], "inline": False},
        {"name": "Why it scored", "value": " · ".join(reasons)[:1000] or "n/a", "inline": False},
    ]
    return {
        "title": f"👀 WATCH · {side} {m.name} ({m.root}) · {sc}/100",
        "description": "\n".join(lines)[:4000],
        "color": GREEN if t["dir"] == 1 else RED,
        "fields": fields,
        "footer": {"text": "Watch, not take: your entry signature decides. Hold it, max 2 trades a day. Not advice."},
        "timestamp": datetime.now(SYD).isoformat(),
    }


def leaderboard_embed(rows: list[tuple], label: str) -> dict:
    """rows: (market, reading, score, reasons)"""
    lines = []
    for i, (m, r, sc, reasons) in enumerate(rows, 1):
        lean = ""
        if r.thesis:
            lean = " · lean " + ("long" if r.thesis["dir"] == 1 else "short")
        c = getattr(r, "contract", None) or {}
        cont, _ = symbols.tradingview(m.root, c.get("name"))
        lines.append(f"**{i}. {m.name}** · **{sc}**{lean}\n   TopstepX `{c.get('name') or m.root}` · TradingView `{cont}`\n   "
                     f"{' · '.join(reasons[:3]) or 'quiet'}")
    return {
        "title": f"📊 Biggest movers · {label}",
        "description": "\n".join(lines)[:4000] or "No market is far from normal right now.",
        "color": GREY,
        "footer": {"text": "Ranked by how far each market is from its own normal range, volume and candle size."},
        "timestamp": datetime.now(SYD).isoformat(),
    }


async def post(session: aiohttp.ClientSession, webhook: str, embeds: list[dict], dry_run: bool = False) -> bool:
    payload = {"username": "Klaxton Scanner", "embeds": embeds[:10]}
    if dry_run or not webhook:
        import json
        print(json.dumps(payload, indent=1, default=str))
        return True
    async with session.post(webhook, json=payload) as r:
        if r.status not in (200, 204):
            log.warning("Discord post failed: HTTP %s %s", r.status, (await r.text())[:200])
            return False
    return True
