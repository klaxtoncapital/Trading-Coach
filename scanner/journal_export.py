"""Trade journal exporter: TopstepX fills -> desk-data/journal for the journal agent.

Every evening (JOURNAL_EXPORT_AT, Sydney time) this pulls the fills and orders on every
active TopstepX account, turns them into trades, adds the chart context Dave grades
against (6PM New York open, previous day high/low, excursion in ticks), draws a chart,
and saves it all to the public desk-data branch:

  journal/topstep/<Sydney date>.json   one file per Sydney date the trades closed on
  journal/charts/<trade key>.png       one 5-minute chart per trade

A scheduled Claude task reads those files at 8:25pm Sydney, tags the entry signature
from the chart, checks the trade against the playbook and writes it into the Klaxton
Trade Journal. This service never places, changes or cancels anything: read-only.

Rules for turning fills into trades (Dave's journal rules):
  * a trade runs from flat to flat on one account and contract; adding to a position
    (a pyramid / scale-in) is part of the same trade, never a new one
  * copier accounts (the same contract, side and size within COPY_WINDOW seconds) are
    one trade, recorded once with the number of accounts
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp

from .projectx import UNIT_MINUTE, ProjectXClient, contract_root

log = logging.getLogger("scanner.journal")
SYD = ZoneInfo("Australia/Sydney")
NY = ZoneInfo("America/New_York")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
JOURNAL_REPO = os.getenv("JOURNAL_REPO", "klaxtoncapital/trading-coach")
JOURNAL_BRANCH = os.getenv("JOURNAL_BRANCH", "desk-data")
EXPORT_AT = os.getenv("JOURNAL_EXPORT_AT", "20:10")        # Sydney time, daily
LOOKBACK_DAYS = int(os.getenv("JOURNAL_LOOKBACK_DAYS", "3"))  # each run re-exports this many days
STATE_FILE = os.getenv("JOURNAL_STATE_FILE", "journal_state.json")
COPY_WINDOW = 15     # seconds: fills this close together on other accounts are copier copies
REENTRY_MERGE = 60   # seconds: back in the same direction this soon after going flat = same trade
ORDER_TYPES = {1: "Limit", 2: "Market", 3: "StopLimit", 4: "Stop", 5: "TrailingStop"}


# ── ProjectX calls this module needs ──
async def _orders(self: ProjectXClient, account_id: int, start: datetime, end: datetime) -> list[dict]:
    await self._ensure_token()
    body = {"accountId": account_id, "startTimestamp": _iso(start), "endTimestamp": _iso(end)}
    data = await self._post("/api/Order/search", body)
    return data.get("orders", [])


async def _trades_between(self: ProjectXClient, account_id: int, start: datetime, end: datetime) -> list[dict]:
    await self._ensure_token()
    body = {"accountId": account_id, "startTimestamp": _iso(start), "endTimestamp": _iso(end)}
    data = await self._post("/api/Trade/search", body)
    return [t for t in data.get("trades", []) if not t.get("voided")]


async def _contract(self: ProjectXClient, contract_id: str) -> dict:
    await self._ensure_token()
    data = await self._post("/api/Contract/searchById", {"contractId": contract_id})
    return data.get("contract") or {}


async def _bars_between(self: ProjectXClient, contract_id: str, start: datetime, end: datetime,
                        minutes: int = 1) -> list[dict]:
    await self._ensure_token()
    body = {"contractId": contract_id, "live": self.live, "startTime": _iso(start), "endTime": _iso(end),
            "unit": UNIT_MINUTE, "unitNumber": minutes, "limit": 20000, "includePartialBar": False}
    data = await self._post("/api/History/retrieveBars", body, bars=True)
    out = [{"t": _ts(b["t"]), "o": float(b["o"]), "h": float(b["h"]), "l": float(b["l"]),
            "c": float(b["c"]), "v": float(b.get("v") or 0)} for b in data.get("bars", [])]
    out.sort(key=lambda x: x["t"])
    return out


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(s) -> datetime:
    t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


# ── fills -> trades ──
@dataclass
class Trade:
    account_id: int
    account: str
    contract_id: str
    side: str                       # "Long" / "Short"
    legs: list[dict] = field(default_factory=list)
    reentries: int = 0

    @property
    def max_position(self) -> int:
        pos = top = 0
        for x in self.legs:
            pos += x["size"] if x["side"] == 0 else -x["size"]
            top = max(top, abs(pos))
        return top

    @property
    def entries(self):
        want = 0 if self.side == "Long" else 1
        return [x for x in self.legs if x["side"] == want]

    @property
    def exits(self):
        want = 1 if self.side == "Long" else 0
        return [x for x in self.legs if x["side"] == want]


def _wavg(legs: list[dict]) -> float | None:
    n = sum(x["size"] for x in legs)
    return sum(x["price"] * x["size"] for x in legs) / n if n else None


def build_trades(fills: list[dict], account_id: int, account: str) -> tuple[list[Trade], list[Trade]]:
    """Group one account's fills into flat-to-flat trades. Returns (closed, still_open).

    Fills: ProjectX trades {contractId, creationTimestamp, price, size, side (0 buy, 1 sell),
    profitAndLoss, fees, commissions, orderId}. Adds to a position stay in the same trade.
    """
    by_contract: dict[str, list[dict]] = {}
    for f in fills:
        f = {**f, "t": _ts(f["creationTimestamp"]), "size": int(f["size"]), "price": float(f["price"])}
        by_contract.setdefault(f["contractId"], []).append(f)
    closed, open_ = [], []
    for cid, fs in by_contract.items():
        fs.sort(key=lambda x: (x["t"], x.get("id", 0)))
        pos, cur = 0, None
        for f in fs:
            signed = f["size"] if f["side"] == 0 else -f["size"]
            if pos == 0:
                side = "Long" if signed > 0 else "Short"
                last = closed[-1] if closed and closed[-1].contract_id == cid else None
                if last and last.side == side and (f["t"] - last.legs[-1]["t"]).total_seconds() <= REENTRY_MERGE:
                    # e.g. a mis-tap closed it and he re-entered seconds later: still one trade
                    cur = closed.pop()
                    cur.reentries += 1
                else:
                    cur = Trade(account_id, account, cid, side)
            cur.legs.append(f)
            pos += signed
            if pos == 0:
                closed.append(cur)
                cur = None
        if cur is not None and pos != 0:
            open_.append(cur)
    return closed, open_


def _attach_orders(t: Trade, orders: list[dict]) -> dict:
    """Stop and target from the account's orders on this contract during the trade."""
    t0, t1 = t.legs[0]["t"], t.legs[-1]["t"]
    exit_side = 1 if t.side == "Long" else 0
    mine = []
    for o in orders:
        if o.get("contractId") != t.contract_id or o.get("side") != exit_side:
            continue
        made = _ts(o["creationTimestamp"])
        if made < t0 - timedelta(seconds=30) or made > t1:
            continue
        mine.append({**o, "made": made})
    mine.sort(key=lambda o: o["made"])
    stops = [o for o in mine if o.get("type") in (4, 5)]
    limits = [o for o in mine if o.get("type") == 1]
    exit_ids = {x.get("orderId") for x in t.exits}

    def price(o):
        return o.get("stopPrice") if o.get("type") in (4, 5) else o.get("limitPrice")

    stop0 = stops[0] if stops else None
    target0 = limits[0] if limits else None
    by_id = {o["id"]: o for o in mine if "id" in o}
    last_exit = by_id.get(t.exits[-1].get("orderId")) if t.exits else None
    if last_exit and last_exit.get("type") in (4, 5):
        reason = "Stop"
    elif last_exit and last_exit.get("type") == 1:
        reason = "Target"
    else:
        reason = "Closed by hand"
    entry_t = t.legs[0]["t"]
    return {
        "initial_stop": price(stop0) if stop0 else None,
        "stop_with_entry": bool(stop0 and stop0["made"] <= entry_t + timedelta(seconds=60)),
        "stop_orders": len(stops),
        "final_stop": price(stops[-1]) if stops else None,
        "target": price(target0) if target0 else None,
        "target_with_entry": bool(target0 and target0["made"] <= entry_t + timedelta(seconds=60)),
        "target_cancelled": bool(target0 and target0.get("status") == 3 and target0.get("id") not in exit_ids),
        "exit_reason": reason,
        "orders_note": "Stop/target prices are the orders' current prices: a stop moved by dragging keeps one order, "
                       "so a move only shows when a new stop order was placed.",
    }


