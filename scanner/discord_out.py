"""Discord webhook posts: Watch ideas and the leaderboard."""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

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


def watch_embed(m: Market, r: Reading, sc: int, reasons: list[str], gates: dict) -> dict:
    t = r.thesis
    side = "LONG" if t["dir"] == 1 else "SHORT"
    tradable = m.root + (f" (micro: {', '.join(m.micros)})" if m.micros else "")
    lines = [
        f"**{side} idea · {t['kind']}** · score **{sc}/100**",
        f"{t['why']}.",
        "",
        f"**Entry zone** {_px(t['entry'], r)} (wait for your entry signature)",
        f"**Stop** {_px(t['stop'], r)}  ·  **Target** {_px(t['target'], r)} ({t['target_name']})  ·  **{t['rr']}R**",
        f"Daily ATR {_px(r.atr, r)} · today already {r.range_ratio}x ATR · {r.weekday}",
    ]
    prof = r.profile.get("continuation") or r.profile.get("c2")
    if prof:
        lines.append(f"Weekly profile: {prof['text']}")
    fields = [
        {"name": "Why it's on the list", "value": " · ".join(reasons)[:1000] or "n/a", "inline": False},
        {"name": f"{_GATE_ICON[gates['positioning']['status']]} Gate 2 · Positioning",
         "value": gates["positioning"]["text"][:1000], "inline": False},
        {"name": f"{_GATE_ICON[gates['options']['status']]} Gate 3 · Options",
         "value": (gates["options"]["text"] + f"\n**Structure:** {gates['options']['structure']}")[:1000], "inline": False},
        {"name": f"{_GATE_ICON[gates['catalyst']['status']]} Gate 4 · Catalysts",
         "value": gates["catalyst"]["text"][:1000], "inline": False},
    ]
    return {
        "title": f"👀 WATCH · {m.name} · {tradable}",
        "description": "\n".join(lines)[:4000],
        "color": GREEN if t["dir"] == 1 else RED,
        "fields": fields,
        "footer": {"text": "Watch, not take: your setup decides the entry. Context, not advice."},
        "timestamp": datetime.now(SYD).isoformat(),
    }


def leaderboard_embed(rows: list[tuple], label: str) -> dict:
    """rows: (market, reading, score, reasons)"""
    lines = []
    for i, (m, r, sc, reasons) in enumerate(rows, 1):
        lean = ""
        if r.thesis:
            lean = " · lean " + ("long" if r.thesis["dir"] == 1 else "short")
        lines.append(f"**{i}. {m.root}** {m.name} · **{sc}**{lean}\n   {' · '.join(reasons[:3]) or 'quiet'}")
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
