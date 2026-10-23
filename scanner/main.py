"""Run the scanner.

  python -m scanner.main                  # loop forever (Railway)
  python -m scanner.main --once           # one scan, post to Discord
  python -m scanner.main --once --dry-run # one scan, print posts instead
  python -m scanner.main --list-contracts # show what ProjectX offers and how it maps
  python -m scanner.main --regime         # print the 200-day regime for every market
  python -m scanner.main --news           # print the latest market-moving headlines
  python -m scanner.main --cot-board      # print this week's commodity COT extremes board
  python -m scanner.main --four-charts    # print today's Four Charts bias post
  python -m scanner.main --acd-backtest GC,SI,CL,NQ --days 180   # ACD backtest on ProjectX history
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

from . import acd, acd_backtest, config, cot_board, four_charts, desk, earnings, gates, guardrails, macro, news, regime
from .discord_out import leaderboard_embed, post, verified_embed, watch_embed
from .metrics import build_thesis, compute, to_days
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
        return {"posted": {}, "boards": {}, "ideas": {}, "verified": {}, "rejected": {}}


def _save_state(s: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(s, f)
    except OSError as e:
        log.warning("Couldn't save state: %s", e)


async def scan_once(px: ProjectXClient, http: aiohttp.ClientSession, dry_run: bool, force_board: str | None = None):
    wanted = {m.root for m in config.MARKETS if m.root not in config.EXCLUDE_ROOTS}
    all_contracts = await px.available_contracts()
    contracts = pick_front_contracts(all_contracts, wanted)
    micro_roots = {mi for m in config.MARKETS for mi in m.micros}
    micros = pick_front_contracts(all_contracts, micro_roots)
    missing = sorted(wanted - set(contracts))
    if missing:
        log.info("Not offered by ProjectX right now: %s", ", ".join(missing))

    cal = await gates.macro_calendar(http)
    cal = cal + await earnings.upcoming(http)
    macro_call = await macro.latest(http)
    now = datetime.now(timezone.utc)
    results = []
    readings = {}   # root -> (Reading, days) for the ACD agent
    for root, c in contracts.items():
        m = config.MARKETS_BY_ROOT[root]
        try:
            hourly = await px.bars(c["id"], UNIT_HOUR, 1, days_back=45)
        except Exception as e:  # noqa: BLE001
            log.warning("Bars failed for %s: %s", root, e)
            continue
        r = compute(root, hourly)
        if r:
            r.contract = c
            r.last_t = hourly[-1]["t"] if hourly else None
            r.micro_contracts = [(mi, micros.get(mi)) for mi in m.micros]
        if not r:
            log.info("Not enough history for %s", root)
            continue
        readings[root] = (r, to_days(hourly))
        r.thesis = build_thesis(r)
        sc, reasons = score(r)
        reg = await regime.regime_for(http, px, root, c)
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
        results.append((m, r, sc, reasons, g, reg))

    results.sort(key=lambda x: -x[2])
    state = _load_state()

    # ACD agent: opening-range signals (own post), plus a line on each Watch idea
    try:
        acd_summary = await acd.run_live(px, http, contracts, readings, state, now, post, dry_run)
    except Exception:  # noqa: BLE001
        log.exception("ACD agent failed this scan")
        acd_summary = {}
    for m, r, *_ in results:
        r.acd_line = acd.watch_field(acd_summary, m.root)

    # Watch ideas (once per market, direction and trading day), best first.
    # Every idea goes to the head of desk. The raw idea (with the verdict) goes to
    # #scanner; only VERIFIED ideas go to the desk channel.
    # Guardrails: none in the London window, at most MAX_IDEAS_PER_DAY verified per trading day.
    embeds, verified_embeds = [], []
    for k in ("ideas", "verified", "rejected"):
        state.setdefault(k, {})
    quiet = guardrails.in_no_idea_window(now)
    for m, r, sc, reasons, g, reg in results:
        if not g or classify(sc, r, g, config.WATCH_MIN_SCORE) != "watch":
            continue
        key = f"{m.root}:{r.thesis['dir']}:{r.trading_day}"
        if key in state["posted"]:
            continue
        day = str(r.trading_day)
        if quiet:
            log.info("Skipping %s idea: London window", m.root)
            continue
        v = desk.verify(r, g, reg, macro_call, now, state["verified"].get(day))
        if v["verdict"] == "verified" and state["ideas"].get(day, 0) >= guardrails.MAX_IDEAS_PER_DAY:
            v["verdict"] = "rejected"
            v["reason"] = f"Daily cap: {guardrails.MAX_IDEAS_PER_DAY} ideas already verified today."
        embeds.append(watch_embed(m, r, sc, reasons, g, reg, v))
        state["posted"][key] = now.isoformat()
        if v["verdict"] == "verified":
            verified_embeds.append(verified_embed(m, r, sc, g, reg, v))
            state["ideas"][day] = state["ideas"].get(day, 0) + 1
            state["verified"].setdefault(day, {})[f"{m.root}:{r.thesis['dir']}"] = now.isoformat()
        else:
            side = "long" if r.thesis["dir"] == 1 else "short"
            state["rejected"].setdefault(day, []).append({"market": m.root, "side": side, "reason": v["reason"][:300]})
            log.info("Head of desk rejected %s %s: %s", m.root, side, v["reason"])
    # One embed per message keeps each post under Discord's 6,000-character limit
    for e in embeds:
        await post(http, config.DISCORD_SCANNER_WEBHOOK, [e], dry_run)
    for e in verified_embeds:
        if config.DISCORD_DESK_WEBHOOK or dry_run:
            await post(http, config.DISCORD_DESK_WEBHOOK, [e], dry_run)

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
        top = [(m, r, sc, reasons, reg) for m, r, sc, reasons, _, reg in results[: config.LEADERBOARD_SIZE]]
        await post(http, config.DISCORD_SCANNER_WEBHOOK, [leaderboard_embed(top, label, desk_note(state))], dry_run)

    # Keep state small: drop entries older than 10 days
    cutoff = (now - timedelta(days=10)).isoformat()
    for k in ("posted", "boards"):
        state[k] = {a: b for a, b in state[k].items() if b >= cutoff}
    for k in ("ideas", "verified", "rejected"):
        state[k] = dict(sorted(state.get(k, {}).items())[-10:])
    _save_state(state)
    log.info("Scan done: %d markets, %d ideas, %d verified", len(results), len(embeds), len(verified_embeds))
    return results


def desk_note(state: dict) -> str:
    """Head of desk tally for the leaderboard: today's verified and rejected ideas."""
    if not state.get("verified") and not state.get("rejected"):
        return ""
    day = max(list(state.get("verified", {})) + list(state.get("rejected", {})), default=None)
    if not day:
        return ""
    ver = state.get("verified", {}).get(day, {})
    rej = state.get("rejected", {}).get(day, [])
    lines = [f"🧑‍💼 **Head of desk, trading day {day}:** {len(ver)} verified, {len(rej)} rejected"]
    for x in rej[-4:]:
        lines.append(f"   ❌ {x['market']} {x['side']}: {x['reason'][:140]}")
    return "\n".join(lines)


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
        await post(http, config.DISCORD_SCANNER_WEBHOOK, merge_by_account(embeds)[:10], dry_run)
    _save_seen(seen)


