"""Scanner tests on synthetic bars (no network, no API key)."""
import asyncio
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from scanner import gates, main as scan_main
from scanner.config import MARKETS_BY_ROOT
from scanner.metrics import build_thesis, compute, to_days, trading_day
from scanner.projectx import contract_root, pick_front_contracts
from scanner.score import classify, score

NY = ZoneInfo("America/New_York")


def make_hourly(days=35, base=100.0, end=None, seed=1):
    """Quiet random-walk hourly bars, 23 per Globex day, ending at `end` (NY time)."""
    rnd = random.Random(seed)
    end = end or datetime(2026, 10, 21, 14, 0, tzinfo=NY)  # Wednesday afternoon
    bars, px = [], base
    t = end - timedelta(hours=24 * days * 7 // 5 + 48)
    while t <= end:
        tn = t.astimezone(NY)
        open_ = tn.weekday() < 4 and tn.hour != 17 or (tn.weekday() == 4 and tn.hour < 17) or (tn.weekday() == 6 and tn.hour >= 18)
        if open_:
            o = px
            c = o + rnd.gauss(0, 0.15)
            h, l = max(o, c) + abs(rnd.gauss(0, 0.08)), min(o, c) - abs(rnd.gauss(0, 0.08))
            bars.append({"t": t.astimezone(timezone.utc), "o": o, "h": h, "l": l, "c": c, "v": 1000 + rnd.random() * 200})
            px = c
        t += timedelta(hours=1)
    return bars


def test_trading_day_rolls_at_6pm_new_york():
    sun_6pm = datetime(2026, 10, 18, 18, 0, tzinfo=NY).astimezone(timezone.utc)
    assert trading_day(sun_6pm).strftime("%A") == "Monday"
    mon_5pm = datetime(2026, 10, 19, 16, 0, tzinfo=NY).astimezone(timezone.utc)
    assert trading_day(mon_5pm).strftime("%A") == "Monday"


def test_contract_root_parsing():
    assert contract_root("NQZ6") == "NQ"
    assert contract_root("6EZ6") == "6E"
    assert contract_root("MBTX6") == "MBT"
    assert contract_root("ZN Z6") is None
    cs = [{"name": "NQZ6", "id": "a", "activeContract": False}, {"name": "NQH7", "id": "b", "activeContract": True},
          {"name": "XXZ6", "id": "c", "activeContract": True}]
    assert pick_front_contracts(cs, {"NQ"})["NQ"]["id"] == "b"


def _add_sweep_of_prior_low(bars):
    """Make today drop well below yesterday's low, then close back above it with big volume."""
    days = to_days(bars)
    pdl = days[-2].l
    today = days[-1].bars
    for b in today[-6:-3]:
        b["l"] = pdl - 1.5
        b["c"] = pdl - 1.2
        b["o"] = pdl - 0.2
        b["h"] = pdl
        b["v"] = 6000
    last = today[-1]
    last["o"], last["c"], last["h"], last["l"], last["v"] = pdl - 1.0, pdl + 0.6, pdl + 0.7, pdl - 1.1, 7000
    return bars, pdl


def test_sweep_of_pdl_builds_long_reversal():
    bars, pdl = _add_sweep_of_prior_low(make_hourly())
    r = compute("ES", bars)
    assert r is not None
    assert any(e["type"] == "sweep" and e["level"] == "PDL" and e["dir"] == 1 for e in r.events)
    r.thesis = build_thesis(r)
    assert r.thesis["dir"] == 1 and r.thesis["kind"] == "reversal"
    assert r.thesis["stop"] < pdl - 1.5          # beyond the sweep low
    assert r.range_ratio > 1 and r.relvol > 1
    sc, reasons = score(r)
    assert 0 <= sc <= 100 and reasons


def test_quiet_market_has_no_thesis():
    r = compute("ES", make_hourly(seed=7))
    r.thesis = build_thesis(r)
    sc, _ = score(r)
    assert sc < 55


def test_positioning_gate():
    pos = {"who": "Leveraged funds", "net": -400000, "pct3y": 8, "chg4w": -90000}
    assert gates.positioning_gate(pos, -1)["status"] == "fail"     # shorting into crowded shorts
    assert gates.positioning_gate(pos, 1)["status"] == "support"   # long with squeeze fuel
    pos["pct3y"] = 50
    assert gates.positioning_gate(pos, 1)["status"] == "pass"
    assert gates.positioning_gate(None, 1)["status"] == "n/a"


def test_options_gate_regimes():
    assert gates.options_gate({"series": "VXN", "iv": 15, "pct1y": 10}, 18, 1)["regime"] == "Low"
    hi = gates.options_gate({"series": "VXN", "iv": 35, "pct1y": 90}, 20, -1)
    assert hi["regime"] == "High" and "call credit" in hi["structure"]
    assert gates.options_gate(None, 20, 1)["regime"] == "No IV feed"


def test_catalyst_gate_window():
    m = MARKETS_BY_ROOT["NKD"]
    now = datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc)
    cal = [{"t": now + timedelta(hours=3), "name": "BOJ Policy Rate", "currency": "JPY"},
           {"t": now + timedelta(hours=40), "name": "NFP", "currency": "USD"}]
    g = gates.catalyst_gate(m, cal, now, 24)
    assert g["status"] == "warn" and "BOJ" in g["text"] and "NFP" not in g["text"]


