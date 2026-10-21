"""ACD backtest on ProjectX history: is there more edge in commodities than in NQ/ES?

  python -m scanner.main --acd-backtest GC,SI,CL,HG,NQ,ES --days 180

For each market it rebuilds a continuous 1-minute history by stitching the
monthly contracts (the most-traded contract each session wins, the same as a
volume roll), runs the live ACD state machine on every session, and simulates:

  * A / C: enter on the confirming close, stop at the other side of the opening
    range, target 2R, otherwise out at the session flat (15:05 CT for Topstep)
  * failed A: enter on the close back inside the OR, stop just beyond the
    extreme the A attempt reached, target 2R, otherwise out at the flat

Results print as a table and save to acd_backtest_<date>.csv. Costs and slippage
are not included, so read small edges as no edge.
"""
from __future__ import annotations

import csv
import logging
from datetime import date, datetime, timedelta, timezone

from . import acd, config
from .metrics import to_days
from .projectx import UNIT_HOUR, UNIT_MINUTE, ProjectXError, pick_front_contracts

log = logging.getLogger("scanner.acd_backtest")
MONTHS = "FGHJKMNQUVXZ"
CHUNK_DAYS = 12          # 12 days of 1-min bars stays under the 20,000-bar limit


def candidate_ids(current_id: str, start: date, end: date) -> list[tuple[str, date]]:
    """Contract ids for every month from start to two months past end, from the
    current id's pattern, e.g. CON.F.US.GCE.Z26 -> CON.F.US.GCE.Q26 ...
    Returns (id, first day of the contract month)."""
    prefix, _, suffix = current_id.rpartition(".")
    ylen = len(suffix) - 1
    out, y, mth = [], start.year, start.month
    last = (end.replace(day=1) + timedelta(days=70)).replace(day=1)
    while date(y, mth, 1) <= last:
        yy = str(y)[-ylen:]
        out.append((f"{prefix}.{MONTHS[mth - 1]}{yy}", date(y, mth, 1)))
        mth += 1
        if mth == 13:
            y, mth = y + 1, 1
    return out


async def _bars_between(px, cid: str, unit: int, start: datetime, end: datetime) -> list[dict]:
    body = {"contractId": cid, "live": px.live,
            "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "unit": unit, "unitNumber": 1, "limit": 20000, "includePartialBar": False}
    data = await px._post("/api/History/retrieveBars", body, bars=True)
    out = []
    for b in data.get("bars", []):
        t = datetime.fromisoformat(str(b["t"]).replace("Z", "+00:00"))
        t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        out.append({"t": t.astimezone(timezone.utc), "o": float(b["o"]), "h": float(b["h"]),
                    "l": float(b["l"]), "c": float(b["c"]), "v": float(b.get("v") or 0)})
    return out


async def stitched(px, root: str, current_id: str, start: datetime, end: datetime, unit: int) -> list[dict]:
    """Continuous history: per session date, keep the contract with the most volume."""
    by_contract: dict[str, list[dict]] = {}
    for cid, month in candidate_ids(current_id, start.date(), end.date()):
        c_start = max(start, datetime.combine(month - timedelta(days=100), datetime.min.time(), tzinfo=timezone.utc))
        c_end = min(end, datetime.combine(month + timedelta(days=31), datetime.min.time(), tzinfo=timezone.utc))
        if c_start >= c_end:
            continue
        bars, t = [], c_start
        step = timedelta(days=CHUNK_DAYS if unit == UNIT_MINUTE else 365)
        try:
            while t < c_end:
                bars += await _bars_between(px, cid, unit, t, min(t + step, c_end))
                t += step
        except ProjectXError as e:
            log.info("%s: %s not available (%s)", root, cid, str(e)[:80])
            continue
        if bars:
            by_contract[cid] = bars
            log.info("%s: %s gave %d bars", root, cid, len(bars))
    day_vol: dict[date, dict[str, float]] = {}
    for cid, bars in by_contract.items():
        for b in bars:
            d = b["t"].date()
            day_vol.setdefault(d, {}).setdefault(cid, 0.0)
            day_vol[d][cid] += b["v"]
    best = {d: max(v, key=v.get) for d, v in day_vol.items()}
    out = [b for cid, bars in by_contract.items() for b in bars if best.get(b["t"].date()) == cid]
    out.sort(key=lambda b: b["t"])
    return out


async def run(px, roots: list[str], days: int, a_atrs: list[float] | None = None) -> list[dict]:
    contracts = pick_front_contracts(await px.available_contracts(), set(roots))
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    rows = []
    for root in roots:
        if root not in config.MARKETS_BY_ROOT or root not in acd.SESSIONS:
            print(f"{root}: no ACD session defined, skipped")
            continue
        c = contracts.get(root)
        if not c:
            print(f"{root}: not offered by ProjectX right now, skipped")
            continue
        print(f"{root}: pulling {days} days of 1-minute history (stitching monthly contracts)...")
        minute = await stitched(px, root, c["id"], start, end, UNIT_MINUTE)
        hourly = await stitched(px, root, c["id"], start - timedelta(days=45), end, UNIT_HOUR)
        daily = to_days(hourly)
        if not minute or len(daily) < 25:
            print(f"{root}: not enough history from ProjectX ({len(minute)} 1-min bars, {len(daily)} days)")
            continue
        for a in a_atrs or [acd.A_ATR]:
            bt = acd.backtest_market(root, minute, daily, a_atr=a)
            for row in acd.summarise(bt):
                rows.append({**row, "a_atr": a, "sessions": bt["sessions"], "or_atr": bt["or_atr"]})
    return rows


def print_table(rows: list[dict]) -> None:
    head = f"{'mkt':4} {'A':>5} {'signal':9} {'n':>4} {'/wk':>4} {'win%':>5} {'avgR':>6} {'totR':>6} {'tgts':>4} {'sess':>4} {'OR/ATR':>6}"
    print(head)
    print("-" * len(head))
    for r in rows:
        if not r.get("n"):
            print(f"{r['root']:4} {r['a_atr']:>5} {r['kind']:9} {0:>4}")
            continue
        print(f"{r['root']:4} {r['a_atr']:>5} {r['kind']:9} {r['n']:>4} {r['per_week']:>4} {r['win_pct']:>5} "
              f"{r['avg_r']:>6} {r['total_r']:>6} {r['targets']:>4} {r['sessions']:>4} {r['or_atr']!s:>6}")
    print("\navgR is the expectancy per trade in R (no costs). Above ~0.15R over 50+ trades is worth a closer look.")


def save_csv(rows: list[dict]) -> str:
    name = f"acd_backtest_{date.today().isoformat()}.csv"
    keys = ["root", "a_atr", "kind", "n", "per_week", "win_pct", "avg_r", "total_r", "targets", "sessions", "or_atr"]
    with open(name, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return name