def merge_copies(trades: list[dict]) -> list[dict]:
    """Copier accounts: the same contract, side and size entered within COPY_WINDOW seconds = one trade."""
    trades = sorted(trades, key=lambda x: x["entry_time"])
    out: list[dict] = []
    for tr in trades:
        for g in out:
            if (g["contract_id"] == tr["contract_id"] and g["side"] == tr["side"] and g["qty"] == tr["qty"]
                    and tr["account_id"] not in g["_ids"]
                    and abs((_ts(g["entry_time"]) - _ts(tr["entry_time"])).total_seconds()) <= COPY_WINDOW):
                g["_ids"].add(tr["account_id"])
                g["per_account"].append({k: tr[k] for k in ("account", "entry", "exit", "pnl_usd", "fees_usd")})
                g["accounts"] += 1
                g["pnl_usd_all_accounts"] = round(g["pnl_usd_all_accounts"] + tr["pnl_usd"], 2)
                g["fees_usd_all_accounts"] = round(g["fees_usd_all_accounts"] + tr["fees_usd"], 2)
                break
        else:
            tr = {**tr, "_ids": {tr["account_id"]}, "accounts": 1,
                  "per_account": [{k: tr[k] for k in ("account", "entry", "exit", "pnl_usd", "fees_usd")}],
                  "pnl_usd_all_accounts": tr["pnl_usd"], "fees_usd_all_accounts": tr["fees_usd"]}
            out.append(tr)
    for g in out:
        g.pop("_ids", None)
    return out


