"""Run the scanner.

  python -m scanner.main                  # loop forever (Railway)
  python -m scanner.main --once           # one scan, post to Discord
  python -m scanner.main --once --dry-run # one scan, print posts instead
  python -m scanner.main --list-contracts # show what ProjectX offers and how it maps
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp

from . import config, gates
from .discord_out import leaderboard_embed, post, watch_embed
from .metrics import build_thesis, compute
from .projectx import UNIT_HOUR, ProjectXClient, contract_root, pick_front_contracts
from .score import classify, score

log = logging.getLogger("scanner")
NY = ZoneInfo("America/New_York")
STATE_FILE = os.getenv("SCANNER_STATE_FILE", "scanner_state.json")

# Leaderboard posts (New York time): before Asia, before London, before New York
LEADERBOARD_TIMES = [(17, 45, "Asia open"), (2, 45, "London open"), (9, 15, "New York open")]


def globex_open(now: datetime) -> bool:
    """CME Globex: Sunday 18:00 to Friday 17:00 New York, closed 17:00-18:00 daily."""
    t = now.astimezone(NY)
    wd, h = t.weekday(), t.hour
    if wd == 5:
        return False
    if wd == 6:
        return h >= 18
    if wd == 4 and h >= 17:
        return False
    return h != 17


def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"posted": {}, "boards": {}}


def _save_state(s: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(s, f)
    except OSError as e:
        log.warning("Couldn't save state: %s", e)


async def scan_once(px: ProjectXClient, http: aiohttp.ClientSession, dry_run: bool, force_board: str | None = None):
    wanted = {m.root for m in config.MARKETS if m.root not in config.EXCLUDE_ROOTS}
    contracts = pick_front_contracts(await px.available_contracts(), wanted)
    missing = sorted(wanted - set(contracts))
    if missing:
        log.info("Not offered by ProjectX right now: %s", ", ".join(missing))

    cal = await gates.macro_calendar(http)
    now = datetime.now(timezone.utc)
    results = []
    for root, c in contracts.items():
        m = config.MARKETS_BY_ROOT[root]
        try:
            hourly = await px.bars(c["id"], UNIT_HOUR, 1, days_back=45)
        except Exception as e:  # noqa: BLE001
            log.warning("Bars failed for %s: %s", root, e)
            continue
        r = compute(root, hourly)
        if not r:
            log.info("Not enough history for %s", root)
            continue
        r.thesis = build_thesis(r)
        sc, reasons = score(r)
        g = None
        if r.thesis:
            d = r.thesis["dir"]
            pos = await gates.cot(http, m)
            iv = await gates.implied_vol(http, m.iv_series)
            g = {
                "positioning": gates.positioning_gate(pos, d),
                "options": gates.options_gate(iv, r.rv20, d),
                "catalyst": gates.catalyst_gate(m, cal, now, config.HOLD_WINDOW_HOURS),
            }
        results.append((m, r, sc, reasons, g))

    results.sort(key=lambda x: -x[2])
    state = _load_state()

    # Watch ideas (once per market, direction and trading day)
    embeds = []
    for m, r, sc, reasons, g in results:
        if not g or classify(sc, r, g, config.WATCH_MIN_SCORE) != "watch":
            continue
        key = f"{m.root}:{r.thesis['dir']}:{r.trading_day}"
        if key in state["posted"]:
            continue
        embeds.append(watch_embed(m, r, sc, reasons, g))
        state["posted"][key] = now.isoformat()
    for i in range(0, len(embeds), 5):
        await post(http, config.DISCORD_SCANNER_WEBHOOK, embeds[i:i + 5], dry_run)

    # Leaderboard at set times
    ny_now = now.astimezone(NY)
    label = force_board
    for hh, mm, name in LEADERBOARD_TIMES:
        slot = ny_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if slot <= ny_now < slot + timedelta(minutes=config.SCAN_INTERVAL_MIN):
            key = f"{name}:{ny_now.date()}"
            if key not in state["boards"]:
                state["boards"][key] = now.isoformat()
                label = name
    if label:
        top = [(m, r, sc, reasons) for m, r, sc, reasons, _ in results[: config.LEADERBOARD_SIZE]]
        await post(http, config.DISCORD_SCANNER_WEBHOOK, [leaderboard_embed(top, label)], dry_run)

    # Keep state small: drop entries older than 10 days
    cutoff = (now - timedelta(days=10)).isoformat()
    for k in ("posted", "boards"):
        state[k] = {a: b for a, b in state[k].items() if b >= cutoff}
    _save_state(state)
    log.info("Scan done: %d markets, %d watch ideas posted", len(results), len(embeds))
    return results


async def list_contracts(px: ProjectXClient):
    cs = await px.available_contracts()
    for c in sorted(cs, key=lambda c: c.get("name", "")):
        root = contract_root(c.get("name", ""))
        mapped = "✓" if root in config.MARKETS_BY_ROOT else " "
        print(f"{mapped} {c.get('name',''):10} root={root or '?':5} id={c.get('id')} active={c.get('activeContract')} {c.get('description','')}")


async def run(args):
    async with ProjectXClient(config.PROJECTX_BASE_URL, config.PROJECTX_USERNAME,
                              config.PROJECTX_API_KEY, config.PROJECTX_LIVE_DATA) as px, \
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
        if args.list_contracts:
            await list_contracts(px)
            return
        if args.once:
            await scan_once(px, http, args.dry_run, force_board="manual run")
            return
        log.info("Scanner running every %d min", config.SCAN_INTERVAL_MIN)
        while True:
            now = datetime.now(timezone.utc)
            if globex_open(now):
                try:
                    await scan_once(px, http, args.dry_run)
                except Exception:  # noqa: BLE001
                    log.exception("Scan failed; retrying next cycle")
            await asyncio.sleep(config.SCAN_INTERVAL_MIN * 60)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="Klaxton Desk market scanner")
    p.add_argument("--once", action="store_true", help="run one scan and exit")
    p.add_argument("--dry-run", action="store_true", help="print Discord posts instead of sending")
    p.add_argument("--list-contracts", action="store_true", help="list ProjectX contracts and exit")
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