def merge_by_account(embeds: list[dict]) -> list[dict]:
    """Copied accounts trigger the same message; post it once and list the accounts."""
    merged: dict[str, dict] = {}
    for e in embeds:
        head, _, acct = e["title"].rpartition(" · ")
        key = head or e["title"]
        if key not in merged:
            merged[key] = {**e, "title": key, "_accts": [acct] if head else []}
        elif head:
            merged[key]["_accts"].append(acct)
    out = []
    for e in merged.values():
        accts = e.pop("_accts")
        if accts:
            e["footer"] = {"text": ("Accounts: " + ", ".join(accts))[:2000]}
        out.append(e)
    return out


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


async def print_regimes(px: ProjectXClient, http: aiohttp.ClientSession):
    wanted = {m.root for m in config.MARKETS if m.root not in config.EXCLUDE_ROOTS}
    contracts = pick_front_contracts(await px.available_contracts(), wanted)
    for m in config.MARKETS:
        if m.root not in wanted:
            continue
        reg = await regime.regime_for(http, px, m.root, contracts.get(m.root))
        src = f"  [{reg['source']}, as of {reg['asof']}]" if reg else ""
        print(f"{m.root:4} {regime.summary(reg)}{src}")


async def list_contracts(px: ProjectXClient):
    cs = await px.available_contracts()
    for c in sorted(cs, key=lambda c: c.get("name", "")):
        root = contract_root(c.get("name", ""))
        mapped = "✓" if root in config.MARKETS_BY_ROOT else " "
        print(f"{mapped} {c.get('name',''):10} root={root or '?':5} id={c.get('id')} active={c.get('activeContract')} {c.get('description','')}")


