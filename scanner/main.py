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

from . import config, gates, guardrails
from .discord_out import leaderboard_embed, post, watch_embed
from .metrics import build_thesis, compute
from .projectx import UNIT_HOUR, ProjectXClient, contract_root, pick_front_contracts
from .score import classify, score

log = logging.getLogger("scanner")
NY = ZoneInfo("America/New_York")
STATE_FILE = os.getenv("SCANNER_STATE_FILE", "scanner_state.json")
GUARD_FILE = os.getenv("GUARD_STATE_FILE", "guard_state.json")


def _load_seen() -> set:
    try:
        with open(GUARD_FILE) as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def _save_seen(seen: set) -> None:
    try:
        with open(GUARD_FILE, "w") as f:
            json.dump(sorted(seen)[-3000:], f)
    except OSError as e:
        log.warning("Couldn't save guard state: %s", e)

# Leaderboard posts (New York time): before Asia, before London, before New York
# London board dropped: the London session won 1 trade in 5 in the trade history.
LEADERBOARD_TIMES = [(17, 45, "Asia open"), (9, 15, "New York open")]


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
        return {"posted": {}, "boards": {}, "ideas": {}}


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

    # Watch ideas (once per market, direction and trading day), best first.
    # Guardrails: none in the London window, at most MAX_IDEAS_PER_DAY per trading day.
    embeds = []
    state.setdefault("ideas", {})
    quiet = guardrails.in_no_idea_window(now)
    for m, r, sc, reasons, g in results:
        if not g or classify(sc, r, g, config.WATCH_MIN_SCORE) != "watch":
            continue
        key = f"{m.root}:{r.thesis['dir']}:{r.trading_day}"
        if key in state["posted"]:
            continue
        day = str(r.trading_day)
        if quiet:
            log.info("Skipping %s idea: London window", m.root)
            continue
        if state["ideas"].get(day, 0) >= guardrails.MAX_IDEAS_PER_DAY:
            log.info("Skipping %s idea: daily idea cap reached", m.root)
            continue
        embeds.append(watch_embed(m, r, sc, reasons, g))
        state["posted"][key] = now.isoformat()
        state["ideas"][day] = state["ideas"].get(day, 0) + 1
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
    state["ideas"] = dict(sorted(state.get("ideas", {}).items())[-10:])
    _save_state(state)
    log.info("Scan done: %d markets, %d watch ideas posted", len(results), len(embeds))
    return results


async def guard_once(px: ProjectXClient, http: aiohttp.ClientSession, accounts: list[dict], dry_run: bool):
    """Read-only check of each active account's fills today; posts guardrail messages."""
    now = datetime.now(timezone.utc)
    seen = _load_seen()
    embeds = []
    for a in accounts:
        try:
            fills = await px.trades(a["id"], guardrails.trading_day_start(now))
        except Exception as e:  # noqa: BLE001
            log.warning("Trade check failed for %s: %s", a.get("name"), e)
            continue
        embeds += guardrails.review_account(a.get("name") or str(a["id"]), fills, seen, now)
    if embeds:
        await post(http, config.DISCORD_SCANNER_WEBHOOK, embeds[:10], dry_run)
    _save_seen(seen)


async def guard_loop(px: ProjectXClient, http: aiohttp.ClientSession, dry_run: bool):
    accounts, fetched = [], datetime.min.replace(tzinfo=timezone.utc)
    first = True
    while True:
        now = datetime.now(timezone.utc)
        if globex_open(now):
            try:
                if now - fetched > timedelta(hours=1):
                    accounts = [a for a in await px.active_accounts() if a.get("canTrade", True)]
                    fetched = now
                    log.info("Watching %d account(s): %s", len(accounts), ", ".join(str(a.get("name")) for a in accounts))
                if first:
                    # Don't replay today's old trades on start-up: mark them seen silently.
                    seen = _load_seen()
                    for a in accounts:
                        guardrails.review_account(a.get("name") or str(a["id"]),
                                                  await px.trades(a["id"], guardrails.trading_day_start(now)), seen, now)
                    _save_seen(seen)
                    first = False
                else:
                    await guard_once(px, http, accounts, dry_run)
            except Exception:  # noqa: BLE001
                log.exception("Guard check failed; retrying")
        await asyncio.sleep(60)


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
        log.info("Scanner running every %d min; guardrails checking accounts every minute", config.SCAN_INTERVAL_MIN)
        asyncio.create_task(guard_loop(px, http, args.dry_run))
        first = True
        while True:
            now = datetime.now(timezone.utc)
            if globex_open(now):
                try:
                    # Post a leaderboard on start-up so a redeploy shows the scanner is alive
                    await scan_once(px, http, args.dry_run, force_board="scanner started" if first else None)
                    first = False
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
