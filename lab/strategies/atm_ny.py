"""ATM New York AM model on 1-minute bars (H8), rules signed off by Dave on 2 Nov 2026.

  1. ATM range: high and low of 07:00-09:30 New York. Both must exist.
  2. Window: 09:30-11:00 New York only, one trade per AM session.
  3. Sweep: price trades at least 1 tick beyond the ATM high or low inside the window. The first
     side swept sets the direction: ATM high swept -> short, ATM low swept -> long.
  4. MSS: after the sweep, a 1-minute candle CLOSES beyond the most recent swing point in the new
     direction (a swing = a high/low with `swing_n` bars either side, already confirmed).
  5. Order block, which must be obvious:
       big        the displacement candle's range >= disp_mult x the average 1m range of the 20 bars before
       decisive   its body >= 60% of its range
       engulfing  its body covers the order block candle's body
       fast       the MSS comes within 3 candles of the order block
     Order block = the last opposite-coloured candle before the displacement candle.
     need_fvg=True: the displacement must also leave an FVG (versions C and D).
  6. Entry: limit at the order block's body (top for longs, bottom for shorts), valid until 11:00.
  7. Stop: `stop_pts` beyond the sweep's extreme.
  8. Target: "atm" = the opposite ATM level, or "r2" = a fixed 2R.
  Out at `exit_hour` New York if neither stop nor target is hit.
"""
from __future__ import annotations

from datetime import datetime, time

import numpy as np
import pandas as pd

from .. import data
from ..engine import Setup


def _t(day, hh, mm=0):
    return pd.Timestamp(datetime.combine(day, time(hh, mm)), tz="America/New_York").tz_convert("UTC")


