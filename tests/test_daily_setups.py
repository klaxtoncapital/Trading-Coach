"""Daily setups agent: SFP detection, weekly frameworks, timing and the backtest (synthetic data, no network)."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from scanner import daily_setups as ds
from scanner.metrics import Day

NY = ZoneInfo("America/New_York")


def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def make_days(ohlc: list[tuple], start: date = date(2026, 8, 3)) -> list[Day]:
    """Days from (o, h, l, c). Hourly bars go open -> first extreme -> second extreme -> close,
    visiting the low first on up days and the high first on down days."""
    out = []
    for d, (o, h, l, c) in zip(weekdays(start, len(ohlc)), ohlc):
        t0 = datetime(d.year, d.month, d.day, tzinfo=NY) - timedelta(hours=6)   # 18:00 the evening before
        path = [o, l, h, c] if c >= o else [o, h, l, c]
        bars = []
        for i in range(1, 4):
            a, b = path[i - 1], path[i]
            bars.append({"t": (t0 + timedelta(hours=i)).astimezone(timezone.utc), "o": a,
                         "h": max(a, b), "l": min(a, b), "c": b, "v": 100})
        out.append(Day(d, o, h, l, c, 300, bars))
    return out


def base(n: int = 26, mid: float = 100.0) -> list[tuple]:
    """Quiet days, range 2, alternating closes, no swings."""
    return [(mid, mid + 1, mid - 1, mid + (0.2 if i % 2 else -0.2)) for i in range(n)]


def with_swing_high_then_sfp():
    rows = base(20)
    rows += [(100, 104, 99.5, 103.5),     # rally
             (103.5, 110, 103, 109),      # swing high 110
             (109, 109.5, 104, 105),      # leg down
             (105, 106, 101, 102),        # leg low 101
             (102, 107, 101.5, 106.5),
             (106.5, 108.5, 105.5, 108)]
    rows += [(109.5, 111, 106.5, 107)]    # SFP: takes 110, closes 107
    return make_days(rows)


def test_bearish_sfp_found_with_leg_low_target():
    days = with_swing_high_then_sfp()
    a = ds.atr(days, 20)
    s = ds.find_sfp(days, a)
    assert s and s["dir"] == -1
    assert s["level"]["price"] == 110
    assert s["target"][0] == 101          # the low connected to the swept high
    assert s["stop"] > 111
    assert s["rr"] > 1


def test_no_sfp_when_it_closes_through_the_level():
    days = with_swing_high_then_sfp()
    last = days[-1]
    days[-1] = Day(last.d, 109.5, 112, 109, 111.5, 300, last.bars)
    assert ds.find_sfp(days, ds.atr(days, 20)) is None


def test_bullish_sfp_mirror():
    rows = base(20)
    rows += [(100, 100.5, 96, 96.5), (96.5, 97, 90, 91), (91, 96, 90.5, 95), (95, 99, 94, 98.5),
             (98.5, 98.8, 93.5, 94), (94, 94.5, 92, 92.5)]
    rows += [(92.5, 93, 89, 93.2)]         # takes 90, closes back above
    days = make_days(rows)
    s = ds.find_sfp(days, ds.atr(days, 20))
    assert s and s["dir"] == 1 and s["level"]["price"] == 90
    assert s["target"][0] == 99           # the high connected to the swept low


def test_thursday_sfp_of_the_week_high_is_the_thursday_reversal():
    # 20 base days from Mon 3 Aug end on Fri 28 Aug; the week of 31 Aug is Mon-Thu below
    rows = base(20)
    rows += [(100, 101.5, 99, 101), (101, 103, 100.5, 102.8), (102.8, 104, 102, 103.5),  # Mon-Wed, week high 104
             (103.5, 104.6, 101.5, 101.8)]                                                # Thu takes 104, closes back
    days = make_days(rows)
    assert days[-1].d.weekday() == 3
    out = ds.analyse("CL", days)
    sfp = next(x for x in out if x["kind"] == "sfp")
    assert sfp["dir"] == -1
    assert sfp["framework"].startswith("Thursday reversal")


def test_wednesday_close_below_half_of_tuesday_flags_thursday_continuation():
    rows = base(20)
    rows += [(100, 100.6, 99.6, 100.2), (100.2, 102, 99.8, 101.5), (101.5, 101.6, 99.5, 99.9)]
    days = make_days(rows)
    assert days[-1].d.weekday() == 2
    ctx = ds.week_context(days, ds.atr(days, 20))
    names = [f["name"] for f in ctx["frameworks"]]
    assert "Thursday continuation day" in names
    assert next(f for f in ctx["frameworks"] if f["name"] == "Thursday continuation day")["dir"] == -1


def test_small_monday_and_tuesday_close_outside_flags_wednesday():
    rows = base(20) + [(100, 100.5, 99.7, 100.2), (100.2, 102.5, 100, 102.2)]
    days = make_days(rows)
    ctx = ds.week_context(days, ds.atr(days, 20))
    f = next(f for f in ctx["frameworks"] if f["name"] == "Wednesday continuation day")
    assert f["dir"] == 1


def test_completed_days_drops_the_day_in_progress():
    days = make_days(base(5))
    last = days[-1].d
    during = datetime(last.year, last.month, last.day, 12, 0, tzinfo=NY)
    after = datetime(last.year, last.month, last.day, 17, 20, tzinfo=NY)
    assert ds.completed_days(days, during)[-1].d < last
    assert ds.completed_days(days, after)[-1].d == last


def test_due_after_the_close_once_with_catch_up():
    tue = date(2026, 10, 27)
    at = lambda h, m, d=tue: datetime(d.year, d.month, d.day, h, m, tzinfo=NY)
    assert ds.due(at(17, 0), None) is None
    assert ds.due(at(17, 20), None) == tue
    assert ds.due(at(17, 20), tue.isoformat()) is None
    assert ds.due(at(4, 0, date(2026, 10, 28)), None) == tue          # catch-up within 12h
    assert ds.due(at(10, 0, date(2026, 10, 28)), None) is None        # too late
    assert ds.due(at(2, 0, date(2026, 10, 31)), None) == date(2026, 10, 30)  # Friday's close on Saturday


def test_simulate_target_and_stop_and_same_bar():
    days = with_swing_high_then_sfp()
    s = ds.find_sfp(days, ds.atr(days, 20))
    down = make_days([(107, 107.5, 100, 100.5)], start=days[-1].d + timedelta(days=3))
    r = ds.simulate(s, down)
    assert r["out"] == "target" and r["r"] > 0
    up = make_days([(107, 112, 106.5, 111.5)], start=days[-1].d + timedelta(days=3))
    assert ds.simulate(s, up)["out"] == "stop"
    both = make_days([(107, 112, 100, 101)], start=days[-1].d + timedelta(days=3))
    both[0].bars = [{"t": both[0].bars[0]["t"], "o": 107, "h": 112, "l": 100, "c": 101, "v": 1}]
    assert ds.simulate(s, both)["out"] == "stop"


def test_backtest_and_summary_run():
    days = with_swing_high_then_sfp() + make_days(
        [(107, 107.5, 100, 100.5)] + base(6, 100), start=date(2026, 9, 14))
    trades = ds.backtest_market("CL", days)
    assert trades and trades[0]["dir"] == "short"
    assert {x["mode"] for x in trades} == {"open", "ote"}
    rows = ds.summarise(trades)
    assert rows[0]["n"] == sum(1 for x in trades if x["out"] != "no fill")


def test_embeds_build():
    days = with_swing_high_then_sfp()
    setups = ds.analyse("CL", days)
    from scanner import config
    m = config.MARKETS_BY_ROOT["CL"]
    for s in setups:
        s["conf"] = ds.confluence(s, None, None, None) if s.get("dir") else {}
        s["rank"] = s["score"]
    e = ds.detail_embed(setups[0], m)
    assert "SFP" in e["description"] and e["fields"]
    summ = ds.summary_embed([(s, m) for s in setups], days[-1].d, 36)
    assert "CL" in summ["description"]


def test_ote_entry_fills_in_the_wick_and_improves_r():
    days = with_swing_high_then_sfp()
    s = ds.find_sfp(days, ds.atr(days, 20))
    assert s["ote"] > max(days[-1].o, days[-1].c) and s["ote_rr"] > s["rr"]
    nxt = days[-1].d + timedelta(days=3)
    # Trap Door: pops above the open into the wick, then drops to target
    trap = make_days([(107, 110.5, 100, 100.5)], start=nxt)
    r = ds.simulate(s, trap, mode="ote")
    assert r["out"] == "target" and r["r"] > 3
    # Never comes back up: no fill, no trade
    assert ds.simulate(s, make_days([(107, 107.2, 100, 100.5)], start=nxt), mode="ote")["out"] == "no fill"


def test_bias_embed_high_conviction_and_watch():
    from scanner import config
    days = with_swing_high_then_sfp()
    m = config.MARKETS_BY_ROOT["CL"]
    st = ds.analyse("CL", days)[0]
    st["conf"] = {"lines": ["✅ Macro call bearish (conviction 2)"], "adj": 10}
    st["rank"] = st["score"] + 10
    e = ds.bias_embed(days[-1].d, [(st, m)], 36)
    assert "macro call agree**: watch these first\n**CL**" in e["description"] and e["color"] == ds.RED
    st["conf"] = {"lines": ["❌ Macro call bullish: against this setup"], "adj": -10}
    e = ds.bias_embed(days[-1].d, [(st, m)], 36)
    assert "macro doesn't align: could be a squeeze" in e["description"]
    assert "macro call agree**: watch these first\nNone today." in e["description"]
    assert "No daily data" in ds.bias_embed(None, [], 0)["description"]


def test_four_charts_waits_for_the_daily_close():
    from scanner import four_charts as fc
    utc = timezone.utc
    # Tue 27 Oct 10:10am Sydney: after 10:05 and after the close, due
    assert fc.due(datetime(2026, 10, 26, 23, 10, tzinfo=utc), None)
    # Tue 27 Oct 9:30am Sydney: before 10:05, not yet
    assert not fc.due(datetime(2026, 10, 26, 22, 30, tzinfo=utc), None)
    # Never twice the same day
    assert not fc.due(datetime(2026, 10, 26, 23, 10, tzinfo=utc), "2026-10-27")


def test_no_cap_and_swings_in_play():
    from scanner import config
    days = with_swing_high_then_sfp()
    st = ds.analyse("CL", days)[0]
    st["conf"] = {"lines": [], "adj": 0}
    st["rank"] = st["score"]
    roots = [m.root for m in config.MARKETS][:12]
    found = [({**st, "root": r}, config.MARKETS_BY_ROOT[r]) for r in roots]
    embeds = ds.bias_embeds(days[-1].d, found, 36)
    text = "\n".join(e["description"] for e in embeds)
    assert all(f"**{r}**" in text for r in roots)          # every market listed, no top-4 cap
    assert all(len(e["description"]) <= 4000 for e in embeds)


def test_in_play_finds_nearby_untouched_swing_high():
    rows = base(20) + [(100, 104, 99.5, 103.5), (103.5, 106, 103, 105.5), (105.5, 105.8, 103.5, 104),
                       (104, 104.6, 103, 103.4), (103.4, 105.2, 103.2, 105)]
    days = make_days(rows)
    a = ds.atr(days, 20)
    sw = ds.in_play("GC", days, a)
    hi = next(x for x in sw if x["side"] == "high")
    assert hi["level"]["price"] == 106 and hi["dist_atr"] <= ds.IN_PLAY_ATR


def test_size_field_sizes_every_account_and_never_blocks():
    from types import SimpleNamespace
    from scanner import config, discord_out, symbols
    m = config.MARKETS_BY_ROOT["GC"]
    gc = {"tickSize": 0.1, "tickValue": 10.0}      # $100 a point
    mgc = {"tickSize": 0.1, "tickValue": 1.0}      # $10 a point
    r = SimpleNamespace(atr=76, contract=gc, micro_contracts=[("MGC", mgc)])
    t = {"entry": 4037, "stop": 3993}              # 44 points: GC $4,400, MGC $440
    old = symbols.RISK_ACCOUNTS
    symbols.RISK_ACCOUNTS = [("Topstep", 1000), ("Tradeify", 275)]
    try:
        txt = discord_out._size_field(m, r, t)
    finally:
        symbols.RISK_ACCOUNTS = old
    assert "**Topstep $1,000:** 2 MGC" in txt
    assert "**Tradeify $275:** too wide" in txt
    assert "lower timeframe" in txt


def test_every_sfp_listed_any_score_and_36_fit():
    from scanner import config
    days = with_swing_high_then_sfp()
    base_st = ds.analyse("CL", days)[0]
    found = []
    for i, m in enumerate(config.MARKETS):
        st = dict(base_st, score=20 + i, conf={"lines": ["❌ Macro call bullish: against this setup"] if i % 3 == 0
                                                else ["✅ Macro call bearish (conviction 1)"] if i % 3 == 1 else []})
        found.append((st, m))
    embeds = ds.bias_embeds(days[-1].d, found, 36)
    text = "\n".join(e["description"] for e in embeds)
    assert "**36 daily SFPs**" in text
    assert all(f"**{m.root}**" in text for m in config.MARKETS)        # all 36, including score 20
    assert text.count("could be a squeeze") == 12
    assert all(len(e["description"]) <= 4000 for e in embeds) and len(embeds) >= 2
