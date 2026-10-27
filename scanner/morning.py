"""The one morning message for #general.

Replaces the separate Desk Brief, macro call, Four Charts and Daily SFP posts with one Discord
message (max 10 embeds, 6,000 characters): a header with the day, macro picture and events,
then one card per market in play. NKD always gets a card; other markets get one when they have
a daily SFP, a weekly framework or an untouched swing level within reach.

Each card pulls together what the desk knows about that market: the setup and its levels, the
macro call, the Four Charts lean, the Monday-range rule (NQ, ES, YM, NKD), hedge-fund
positioning (COT), the expected daily move and the trading hours."""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp

from . import config, daily_setups as ds, four_charts as fc, gates, macro, sessions
from .metrics import Day, week_key

log = logging.getLogger("scanner.morning")

SYD = ZoneInfo("Australia/Sydney")
NY = ZoneInfo("America/New_York")
WEBHOOK = os.getenv("DISCORD_GENERAL_WEBHOOK", "")
STATE_FILE = os.getenv("MORNING_STATE_FILE", "morning_state.json")
WAIT_FOR_MACRO_UNTIL = (10, 10)     # Sydney: post without the macro call after this
BRIEF_URL = "https://claude.ai/artifact/UqfJKoKWqabXyY2EqUBmsJ"
ALWAYS = ("NKD",)
MAX_CARDS = 9                       # Discord allows 10 embeds per message: header + 9 cards
CHAR_BUDGET = 5800                  # Discord's limit is 6,000 characters per message
MONDAY_RULE = ("NQ", "ES", "YM", "NKD")
GREEN, RED, GREY, AMBER = 0x1F6B52, 0xA33A2A, 0x5D6661, 0xB7791F


# ─────────────────────────────────────────────
# MONDAY-RANGE RULE (same rule as the weekly-call task, on completed days)
# ─────────────────────────────────────────────
def monday_range(days: list[Day]) -> dict | None:
    """Status of the Monday-range rule for the week of the last completed day."""
    if not days:
        return None
    last = days[-1]
    wk = [d for d in days if week_key(d.d) == week_key(last.d)]
    by = {d.d.weekday(): d for d in wk}
    mon = by.get(0)
    if last.d.weekday() == 4 and len(wk) >= 4:
        nxt = "Monday's range forms today"
    else:
        nxt = None
    if not mon:
        return {"short": "Waiting", "text": "Monday's range forms today."}
    mh, ml = mon.h, mon.l
    out = {"mon_high": mh, "mon_low": ml, "short": "Waiting", "text": ""}
    if last.d == mon.d:
        out["text"] = "Monday's range is set. Tuesday often raids one side."
        return out
    tue, wed = by.get(1), by.get(2)
    if tue and last.d == tue.d:
        if tue.c > mh:
            out.update(short="Wed watch long", text=(
                f"Tuesday closed above Monday's high: Wednesday continuation watch, long. Wednesday took Tuesday's "
                f"high ({tue.h:,.2f}) 78% of the time but closed higher only 43%: take profits into the extension."))
        elif tue.c < ml:
            out.update(short="Wed watch short", text=(
                f"Tuesday closed below Monday's low: Wednesday continuation watch, short. Wednesday took Tuesday's "
                f"low ({tue.l:,.2f}) 78% of the time but closed lower only 43%: take profits into the extension."))
    bars = [b for d in wk if mon.d < d.d <= (wed.d if wed else last.d) for b in d.bars]
    up = next((i for i, b in enumerate(bars) if b["h"] > mh), None)
    dn = next((i for i, b in enumerate(bars) if b["l"] < ml), None)
    if up is None and dn is None:
        if wed and last.d >= wed.d:
            out.update(short="No call", text="No break of Monday's range by Wednesday: no call this week.")
        elif not out["text"]:
            out["text"] = "Still inside Monday's range."
        return out
    first = "high" if (up is not None and (dn is None or up < dn)) else "low"
    i0 = up if first == "high" else dn
    after = bars[i0:i0 + 4]
    lvl = mh if first == "high" else ml
    if len(after) < 4:
        if out["short"].startswith("Wed watch"):
            return out          # Tuesday closed beyond the range: that's the read, even if the break was late
        out.update(short="Testing", text=f"Testing Monday's {first} ({lvl:,.2f}); needs 4 hourly closes to confirm.")
        return out
    held = after[3]["c"] > mh if first == "high" else after[3]["c"] < ml
    if not held:
        out.update(short="No call", text=f"Swept Monday's {first} and closed back inside: no call (sweeps tested no edge).")
        return out
    lean = "long" if first == "high" else "short"
    if not out["text"]:
        out.update(short=f"Lean {lean}", text=f"Broke and held Monday's {first} ({lvl:,.2f}): lean {lean}.")
    if wed and last.d >= wed.d:
        call = 1 if lean == "long" else -1
        pulled = (wed.c - wed.o) * call < 0
        out.update(short=f"Setup {lean}" if pulled else f"Lean {lean}, no pullback",
                   text=(f"Broke and held Monday's {first}, then Wednesday pulled back: SETUP. Look for your entry "
                         f"signature Thu/Fri, {lean}." if pulled else
                         f"Broke and held Monday's {first}, but no Wednesday pullback: no measured edge, stand aside."))
    if nxt:
        out["text"] += f" {nxt} for the new week."
    return out


