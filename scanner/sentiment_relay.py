"""#macro-sentiment relay.

Scheduled Claude tasks (event results, the morning macro wrap, the Saturday COT breakdown) can't reach
Discord from the cloud, and Dave's PC isn't always on. So they append their post to
desk/sentiment.json on the public desk-data branch, and this service, which runs 24/7 on Railway,
polls that file and posts anything new to #macro-sentiment.

File format: a JSON list, newest last, of {"id": str, "created_at": ISO time, "embeds": [Discord embeds]}.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import aiohttp

log = logging.getLogger("scanner.sentiment")

SENTIMENT_URL = os.getenv(
    "SENTIMENT_URL",
    "https://raw.githubusercontent.com/klaxtoncapital/trading-coach/desk-data/desk/sentiment.json",
)
WEBHOOK = os.getenv("DISCORD_SENTIMENT_WEBHOOK", "") or os.getenv("DISCORD_NEWS_WEBHOOK", "")
STATE_FILE = os.getenv("SENTIMENT_STATE_FILE", "sentiment_state.json")
POLL_MIN = 3
FRESH_ON_START = timedelta(minutes=45)   # after a redeploy, post only items newer than this
MAX_AGE = timedelta(hours=12)            # never post anything older than this
CHAR_LIMIT = 5800                        # Discord: 6,000 characters per message, 10 embeds


def _when(item: dict) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(item.get("created_at", "")).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _size(e: dict) -> int:
    n = len(e.get("title", "")) + len(e.get("description", "")) + len((e.get("footer") or {}).get("text", ""))
    return n + sum(len(f.get("name", "")) + len(f.get("value", "")) for f in e.get("fields", []))


def clean_embed(e: dict) -> dict:
    """Clamp an embed to Discord's limits so one long field can't make the whole post fail."""
    out = {k: e[k] for k in ("title", "description", "color", "url", "footer", "timestamp") if k in e}
    if "title" in out:
        out["title"] = str(out["title"])[:256]
    if "description" in out:
        out["description"] = str(out["description"])[:4000]
    fields = []
    for f in (e.get("fields") or [])[:25]:
        if f.get("name") and f.get("value"):
            fields.append({"name": str(f["name"])[:256], "value": str(f["value"])[:1024], "inline": bool(f.get("inline"))})
    if fields:
        out["fields"] = fields
    return out


def messages(item: dict) -> list[list[dict]]:
    """An item's embeds split into as many Discord messages as needed."""
    out, cur, size = [], [], 0
    for e in (clean_embed(x) for x in item.get("embeds", []) if isinstance(x, dict)):
        s = _size(e)
        if cur and (len(cur) == 10 or size + s > CHAR_LIMIT):
            out.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += s
    if cur:
        out.append(cur)
    return out


def to_post(items: list, seen: set, now: datetime, started: datetime) -> list[dict]:
    """New, valid, recent items, oldest first. Items already old when the service started are skipped,
    so a redeploy doesn't repost the backlog."""
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or not it.get("id") or it["id"] in seen:
            continue
        t = _when(it)
        if t is None or now - t > MAX_AGE or t < started - FRESH_ON_START:
            continue
        out.append(it)
    return sorted(out, key=_when)


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
        log.warning("Couldn't save sentiment state: %s", e)


async def fetch(http: aiohttp.ClientSession) -> list:
    try:
        async with http.get(SENTIMENT_URL, headers={"Cache-Control": "no-cache"}) as r:
            if r.status != 200:
                return []
            return json.loads(await r.text())
    except Exception as e:  # noqa: BLE001
        log.info("Sentiment feed fetch failed: %s", e)
        return []


async def loop(http: aiohttp.ClientSession, post, dry_run: bool):
    import asyncio
    started = datetime.now(timezone.utc)
    while True:
        try:
            st = _load()
            seen = set(st.get("seen", []))
            now = datetime.now(timezone.utc)
            for it in to_post(await fetch(http), seen, now, started):
                ok = True
                for msg in messages(it):
                    ok = await post(http, WEBHOOK, msg, dry_run) and ok
                seen.add(it["id"])
                log.info("Macro sentiment posted: %s%s", it["id"], "" if ok else " (Discord refused part of it)")
            st["seen"] = sorted(seen)[-300:]
            _save(st)
        except Exception:  # noqa: BLE001
            log.exception("Sentiment relay failed; retrying")
        await asyncio.sleep(POLL_MIN * 60)