# ── chart context ──
def cme_trade_date(t: datetime) -> date:
    """CME trade date: the session that opens 18:00 New York belongs to the next day."""
    return (t.astimezone(NY) + timedelta(hours=6)).date()


def session_open_utc(td: date) -> datetime:
    """18:00 New York on the evening before trade date `td` (Sunday evening for a Monday)."""
    prev = td - timedelta(days=1)
    while prev.weekday() == 5:   # Saturday: the Monday session opens Sunday 18:00
        prev -= timedelta(days=1)
    return datetime(prev.year, prev.month, prev.day, 18, 0, tzinfo=NY).astimezone(timezone.utc)


def context(tr: dict, bars: list[dict], tick: float) -> dict:
    """6PM open, previous day high/low, side of the open at entry, MFE/MAE in ticks."""
    entry_t, exit_t = _ts(tr["entry_time"]), _ts(tr["exit_time"])
    td = cme_trade_date(entry_t)
    by_td: dict[date, list[dict]] = {}
    for b in bars:
        by_td.setdefault(cme_trade_date(b["t"]), []).append(b)
    today = by_td.get(td, [])
    prev_days = sorted(d for d in by_td if d < td)
    prev = by_td[prev_days[-1]] if prev_days else []
    six = today[0]["o"] if today and today[0]["t"] <= session_open_utc(td) + timedelta(minutes=5) else None
    pdh = max(b["h"] for b in prev) if prev else None
    pdl = min(b["l"] for b in prev) if prev else None
    held = [b for b in bars if entry_t - timedelta(minutes=1) < b["t"] <= exit_t]
    out = {"cme_trade_date": td.isoformat(), "weekday": td.strftime("%a"),
           "six_pm_open": six, "pdh": pdh, "pdl": pdl,
           "today_high_before_entry": max((b["h"] for b in today if b["t"] < entry_t), default=None),
           "today_low_before_entry": min((b["l"] for b in today if b["t"] < entry_t), default=None),
           "mfe_ticks": None, "mae_ticks": None, "entry_vs_open": None, "in_ote": None}
    e = tr["entry"]
    if held and tick:
        hi, lo = max(b["h"] for b in held), min(b["l"] for b in held)
        fav, adv = (hi - e, e - lo) if tr["side"] == "Long" else (e - lo, hi - e)
        out["mfe_ticks"], out["mae_ticks"] = round(max(fav, 0) / tick), round(max(adv, 0) / tick)
    if six is not None:
        out["entry_vs_open"] = "Above" if e > six else "Below" if e < six else "At"
        # OTE: shorts above the 6PM open, longs below it
        out["in_ote"] = (e >= six) if tr["side"] == "Short" else (e <= six)
    if tick and tr.get("initial_stop") is not None:
        risk_ticks = abs(e - tr["initial_stop"]) / tick
        out["stop_ticks"] = round(risk_ticks)
        lvl = pdl if tr["side"] == "Short" else pdh
        if lvl is not None and risk_ticks:
            room = (e - lvl) if tr["side"] == "Short" else (lvl - e)
            out["r_to_opposite_level"] = round(room / tick / risk_ticks, 2)
    return out


