"""Reads the macro agent's daily direction call.

The macro agent is a scheduled Claude run (8:30am Sydney, weekdays) that
writes desk/macro_call.json to the repo's `desk-data` branch:

  {
    "generated_at": "2026-10-20T08:30:00+11:00",
    "markets": {
      "NQ": {"bias": "bearish", "conviction": 2, "why": "..."},
      "NKD": {"bias": "neutral", "conviction": 1, "why": "..."}
    }
  }

bias is bullish / bearish / neutral; conviction 1-3. The scanner reads it
every 20 minutes. A call older than MACRO_MAX_AGE_HOURS is ignored.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import aiohttp

log = logging.getLogger("scanner.macro")

MACRO_CALL_URL = os.getenv(
    "MACRO_CALL_URL",
    "https://raw.githubusercontent.com/klaxtoncapital/trading-coach/desk-data/desk/macro_call.json",
)
MACRO_MAX_AGE_HOURS = int(os.getenv("MACRO_MAX_AGE_HOURS", "") or 26)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")   # only needed if the repo is private

_cache: tuple[float, dict | None] = (0.0, None)


def _fresh(call: dict | None, now: datetime) -> dict | None:
    if not call or not isinstance(call.get("markets"), dict):
        return None
    try:
        t = datetime.fromisoformat(str(call["generated_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    if now - t > timedelta(hours=MACRO_MAX_AGE_HOURS):
        return None
    return call


async def latest(http: aiohttp.ClientSession, now: datetime | None = None) -> dict | None:
    global _cache
    now = now or datetime.now(timezone.utc)
    if time.time() - _cache[0] < 20 * 60:
        return _fresh(_cache[1], now)
    call = None
    if MACRO_CALL_URL:
        headers = {"Authorization": f"Bearer {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
        try:
            async with http.get(MACRO_CALL_URL, headers=headers) as r:
                if r.status == 200:
                    call = json.loads(await r.text())
                else:
                    log.info("Macro call not available (HTTP %s)", r.status)
        except Exception as e:  # noqa: BLE001
            log.info("Macro call fetch failed: %s", e)
    _cache = (time.time(), call)
    return _fresh(call, now)


def for_market(call: dict | None, root: str) -> dict | None:
    if not call:
        return None
    m = call["markets"].get(root)
    if not isinstance(m, dict) or m.get("bias") not in ("bullish", "bearish", "neutral"):
        return None
    return m
