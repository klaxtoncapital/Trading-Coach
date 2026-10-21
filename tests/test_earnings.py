"""Tests for the earnings agent and how earnings reach the catalyst gate and the digest."""
import json
from datetime import date, datetime, timedelta, timezone

from scanner import config, earnings, gates, news

NASDAQ_SAMPLE = {"data": {"asOf": "Wed, Oct 21, 2026", "rows": [
    {"symbol": "META", "name": "Meta Platforms, Inc.", "time": "time-after-hours"},
    {"symbol": "TSLA", "name": "Tesla, Inc.", "time": "time-after-hours"},
    {"symbol": "PG", "name": "Procter & Gamble", "time": "time-pre-market"},   # not on the watchlist
    {"symbol": "NVDA", "name": "NVIDIA", "time": "time-not-supplied"},
]}}
MKT = {m.root: m for m in config.MARKETS}


def test_parse_keeps_watchlist_and_maps_times():
    evs = earnings.parse_nasdaq(NASDAQ_SAMPLE, date(2026, 10, 21))
    syms = {e["symbol"] for e in evs}
    assert syms == {"META", "TSLA", "NVDA"}
    meta = next(e for e in evs if e["symbol"] == "META")
    # 4:05pm New York on 21 Oct (EDT) = 20:05 UTC = 7:05am Sydney on the 22nd
    assert meta["t"] == datetime(2026, 10, 21, 20, 5, tzinfo=timezone.utc)
    assert meta["roots"] == ("NQ", "ES") and meta["currency"] == "earnings"
    nvda = next(e for e in evs if e["symbol"] == "NVDA")
    assert "not confirmed" in nvda["when"]


def test_empty_days_are_safe():
    assert earnings.parse_nasdaq({"data": {"rows": None}}, date(2026, 10, 24)) == []
    assert earnings.parse_nasdaq({}, date(2026, 10, 24)) == []


def test_japan_file(tmp_path):
    f = tmp_path / "jp.json"
    f.write_text(json.dumps([
        {"symbol": "6857", "name": "Advantest", "date": "2026-10-28", "session": "after-close"},
        {"symbol": "9984", "name": "SoftBank Group", "date": "2026-11-10", "time": "15:00"},
        {"symbol": "x", "name": "bad row"},
    ]))
    evs = earnings.jp_events(f)
    assert len(evs) == 2
    adv = evs[0]
    # 15:30 Tokyo = 06:30 UTC = 5:30pm Sydney (AEDT)
    assert adv["t"] == datetime(2026, 10, 28, 6, 30, tzinfo=timezone.utc) and adv["roots"] == ("NKD",)
    assert evs[1]["t"] == datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)
    assert earnings.jp_events(tmp_path / "missing.json") == []


def test_repo_japan_file_is_valid():
    evs = earnings.jp_events()
    assert evs and all(e["roots"] == ("NKD",) for e in evs)


def test_gate_warns_only_the_markets_earnings_move():
    now = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)
    cal = earnings.parse_nasdaq(NASDAQ_SAMPLE, date(2026, 10, 21))
    nq = gates.catalyst_gate(MKT["NQ"], cal, now, 24)
    assert nq["status"] == "warn" and "META earnings (after the close)" in nq["text"]
    # A USD-currency market that earnings don't move stays clean
    assert gates.catalyst_gate(MKT["ZN"], cal, now, 24)["status"] == "pass"
    assert gates.catalyst_gate(MKT["NKD"], cal, now, 24)["status"] == "pass"


def test_macro_events_still_work_alongside_earnings():
    now = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
    cal = [{"t": now + timedelta(hours=2), "name": "Unemployment Claims", "currency": "USD"}]
    g = gates.catalyst_gate(MKT["ZN"], cal, now, 24)
    assert g["status"] == "warn" and "Unemployment Claims (USD)" in g["text"]


def test_digest_lists_earnings():
    now = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)
    evs = earnings.parse_nasdaq(NASDAQ_SAMPLE, date(2026, 10, 21))
    later = earnings.parse_nasdaq({"data": {"rows": [{"symbol": "MSFT", "name": "Microsoft", "time": "time-after-hours"}]}},
                                  date(2026, 10, 27))
    emb = news.calendar_embed([], now, evs + later)
    d = emb["description"]
    assert "💼 Earnings" in d and "Meta Platforms" in d and "moves NQ/ES" in d
    assert "Later this week: MSFT" in d
    assert "No high or medium impact events" in d   # macro line still shown
    assert "💼" not in news.calendar_embed([], now)["description"]