# ─────────────────────────────────────────────
# DAY NOTE (incl. Friday's weekly profile)
# ─────────────────────────────────────────────
def trading_weekday(now: datetime) -> int:
    ny = now.astimezone(NY)
    return (ny + timedelta(hours=6)).weekday()


def friday_profile(root: str, days: list[Day], std: float | None) -> str:
    wk = [d for d in days if week_key(d.d) == week_key(days[-1].d)] if days else []
    by = {d.d.weekday(): d for d in wk}
    prev = [d for d in days if d.d < (by[0].d if 0 in by else date.min)]
    if not std or not prev or not all(k in by for k in range(4)):
        return f"{root}: no clear weekly profile. Friday is A+ setups only."
    closes = [prev[-1].c] + [by[k].c for k in range(4)]
    mv = [closes[i + 1] - closes[i] for i in range(4)]          # Mon..Thu
    tue_thu = mv[1:]
    if all(x > 0 for x in tue_thu) or all(x < 0 for x in tue_thu):
        if abs(sum(tue_thu)) >= 1.5 * std:
            return (f"{root}: classic expansion week ({'up' if sum(tue_thu) > 0 else 'down'} Tue-Thu). "
                    "Friday is the fade: sit out or take profits, no new positions.")
    thu, m_w = mv[3], sum(mv[:3])
    if abs(thu) >= std and (m_w * thu < 0 or abs(m_w) < std):
        side = "above the 6pm open for shorts" if thu < 0 else "below the 6pm open for longs"
        return (f"{root}: reversal week, Thursday expanded {'down' if thu < 0 else 'up'} ({abs(thu):,.0f} pts). "
                f"Friday is a continuation day: trade with Thursday's direction, OTE {side}.")
    return f"{root}: no clear weekly profile. Friday is A+ setups only."


def day_note(now: datetime, days_by_root: dict, em: dict) -> tuple[str, str]:
    wd = trading_weekday(now)
    if wd == 0:
        return "Monday", "Let it pass. Mark Monday's high and low; Tuesday often raids one."
    if wd == 1:
        return "Tuesday", "Watch for a sweep of Monday's high or low and a close back inside."
    if wd in (2, 3):
        return ("Wednesday" if wd == 2 else "Thursday"), "Main expansion day. Look for continuation from the 6pm open."
    notes = [friday_profile(r, days_by_root.get(r, []), (em.get(r) or {}).get("pts")) for r in ("NKD", "NQ")]
    return "Friday", "\n".join(notes)


