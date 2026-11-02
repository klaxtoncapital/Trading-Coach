"""Strategy Lab: the engine must count trades honestly, and the checks must catch cheating."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from lab import data, engine, metrics, robust, tearsheet
from lab.__main__ import compare_retrace, synthetic
from lab.engine import Costs, Setup
from lab.strategies.retrace import Retrace

NOCOST = Costs(0.0, 0.0, 1.0, 10.0)


def bars(rows, start="2026-10-27 22:00"):
    """Hourly bars from (o, h, l, c) tuples."""
    idx = pd.date_range(start, periods=len(rows), freq="h", tz="UTC", name="t")
    df = pd.DataFrame(rows, columns=["o", "h", "l", "c"], index=idx)
    df["v"], df["contract"] = 100.0, "X"
    return df


def T(df, i):
    return df.index[i]


# ── engine ───────────────────────────────────────────────────────────────

def test_market_long_hits_target():
    b = bars([(100, 101, 99, 100), (100, 103, 99, 102), (102, 111, 101, 110)])
    t = engine.simulate(b, [Setup(T(b, 1), 1, stop_dist=5, target_dist=10)], NOCOST)
    assert len(t) == 1 and t.loc[0, "result"] == "target"
    assert t.loc[0, "entry"] == 100 and t.loc[0, "r"] == 2.0 and t.loc[0, "net_usd"] == 100


def test_bar_touching_stop_and_target_is_a_loss():
    b = bars([(100, 100, 100, 100), (100, 120, 80, 100)])
    t = engine.simulate(b, [Setup(T(b, 1), 1, stop_dist=5, target_dist=10)], NOCOST)
    assert t.loc[0, "result"] == "stop" and t.loc[0, "r"] == -1


def test_short_time_exit_at_close():
    b = bars([(100, 101, 99, 100), (100, 101, 97, 98), (98, 99, 96, 97), (97, 98, 90, 91)])
    t = engine.simulate(b, [Setup(T(b, 0), -1, stop_dist=5, target_dist=50, exit_by=T(b, 3))], NOCOST)
    assert t.loc[0, "result"] == "time" and t.loc[0, "exit"] == 97 and t.loc[0, "r"] == pytest.approx(0.6)


def test_limit_entry_and_cancel_if_target_first():
    b = bars([(100, 101, 99, 100), (100, 112, 100, 111), (111, 111, 94, 95), (95, 96, 80, 81)])
    s = Setup(T(b, 1), 1, stop=90, target=110, entry=95)
    t = engine.simulate(b, [s], NOCOST)
    assert t.loc[0, "entry"] == 95 and t.loc[0, "result"] == "stop"
    s.cancel_if_target_first = True
    assert engine.simulate(b, [s], NOCOST).empty          # target came first: setup over


def test_gap_through_stop_fills_at_the_open():
    b = bars([(100, 101, 99, 100), (100, 101, 98, 99), (90, 92, 88, 91)])
    s = Setup(T(b, 1), 1, stop=95, target=120)
    assert engine.simulate(b, [s], NOCOST).loc[0, "exit"] == 90
    assert engine.simulate(b, [s], NOCOST, gap_fills=False).loc[0, "exit"] == 95


def test_costs():
    b = bars([(100, 101, 99, 100), (100, 103, 99, 102), (102, 111, 101, 110)])
    c = Costs(commission_rt=4.0, slip_ticks=1, tick=0.25, point_value=20.0)
    t = engine.simulate(b, [Setup(T(b, 1), 1, stop_dist=5, target_dist=10)], c)
    # target: slippage only on the market entry (0.25pt = $5) plus $4 commission
    assert t.loc[0, "gross_usd"] == 200 and t.loc[0, "cost_usd"] == 9 and t.loc[0, "net_usd"] == 191
    assert t.loc[0, "r"] == pytest.approx(191 / 100)


# ── look-ahead detector ──────────────────────────────────────────────────

class Cheat:
    """Goes long on days that close up: uses the close of the bar it trades on."""
    name, timeframe, defaults, hypothesis = "cheat", "1h", {}, ""

    def setups(self, b):
        return [Setup(b.index[i], 1 if b["c"].iloc[i] > b["o"].iloc[i] else -1, stop_dist=50, target_dist=50)
                for i in range(len(b))]


def test_lookahead_check_catches_a_cheat_and_clears_retrace():
    b = synthetic(200)
    ok, probs = engine.lookahead_check(Cheat(), b)
    assert not ok and probs
    ok, probs = engine.lookahead_check(Retrace(), b, {"tick": 5.0})
    assert ok, probs


# ── the Stage 0 gate on made-up data ─────────────────────────────────────

def test_retrace_matches_old_backtest():
    b = synthetic(400)
    for params in ({"mode": "ticks"}, {"mode": "ticks", "entry": 10}, {"mode": "pct", "cont": "lowhigh"}):
        j, st = compare_retrace(b, "NKD", 5.0, 5.0, params)
        assert st["old_trades"] == st["lab_trades"] > 50
        assert st["matching"] == st["old_trades"], j[~j["match"]]


# ── metrics and robustness ───────────────────────────────────────────────

def test_metrics_signs_agree():
    t = engine.simulate(synthetic(500), Retrace().setups(synthetic(500), tick=5.0), Costs(4, 1, 5, 5))
    s = metrics.summary(t)
    assert s["n"] > 100
    assert np.sign(s["sharpe"]) == np.sign(s["expectancy_r"]) == np.sign(s["total_r"])
    assert s["expectancy_ci"][0] < s["expectancy_r"] < s["expectancy_ci"][1]
    assert metrics.longest_losing_streak([1, -1, -1, 0, 2, -1]) == 3


def test_monte_carlo_breach_rises_with_risk():
    r = np.r_[np.full(40, 2.0), np.full(60, -1.0)]
    low = robust.monte_carlo(r, risk_usd=100, limit_usd=4500, sims=2000)
    high = robust.monte_carlo(r, risk_usd=600, limit_usd=4500, sims=2000)
    assert low.p_breach < high.p_breach and low.dd_p95 <= low.dd_p50


def test_walk_forward_only_trades_after_training():
    b = synthetic(700)
    oos, win = robust.walk_forward(Retrace(), b, NOCOST, {"stop": [40, 50], "target": [150, 185]},
                                   {"tick": 5.0}, train_days=365, test_days=90, min_trades=10)
    assert len(win) and len(oos)
    assert oos["entry_time"].min() >= b.index[0] + pd.Timedelta(days=365)


def test_tearsheet_renders():
    b = synthetic(500)
    t = engine.simulate(b, Retrace().setups(b, tick=5.0), NOCOST)
    rep = tearsheet.Report("retrace", "TEST", {"tick": 5.0}, "test", t, "none",
                           lookahead=(True, []), mc=robust.monte_carlo(t["r"], 275, 4500, sims=500))
    html = tearsheet.render(rep)
    assert html.startswith("<!DOCTYPE html>") and "Validation checklist" in html and "<svg" in html


# ── data store ───────────────────────────────────────────────────────────

def test_trading_day_sunday_evening_is_monday():
    b = bars([(1, 1, 1, 1)], start="2026-10-25 23:00")          # Sun 7pm New York
    assert data.trading_days(b).iloc[0] == date(2026, 10, 26)


def test_stitch_volume_roll_and_back_adjust():
    a = bars([(100, 101, 99, 100)] * 48, start="2026-12-01 00:00")
    nb = bars([(110, 111, 109, 110)] * 48, start="2026-12-01 00:00")
    nb["v"] = 10.0
    a.loc[a.index >= "2026-12-02", "v"] = 5.0                   # day 2: the new contract has the volume
    s = data.stitch({"NKDZ6": a, "NKDH7": nb})
    assert list(s["contract"].unique()) == ["NKDZ6", "NKDH7"]
    adj = data.back_adjust(s)
    assert adj["c"].iloc[0] == 110 and adj["c"].iloc[-1] == 110  # roll gap gone, newest prices real


def test_store_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "DATA_DIR", tmp_path)
    b = synthetic(30)
    data.save("NKD", "1h", b, {"tick_size": 5.0, "point_value": 5.0})
    back = data.load("NKD", "1h")
    assert len(back) == len(b) and data.load_meta("NKD")["tick_size"] == 5.0
    assert data.available()[0][:2] == ("NKD", "1h")


# ── prop firm Combine simulator ──────────────────────────────────────────

from lab import prop  # noqa: E402

R = prop.Rules(max_days=30)


def days(*pnls):
    """One trade a day: (net, worst during the trade)."""
    return [[(p, min(p, 0.0))] if p is not None else [] for p in pnls]


def test_prop_pass_needs_target_and_consistency():
    a = prop.run_attempt(days(*[1000] * 9), R)
    assert a.outcome == "PASS" and a.days == 9
    # one $9,000 day reaches the target but breaks the 50% consistency target: keep trading
    a = prop.run_attempt(days(9000, *[1000] * 9), R)
    assert a.outcome == "PASS" and a.days == 10 and a.profit == 18000


def test_prop_fails_on_intraday_touch_of_the_limit():
    a = prop.run_attempt([[(-1000.0, -4600.0)]], R)          # closed at -1,000 but was -4,600 inside the trade
    assert a.outcome == "FAIL" and a.days == 1


def test_prop_loss_limit_trails_end_of_day_and_locks_at_start():
    assert prop.run_attempt(days(4000, -4600), R).outcome == "FAIL"     # floor moved up to 149,500
    assert prop.run_attempt(days(4000, -4400), R).outcome == "TIMEOUT"
    # after +6,000 the floor stops at the 150,000 start, so a 5,900 drop is still alive
    assert prop.run_attempt(days(6000, -5900), R).outcome == "TIMEOUT"
    assert prop.run_attempt(days(6000, -6000), R).outcome == "FAIL"


def test_prop_daily_loss_limit_ends_the_day_not_the_attempt():
    r = prop.Rules(max_days=5, dll=1000)
    a = prop.run_attempt([[(-1000.0, -1000.0), (-1000.0, -1000.0), (-1000.0, -1000.0)]], r)
    assert a.outcome == "TIMEOUT" and a.trades == 1


def test_prop_simulate_scales_contracts():
    b = synthetic(500)
    t = engine.simulate(b, Retrace().setups(b, tick=5.0), Costs(4, 1, 5, 5))
    one = prop.simulate(t, prop.Rules(), contracts=1, method="historical")
    three = prop.simulate(t, prop.Rules(), contracts=3, method="historical")
    assert len(one.attempts) > 20
    assert three.fail_rate >= one.fail_rate                 # same trades, 3x size: never safer
    boot = prop.simulate(t, prop.Rules(), contracts=1, method="bootstrap", sims=300)
    assert len(boot.attempts) == 300 and abs(sum([boot.pass_rate, boot.fail_rate, boot.timeout_rate]) - 1) < 1e-9


def test_import_history_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "DATA_DIR", tmp_path / "store")
    src = tmp_path / "history"
    src.mkdir()
    for code, px, start in (("Z2025", 100, "2025-12-01"), ("H2026", 110, "2025-12-01")):
        idx = pd.date_range(start, periods=48, freq="h", tz="UTC")
        vol = [100] * 24 + [5] * 24 if code == "Z2025" else [5] * 24 + [100] * 24
        pd.DataFrame({"time_utc": idx.strftime("%Y-%m-%dT%H:%M:%S+00:00"), "open": px, "high": px + 1,
                      "low": px - 1, "close": px, "volume": vol}).to_csv(src / f"NKD_{code}.csv", index=False)
    (src / "contracts.json").write_text('{"NKD": {"tickSize": 5, "tickValue": 25}}')
    out = data.import_history_dir(src)
    assert len(out) == 1 and data.load_meta("NKD")["point_value"] == 5.0
    b = data.load("NKD", "1h")
    assert list(b["contract"].unique()) == ["NKD_Z2025", "NKD_H2026"]


def test_import_databento_fills_older_history(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "DATA_DIR", tmp_path)
    recent = synthetic(20)
    data.save("NKD", "1h", recent, {"tick_size": 5.0, "point_value": 5.0})
    idx = pd.date_range(recent.index[0] - pd.Timedelta(days=30), recent.index[0] + pd.Timedelta(days=5), freq="h", tz="UTC")
    f = tmp_path / "NKD_db.csv"
    pd.DataFrame({"ts_event": idx.strftime("%Y-%m-%dT%H:%M:%S.000000000Z"), "rtype": 34, "publisher_id": 1,
                  "instrument_id": [111] * 400 + [222] * (len(idx) - 400), "open": 1.0, "high": 2.0, "low": 0.5,
                  "close": 1.5, "volume": 10, "symbol": "NKD.v.0"}).to_csv(f, index=False)
    data.import_databento_csv(f, "NKD")
    b = data.load("NKD", "1h")
    assert b.index[0] == idx[0]                                  # older history added
    assert (b.loc[recent.index, "c"].to_numpy() == recent["c"].to_numpy()).all()   # newer source kept on overlap
    assert data.load_meta("NKD")["point_value"] == 5.0


def test_profile_renders(tmp_path, monkeypatch):
    from lab import profile
    monkeypatch.setattr(data, "DATA_DIR", tmp_path)
    data.save("NKD", "1h", synthetic(300), {"tick_size": 5.0, "point_value": 5.0})
    page, facts = profile.render(["NKD"])
    assert page.startswith("<!DOCTYPE html>") and "Session range by weekday" in page
    assert set(facts[0]["weekday"].index) == set(profile.DAYS) and len(facts[0]["top_days"]) == 2


def test_nkd_expansion_no_lookahead_and_controls():
    from lab.strategies.nkd_expansion import NKDExpansion
    b = synthetic(400)
    s = NKDExpansion()
    ok, probs = engine.lookahead_check(s, b, {"tick": 5.0}, samples=20)
    assert ok, probs
    w = s.setups(b, tick=5.0, bias="with")
    a = s.setups(b, tick=5.0, bias="against")
    n = s.setups(b, tick=5.0, bias="none")
    assert w and a and len(n) >= max(len(w), len(a)) * 0.9
    assert {x.tag.split()[0] for x in n} <= {"sweep", "strong"}


def synthetic_5m(days=60, seed=3):
    rng = np.random.default_rng(seed)
    idx = []
    d = pd.Timestamp("2024-01-07 18:00", tz="America/New_York")
    for _ in range(days):
        if d.weekday() in (4, 5):
            d += pd.Timedelta(days=1)
            continue
        idx += list(pd.date_range(d, periods=23 * 12, freq="5min").tz_convert("UTC"))
        d += pd.Timedelta(days=1)
    n = len(idx)
    c = 30000 + np.cumsum(rng.standard_t(3, n) * 12)
    o = np.r_[30000, c[:-1]]
    w = np.abs(rng.normal(0, 8, n))
    df = pd.DataFrame({"o": o, "h": np.maximum(o, c) + w, "l": np.minimum(o, c) - w, "c": c,
                       "v": rng.integers(5, 200, n).astype(float), "contract": "X"}, index=pd.DatetimeIndex(idx, name="t"))
    for col in ("o", "h", "l", "c"):
        df[col] = (df[col] / 5).round() * 5
    df["h"] = df[["o", "h", "c"]].max(axis=1)
    df["l"] = df[["o", "l", "c"]].min(axis=1)
    return df


def test_nkd_signature_finds_setups_without_lookahead():
    from lab.strategies.nkd_signature import NKDSignature
    b = synthetic_5m(60)
    s = NKDSignature()
    setups = s.setups(b, tick=5.0, bias="none", vol_filter=False, disp_mult=1.2)
    assert len(setups) > 5
    ok, probs = engine.lookahead_check(s, b, {"tick": 5.0, "bias": "none", "vol_filter": False, "disp_mult": 1.2}, samples=10)
    assert ok, probs
    t = engine.simulate(b, setups, Costs(0, 0, 5.0, 5.0))
    assert len(t) > 0 and (t["entry_time"] >= t["decided_at"]).all()


def test_nkd_sweep_reclaim_rules_and_no_lookahead():
    from lab.strategies.nkd_sweep_reclaim import NKDSweepReclaim
    b = synthetic_5m(80, seed=8)
    s = NKDSweepReclaim()
    st = s.setups(b, tick=5.0)
    assert len(st) > 5
    for x in st:
        risk = abs(x.info["ref_entry"] - x.stop)
        assert abs(x.target - x.info["ref_entry"]) >= 1.5 * risk - 1e-9      # room rule
        assert (x.target - x.info["ref_entry"]) * x.side > 0
    ok, probs = engine.lookahead_check(s, b, {"tick": 5.0}, samples=10)
    assert ok, probs
    assert len(s.setups(b, tick=5.0, reclaim_bars=0)) >= len(st) * 0.8         # no limit runs
