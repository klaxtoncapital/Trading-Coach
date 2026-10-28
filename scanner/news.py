"""News agent: headlines that move the markets you trade.

Sources (public, keyless; each can be switched off or replaced with env vars):
  * Forex Factory weekly calendar: a morning digest of today's high- and
    medium-impact events in Sydney time, with forecast and previous
  * Trump's Truth Social posts, via the independent trumpstruth.org archive feed
  * White House news
  * Federal Reserve: monetary policy press releases, and speeches/testimony
  * Bank of Japan: What's New
  * CNBC: top news, economy, world

Every headline is tagged with the markets it can move (keyword rules below).
Market-moving ones are posted to the news channel; the head of desk also
checks recent tagged headlines before it verifies an idea.

Only headline titles and links are posted, never article text.
"""
from __future__ import annotations

import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import aiohttp

from . import config
from . import earnings as earnings_mod

log = logging.getLogger("scanner.news")
SYD = ZoneInfo("Australia/Sydney")
NEWS_STATE_FILE = os.getenv("NEWS_STATE_FILE", "news_state.json")
DISCORD_NEWS_WEBHOOK = os.getenv("DISCORD_NEWS_WEBHOOK", "")
NEWS_INTERVAL_MIN = int(os.getenv("NEWS_INTERVAL_MIN", "") or 5)
# Hour (Sydney) for the morning calendar digest, weekdays.
CALENDAR_DIGEST_HOUR = int(os.getenv("CALENDAR_DIGEST_HOUR_SYD", "") or 8)
# Off by default since 29 Oct 2026: the #general morning message already lists the day's events.
CALENDAR_DIGEST_ON = os.getenv("CALENDAR_DIGEST", "off").lower() == "on"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; KlaxtonDesk/1.0)"}

# (key, name, url, always_post_if_tagged)
FEEDS = [
    ("trump", "Trump (Truth Social)", os.getenv("FEED_TRUMP", "https://www.trumpstruth.org/feed"), True),
    ("whitehouse", "White House", os.getenv("FEED_WHITEHOUSE", "https://www.whitehouse.gov/news/feed/"), True),
    ("fed_policy", "Fed policy", os.getenv("FEED_FED_POLICY", "https://www.federalreserve.gov/feeds/press_monetary.xml"), True),
    ("fed_speech", "Fed speeches", os.getenv("FEED_FED_SPEECH", "https://www.federalreserve.gov/feeds/speeches_and_testimony.xml"), True),
    ("boj", "Bank of Japan", os.getenv("FEED_BOJ", "https://www.boj.or.jp/en/rss/whatsnew.xml"), True),
    ("cnbc_top", "CNBC", os.getenv("FEED_CNBC_TOP", "https://www.cnbc.com/id/100003114/device/rss/rss.html"), False),
    ("cnbc_econ", "CNBC economy", os.getenv("FEED_CNBC_ECON", "https://www.cnbc.com/id/20910258/device/rss/rss.html"), False),
    ("cnbc_world", "CNBC world", os.getenv("FEED_CNBC_WORLD", "https://www.cnbc.com/id/100727362/device/rss/rss.html"), False),
]
DISABLED_FEEDS = {s.strip() for s in os.getenv("NEWS_FEEDS_OFF", "").split(",") if s.strip()}