# ─────────────────────────────────────────────
# CARDS
# ─────────────────────────────────────────────
def _dots(n) -> str:
    n = int(n) if str(n).isdigit() else 0
    return "●" * n + "○" * (3 - n)


def _cot_label(p: int) -> str:
    return ("Crowded long" if p >= 90 else "Leaning long" if p >= 75 else "Neutral" if p > 25
            else "Heavily short" if p > 10 else "Crowded short")


def pick_in_play(found: list[tuple]) -> tuple[list[str], dict[str, list[dict]], list[tuple]]:
    """Roots in card order (NKD first, then SFPs best first, frameworks, swing levels) and each root's items."""
    items: dict[str, list[dict]] = {}
    for st, m in found:
        if st["kind"] == "move":
            continue
        items.setdefault(m.root, []).append(st)
    cl = lambda st: st.get("conf", {}).get("lines", [])
    def tier(st):
        if st["kind"] == "sfp":
            bad = any(l.startswith("❌") for l in cl(st))
            ok = any(l.startswith("✅ Macro call") for l in cl(st))
            return (0 if ok and not bad else 1 if not bad else 2, -st["score"])
        if st["kind"] == "swing":
            return (4, st["dist_atr"])
        return (3, -st.get("score", 0))
    best = sorted(((min(tier(s) for s in v), r) for r, v in items.items()))
    order = list(ALWAYS) + [r for _, r in best if r not in ALWAYS]
    return order, items, [x for x in found if x[0]["kind"] == "move"]


def _setup_lines(sts: list[dict], m) -> tuple[list[str], int, list[str]]:
    lines, direction, warn = [], 0, []
    cl = lambda st: st.get("conf", {}).get("lines", [])
    for st in sorted(sts, key=lambda s: {"sfp": 0, "swing": 2}.get(s["kind"], 1)):
        a = st["atr"]
        if st["kind"] == "sfp":
            direction = direction or st["dir"]
            fw = f" · {st['framework'].split(':')[0]}" if st.get("framework") else ""
            lines.append(f"**Daily SFP {ds._arrow(st['dir'])}** · score {st['score']}{fw}\n"
                         f"Swept {ds._fmt(st['level']['price'], a)} ({st['level']['name']}) · OTE {ds._fmt(st['ote'], a)}"
                         f" · stop {ds._fmt(st['stop'], a)} · T1 {ds._fmt(st['target'][0], a)} ({st['ote_rr']}R)")
            if any(l.startswith("❌ Macro call") for l in cl(st)):
                warn.append("⚠️ Macro call is against this SFP: could be a squeeze")
            if any(l.startswith("❌") and not l.startswith("❌ Macro call") for l in cl(st)):
                warn.append("⚠️ Positioning crowded against it")
        elif st["kind"] == "swing":
            side = "SFP short" if st["side"] == "high" else "SFP long"
            lines.append(f"**Level in play** {'⬆️' if st['side'] == 'high' else '⬇️'} {ds._fmt(st['level']['price'], a)} "
                         f"{st['level']['name']}, {st['dist_atr']} ATR away. {side} if it's taken and closes back.")
        else:
            direction = direction or st.get("dir", 0)
            lines.append(f"**{st['name']}** {ds._arrow(st.get('dir', 0))}")
    return lines, direction, warn


