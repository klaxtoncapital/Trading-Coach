"""The one #general morning message: Monday-range rule, card selection, size limits (no network)."""
import asyncio
from datetime import date, datetime, timezone

from scanner import config, daily_setups as ds, gates, macro, morning
from scanner import four_charts as fc
from tests.test_daily_setups import make_days, base, with_swing_high_then_sfp


def week(rows, start=date(2026, 10, 19)):
    """Prior quiet weeks then the given Mon.. rows, starting on Monday 19 Oct."""
    return make_days(base(10) + rows, start=date(2026, 10, 5))


def test_monday_range_wed_watch_after_tuesday_closes_above():
    days = week([(100, 102, 99, 101), (101, 104, 100.5, 103.5)])     # Mon 99-102, Tue closes 103.5
    mr = morning.monday_range(days)
    assert mr["short"] == "Wed watch long" and "Tuesday's high (104.00)" in mr["text"]


def test_monday_range_sweep_back_inside_is_no_call():
    mon = (100, 102, 99, 101)
    # Tuesday pokes above 102 in its first hour then closes back inside
    days = week([mon, (101, 102.5, 100, 100.5), (100.5, 101.5, 99.5, 101)])
    mr = morning.monday_range(days)
    assert mr["short"] == "No call"


def test_monday_range_setup_after_hold_and_wednesday_pullback():
    days = week([(100, 102, 99, 101), (101, 106, 101, 105), (105, 106.5, 101.5, 102)])
    days[-2].bars += [{**days[-2].bars[-1], "c": 105.5}] * 3              # 4 hourly closes above Monday's high
    mr = morning.monday_range(days)
    assert mr["short"] == "Setup long"


def _fake(monkeypatch, found, call):
    async def scan(px, http, now=None):
        ds.LAST.clear()
        ds.LAST.update({"days": {}, "em": {"NKD": ds.expected_move(50000, rv20=20)}, "macro": call})
        return date(2026, 10, 27), found, 36
    async def cot(http, m):
        return {"who": "Managed money", "net": -46813, "pct3y": 1, "report_date": "2026-10-20", "chg4w": -4000}
    async def fetch_all(http, today):
        return {}
    monkeypatch.setattr(ds, "scan", scan)
    monkeypatch.setattr(gates, "cot", cot)
    monkeypatch.setattr(fc, "fetch_all", fetch_all)
    monkeypatch.setattr(fc, "read", lambda d: {"curve": None, "front": None, "us10y": None, "breadth": None,
                                               "ai": None, "soxx": None})


CALL = {"trading_day": "2026-10-28", "generated_at": "2026-10-28T09:42:00+11:00", "summary": "Risk-off overnight.",
        "events_next_24h": ["Thu 5:00am USD FOMC"], "markets": {
            "NKD": {"bias": "bearish", "conviction": 2, "why": "Yen bid.", "risk": "BoJ"},
            "CL": {"bias": "bullish", "conviction": 2, "why": "Supply.", "risk": "OPEC"}}}


def test_one_message_nkd_first_then_setups(monkeypatch):
    days = with_swing_high_then_sfp()
    st = ds.analyse("CL", days)[0]
    st["conf"] = ds.confluence(st, CALL, None, None)
    st["rank"] = st["score"]
    he = {**st, "root": "HE"}
    _fake(monkeypatch, [(st, config.MARKETS_BY_ROOT["CL"]), (he, config.MARKETS_BY_ROOT["HE"])], CALL)
    now = datetime(2026, 10, 27, 22, 50, tzinfo=timezone.utc)
    embeds = asyncio.run(morning.build(object(), None, now))
    titles = [e["title"] for e in embeds]
    assert titles[0].startswith("☀️ Klaxton Desk · Wed 28 Oct")
    assert titles[1].startswith("NKD") and "No daily setup today." in embeds[1]["description"]
    assert "🧭 **Macro** 🔴 Bearish ●●○" in embeds[1]["description"]
    assert any(t.startswith("CL") for t in titles) and any(t.startswith("HE") for t in titles)
    he_card = next(e for e in embeds if e["title"].startswith("HE"))
    assert "Short hours" in he_card["description"] and "Crowded short" in he_card["description"]
    assert "Risk-off overnight." in embeds[0]["description"] and "FOMC" in embeds[0]["description"]
    assert len(embeds) <= 10 and sum(morning._size(e) for e in embeds) <= 6000


def test_many_setups_fit_one_message(monkeypatch):
    days = with_swing_high_then_sfp()
    st = ds.analyse("CL", days)[0]
    st["conf"] = {"lines": [], "adj": 0}
    roots = [m.root for m in config.MARKETS if m.root != "NKD"][:20]
    _fake(monkeypatch, [({**st, "root": r, "score": 90 - i}, config.MARKETS_BY_ROOT[r]) for i, r in enumerate(roots)], CALL)
    embeds = asyncio.run(morning.build(object(), None, datetime(2026, 10, 27, 22, 50, tzinfo=timezone.utc)))
    assert len(embeds) <= 10 and sum(morning._size(e) for e in embeds) <= 6000
    assert "Also in play" in embeds[0]["description"]
    head = embeds[0]["description"]
    assert all(f"**{r}**" in head or any(e['title'].startswith(r + " ") for e in embeds[1:]) for r in roots)


def test_waits_for_todays_macro_call_until_10_10():
    utc = timezone.utc
    assert not morning.macro_ready({"trading_day": "2026-10-27"}, datetime(2026, 10, 27, 22, 50, tzinfo=utc))
    assert morning.macro_ready(CALL, datetime(2026, 10, 27, 22, 50, tzinfo=utc))
    assert morning.macro_ready(None, datetime(2026, 10, 27, 23, 11, tzinfo=utc))


def test_friday_profile_classic_expansion():
    rows = [(100, 101, 99, 100.5), (100.5, 103, 100, 102.8), (102.8, 105, 102.5, 104.9), (104.9, 107, 104.5, 106.8)]
    days = make_days(base(10) + rows, start=date(2026, 10, 5))
    assert "classic expansion" in morning.friday_profile("NKD", days, 2.0)