_US_IDX = ("ES", "NQ", "YM", "RTY")
_RATES = ("ZT", "ZF", "ZN", "TN", "ZB", "UB")
# keyword group -> (patterns, markets)
TOPICS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "Fed": ((r"\bfed\b", r"federal reserve", r"\bpowell\b", r"\bfomc\b", r"rate cuts?", r"rate hikes?",
             r"interest rates?", r"\bwaller\b", r"\bjefferson\b", r"\bbowman\b"),
            _US_IDX + _RATES + ("6E", "6J", "GC", "NKD")),
    "BOJ / yen": ((r"\bboj\b", r"bank of japan", r"\bueda\b", r"\byen\b", r"intervention", r"\bjapan", r"\bnikkei\b",
                   r"\btakaichi\b", r"\bjgbs?\b"),
                  ("NKD", "6J")),
    "Trade / tariffs": ((r"tariffs?", r"trade (deal|war|talks)", r"sanctions?", r"export controls?", r"\bchina\b",
                         r"\bxi\b", r"\bbeijing\b"),
                        _US_IDX + ("NKD", "6J", "HG", "ZS", "6A")),
    "Inflation / jobs": ((r"\bcpi\b", r"inflation", r"payrolls", r"jobs report", r"unemployment", r"\bpce\b",
                          r"\bgdp\b", r"recession"),
                         _US_IDX + _RATES + ("6E", "6J", "GC")),
    "Oil / Middle East": ((r"\boil\b", r"\bopec", r"crude", r"\biran", r"\bsaudi", r"\bisrael", r"strait of hormuz"),
                          ("CL", "RB", "HO", "GC")),
    "Tech / AI": ((r"\bnvidia\b", r"semiconductor", r"\bchips?\b", r"\bapple\b", r"\bmicrosoft\b", r"\bopenai\b",
                   r"\bai\b"),
                  ("NQ", "ES")),
    "Crypto": ((r"bitcoin", r"\bcrypto", r"\bether(eum)?\b"), ("MBT", "MET")),
    "Gold": ((r"\bgold\b",), ("GC", "SI")),
    "ECB / euro": ((r"\becb\b", r"lagarde", r"\beuro\b", r"eurozone"), ("6E",)),
    "BOE / pound": ((r"bank of england", r"\bbailey\b", r"\bsterling\b", r"\bpound\b"), ("6B",)),
    "RBA / Aussie": ((r"\brba\b", r"reserve bank of australia", r"\baussie dollar\b"), ("6A",)),
    "Risk events": ((r"\bwar\b", r"missiles?", r"invasion", r"nuclear", r"ceasefire", r"shutdown", r"\bdefault\b",
                     r"impeach", r"emergency"),
                    _US_IDX + ("NKD", "CL", "GC", "6J")),
    "Markets": ((r"stock market", r"\bstocks\b", r"\bdow\b", r"s&p", r"nasdaq", r"treasur(y|ies)", r"bond yields?",
                 r"\bdollar\b"),
                _US_IDX + _RATES),
}
_COMPILED = {k: ([re.compile(p, re.I) for p in pats], mk) for k, (pats, mk) in TOPICS.items()}
HIGH = re.compile(r"tariff|rate cut|rate hike|\bfomc\b|intervention|emergency|ceasefire|invasion|sanction|shutdown|"
                  r"\bdefault\b|fire[sd]? powell|policy rate|monetary policy|statement", re.I)

_recent: list[dict] = []          # tagged headlines seen this run, newest last


def tag(text: str) -> tuple[list[str], list[str]]:
    """(topics, markets) a headline touches."""
    topics, markets = [], []
    for name, (pats, mk) in _COMPILED.items():
        if any(p.search(text) for p in pats):
            topics.append(name)
            for m in mk:
                if m not in markets:
                    markets.append(m)
    return topics, markets


