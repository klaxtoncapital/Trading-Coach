import asyncio
import io
import unittest
from datetime import datetime, timedelta, timezone

from scanner import journal_export as je

UTC = timezone.utc


def fill(t, side, size, price, pnl=None, oid=1, cid="CON.F.US.NKD.Z26"):
    return {"id": hash((t, side, price)) & 0xffff, "contractId": cid, "creationTimestamp": t.isoformat(),
            "price": price, "size": size, "side": side, "profitAndLoss": pnl, "fees": 2.66,
            "commissions": 0, "orderId": oid, "voided": False}


# Thu 5 Nov 2026 10:50 Sydney = Wed 4 Nov 23:50 UTC (Sydney UTC+11)
T0 = datetime(2026, 11, 4, 23, 50, 18, tzinfo=UTC)


class FakePX:
    """Leader + one copy: short 1 NKD, mis-tap close, re-short 14s later, cover by hand."""

    def __init__(self):
        self.live = False

    async def active_accounts(self):
        return [{"id": 1, "name": "LEADER"}, {"id": 2, "name": "COPY1"}]

    async def trades_between(self, aid, start, end):
        d = 2 if aid == 2 else 0
        s = timedelta(seconds=d)
        return [fill(T0 + s, 1, 1, 63040, oid=10),
                fill(T0 + timedelta(minutes=2) + s, 0, 1, 62995, pnl=225, oid=11),
                fill(T0 + timedelta(minutes=2, seconds=14) + s, 1, 1, 62950, oid=12),
                fill(T0 + timedelta(minutes=13) + s, 0, 1, 62370, pnl=2900, oid=13)]

    async def orders(self, aid, start, end):
        return [{"id": 20, "contractId": "CON.F.US.NKD.Z26", "creationTimestamp": (T0 + timedelta(seconds=1)).isoformat(),
                 "side": 0, "type": 4, "stopPrice": 63300, "status": 3},
                {"id": 21, "contractId": "CON.F.US.NKD.Z26", "creationTimestamp": (T0 + timedelta(seconds=1)).isoformat(),
                 "side": 0, "type": 1, "limitPrice": 61900, "status": 3}]

    async def contract(self, cid):
        return {"name": "NKDZ6", "description": "Nikkei 225 (USD)", "tickSize": 5, "tickValue": 25}

    async def bars_between(self, cid, start, end, minutes=1):
        out, t, p = [], start, 63000.0
        open_t = je.session_open_utc(je.cme_trade_date(T0))
        while t < end:
            if t < open_t:
                o, h, l, c = p, p + 400, p - 900, p   # previous day: PDH 63400 / PDL 62100
            elif t == open_t:
                o, h, l, c = 63175, 63180, 63170, 63175
            else:
                o, h, l, c = p, p + 60, p - 60, p
                if T0 <= t <= T0 + timedelta(minutes=13):
                    h, l = 63100, 62300
            out.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": 10})
            t += timedelta(minutes=minutes)
        return out


class JournalExportTests(unittest.TestCase):
    def test_pyramid_is_one_trade(self):
        fills = [fill(T0, 1, 1, 100), fill(T0 + timedelta(minutes=5), 1, 1, 95),
                 fill(T0 + timedelta(minutes=20), 0, 2, 90, pnl=50)]
        closed, open_ = je.build_trades(fills, 1, "A")
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].max_position, 2)
        self.assertEqual(open_, [])

    def test_separate_trades_after_gap(self):
        fills = [fill(T0, 0, 1, 100), fill(T0 + timedelta(minutes=5), 1, 1, 101, pnl=5),
                 fill(T0 + timedelta(minutes=40), 0, 1, 100), fill(T0 + timedelta(minutes=50), 1, 1, 99, pnl=-5)]
        closed, _ = je.build_trades(fills, 1, "A")
        self.assertEqual(len(closed), 2)

    def test_open_position_not_closed(self):
        closed, open_ = je.build_trades([fill(T0, 0, 1, 100)], 1, "A")
        self.assertEqual((len(closed), len(open_)), (0, 1))

    def test_session_open_monday(self):
        mon = je.cme_trade_date(datetime(2026, 11, 9, 1, 0, tzinfo=UTC))  # Mon 9 Nov 12pm Sydney
        self.assertEqual(je.session_open_utc(mon).astimezone(je.NY).strftime("%a %H:%M"), "Sun 18:00")

    def test_export_end_to_end(self):
        now = T0 + timedelta(hours=10)
        res = asyncio.run(je.export(FakePX(), 3, now))
        trades = [t for d in res["days"].values() for t in d["trades"]]
        self.assertEqual(len(trades), 1)              # copy merged, re-entry merged
        t = trades[0]
        self.assertEqual(t["accounts"], 2)
        self.assertEqual(t["quick_reentries"], 1)
        self.assertEqual(t["qty"], 1)
        self.assertEqual(t["pnl_usd"], 3125)
        self.assertEqual(t["pnl_usd_all_accounts"], 6250)
        self.assertEqual(t["exit_reason"], "Closed by hand")
        self.assertTrue(t["stop_with_entry"])
        self.assertTrue(t["target_cancelled"])
        c = t["context"]
        self.assertEqual(c["six_pm_open"], 63175)
        self.assertEqual(c["weekday"], "Thu")
        self.assertEqual((c["pdh"], c["pdl"]), (63400, 62100))
        self.assertEqual(c["entry_vs_open"], "Below")    # avg entry 62,995 after the re-entry
        self.assertIn(t["key"], res["charts"])
        self.assertTrue(res["charts"][t["key"]].startswith(b"\x89PNG"))
        self.assertIn("2026-11-05", res["days"])       # Sydney date


if __name__ == "__main__":
    unittest.main()