def draw_chart(tr: dict, bars5: list[dict], ctx: dict, path_or_buf) -> bool:
    """5-minute candles with the 6PM open, PDH/PDL, entry, exit, stop and target."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except ImportError:
        log.warning("matplotlib not installed: no charts")
        return False
    if not bars5:
        return False
    fig, ax = plt.subplots(figsize=(12, 6.5), dpi=110)
    bg, fg, up, dn = "#111418", "#c9d1d9", "#26a69a", "#ef5350"
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)
    w = 0.6
    xs = list(range(len(bars5)))
    for i, b in zip(xs, bars5):
        col = up if b["c"] >= b["o"] else dn
        ax.plot([i, i], [b["l"], b["h"]], color=col, linewidth=0.8)
        ax.add_patch(Rectangle((i - w / 2, min(b["o"], b["c"])), w, max(abs(b["c"] - b["o"]), 1e-9),
                               color=col, linewidth=0))

    def idx(t):
        t = _ts(t)
        best = 0
        for i, b in enumerate(bars5):
            if b["t"] <= t:
                best = i
        return best

    def hline(y, label, color, style="-"):
        if y is None:
            return
        ax.axhline(y, color=color, linestyle=style, linewidth=1)
        ax.text(len(bars5) - 0.5, y, f" {label} {y:,.2f}".rstrip("0").rstrip("."), color=color,
                va="center", fontsize=8, clip_on=False,
                bbox={"facecolor": bg, "edgecolor": "none", "pad": 1})

    hline(ctx.get("six_pm_open"), "6PM open", "#f0a030")
    hline(ctx.get("pdh"), "PDH", "#8b949e", "--")
    hline(ctx.get("pdl"), "PDL", "#8b949e", "--")
    hline(tr.get("initial_stop"), "Stop", "#ef5350", ":")
    hline(tr.get("target"), "Target", "#26a69a", ":")
    long_ = tr["side"] == "Long"
    ei, xi = idx(tr["entry_time"]), idx(tr["exit_time"])
    for f in tr.get("fills", []):   # every fill: adds, partial exits, re-entries
        is_entry = (f["side"] == "Buy") == long_
        ax.scatter([idx(f["time"])], [f["price"]], marker=("^" if long_ else "v") if is_entry else "x",
                   s=45, color="#4ea1ff" if is_entry else "#ffffff", alpha=0.6, zorder=4)
    ax.scatter([ei], [tr["entry"]], marker="^" if long_ else "v", s=120, color="#4ea1ff", zorder=5)
    ax.scatter([xi], [tr["exit"]], marker="X", s=90, color="#ffffff", zorder=5)
    ax.axvspan(ei - 0.5, xi + 0.5, color="#4ea1ff", alpha=0.07)
    step = max(1, len(bars5) // 10)
    ax.set_xticks(xs[::step])
    ax.set_xticklabels([b["t"].astimezone(SYD).strftime("%a %H:%M") for b in bars5[::step]],
                       rotation=0, fontsize=8, color=fg)
    ax.tick_params(colors=fg, labelsize=8)
    for s in ax.spines.values():
        s.set_color("#30363d")
    ax.grid(color="#21262d", linewidth=0.5)
    pnl = tr.get("pnl_usd", 0)
    ax.set_title(f"{tr['contract']} · {tr['side']} {tr['qty']} · {tr['account']}"
                 f"{' x' + str(tr['accounts']) if tr.get('accounts', 1) > 1 else ''} · "
                 f"{'+' if pnl >= 0 else '−'}${abs(pnl):,.0f} · 5m, Sydney time",
                 color=fg, fontsize=11, loc="left")
    ax.set_xlim(-1, len(bars5) + 6)
    fig.tight_layout()
    fig.savefig(path_or_buf, format="png", facecolor=bg)
    plt.close(fig)
    return True


# ── one export run ──
def trade_key(tr: dict) -> str:
    return f"{tr['contract']}-{_ts(tr['entry_time']).strftime('%Y%m%dT%H%M%S')}"


async def export(px: ProjectXClient, days: int = LOOKBACK_DAYS, now: datetime | None = None) -> dict:
    """Pull and shape the last `days` days of trades. Returns {syd_date: payload} and charts."""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    accounts = await px.active_accounts()
    raw, open_positions = [], []
    contracts: dict[str, dict] = {}
    for a in accounts:
        aid, name = a["id"], a.get("name") or str(a["id"])
        fills = await px.trades_between(aid, start, now)
        if not fills:
            continue
        orders = await px.orders(aid, start - timedelta(hours=1), now)
        closed, still_open = build_trades(fills, aid, name)
        open_positions += [f"{name} {t.side} {t.contract_id}" for t in still_open]
        for t in closed:
            if t.contract_id not in contracts:
                try:
                    contracts[t.contract_id] = await px.contract(t.contract_id)
                except Exception as e:  # noqa: BLE001
                    log.warning("Contract lookup failed for %s: %s", t.contract_id, e)
                    contracts[t.contract_id] = {}
            c = contracts[t.contract_id]
            fees = sum(float(x.get("fees") or 0) + float(x.get("commissions") or 0) for x in t.legs)
            raw.append({
                "account_id": aid, "account": name, "platform": "TopstepX",
                "contract_id": t.contract_id, "contract": c.get("name") or t.contract_id.split(".")[-1],
                "root": contract_root(c.get("name", "")), "instrument": c.get("description"),
                "tick_size": c.get("tickSize"), "tick_value": c.get("tickValue"),
                "side": t.side, "qty": t.max_position,
                "adds": max(0, len(t.entries) - 1 - t.reentries), "partial_exits": max(0, len(t.exits) - 1 - t.reentries),
                "quick_reentries": t.reentries,
                "entry": round(_wavg(t.entries), 6), "exit": round(_wavg(t.exits), 6),
                "entry_time": t.legs[0]["t"].isoformat(), "exit_time": t.legs[-1]["t"].isoformat(),
                "pnl_usd": round(sum(float(x.get("profitAndLoss") or 0) for x in t.legs), 2),
                "fees_usd": round(fees, 2),
                "fills": [{"time": x["t"].isoformat(), "side": "Buy" if x["side"] == 0 else "Sell",
                           "size": x["size"], "price": x["price"]} for x in t.legs],
                **_attach_orders(t, orders),
            })
    merged = merge_copies(raw)
    by_day: dict[str, dict] = {}
    charts: dict[str, bytes] = {}
    for tr in merged:
        tick = tr.get("tick_size") or 0
        entry_t, exit_t = _ts(tr["entry_time"]), _ts(tr["exit_time"])
        td = cme_trade_date(entry_t)
        b_start = session_open_utc(td) - timedelta(days=3)   # covers the previous session over a weekend
        try:
            bars1 = await px.bars_between(tr["contract_id"], b_start, min(now, exit_t + timedelta(hours=2)), 1)
            bars5 = await px.bars_between(tr["contract_id"], max(session_open_utc(td), entry_t - timedelta(hours=4)),
                                          min(now, exit_t + timedelta(minutes=90)), 5)
        except Exception as e:  # noqa: BLE001
            log.warning("Bars failed for %s: %s", tr["contract"], e)
            bars1, bars5 = [], []
        tr["context"] = context(tr, bars1, tick)
        if tick and tr.get("tick_value") and tr.get("initial_stop") is not None:
            risk = abs(tr["entry"] - tr["initial_stop"]) / tick * tr["tick_value"] * tr["qty"]
            tr["risk_usd"] = round(risk, 2)
            tr["r_multiple"] = round(tr["pnl_usd"] / risk, 2) if risk else None
        key = trade_key(tr)
        tr["key"] = key
        buf = io.BytesIO()
        if draw_chart(tr, bars5, tr["context"], buf):
            charts[key] = buf.getvalue()
            tr["chart"] = f"journal/charts/{key}.png"
            tr["chart_url"] = f"https://raw.githubusercontent.com/{JOURNAL_REPO}/{JOURNAL_BRANCH}/journal/charts/{key}.png"
        syd_day = exit_t.astimezone(SYD).date().isoformat()
        day = by_day.setdefault(syd_day, {"sydney_date": syd_day, "source": "TopstepX via ProjectX API",
                                          "generated_at": now.isoformat(), "trades": []})
        day["trades"].append(tr)
    for d in by_day.values():
        d["trades"].sort(key=lambda x: x["entry_time"])
        d["open_positions_at_export"] = open_positions
    return {"days": by_day, "charts": charts, "open_positions": open_positions}


# ── GitHub (desk-data branch) ──
async def _gh_put(http: aiohttp.ClientSession, path: str, content: bytes, message: str) -> bool:
    url = f"https://api.github.com/repos/{JOURNAL_REPO}/contents/{path}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json",
               "User-Agent": "klaxton-desk-journal"}
    sha = None
    async with http.get(url, params={"ref": JOURNAL_BRANCH}, headers=headers) as r:
        if r.status == 200:
            j = await r.json()
            sha = j.get("sha")
            try:
                if base64.b64decode(j.get("content", "")) == content:
                    return True   # unchanged
            except ValueError:
                pass
    body = {"message": message, "content": base64.b64encode(content).decode(), "branch": JOURNAL_BRANCH}
    if sha:
        body["sha"] = sha
    async with http.put(url, json=body, headers=headers) as r:
        if r.status in (200, 201):
            return True
        log.warning("GitHub write %s failed: HTTP %s %s", path, r.status, (await r.text())[:200])
        return False


async def publish(http: aiohttp.ClientSession, result: dict, out_dir: str | None = None) -> int:
    """Write the day files and charts. out_dir = write locally instead (testing)."""
    n = 0
    files = {f"journal/charts/{k}.png": v for k, v in result["charts"].items()}
    files.update({f"journal/topstep/{d}.json": json.dumps(p, indent=1, default=str).encode()
                  for d, p in result["days"].items()})
    for path, content in files.items():
        if out_dir:
            full = os.path.join(out_dir, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(content)
            n += 1
        elif await _gh_put(http, path, content, f"Journal export: {os.path.basename(path)}"):
            n += 1
    return n


def _due(now: datetime, state: dict) -> bool:
    syd = now.astimezone(SYD)
    hh, mm = (int(x) for x in EXPORT_AT.split(":"))
    slot = syd.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return syd >= slot and state.get("last_run_day") != syd.date().isoformat()


async def loop(px: ProjectXClient, http: aiohttp.ClientSession, dry_run: bool):
    if not GITHUB_TOKEN and not dry_run:
        log.warning("Journal export off: set GITHUB_TOKEN (contents write on %s) to turn it on", JOURNAL_REPO)
        return
    while True:
        now = datetime.now(timezone.utc)
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
        if _due(now, state):
            try:
                res = await export(px, LOOKBACK_DAYS, now)
                n = await publish(http, res, out_dir="journal_out" if dry_run else None)
                log.info("Journal export: %d trades over %d day(s), %d file(s) written",
                         sum(len(d["trades"]) for d in res["days"].values()), len(res["days"]), n)
                state["last_run_day"] = now.astimezone(SYD).date().isoformat()
                with open(STATE_FILE, "w") as f:
                    json.dump(state, f)
            except Exception:  # noqa: BLE001
                log.exception("Journal export failed; retrying in 10 minutes")
                await asyncio.sleep(540)
        await asyncio.sleep(60)


ProjectXClient.orders = _orders
ProjectXClient.trades_between = _trades_between
ProjectXClient.contract = _contract
ProjectXClient.bars_between = _bars_between
