"""Tests for the desk agents: technicals (regime), news, macro call and head of desk."""
from datetime import date, datetime, timedelta, timezone

from scanner import desk, macro, news, regime
from scanner.metrics import build_thesis, compute
from tests.test_scanner import _add_sweep_of_prior_low, make_hourly


def series(fn, n=600, start=date(2024, 1, 1)):
    return [(start + timedelta(days=i), fn(i)) for i in range(n)]


# ── technicals agent ──
def test_uptrend_is_above_rising_full_long():
    reg = regime.compute_regime(series(lambda i: 100 + 0.1 * i))
    assert reg["state"] == "above_rising"
    assert regime.ceiling(reg, 1)[0] == "full"
    assert regime.ceiling(reg, -1)[0] == "half"


def test_downtrend_keeps_longs_to_a_starter():
    reg = regime.compute_regime(series(lambda i: 200 * 0.999 ** i))   # steady 0.1% a day decline
    assert reg["state"] == "below_falling" and "exhausted_down" not in reg["flags"]
    step, why = regime.ceiling(reg, 1)
    assert step == "starter" and "below a falling 200-day" in why[0]
    assert regime.ceiling(reg, -1)[0] == "full"


def test_short_history_gives_no_reading():
    assert regime.compute_regime(series(lambda i: 100.0, n=150)) is None
    step, why = regime.ceiling(None, 1)
    assert step is None and "No 200-day reading" in why[0]


def test_exhausted_run_steps_size_down():
    # Gentle uptrend, then a vertical 50-day run: top of ROC history
    pts = series(lambda i: 100 + 0.02 * i if i < 550 else 111 + 1.2 * (i - 550))
    reg = regime.compute_regime(pts)
    assert "exhausted_up" in reg["flags"]
    step, why = regime.ceiling(reg, 1)
    assert step == "half" and any("too far, too fast" in w for w in why)


def test_reset_and_fresh_cross_flags():
    flat_after_trend = series(lambda i: 100 + 0.1 * min(i, 500))   # trend, then 100 flat days
    reg = regime.compute_regime(flat_after_trend)
    assert "reset" in reg["flags"] and reg["state"] == "above_rising"
    # Long fall, then a sharp pop back over the 200-day in the last 2 sessions
    pts = series(lambda i: 200 - 0.1 * i if i < 598 else 200)
    reg2 = regime.compute_regime(pts)
    assert reg2["above"] and "fresh_cross" in reg2["flags"]
    assert regime.ceiling(reg2, 1)[0] in ("starter", "half")


def test_fred_csv_transforms():
    text = "observation_date,DEXJPUS\n2026-10-15,150.0\n2026-10-16,.\n2026-10-17,149.5\n"
    pts = regime.parse_fred_csv(text, "inv")
    assert len(pts) == 2 and abs(pts[0][1] - 1 / 150) < 1e-12
    y = regime.parse_fred_csv("d,DGS10\n2026-10-15,4.25\n", "yield")
    assert y[0][1] == 95.75


