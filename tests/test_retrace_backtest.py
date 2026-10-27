"""Retrace Rule backtest: the Python copy must follow the Pine study's rules."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from scanner import retrace_backtest as rb

NY = ZoneInfo("America/New_York")


def session(d: date, bars: list[tuple[float, float, float, float]]) -> list[dict]:
    """Hourly bars for the trading day `d`, starting at 6PM New York the evening before."""
    start = datetime(d.year, d.month, d.day, 18, tzinfo=NY) - timedelta(days=1)
    return [{"t": (start + timedelta(hours=i)).astimezone(timezone.utc),
             "o": o, "h": h, "l": l, "c": c, "v": 100.0} for i, (o, h, l, c) in enumerate(bars)]


def flat(d: date, px: float) -> list[dict]:
    return session(d, [(px, px + 1, px - 1, px)] * 3)


TICKS = rb.Settings(mode="ticks", stop=50, target=185)   # tick = 5 below, like NKD


def run(hourly):
    return rb.backtest_market("NKD", hourly, 5.0, 5.0, TICKS)


def test_direction_rules():
    assert rb.direction(105, 100, 110, 90, 100, "close") == 1
    assert rb.direction(95, 100, 110, 90, 100, "close") == -1
    assert rb.direction(105, 100, 110, 90, 100, "lowhigh") == 0
    assert rb.direction(111, 100, 110, 90, 100, "lowhigh") == 1
    assert rb.direction(105, 100, 110, 90, 100, "half") == 1       # above open and above the 50%


def test_bull_wednesday_hits_target():
    # Mon 26 Oct 2026 closes at 1000, Tue 27 at 1100 -> Wed 28 is a bull day.
    h = flat(date(2026, 10, 26), 1000) + flat(date(2026, 10, 27), 1100)
    h += session(date(2026, 10, 28), [(1100, 1110, 1050, 1100),      # 50 against: stop is 250, still alive
                                      (1100, 2030, 1090, 2000),      # +930 >= 925 target
                                      (2000, 2010, 1990, 2000)])
    t = run(h)
    assert len(t) == 1 and t[0]["weekday"] == "WED" and t[0]["side"] == "Bull"
    assert t[0]["result"] == "target" and t[0]["r"] == 3.7 and t[0]["usd"] == 4625


def test_same_bar_stop_and_target_is_a_loss():
    h = flat(date(2026, 10, 26), 1000) + flat(date(2026, 10, 27), 1100)
    h += session(date(2026, 10, 28), [(1100, 1105, 1095, 1100),
                                      (1100, 2100, 800, 1500)])      # both sides in one bar
    t = run(h)
    assert t[0]["result"] == "stop" and t[0]["r"] == -1 and t[0]["usd"] == -1250


def test_neither_closes_at_day_close():
    h = flat(date(2026, 10, 26), 1000) + flat(date(2026, 10, 27), 1100)
    h += session(date(2026, 10, 28), [(1100, 1200, 1000, 1150), (1150, 1300, 1100, 1250)])
    t = run(h)
    assert t[0]["result"] == "close" and t[0]["pnl_pts"] == 150 and t[0]["r"] == 0.6


def test_first_bar_target_is_held_to_close_like_pine():
    # Pine enters on the first bar and can't credit a target on the entry bar: the trade runs to the close.
    h = flat(date(2026, 10, 26), 1000) + flat(date(2026, 10, 27), 1100)
    h += session(date(2026, 10, 28), [(1100, 2100, 1095, 2000), (2000, 2050, 1900, 1950)])
    t = run(h)
    assert t[0]["result"] == "close" and t[0]["target_hit"] and t[0]["pnl_pts"] == 850


def test_bear_thursday_and_monday_uses_friday_vs_thursday():
    h = flat(date(2026, 10, 21), 1200) + flat(date(2026, 10, 22), 1100)        # Wed > Thu close
    h += session(date(2026, 10, 23), [(1100, 1110, 1090, 1150)])               # Thu closed below Wed -> bear Friday
    h += session(date(2026, 10, 26), [(1150, 1160, 1140, 1150)])               # Monday: Fri 1150 > Thu 1100 -> bull
    t = run(h)
    assert [(x["weekday"], x["side"]) for x in t] == [("FRI", "Bear"), ("MON", "Bull")]


def test_weekend_bars_and_since_filter():
    h = flat(date(2026, 10, 26), 1000) + flat(date(2026, 10, 27), 1100) + flat(date(2026, 10, 28), 1200)
    assert rb.backtest_market("NKD", h, 5.0, 5.0, TICKS, since=date(2026, 10, 29)) == []


def test_atr_mode_waits_for_history_and_scales():
    h = []
    d, px = date(2026, 1, 5), 1000.0
    for _ in range(30):
        if d.weekday() < 5:
            h += session(d, [(px, px + 50, px - 50, px + 10)])      # 100-point range, +10 a day
            px += 10
        d += timedelta(days=1)
    t = rb.backtest_market("X", h, 1.0, 1.0, rb.Settings(mode="atr"))
    assert t and all(x["date"] >= "2026-02-02" for x in t)           # needs 20 prior days
    assert abs(t[0]["stop_dist"] - 22) < 0.01                        # 0.22 x ATR of 100


def test_summary_streak_drawdown_and_every_year_flag():
    trades = []
    for y, rs in ((2025, [1, -1, -1, -1, 2] * 2), (2026, [3, -1] * 5)):
        for i, r in enumerate(rs):
            trades.append({"root": "NKD", "date": f"{y}-01-{i + 1:02d}", "year": y, "weekday": "MON",
                           "side": "Bull", "target_hit": r > 0, "r": r, "usd": r * 1250})
    row = rb.summarise(trades)[0]
    assert row["n"] == 20 and row["worst_streak"] == 3 and row["max_dd_r"] == -3
    assert row["years"] == {2025: (10, 0), 2026: (10, 10)}
    assert row["every_year_positive"] is False                       # 2025 nets to zero
