"""Discord webhook posts: Watch ideas and the leaderboard."""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

from . import desk, regime, symbols
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


def _size_field(m: Market, r: Reading, t: dict, ceiling: str | None = None) -> str:
    """Contracts per account at this stop. Information only, never a filter."""
    dist = abs(t["entry"] - t["stop"])
    f = regime.STEP_FACTOR[ceiling] if ceiling else 1.0
    out = [f"Stop distance {_px(dist, r)} points"]
    if ceiling:
        out.append(f"**Ceiling: {regime.STEP_TEXT[ceiling]}** (budgets x{f:g})")
    contracts = [(m.root, getattr(r, "contract", None))] + [(mi, mc) for mi, mc in getattr(r, "micro_contracts", []) or []]
    priced = [(name, symbols.point_value(c)) for name, c in contracts]
    priced = [(name, pv) for name, pv in priced if pv]
    if not priced:
        return "\n".join(out)
    per = {name: dist * pv for name, pv in priced}
    out.append(" · ".join(f"1 {name} = ${v:,.0f}" for name, v in per.items()))
    any_fit = False
    for acct, budget in symbols.RISK_ACCOUNTS:
        b = budget * f
        fits = [(name, int(b // v)) for name, v in per.items() if v and int(b // v) >= 1]
        if fits:
            any_fit = True
            out.append(f"**{acct} ${b:,.0f}:** " + " or ".join(f"{n} {name}" for name, n in fits))
        else:
            out.append(f"**{acct} ${b:,.0f}:** too wide at this stop")
    if not any_fit:
        out.append("Refine on a lower timeframe for a tighter stop.")
    elif any("too wide" in x for x in out):
        out.append("Smaller account: refine the entry on a lower timeframe.")
    return "\n".join(out)


def _levels_field(r: Reading) -> str:
    names = {"PWH": "Prev week high", "PDH": "Prev day high", "MonH": "Monday high",
             "MonL": "Monday low", "PDL": "Prev day low", "PWL": "Prev week low"}
    rows = [(v, names.get(k, k)) for k, v in r.levels.items()] + [(r.last, "**Price now**")]
    rows.sort(key=lambda x: -x[0])
    return "\n".join(f"{_px(v, r)}  {lab}" for v, lab in rows)


def watch_embed(m: Market, r: Reading, sc: int, reasons: list[str], gates: dict,
                reg: dict | None = None, verdict: dict | None = None) -> dict:
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
        {"name": "📏 Size", "value": _size_field(m, r, t, (verdict or {}).get("ceiling"))[:1000], "inline": True},
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
        {"name": "📐 Trend regime (200-day)", "value": regime.summary(reg)[:1000], "inline": False},
        {"name": "Why it scored", "value": " · ".join(reasons)[:1000] or "n/a", "inline": False},
    ]
    if getattr(r, "acd_line", None):
        fields.insert(-1, {"name": "📐 ACD (opening range)", "value": r.acd_line[:1000], "inline": False})
    if verdict:
        if verdict["verdict"] == "verified":
            head = "✅ **Verified** and sent to the desk channel."
        else:
            head = "❌ **Rejected:** " + verdict["reason"]
        fields.append({"name": "🧑‍💼 Head of desk", "value": head[:1000], "inline": False})
    return {
        "title": f"👀 WATCH · {side} {m.name} ({m.root}) · {sc}/100",
        "description": "\n".join(lines)[:4000],
        "color": GREEN if t["dir"] == 1 else RED,
        "fields": fields,
        "footer": {"text": "Watch, not take: your entry signature decides. Hold it, max 2 trades a day. Not advice."},
        "timestamp": datetime.now(SYD).isoformat(),
    }


def verified_embed(m: Market, r: Reading, sc: int, gates: dict, reg: dict | None, verdict: dict) -> dict:
    """The idea as the head of desk passes it on: plan, size under the ceiling, every check."""
    t = r.thesis
    side = "LONG" if t["dir"] == 1 else "SHORT"
    lean = "bullish" if t["dir"] == 1 else "bearish"
    reward = abs((t["target"] or t["entry"]) - t["entry"])
    ceiling = verdict.get("ceiling")
    desc = [
        f"**{t['why']}.** A **{lean} {t['kind']}** with room to {t['target_name']} ({t['rr']}R).",
        f"Size ceiling: **{regime.STEP_TEXT[ceiling]}**." if ceiling else "No trend regime reading: size by your normal plan.",
        "",
        f"🔎 **Look for:** {LOOK_FOR.get(t['kind'], '')}",
    ]
    fields = [
        {"name": "📍 Symbols", "value": _symbols_field(m, r)[:1000], "inline": False},
        {"name": "🎯 Plan", "value": (f"**Entry zone** {_px(t['entry'], r)}\n"
                                     f"**Stop** {_px(t['stop'], r)}\n"
                                     f"**Target** {_px(t['target'], r)}: {t['target_name']} ({_px(reward, r)} pts, {t['rr']}R)"),
         "inline": True},
        {"name": "📏 Size", "value": _size_field(m, r, t, ceiling)[:1000], "inline": True},
        {"name": "🧑‍💼 Head of desk checks", "value": desk.checks_text(verdict), "inline": False},
        {"name": "📐 Trend regime (200-day)", "value": regime.summary(reg)[:1000], "inline": False},
        {"name": "Options structure", "value": (gates["options"]["text"] + f"\n**Use:** {gates['options']['structure']}")[:1000],
         "inline": False},
    ]
    return {
        "title": f"✅ VERIFIED · {side} {m.name} ({m.root}) · {sc}/100",
        "description": "\n".join(desc)[:3000],
        "color": GREEN if t["dir"] == 1 else RED,
        "fields": fields,
        "footer": {"text": "Checked by the head of desk. Your entry signature still decides. Max 2 trades a day. Not advice."},
        "timestamp": datetime.now(SYD).isoformat(),
    }


def leaderboard_embed(rows: list[tuple], label: str, desk_note: str = "") -> dict:
    """rows: (market, reading, score, reasons)"""
    lines = []
    for i, row in enumerate(rows, 1):
        m, r, sc, reasons = row[:4]
        reg = row[4] if len(row) > 4 else None
        lean = ""
        if r.thesis:
            lean = " · lean " + ("long" if r.thesis["dir"] == 1 else "short")
        c = getattr(r, "contract", None) or {}
        cont, _ = symbols.tradingview(m.root, c.get("name"))
        lines.append(f"**{i}. {m.name}** · **{sc}**{lean}\n   TopstepX `{c.get('name') or m.root}` · TradingView `{cont}`\n   "
                     f"{' · '.join(reasons[:3]) or 'quiet'}"
                     + (f"\n   📐 {reg['state_text']} · ROC 50 {reg['roc50']:+.1f}%" if reg else ""))
    body = "\n".join(lines) or "No market is far from normal right now."
    if desk_note:
        body += "\n\n" + desk_note
    return {
        "title": f"📊 Biggest movers · {label}",
        "description": body[:4000],
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