async def run(args):
    if args.four_charts:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
            await post(http, "", [await four_charts.build(http)], True)
        return
    if args.cot_board:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
            data = await cot_board.fetch(http)
            emb = cot_board.board_embed(data)
            print(emb["title"] + "\n\n" + emb["description"])
            for f in emb["fields"]:
                print(f"\n{f['name']}\n{f['value']}")
        return
    async with ProjectXClient(config.PROJECTX_BASE_URL, config.PROJECTX_USERNAME,
                              config.PROJECTX_API_KEY, config.PROJECTX_LIVE_DATA) as px, \
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
        if args.list_contracts:
            await list_contracts(px)
            return
        if args.regime:
            await print_regimes(px, http)
            return
        if args.news:
            await news.poll(http, first_run=True)
            every_market = {x for _, mk in news.TOPICS.values() for x in mk}
            for h in news.recent(every_market, 24 * 60, high_only=False)[-25:]:
                print(f"{'HIGH' if h['high'] else '    '} {h['source']:22} {h['title'][:100]}  -> {', '.join(h['markets'][:5])}")
            return
        if args.acd_backtest:
            roots = [x.strip().upper() for x in args.acd_backtest.split(",") if x.strip()]
            a_atrs = [float(x) for x in args.a_atr.split(",")] if args.a_atr else None
            rows = await acd_backtest.run(px, roots, args.days, a_atrs)
            acd_backtest.print_table(rows)
            if rows:
                print("Saved", acd_backtest.save_csv(rows))
            return
        if args.once:
            await scan_once(px, http, args.dry_run, force_board="manual run")
            return
        log.info("Scanner running every %d min; guardrails checking accounts every minute", config.SCAN_INTERVAL_MIN)
        log.info("News agent every %d min (%s); desk channel %s; macro call from %s",
                 news.NEWS_INTERVAL_MIN, "posting" if news.DISCORD_NEWS_WEBHOOK else "checks only, no news webhook",
                 "on" if config.DISCORD_DESK_WEBHOOK else "OFF (set DISCORD_DESK_WEBHOOK)", macro.MACRO_CALL_URL or "nowhere")
        asyncio.create_task(news.news_loop(http, post, args.dry_run))
        asyncio.create_task(cot_board.board_loop(http, post, args.dry_run))
        asyncio.create_task(four_charts.loop(http, post, args.dry_run))
        log.info("Four Charts post weekdays %d:%02d Sydney to %s", four_charts.POST_HOUR, four_charts.POST_MINUTE,
                 "#general" if four_charts.WEBHOOK else "the desk channel (set DISCORD_GENERAL_WEBHOOK for #general)")
        log.info("ACD agent on %s; COT board Saturdays %d:00 Sydney",
                 ", ".join(sorted(r for r in acd.SESSIONS if acd.enabled(r))), cot_board.COT_BOARD_HOUR_SYD)
        log.info("Guardrails: %d ideas/day, %d trades/day, %d min cool-down, daily stop $%.0f, profit cap $%.0f",
                 guardrails.MAX_IDEAS_PER_DAY, guardrails.MAX_TRADES_PER_DAY, guardrails.COOLDOWN_MIN,
                 guardrails.PERSONAL_DAILY_STOP, guardrails.DAILY_PROFIT_CAP)
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
    p.add_argument("--regime", action="store_true", help="print the 200-day regime for every market and exit")
    p.add_argument("--news", action="store_true", help="print the latest market-moving headlines and exit")
    p.add_argument("--four-charts", action="store_true", help="print today's Four Charts bias post and exit")
    p.add_argument("--cot-board", action="store_true", help="print this week's commodity COT extremes board and exit")
    p.add_argument("--acd-backtest", metavar="ROOTS", help="ACD backtest, e.g. GC,SI,CL,NQ")
    p.add_argument("--days", type=int, default=180, help="days of history for --acd-backtest (default 180)")
    p.add_argument("--a-atr", help="A value(s) as a fraction of ATR, e.g. 0.1,0.15,0.2 (default ACD_A_ATR)")
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