async def card(http, m, sts: list[dict], call, leans: dict, now: datetime) -> dict:
    root = m.root
    lines, direction, warn = _setup_lines(sts, m)
    if not lines:
        lines.append("No daily setup today.")
    mc = macro.for_market(call, root)
    if mc:
        icon = {"bullish": "🟢 Bullish", "bearish": "🔴 Bearish", "neutral": "⚪ Neutral"}[mc["bias"]]
        lines.append(f"🧭 **Macro** {icon} {_dots(mc.get('conviction'))} · {mc.get('why', '')}"[:260])
    fl = leans.get(root)
    if fl and fl["conv"] not in ("none",):
        word = "Bullish" if fl["dir"] == 1 else "Bearish" if fl["dir"] == -1 else "Mixed"
        lines.append(f"📊 **Four Charts** {word}" + ("" if fl["conv"] == "mixed" else f" · {fl['conv']}"))
    if root in MONDAY_RULE:
        mr = monday_range(ds.LAST.get("days", {}).get(root, []))
        if mr:
            lines.append(f"📐 **Monday range** {mr['short']}: {mr['text']}"[:300])
    pos = await gates.cot(http, m)
    if pos:
        lines.append(f"🏦 **Funds** net {pos['net']:+,} · {gates.ordinal(pos['pct3y'])} pct (3y) · {_cot_label(pos['pct3y'])}")
    em = ds.LAST.get("em", {}).get(root)
    a = next((s["atr"] for s in sts), None) or (em or {}).get("pts") or 1
    if em:
        lines.append(f"📏 **Expected move** ±{ds._fmt(em['pts'], a)} a day ({em['pct']:.1f}%, {em['src']} vol)")
    st = sessions.status(root, now)
    if sessions.short_hours(root):
        syd = sessions.schedule_lines(root, now)[0].replace("**Sydney:** ", "")
        state = (f"open, closes {sessions._t(st['until'], SYD, True)}" if st["open"]
                 else f"closed, opens {sessions._t(st['opens'], SYD, True)}" if st.get("opens") else "closed")
        lines.append(f"⏰ **Short hours** · {state} Sydney · trades {syd}")
    lines += warn
    direction = direction or {"bullish": 1, "bearish": -1}.get((mc or {}).get("bias"), 0)
    colour = GREEN if direction == 1 else RED if direction == -1 else GREY
    if warn:
        colour = AMBER
    tag = " · 🟢 long setup" if direction == 1 and sts else " · 🔴 short setup" if direction == -1 and sts else ""
    return {"title": f"{root} · {m.name}{tag}", "color": colour, "description": "\n".join(lines)[:1500]}


def _overflow_line(m, sts: list[dict]) -> str:
    st = sts[0]
    if st["kind"] == "sfp":
        return f"**{m.root}** daily SFP {ds._arrow(st['dir'])} · score {st['score']}{sessions.board_tag(m.root)}"
    if st["kind"] == "swing":
        return (f"**{m.root}** {'⬆️' if st['side'] == 'high' else '⬇️'} {st['level']['name']} "
                f"{ds._fmt(st['level']['price'], st['atr'])}, {st['dist_atr']} ATR away{sessions.board_tag(m.root)}")
    return f"**{m.root}** {st.get('name', 'framework')}{sessions.board_tag(m.root)}"


def header(now: datetime, call, fc_read, leans, conv, wd_name, note, in_play, overflow, moves, scanned) -> dict:
    lines = [f"**{wd_name}:** {note}"]
    if call:
        lines.append(f"\n🧭 **Macro:** {call.get('summary', '')}"[:600])
        ev = call.get("events_next_24h") or []
        lines.append("⏱️ **Next 24h:** " + (" · ".join(ev[:6]) if ev else "nothing tier 1. Clear to hold through."))
        t1 = call.get("tier1_next_14d") or []
        if t1:
            lines.append("🔴 **Tier 1 ahead:** " + " · ".join(t1[:5]))
    else:
        lines.append("\n🧭 **Macro:** the macro call isn't in yet today (it needs your PC on). Cards show no macro line.")
    if fc_read is not None:
        lines.append(f"📊 **Four Charts** ({conv}): {fc.headline(leans, conv)}")
    if moves:
        mv = " · ".join(f"{m.root} ±{ds._fmt(st['em']['pts'], st['atr'])}" for st, m in moves)
        lines.append(f"📏 **Commodity 1 STD moves:** {mv}"[:500])
    lines.append(f"\n**In play ({len(in_play)} of {scanned} markets):** " + (", ".join(in_play) or "none"))
    if overflow:
        lines.append("**Also in play** (no room for a card):\n" + "\n".join(overflow))
    lines.append(f"[Open the full Desk Brief →]({BRIEF_URL})")
    return {"title": f"☀️ Klaxton Desk · {now.astimezone(SYD):%a %-d %b}", "url": BRIEF_URL, "color": 0x23456B,
            "description": "\n".join(lines)[:4000]}