class ATMNewYork:
    name = "atm_ny"
    timeframe = "1m"
    defaults = {"disp_mult": 2.0, "need_fvg": False, "target": "atm", "stop_pts": 2.0, "swing_n": 2,
                "fast_bars": 3, "body_pct": 0.6, "exit_hour": 12, "tick": None, "since": None, "only": ""}
    hypothesis = ("A sweep of the 07:00-09:30 ATM high or low inside the 09:30-11:00 window, followed by an obvious "
                  "displaced market structure shift, marks the turn; entering at the order block with the stop past "
                  "the sweep and targeting the opposite ATM level has positive expectancy.")

    def resolve(self, params: dict) -> dict:
        return {**self.defaults, **params}

    def setups(self, bars: pd.DataFrame, disp_mult=2.0, need_fvg=False, target="atm", stop_pts=2.0, swing_n=2,
               fast_bars=3, body_pct=0.6, exit_hour=12, tick=None, since=None, only="") -> list[Setup]:
        if bars.empty:
            return []
        tick = float(tick or 0.25)
        b = bars.copy()
        rng = (b["h"] - b["l"]).to_numpy()
        avg_rng = pd.Series(rng).rolling(20, min_periods=20).mean().shift(1).to_numpy()   # the 20 bars before
        ny = b.index.tz_convert("America/New_York")
        dates = ny.normalize().tz_localize(None).date
        since_d = pd.Timestamp(since).date() if since else None
        wanted = {w.strip().upper() for w in only.split(",") if w.strip()} if only else None
        o, h, l, c = (b[k].to_numpy() for k in ("o", "h", "l", "c"))
        t = b.index
        pos_by_date: dict = {}
        for k, d in enumerate(dates):
            pos_by_date.setdefault(d, []).append(k)
        out = []
        for day, pos in pos_by_date.items():
            if day.weekday() > 4 or (since_d and day < since_d):
                continue
            if wanted and ["MON", "TUE", "WED", "THU", "FRI"][day.weekday()] not in wanted:
                continue
            p0, p1 = pos[0], pos[-1] + 1
            ts = t[p0:p1]
            a0, a1 = ts.searchsorted(_t(day, 7)), ts.searchsorted(_t(day, 9, 30))
            w1 = ts.searchsorted(_t(day, 11))
            if a1 - a0 < 30 or w1 - a1 < 10:
                continue
            a0, a1, w1 = p0 + a0, p0 + a1, p0 + w1
            atm_h, atm_l = h[a0:a1].max(), l[a0:a1].min()
            s = self._session(o, h, l, c, rng, avg_rng, a1, w1, atm_h, atm_l, tick, disp_mult, need_fvg,
                              swing_n, fast_bars, body_pct)
            if not s:
                continue
            decided, side, entry, sweep_ext = s
            stop = sweep_ext - side * stop_pts
            risk = (entry - stop) * side
            if risk <= 0:
                continue
            tgt = (atm_h if side == 1 else atm_l) if target == "atm" else entry + side * 2 * risk
            if (tgt - entry) * side <= 0:
                continue
            out.append(Setup(t[decided], side, stop=stop, target=tgt, entry=entry, expires=_t(day, 11),
                             exit_by=_t(day, exit_hour), target_on_fill_bar=False,
                             tag=f"ATM {'low sweep, long' if side == 1 else 'high sweep, short'}",
                             info={"atm_h": atm_h, "atm_l": atm_l}))
        return out

    @staticmethod
    def _session(o, h, l, c, rng, avg_rng, w0, w1, atm_h, atm_l, tick, disp_mult, need_fvg, swing_n,
                 fast_bars, body_pct):
        side = 0
        k0 = None
        for k in range(w0, w1):
            up, dn = h[k] >= atm_h + tick, l[k] <= atm_l - tick
            if up and dn:
                return None
            if up or dn:
                side, k0 = (-1 if up else 1), k
                break
        if not side:
            return None
        ext = h[k0] if side == -1 else l[k0]
        lookback = max(w0 - 60, swing_n)
        for j in range(k0 + 1, w1):
            ext = max(ext, h[j]) if side == -1 else min(ext, l[j])
            # most recent confirmed swing in the new direction, before candle j
            swing = None
            for p in range(j - 1 - swing_n, lookback - 1, -1):
                if side == -1:      # short: break a swing low
                    if l[p] < l[p - swing_n:p].min() and l[p] < l[p + 1:p + 1 + swing_n].min():
                        swing = l[p]
                        break
                else:               # long: break a swing high
                    if h[p] > h[p - swing_n:p].max() and h[p] > h[p + 1:p + 1 + swing_n].max():
                        swing = h[p]
                        break
            if swing is None:
                continue
            if not ((side == -1 and c[j] < swing) or (side == 1 and c[j] > swing)):
                continue
            # MSS on candle j: find the displacement candle and its order block
            for d in range(j, max(j - fast_bars, k0) - 1, -1):
                body = (c[d] - o[d]) * side
                if body <= 0 or not np.isfinite(avg_rng[d]) or rng[d] < disp_mult * avg_rng[d] or body < body_pct * rng[d]:
                    continue
                ob = None
                for q in range(d - 1, max(d - 1 - fast_bars, k0 - 1), -1):
                    if (c[q] - o[q]) * side < 0:        # opposite-coloured candle
                        ob = q
                        break
                if ob is None or j - ob > fast_bars:
                    continue
                ob_top, ob_bot = max(o[ob], c[ob]), min(o[ob], c[ob])
                d_top, d_bot = max(o[d], c[d]), min(o[d], c[d])
                if not (d_top >= ob_top and d_bot <= ob_bot):      # engulfing
                    continue
                decide = j + 1
                if need_fvg:
                    if d + 1 > j:
                        decide = d + 2                            # wait for the candle after displacement
                        if decide >= w1:
                            continue
                    if side == 1 and not (l[d + 1] > h[d - 1]):
                        continue
                    if side == -1 and not (h[d + 1] < l[d - 1]):
                        continue
                    ext = max(ext, h[d + 1]) if side == -1 else min(ext, l[d + 1])
                if decide >= w1:
                    return None
                entry = ob_top if side == 1 else ob_bot
                return decide, side, entry, ext
        return None