# ── news agent ──
RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Trump says new tariffs on Japan cars start next week</title><link>https://example.com/a</link>
<guid>a1</guid><pubDate>Mon, 19 Oct 2026 08:00:00 GMT</pubDate></item>
<item><title>Local bakery wins award</title><link>https://example.com/b</link><guid>b1</guid>
<pubDate>Mon, 19 Oct 2026 07:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>BOJ Governor Ueda speech on policy rate</title>
<link href="https://example.com/boj"/><id>boj-1</id><updated>2026-10-19T06:00:00Z</updated></entry></feed>"""


def test_parse_rss_and_atom():
    items = news.parse_feed(RSS)
    assert [i["id"] for i in items] == ["a1", "b1"]
    assert items[0]["t"] == datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)
    atom = news.parse_feed(ATOM)
    assert atom[0]["link"] == "https://example.com/boj" and atom[0]["id"] == "boj-1"
    assert news.parse_feed("not xml") == []


def test_tagging_maps_headlines_to_markets():
    topics, markets = news.tag("Trump says new tariffs on Japan cars start next week")
    assert "Trade / tariffs" in topics and "NKD" in markets and "NQ" in markets
    assert news.tag("Local bakery wins award") == ([], [])
    assert "6J" in news.tag("BOJ Governor Ueda speech on policy rate")[1]
    assert news.HIGH.search("Fed signals rate cut in December")


def test_recent_headlines_lookup():
    now = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)
    news._recent.clear()
    news._recent.append({"title": "x", "t": now - timedelta(minutes=10), "markets": ["NKD"], "high": True, "source": "BOJ"})
    news._recent.append({"title": "y", "t": now - timedelta(minutes=90), "markets": ["NKD"], "high": True, "source": "BOJ"})
    assert len(news.recent(["NKD"], 30, now)) == 1
    assert news.recent(["CL"], 30, now) == []
    news._recent.clear()


def test_calendar_digest_window():
    now = datetime(2026, 10, 19, 21, 0, tzinfo=timezone.utc)   # 8am Sydney Tuesday
    evs = [{"t": now + timedelta(hours=3), "title": "CPI", "currency": "USD", "impact": "High", "forecast": "0.3%", "previous": "0.4%"},
           {"t": now + timedelta(hours=30), "title": "Later", "currency": "USD", "impact": "High", "forecast": "", "previous": ""}]
    e = news.calendar_embed(evs, now)
    assert "CPI" in e["description"] and "Later" not in e["description"] and "f/c 0.3%" in e["description"]
    assert news.digest_due(now, {"digest": ""})
    assert not news.digest_due(now, {"digest": "2026-10-20"})


# ── macro call ──
def test_macro_call_freshness_and_lookup():
    now = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)
    call = {"generated_at": "2026-10-19T21:30:00Z", "markets": {"NQ": {"bias": "bearish", "conviction": 2, "why": "w"},
                                                               "ES": {"bias": "sideways"}}}
    assert macro._fresh(call, now) is call
    assert macro._fresh(call, now + timedelta(hours=40)) is None
    assert macro.for_market(call, "NQ")["bias"] == "bearish"
    assert macro.for_market(call, "ES") is None          # unknown bias is ignored
    assert macro.for_market(None, "NQ") is None


# ── head of desk ──
def _idea():
    bars, _ = _add_sweep_of_prior_low(make_hourly(seed=3))
    r = compute("NQ", bars)
    r.thesis = build_thesis(r)
    # A clean long: stop half an ATR below, target 2.5R above
    e = r.last
    stop = e - 0.5 * r.atr
    r.thesis.update({"dir": 1, "entry": e, "stop": stop, "target": e + 2.5 * (e - stop),
                     "target_name": "test liquidity", "rr": 2.5})
    r.last_t = bars[-1]["t"]
    now = bars[-1]["t"] + timedelta(minutes=20)
    gates = {"positioning": {"status": "pass", "text": "ok"},
             "options": {"status": "n/a", "text": "", "structure": ""},
             "catalyst": {"status": "pass", "events": [], "text": "Nothing high-impact."}}
    return r, gates, now


def _status(v, name):
    return next(c["status"] for c in v["checks"] if c["name"] == name)


def test_clean_idea_is_verified():
    r, g, now = _idea()
    reg = regime.compute_regime(series(lambda i: 100 + 0.1 * i))
    v = desk.verify(r, g, reg, None, now)
    assert r.thesis["dir"] == 1
    assert v["verdict"] == "verified", v["reason"]
    assert v["ceiling"] == "full" and _status(v, "Macro call") == "info"


def test_macro_disagreement_rejects():
    r, g, now = _idea()
    call = {"generated_at": now.isoformat(), "markets": {"NQ": {"bias": "bearish", "conviction": 3}}}
    v = desk.verify(r, g, None, call, now)
    assert v["verdict"] == "rejected" and "Macro call" in v["reason"]


def test_event_too_close_rejects_and_stale_data_rejects():
    r, g, now = _idea()
    g["catalyst"] = {"status": "warn", "events": [{"t": now + timedelta(minutes=20), "name": "CPI", "currency": "USD"}],
                     "text": "CPI inside the window"}
    v = desk.verify(r, g, None, None, now)
    assert v["verdict"] == "rejected" and "CPI" in v["reason"]
    r2, g2, now2 = _idea()
    v2 = desk.verify(r2, g2, None, None, now2 + timedelta(hours=5))
    assert _status(v2, "Data") == "fail"


def test_bad_levels_and_conflict_reject():
    r, g, now = _idea()
    r.thesis = {**r.thesis, "stop": r.thesis["entry"] + 1}          # stop on the wrong side of a long
    assert desk.verify(r, g, None, None, now)["verdict"] == "rejected"
    r, g, now = _idea()
    v = desk.verify(r, g, None, None, now, verified_today={"NQ:-1": "earlier"})
    assert _status(v, "Conflict") == "fail"


def test_counter_trend_long_is_flagged_not_killed():
    r, g, now = _idea()
    reg = regime.compute_regime(series(lambda i: 200 - 0.1 * i))
    v = desk.verify(r, g, reg, None, now)
    assert v["verdict"] == "verified" and v["ceiling"] == "starter"
    assert _status(v, "Trend regime") == "warn"