def _size(e: dict) -> int:
    return len(e.get("title", "")) + len(e.get("description", "")) + len((e.get("footer") or {}).get("text", ""))


async def build(px, http, now: datetime | None = None) -> list[dict]:
    """[header, card, card, ...] as one message's embeds."""
    now = now or datetime.now(timezone.utc)
    found, scanned = [], 0
    if px is not None:
        try:
            _, found, scanned = await ds.scan(px, http, now)
        except Exception:  # noqa: BLE001
            log.exception("Morning post: daily scan failed; posting without setups")
    call = ds.LAST.get("macro") if ds.LAST else await macro.latest(http, now)
    try:
        fc_read = fc.read(await fc.fetch_all(http, now.astimezone(SYD).date()))
        leans = {m: fc.lean(v) for m, v in fc.votes(fc_read).items()}
        conv = fc.overall(fc_read, leans)
    except Exception:  # noqa: BLE001
        log.exception("Morning post: Four Charts failed")
        fc_read, leans, conv = None, {}, "LOW"
    order, items, moves = pick_in_play(found)
    wd_name, note = day_note(now, ds.LAST.get("days", {}), ds.LAST.get("em", {}))
    cards, overflow = [], []
    for r in order:
        m = config.MARKETS_BY_ROOT.get(r)
        if m is None:
            continue
        if len(cards) < MAX_CARDS:
            cards.append(await card(http, m, items.get(r, []), call, leans, now))
        else:
            overflow.append(_overflow_line(m, items[r]))
    in_play = [r for r in order if r in items]
    head = header(now, call, fc_read, leans, conv, wd_name, note, in_play, overflow, moves, scanned)
    cards[-1:] = [{**cards[-1], "footer": {"text": "Context, not signals. A setup is a reason to look: your hourly "
                                                    "model decides. OTE above the 6PM open for shorts, below for longs."}}] if cards else []
    out = [head] + cards
    # Stay inside Discord's 6,000-character message limit: drop the last cards into the overflow list
    while sum(_size(e) for e in out) > CHAR_BUDGET and len(out) > 2:
        gone = out.pop()
        r = gone["title"].split(" · ")[0]
        if r in items:
            overflow.insert(0, _overflow_line(config.MARKETS_BY_ROOT[r], items[r]))
            out[0] = header(now, call, fc_read, leans, conv, wd_name, note, in_play, overflow, moves, scanned)
    return out


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
        log.warning("Couldn't save morning state: %s", e)


def macro_ready(call: dict | None, now: datetime) -> bool:
    s = now.astimezone(SYD)
    if call and str(call.get("trading_day", "")) == s.date().isoformat():
        return True
    return (s.hour, s.minute) >= WAIT_FOR_MACRO_UNTIL


async def loop(px, http: aiohttp.ClientSession, post, dry_run: bool):
    import asyncio
    while True:
        try:
            now = datetime.now(timezone.utc)
            st = _load()
            call = await macro.latest(http, now) if fc.due(now, st.get("posted")) else None
            if call is None or str(call.get("trading_day", "")) != now.astimezone(SYD).date().isoformat():
                macro._cache = (0.0, None)      # don't let a 20-minute cache hide today's call
            if fc.due(now, st.get("posted")) and macro_ready(call, now):
                embeds = await build(px, http, now)
                await post(http, WEBHOOK or config.DISCORD_DESK_WEBHOOK, embeds[:10], dry_run)
                st["posted"] = now.astimezone(SYD).date().isoformat()
                _save(st)
                log.info("Morning post sent: %d cards", len(embeds) - 1)
        except Exception:  # noqa: BLE001
            log.exception("Morning post failed; retrying in 5 min")
        await asyncio.sleep(300)
