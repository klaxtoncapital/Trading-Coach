"""Four Charts bias post (synthetic data, no network)."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from scanner import four_charts as fc

SYD = ZoneInfo("Australia/Sydney")


def series(n=150, start=100.0, drift=0.0, tail=0.0, tail_len=10, wiggle=0.3):
    """Mild zig-zag with a drift, plus an extra move over the last tail_len sessions."""
    out, x, d = [], start, date(2026, 4, 1)
    for i in range(n):
        x += drift + (wiggle if i % 2 else -wiggle) + (tail / tail_len if i >= n - tail_len else 0)
        out.append((d + timedelta(days=i), x))
    return out


def data(**kw):
    base = {
        "front": series(start=80), "back": series(start=70), "us10y": series(start=4.2, wiggle=0.02),
        "RSP": series(start=180), "SPY": series(start=600, wiggle=0.6), "MAGS": series(start=60, wiggle=0.2),
        "SOXX": series(start=250, wiggle=0.5), "_back_symbol": "CLZ27.NYM",
    }
    base.update(kw)
    return base


def test_red_dec_symbol_is_next_december():
    assert fc.red_dec_symbol(date(2026, 10, 23)) == "CLZ27.NYM"


def test_parse_yahoo_and_stooq():
    y = {"chart": {"result": [{"timestamp": [1792540800, 1792627200], "indicators": {"quote": [{"close": [100.5, None]}]}}]}}
    assert fc.parse_yahoo(y) == [(datetime.fromtimestamp(1792540800, timezone.utc).date(), 100.5)]
    assert fc.parse_yahoo({}) == []
    assert fc.parse_stooq("Date,Open,High,Low,Close,Volume\n2026-10-20,1,2,0.5,1.5,10\n") == [(date(2026, 10, 20), 1.5)]


def test_quiet_market_is_low_conviction():
    r = fc.read(data())
    e = fc.embed(r, datetime(2026, 10, 23, 8, 40, tzinfo=SYD))
    assert "conviction LOW" in e["title"]
    assert "boxed in" in e["description"]


def test_aligned_rotation_day_is_high_conviction():
    d = data(
        front=series(start=80, tail=8), back=series(start=70, tail=2),        # curve tightening, front up
        RSP=series(start=180, tail=12),                                         # breadth widening
        MAGS=series(start=60, tail=-6), SOXX=series(start=250, tail=40),       # chips leading, AI rising
    )
    r = fc.read(d)
    assert r["curve"]["dir"] == 1 and r["breadth"]["dir"] == 1 and r["ai"]["dir"] == -1 and r["soxx"]["dir"] == 1
    leans = {m: fc.lean(v) for m, v in fc.votes(r).items()}
    assert leans["CL"]["conv"] == "HIGH" and leans["CL"]["dir"] == 1
    assert leans["NKD"]["conv"] == "HIGH" and leans["NKD"]["dir"] == 1
    assert leans["RTY"]["dir"] == 1
    e = fc.embed(r, datetime(2026, 10, 23, 8, 40, tzinfo=SYD))
    assert "conviction HIGH" in e["title"] and "Best aligned" in e["description"]


def test_yield_breakout_votes_against_nq_and_gold():
    r = fc.read(data(us10y=series(start=4.2, wiggle=0.02, tail=0.4, tail_len=1)))
    assert r["us10y"]["state"] == "up"
    v = fc.votes(r)
    assert all(s == -1 for s, _ in v["NQ"]) and v["GC"] == [(-1, "10Y breaking up")]


def test_post_once_per_sydney_weekday_morning():
    fri = datetime(2026, 10, 23, 10, 10, tzinfo=SYD)
    assert fc.due(fri, None)
    assert not fc.due(fri, "2026-10-23")
    assert not fc.due(datetime(2026, 10, 23, 9, 50, tzinfo=SYD), None)
    assert not fc.due(datetime(2026, 10, 24, 10, 10, tzinfo=SYD), None)   # Saturday
