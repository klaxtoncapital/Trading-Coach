"""Minimal async client for the ProjectX Gateway API (TopstepX).

Endpoints used (all POST, JSON):
  /api/Auth/loginKey        {userName, apiKey}            -> {token}
  /api/Auth/validate        (Bearer token)                 -> {newToken}
  /api/Contract/available   {live}                         -> {contracts:[...]}
  /api/History/retrieveBars {contractId, live, startTime, endTime,
                             unit, unitNumber, limit, includePartialBar}
                                                           -> {bars:[{t,o,h,l,c,v}]}

Rate limits: retrieveBars 50 requests / 30 s, everything else 200 / 60 s.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import aiohttp

log = logging.getLogger("scanner.projectx")

UNIT_MINUTE, UNIT_HOUR, UNIT_DAY = 2, 3, 4

# Futures month codes: F G H J K M N Q U V X Z
_CONTRACT_NAME = re.compile(r"^([A-Z0-9]+?)([FGHJKMNQUVXZ])(\d{1,2})$")


class ProjectXError(RuntimeError):
    pass


class _RateLimiter:
    """Sliding-window limiter: at most `max_calls` per `period` seconds."""

    def __init__(self, max_calls: int, period: float):
        self.max_calls, self.period = max_calls, period
        self.calls: deque[float] = deque()
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                while self.calls and now - self.calls[0] > self.period:
                    self.calls.popleft()
                if len(self.calls) < self.max_calls:
                    self.calls.append(now)
                    return
                await asyncio.sleep(self.period - (now - self.calls[0]) + 0.05)


def contract_root(name: str) -> str | None:
    """'NQZ6' -> 'NQ', '6EZ6' -> '6E', 'MBTX6' -> 'MBT'."""
    m = _CONTRACT_NAME.match((name or "").upper())
    return m.group(1) if m else None


class ProjectXClient:
    def __init__(self, base_url: str, username: str, api_key: str, live: bool = False):
        if not username or not api_key:
            raise ProjectXError("PROJECTX_USERNAME and PROJECTX_API_KEY must be set in the environment.")
        self.base_url, self.username, self._api_key, self.live = base_url, username, api_key, live
        self._token: str | None = None
        self._token_at = 0.0
        self._session: aiohttp.ClientSession | None = None
        self._bars_limit = _RateLimiter(45, 30)    # stay under 50/30s
        self._other_limit = _RateLimiter(180, 60)  # stay under 200/60s

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    # ── auth ──
    async def _login(self) -> None:
        data = await self._post("/api/Auth/loginKey", {"userName": self.username, "apiKey": self._api_key}, auth=False)
        self._token, self._token_at = data["token"], time.monotonic()
        log.info("ProjectX login OK")

    async def _ensure_token(self) -> None:
        if not self._token:
            await self._login()
        elif time.monotonic() - self._token_at > 20 * 3600:  # tokens last 24h
            try:
                data = await self._post("/api/Auth/validate", {}, auth=True)
                self._token = data.get("newToken") or self._token
                self._token_at = time.monotonic()
            except ProjectXError:
                await self._login()

    async def _post(self, path: str, body: dict, auth: bool = True, bars: bool = False, _retry: bool = True) -> dict:
        assert self._session is not None, "use 'async with ProjectXClient(...)'"
        await (self._bars_limit if bars else self._other_limit).wait()
        headers = {"accept": "text/plain", "Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self._token}"
        async with self._session.post(self.base_url + path, json=body, headers=headers) as r:
            if r.status == 401 and auth and _retry:
                await self._login()
                return await self._post(path, body, auth, bars, _retry=False)
            if r.status == 429 and _retry:
                await asyncio.sleep(5)
                return await self._post(path, body, auth, bars, _retry=False)
            if r.status != 200:
                raise ProjectXError(f"{path} returned HTTP {r.status}: {(await r.text())[:200]}")
            data = await r.json(content_type=None)
        if not data.get("success", False) or data.get("errorCode", 0) != 0:
            raise ProjectXError(f"{path} failed: code {data.get('errorCode')} {data.get('errorMessage')}")
        return data

    # ── data ──
    async def available_contracts(self) -> list[dict]:
        await self._ensure_token()
        data = await self._post("/api/Contract/available", {"live": self.live})
        return data.get("contracts", [])

    async def bars(self, contract_id: str, unit: int, unit_number: int, days_back: int,
                   include_partial: bool = True) -> list[dict]:
        """Bars oldest-first as dicts {t: datetime(UTC), o, h, l, c, v}."""
        await self._ensure_token()
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days_back)
        body = {
            "contractId": contract_id,
            "live": self.live,
            "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "unit": unit,
            "unitNumber": unit_number,
            "limit": 20000,
            "includePartialBar": include_partial,
        }
        data = await self._post("/api/History/retrieveBars", body, bars=True)
        out = []
        for b in data.get("bars", []):
            t = datetime.fromisoformat(str(b["t"]).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            out.append({"t": t.astimezone(timezone.utc), "o": float(b["o"]), "h": float(b["h"]),
                        "l": float(b["l"]), "c": float(b["c"]), "v": float(b.get("v") or 0)})
        out.sort(key=lambda x: x["t"])
        return out


def pick_front_contracts(contracts: list[dict], wanted_roots: set[str]) -> dict[str, dict]:
    """One active contract per wanted root (prefers activeContract=True)."""
    chosen: dict[str, dict] = {}
    for c in contracts:
        root = contract_root(c.get("name", ""))
        if root not in wanted_roots:
            continue
        prev = chosen.get(root)
        if prev is None or (c.get("activeContract") and not prev.get("activeContract")):
            chosen[root] = c
    return chosen


# ── account monitoring (read-only) ──
async def _active_accounts(self) -> list[dict]:
    await self._ensure_token()
    data = await self._post("/api/Account/search", {"onlyActiveAccounts": True})
    return data.get("accounts", [])


async def _trades(self, account_id: int, since: datetime) -> list[dict]:
    """Fills on an account since `since`. profitAndLoss is None on opening (half-turn) fills."""
    await self._ensure_token()
    body = {"accountId": account_id, "startTimestamp": since.strftime("%Y-%m-%dT%H:%M:%SZ")}
    data = await self._post("/api/Trade/search", body)
    out = []
    for t in data.get("trades", []):
        if t.get("voided"):
            continue
        ts = datetime.fromisoformat(str(t["creationTimestamp"]).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append({**t, "t": ts.astimezone(timezone.utc)})
    out.sort(key=lambda x: x["t"])
    return out


ProjectXClient.active_accounts = _active_accounts
ProjectXClient.trades = _trades
