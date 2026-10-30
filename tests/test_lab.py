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
