"""ACD agent, ACD backtest helpers and the COT extremes board (synthetic data, no network)."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from scanner import acd, acd_backtest, cot_board

NY = ZoneInfo("America/New_York")
SYD = ZoneInfo("Australia/Sydney")


def minute_bars(start: datetime, path: list[float], spread: float = 0.05) -> list[dict]:
    """One 1-min bar per price in path: open = previous close."""
    out, prev = [], path[0]
    for i, c in enumerate(path):
        t = (start + timedelta(minutes=i)).astimezone(timezone.utc)
        out.append({"t": t, "o": prev, "h": max(prev, c) + spread, "l": min(prev, c) - spread, "c": c, "v": 100})
        prev = c
    return out


def window(d=date(2026, 10, 21), root="GC"):
    return acd.session_window(root, d)


def test_session_window_uses_exchange_time_and_topstep_flat():
    start, end, flat = window()
    assert start.astimezone(NY).strftime("%H:%M") == "08:20"
    assert (end - start) == timedelta(minutes=acd.OR_MIN)
    assert flat.astimezone(ZoneInfo("America/Chicago")).strftime("%H:%M") == "15:05"
    s, _, f = acd.session_window("NKD", date(2026, 10, 21))
    assert s.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%H:%M") == "09:00"


def test_a_up_confirms_after_holding_half_the_or():
    start, end, flat = window()
    path = [100 + (i % 3) * 0.3 for i in range(15)]       # OR 100 to 100.6 (+spread)
    path += [100.7, 101.2] + [101.5] * 10                 # break and hold beyond A up
    res = acd.evaluate_day(minute_bars(start.astimezone(NY), path), start, end, flat, a_value=0.5)
    kinds = [(e["kind"], e["dir"]) for e in res["events"]]
    assert kinds == [("A", 1)]
    assert res["a_up"] == res["orh"] + 0.5


def test_failed_a_then_c_down():
    start, end, flat = window()
    path = [100 + (i % 3) * 0.3 for i in range(15)]
    path += [101.3, 101.4, 100.3]                         # touch A up, close back inside OR = failed A up
    path += [99.6, 99.0] + [98.8] * 10                    # through A down and hold = C down
    res = acd.evaluate_day(minute_bars(start.astimezone(NY), path), start, end, flat, a_value=0.5)
    kinds = [(e["kind"], e["dir"]) for e in res["events"]]
    assert kinds[0] == ("failed A", 1)
    assert ("C", -1) in kinds
    assert acd._lean(res["events"][0]) == -1


def test_no_event_inside_the_a_levels():
    start, end, flat = window()
    path = [100 + (i % 3) * 0.3 for i in range(60)]
    res = acd.evaluate_day(minute_bars(start.astimezone(NY), path), start, end, flat, a_value=0.5)
    assert res["events"] == []


def test_trade_from_event_hits_target_or_stop():
    res = {"orh": 100.6, "orl": 99.9, "a": 0.5}
    ev = {"kind": "A", "dir": 1, "price": 101.2, "extreme": 101.3}
    t0 = datetime(2026, 10, 21, 13, 0, tzinfo=timezone.utc)
    up = [{"t": t0, "o": 101.2, "h": 104.0, "l": 101.0, "c": 103.9, "v": 1}]
    assert acd.trade_from_event(ev, res, up)["exit"] == "target"
    down = [{"t": t0, "o": 101.2, "h": 101.3, "l": 99.5, "c": 99.6, "v": 1}]
    assert acd.trade_from_event(ev, res, down)["r"] == -1.0
    flat = [{"t": t0, "o": 101.2, "h": 101.5, "l": 101.0, "c": 101.85, "v": 1}]
    assert acd.trade_from_event(ev, res, flat)["exit"] == "flat"


def test_pivot_range_and_event_text():
    lo, hi = acd.pivot_range(110, 100, 108)
    assert (lo, hi) == (105, 106)
    res = {"a": 0.5, "orh": 100.6, "orl": 99.9}
    ev = {"kind": "failed A", "dir": 1, "t": datetime(2026, 10, 21, 13, 0, tzinfo=timezone.utc),
          "price": 100.2, "level": 101.1, "extreme": 101.4}
    txt = acd.event_line("GC", ev, res, (101.0, 101.5))
    assert "failed A up" in txt and "below the pivot range (agrees)" in txt


def test_candidate_ids_follow_the_contract_pattern():
    ids = acd_backtest.candidate_ids("CON.F.US.GCE.Z26", date(2026, 5, 1), date(2026, 10, 21))
    names = [i for i, _ in ids]
    assert names[0] == "CON.F.US.GCE.K26" and "CON.F.US.GCE.Z26" in names


def _cot_rows(mm_now, com_now, weeks=156):
    rows = []
    for i in range(weeks):
        mm = mm_now if i == 0 else (i % 50) * 1000 - 20000
        com = com_now if i == 0 else -((i % 50) * 1000 - 20000)
        rows.append({"report_date_as_yyyy_mm_dd": "2026-10-13T00:00:00.000", "open_interest_all": "500000",
                     "m_money_positions_long_all": str(max(mm, 0) + 50000), "m_money_positions_short_all": str(max(-mm, 0) + 50000),
                     "m_money_positions_long_old": "1",
                     "prod_merc_positions_long": str(max(com, 0) + 80000), "prod_merc_positions_short": str(max(-com, 0) + 80000)})
    return rows


def test_cot_board_flags_opposite_extremes():
    x = cot_board.read_market(_cot_rows(mm_now=-60000, com_now=60000))
    assert x["mm_pct"] == 0 and x["com_pct"] == 100
    assert cot_board.flag(x) == "squeeze-up risk"
    y = cot_board.read_market(_cot_rows(mm_now=60000, com_now=-60000))
    assert cot_board.flag(y) == "flush-down risk"
    z = cot_board.read_market(_cot_rows(mm_now=0, com_now=0))
    assert cot_board.flag(z) is None
    emb = cot_board.board_embed({"GC": {**x, "name": "Gold", "micros": ()}, "CL": {**z, "name": "Crude oil", "micros": ()}})
    assert emb["description"].index("GC") < emb["description"].index("CL")
    assert "Opposite extremes" in emb["fields"][0]["name"]


def test_cot_board_due_once_per_week_from_saturday_morning():
    sat_7am = datetime(2026, 10, 24, 7, 0, tzinfo=SYD)
    sat_10am = datetime(2026, 10, 24, 10, 0, tzinfo=SYD)
    tue = datetime(2026, 10, 27, 10, 0, tzinfo=SYD)
    assert not cot_board.due(sat_7am, None)
    assert cot_board.due(sat_10am, None)
    assert not cot_board.due(sat_10am, "2026-10-24")
    assert not cot_board.due(tue, "2026-10-24")      # same report week
    assert cot_board.due(tue, "2026-10-17")          # missed Saturday: catch up