def test_dry_run_end_to_end(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(scan_main, "STATE_FILE", str(tmp_path / "state.json"))
    swept, _ = _add_sweep_of_prior_low(make_hourly(seed=3))
    quiet = make_hourly(seed=9, base=50)

    class FakePX:
        async def available_contracts(self):
            return [{"name": "ESZ6", "id": "CON.F.US.EP.Z26", "activeContract": True},
                    {"name": "GCZ6", "id": "CON.F.US.GCE.Z26", "activeContract": True}]

        async def bars(self, cid, unit, n, days_back):
            return swept if "EP" in cid else quiet

    async def fake_cot(http, m):
        return {"who": "Leveraged funds", "net": -410518, "pct3y": 22, "chg4w": -98687, "report_date": "2026-10-13"}

    async def fake_iv(http, s):
        return {"series": "VIX", "iv": 16.5, "pct1y": 39} if s else None

    async def fake_cal(http):
        return []

    monkeypatch.setattr(gates, "cot", fake_cot)
    monkeypatch.setattr(gates, "implied_vol", fake_iv)
    monkeypatch.setattr(gates, "macro_calendar", fake_cal)
    results = asyncio.run(scan_main.scan_once(FakePX(), None, dry_run=True, force_board="test"))
    out = capsys.readouterr().out
    assert results[0][0].root == "ES"
    assert "Biggest movers" in out
    es = results[0]
    if classify(es[2], es[1], es[4], 55) == "watch":
        assert "WATCH" in out and "Gate 2" in out


# ── guardrails ──
from scanner import guardrails


def _fill(i, minute, contract, pnl, size=1):
    t = datetime(2026, 10, 20, 0, minute, tzinfo=timezone.utc)
    return {"id": i, "contractId": f"CON.F.US.{contract}.Z26", "t": t, "profitAndLoss": pnl, "fees": 2.0, "size": size}


def test_guardrails_cooldown_quick_exit_sizeup_and_cap():
    now = datetime(2026, 10, 20, 1, 0, tzinfo=timezone.utc)
    fills = [
        _fill(1, 0, "ENQ", None, 2), _fill(2, 4, "ENQ", -150.0, 2),   # loss after 4 min
        _fill(3, 6, "ENQ", None, 4), _fill(4, 40, "ENQ", 300.0, 4),   # sized up after loss, held 34 min
    ]
    seen = set()
    titles = [e["title"] for e in guardrails.review_account("TS-50K", fills, seen, now)]
    assert any("cool-down" in t for t in titles)
    assert any("Closed after 4 min" in t for t in titles)
    assert any("Size went UP" in t for t in titles)
    assert any("2 trades done" in t for t in titles)
    # Nothing repeats on the next check
    assert guardrails.review_account("TS-50K", fills, seen, now) == []


def test_no_idea_window_is_london_in_sydney_time():
    assert guardrails.in_no_idea_window(datetime(2026, 10, 20, 5, 0, tzinfo=timezone.utc))      # 4pm Sydney
    assert not guardrails.in_no_idea_window(datetime(2026, 10, 19, 23, 0, tzinfo=timezone.utc))  # 10am Sydney


def test_idea_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(scan_main, "STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(guardrails, "in_no_idea_window", lambda now: False)
    monkeypatch.setattr(guardrails, "MAX_IDEAS_PER_DAY", 1)
    import scanner.main as sm
    from scanner import score as sc_mod
    monkeypatch.setattr(sm, "classify", lambda *a, **k: "watch")
    swept, _ = _add_sweep_of_prior_low(make_hourly(seed=3))
    swept2, _ = _add_sweep_of_prior_low(make_hourly(seed=5, base=50))

    class FakePX:
        async def available_contracts(self):
            return [{"name": "ESZ6", "id": "A", "activeContract": True}, {"name": "GCZ6", "id": "B", "activeContract": True}]

        async def bars(self, cid, unit, n, days_back, **k):
            return swept if cid == "A" else swept2

    async def none(*a, **k):
        return None

    async def empty(*a, **k):
        return []
    monkeypatch.setattr(gates, "cot", none)
    monkeypatch.setattr(gates, "implied_vol", none)
    monkeypatch.setattr(gates, "macro_calendar", empty)
    from scanner import desk as desk_mod

    def all_pass(r, g, reg, macro_call, now, verified_today=None):
        return {"verdict": "verified", "reason": "", "checks": [], "ceiling": None, "ceiling_factor": 1.0}
    monkeypatch.setattr(desk_mod, "verify", all_pass)

    async def no_macro(*a, **k):
        return None
    monkeypatch.setattr(sm.macro, "latest", no_macro)
    raw, verified = [], []

    async def fake_post(http, hook, embeds, dry):
        raw.extend(e for e in embeds if "WATCH" in e.get("title", ""))
        verified.extend(e for e in embeds if "VERIFIED" in e.get("title", ""))
        return True
    monkeypatch.setattr(sm, "post", fake_post)
    asyncio.run(sm.scan_once(FakePX(), None, dry_run=True))
    # Both ideas show in the raw feed; the cap lets only one through as verified
    assert len(raw) == 2 and len(verified) == 1
    capped = [e for e in raw if any("Daily cap" in f["value"] for f in e["fields"])]
    assert len(capped) == 1


def test_merge_by_account():
    es = [{"title": "🛑 30-minute cool-down · A", "description": "x"}, {"title": "🛑 30-minute cool-down · B", "description": "x"},
          {"title": "✅ 2 trades done · A", "description": "y"}]
    m = scan_main.merge_by_account(es)
    assert len(m) == 2 and m[0]["footer"]["text"] == "Accounts: A, B"


def test_daily_stop_and_profit_cap(monkeypatch):
    now = datetime(2026, 10, 20, 1, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(guardrails, "PERSONAL_DAILY_STOP", 550.0)
    monkeypatch.setattr(guardrails, "DAILY_PROFIT_CAP", 3450.0)
    losing = [_fill(1, 0, "NKD", None), _fill(2, 30, "NKD", -600.0)]
    assert any("daily stop" in e["title"] for e in guardrails.review_account("A", losing, set(), now))
    winning = [_fill(1, 0, "NKD", None), _fill(2, 50, "NKD", 3500.0)]
    assert any("Consistency cap" in e["title"] for e in guardrails.review_account("A", winning, set(), now))


def test_symbols_and_size():
    from scanner import symbols
    assert symbols.tradingview("NQ", "NQZ6") == ("CME_MINI:NQ1!", "CME_MINI:NQZ2026")
    assert symbols.tradingview("GC", "GCZ6")[0] == "COMEX:GC1!"
    assert symbols.tradingview("NKD", None) == ("CME:NKD1!", None)
    pv = symbols.point_value({"tickSize": 5, "tickValue": 25})   # NKD: $5 a point
    assert pv == 5.0
    assert symbols.size_for_risk(90, pv, 450) == (1, 450.0)
    assert symbols.size_for_risk(200, pv, 275)[0] == 0