def _clean(s: str | None) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"&(amp|#38);", "&", s)
    s = re.sub(r"&(quot|#34);", '"', s)
    s = re.sub(r"&(#39|apos|#8217|rsquo);", "'", s)
    s = re.sub(r"&[a-z#0-9]+;", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _when(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        t = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            t = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def parse_feed(xml_text: str) -> list[dict]:
    """RSS 2.0 or Atom -> [{id, title, link, t}] newest first."""
    try:
        root = ET.fromstring(xml_text.strip().lstrip("﻿"))
    except ET.ParseError:
        return []
    items = []
    for it in root.iter():
        tagname = it.tag.split("}")[-1]
        if tagname not in ("item", "entry"):
            continue
        def get(*names):
            for name in names:
                el = next((c for c in it if c.tag.split("}")[-1] == name), None)
                if el is not None:
                    return el
            return None
        title_el, link_el = get("title"), get("link")
        title = _clean(title_el.text if title_el is not None else "")
        if not title:
            desc = get("description")
            title = _clean(desc.text if desc is not None else "")[:200]
        link = ""
        if link_el is not None:
            link = (link_el.text or link_el.get("href") or "").strip()
        guid_el = get("guid", "id")
        date_el = get("pubDate", "published", "updated", "date")
        items.append({
            "id": (guid_el.text.strip() if guid_el is not None and guid_el.text else link or title),
            "title": title[:240], "link": link, "t": _when(date_el.text if date_el is not None else None),
        })
    items.sort(key=lambda x: x["t"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return items


def _load_state() -> dict:
    try:
        with open(NEWS_STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"seen": [], "digest": ""}


def _save_state(s: dict) -> None:
    s["seen"] = s["seen"][-4000:]
    try:
        with open(NEWS_STATE_FILE, "w") as f:
            json.dump(s, f)
    except OSError as e:
        log.warning("Couldn't save news state: %s", e)


async def _get(http: aiohttp.ClientSession, url: str) -> str | None:
    try:
        async with http.get(url, headers=HEADERS) as r:
            if r.status != 200:
                log.info("News feed %s returned HTTP %s", url, r.status)
                return None
            return await r.text()
    except Exception as e:  # noqa: BLE001
        log.info("News feed %s failed: %s", url, e)
        return None


def recent(markets: set[str] | list[str], minutes: int, now: datetime | None = None, high_only: bool = True) -> list[dict]:
    """Tagged headlines in the last `minutes` that touch any of `markets`."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=minutes)
    ms = set(markets)
    return [h for h in _recent if h["t"] and h["t"] >= cutoff and ms & set(h["markets"]) and (h["high"] or not high_only)]


def remember(item: dict) -> None:
    _recent.append(item)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    _recent[:] = [h for h in _recent if (h["t"] or cutoff) >= cutoff][-500:]


async def poll(http: aiohttp.ClientSession, first_run: bool = False) -> list[dict]:
    """Fetch every feed, return new market-moving headlines (and remember all tagged ones)."""
    state = _load_state()
    seen = set(state["seen"])
    fresh = []
    now = datetime.now(timezone.utc)
    for key, name, url, always in FEEDS:
        if key in DISABLED_FEEDS or not url:
            continue
        text = await _get(http, url)
        if not text:
            continue
        for it in parse_feed(text)[:30]:
            uid = f"{key}:{it['id']}"
            if uid in seen:
                continue
            seen.add(uid)
            state["seen"].append(uid)
            topics, markets = tag(it["title"])
            if not markets:
                continue
            high = bool(HIGH.search(it["title"])) or key in ("trump", "fed_policy", "boj")
            item = {**it, "source": name, "key": key, "topics": topics, "markets": markets, "high": high}
            # Old items in a feed (first run, or a feed that was down) are remembered, not posted
            stale = it["t"] is not None and now - it["t"] > timedelta(hours=3)
            remember(item)
            if first_run or stale:
                continue
            if always or high:
                fresh.append(item)
    _save_state(state)
    return fresh


def news_embeds(items: list[dict]) -> list[dict]:
    if not items:
        return []
    lines = []
    for h in items[:15]:
        when = h["t"].astimezone(SYD).strftime("%-I:%M%p").lower() if h["t"] else ""
        mk = ", ".join(h["markets"][:6])
        flag = "🔴 " if h["high"] else ""
        title = h["title"].replace("[", "(").replace("]", ")")
        link = f"[{title}]({h['link']})" if h["link"].startswith("http") else title
        lines.append(f"{flag}**{h['source']}** {when}: {link}\n   ↳ {', '.join(h['topics'][:3])} · moves {mk}")
    return [{
        "title": "📰 Headline alert",
        "description": "\n".join(lines)[:4000],
        "color": 0x23456B,
        "footer": {"text": "🔴 = high priority (policy, tariffs, intervention, Trump). Titles and links only: "
                           "the explained version comes in the morning macro wrap."},
        "timestamp": datetime.now(SYD).isoformat(),
    }]


# ─────────────────────────────────────────────
# MORNING CALENDAR DIGEST (Forex Factory)
# ─────────────────────────────────────────────
DIGEST_CURRENCIES = {"USD", "JPY", "EUR", "GBP", "AUD", "CNY", "CAD", "CHF", "NZD"}


async def calendar_items(http: aiohttp.ClientSession) -> list[dict]:
    text = await _get(http, config.FF_CALENDAR)
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out = []
    for ev in data or []:
        impact = str(ev.get("impact", "")).title()
        if impact not in ("High", "Medium") or ev.get("country") not in DIGEST_CURRENCIES:
            continue
        try:
            t = datetime.fromisoformat(ev["date"]).astimezone(timezone.utc)
        except (KeyError, ValueError):
            continue
        out.append({"t": t, "title": ev.get("title", ""), "currency": ev.get("country", ""), "impact": impact,
                    "forecast": ev.get("forecast") or "", "previous": ev.get("previous") or ""})
    return sorted(out, key=lambda e: e["t"])


def calendar_embed(events: list[dict], now: datetime, earnings: list[dict] | None = None) -> dict:
    window = [e for e in events if now <= e["t"] <= now + timedelta(hours=24)]
    lines = []
    for e in window[:25]:
        icon = "🔴" if e["impact"] == "High" else "🟠"
        fp = []
        if e["forecast"]:
            fp.append(f"f/c {e['forecast']}")
        if e["previous"]:
            fp.append(f"prev {e['previous']}")
        lines.append(f"{icon} **{e['t'].astimezone(SYD):%a %-I:%M%p}** {e['currency']} {e['title']}"
                     + (f" ({', '.join(fp)})" if fp else ""))
    lines = lines or ["No high or medium impact events in the next 24 hours."]
    lines += earnings_lines(earnings or [], now)
    return {
        "title": f"🗓️ Next 24 hours · {now.astimezone(SYD):%a %-d %b}",
        "description": "\n".join(lines)[:4000],
        "color": 0x23456B,
        "footer": {"text": "Forex Factory calendar + Nasdaq earnings, Sydney time. 🔴 high · 🟠 medium · 💼 earnings."},
        "timestamp": now.astimezone(SYD).isoformat(),
    }


def earnings_lines(evs: list[dict], now: datetime) -> list[str]:
    """Earnings in the next 24h, then anything else on the watchlist this week."""
    soon = [e for e in evs if now <= e["t"] <= now + timedelta(hours=24)]
    later = [e for e in evs if now + timedelta(hours=24) < e["t"] <= now + timedelta(days=7)]
    if not soon and not later:
        return []
    out = ["", "**💼 Earnings**"]
    for e in soon:
        out.append(f"💼 **{e['t'].astimezone(SYD):%a %-I:%M%p}** {e['company']} ({e['symbol']}) {e['when']}"
                   f" · moves {'/'.join(e['roots'])}")
    if later:
        out.append("Later this week: " + ", ".join(
            f"{e['symbol']} {e['t'].astimezone(SYD):%a}" for e in later[:12]))
    return out


def digest_due(now: datetime, state: dict) -> bool:
    s = now.astimezone(SYD)
    return s.weekday() < 5 and s.hour == CALENDAR_DIGEST_HOUR and state.get("digest") != s.date().isoformat()


async def news_loop(http: aiohttp.ClientSession, post, dry_run: bool):
    """Runs alongside the scanner, every NEWS_INTERVAL_MIN minutes, all week (weekend posts move Monday)."""
    import asyncio
    first = True
    while True:
        try:
            items = await poll(http, first_run=first)
            first = False
            if items and DISCORD_NEWS_WEBHOOK:
                await post(http, DISCORD_NEWS_WEBHOOK, news_embeds(items), dry_run)
            now = datetime.now(timezone.utc)
            state = _load_state()
            if CALENDAR_DIGEST_ON and digest_due(now, state):
                try:
                    evs = await earnings_mod.upcoming(http, now)
                except Exception:  # noqa: BLE001  earnings must never stop the digest
                    log.exception("Earnings fetch failed")
                    evs = []
                emb = calendar_embed(await calendar_items(http), now, evs)
                if DISCORD_NEWS_WEBHOOK:
                    await post(http, DISCORD_NEWS_WEBHOOK, [emb], dry_run)
                state["digest"] = now.astimezone(SYD).date().isoformat()
                _save_state(state)
        except Exception:  # noqa: BLE001
            log.exception("News check failed; retrying")
        await asyncio.sleep(NEWS_INTERVAL_MIN * 60)


def headline_check_window() -> int:
    return int(os.getenv("NEWS_BLOCK_MIN", "") or 30)

