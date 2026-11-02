"""Market profile: WHEN does each market move, and is it the same every year?

  python -m lab profile NKD,NQ,ES

For each market (hourly bars, all history in the store):
  * by hour of day: share of the day's volume and average hourly range, on the
    market's home clock (Tokyo for NKD, New York for the US markets) with the
    Sydney time beside it
  * by weekday (Globex trading day, so Sydney's Tuesday morning Asia session is
    Tuesday): average daily range, average open-to-close move, relative volume,
    and in how many years that weekday was one of the two biggest-range days
  * by session x weekday: Asia (Tokyo 08:00-16:00), London (08:00-13:00 London),
    New York (08:00-16:00 New York): average range of each session on each day

Ranges are % of price so 2015 and 2026 are comparable. Volume is relative to its
own 20-day average so the growth of the market over the years doesn't distort it.
This shows where the movement and liquidity are. It does not say which way.
"""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import data
from .tearsheet import CSS, REPORT_DIR

HOME = {"NKD": "Asia/Tokyo", "NIY": "Asia/Tokyo", "MNK": "Asia/Tokyo"}
SESSIONS = [("Asia", "Asia/Tokyo", 8, 16), ("London", "Europe/London", 8, 13), ("New York", "America/New_York", 8, 16)]
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]


def home_tz(root: str) -> str:
    return HOME.get(root, "America/New_York")


def prepare(bars: pd.DataFrame) -> pd.DataFrame:
    b = bars.copy()
    b["day"] = data.trading_days(b).values
    b = b[pd.to_datetime(b["day"]).dt.weekday < 5]
    b["range_pct"] = (b["h"] - b["l"]) / b["c"] * 100
    return b


def by_hour(b: pd.DataFrame, tz: str) -> pd.DataFrame:
    local = b.index.tz_convert(tz)
    x = b.assign(hour=local.hour)
    dv = x.groupby("day")["v"].transform("sum")
    x["vol_share"] = x["v"] / dv * 100
    g = x.groupby("hour").agg(vol_share=("vol_share", "mean"), range_pct=("range_pct", "mean"), n=("v", "size"))
    g = g[g["n"] >= 0.2 * g["n"].max()]          # drop hours that only exist in a few sessions
    now = pd.Timestamp.now(tz="Australia/Sydney")
    g["sydney"] = [pd.Timestamp(now.date(), tz=tz).replace(hour=int(h)).tz_convert("Australia/Sydney").strftime("%H:00")
                   for h in g.index]
    return g


def daily(b: pd.DataFrame) -> pd.DataFrame:
    g = b.groupby("day")
    d = pd.DataFrame({"o": g["o"].first(), "h": g["h"].max(), "l": g["l"].min(), "c": g["c"].last(), "v": g["v"].sum()})
    d.index = pd.to_datetime(d.index)
    d["range_pct"] = (d["h"] - d["l"]) / d["c"] * 100
    d["move_pct"] = (d["c"] - d["o"]).abs() / d["o"] * 100
    d["rel_vol"] = d["v"] / d["v"].rolling(20, min_periods=10).mean().shift(1)
    d["weekday"] = d.index.weekday.map(dict(enumerate(DAYS)))
    d["year"] = d.index.year
    return d


def by_weekday(d: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    w = d.groupby("weekday").agg(range_pct=("range_pct", "mean"), move_pct=("move_pct", "mean"),
                                 rel_vol=("rel_vol", "mean"), n=("range_pct", "size")).reindex(DAYS)
    yr = d.groupby(["year", "weekday"])["range_pct"].mean().unstack().reindex(columns=DAYS)
    yr = yr[d.groupby("year").size().reindex(yr.index) >= 100]          # full-ish years only
    top2 = yr.rank(axis=1, ascending=False) <= 2
    w["top2_years"] = top2.sum().reindex(DAYS)
    w["years"] = len(yr)
    w["range_vs_avg"] = w["range_pct"] / d["range_pct"].mean() - 1
    return w, yr


def by_session(b: pd.DataFrame) -> pd.DataFrame:
    out = {}
    for name, tz, h0, h1 in SESSIONS:
        local = b.index.tz_convert(tz)
        m = (local.hour >= h0) & (local.hour < h1)
        s = b[m]
        g = s.groupby("day").agg(h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum"))
        g["range_pct"] = (g["h"] - g["l"]) / g["c"] * 100
        g.index = pd.to_datetime(g.index)
        out[name] = g.groupby(g.index.weekday)["range_pct"].mean().rename(index=dict(enumerate(DAYS)))
    return pd.DataFrame(out).reindex(DAYS)


# ── page ─────────────────────────────────────────────────────────────────

def _bars_svg(labels, values, sub=None, w=900, h=230, fmt="{:.2f}", hi=None) -> str:
    vals = np.asarray(values, dtype=float)
    mx = np.nanmax(vals) if len(vals) else 1
    bw = (w - 20) / max(len(vals), 1)
    hi = set(hi or [])
    parts = [f'<svg viewBox="0 0 {w} {h}" class="chart">']
    for i, (lab, v) in enumerate(zip(labels, vals)):
        bh = (h - 58) * (v / mx if mx else 0)
        x = 10 + i * bw
        cls = "barpos" if i in hi else "barmid"
        parts.append(f'<rect x="{x + bw * 0.12:.1f}" y="{h - 40 - bh:.1f}" width="{bw * 0.76:.1f}" height="{bh:.1f}" class="{cls}">'
                     f'<title>{html.escape(str(lab))}: {fmt.format(v)}</title></rect>')
        if len(vals) <= 24:
            parts.append(f'<text x="{x + bw / 2:.1f}" y="{h - 44 - bh:.1f}" class="ax" text-anchor="middle">{fmt.format(v)}</text>')
        parts.append(f'<text x="{x + bw / 2:.1f}" y="{h - 26}" class="ax" text-anchor="middle">{html.escape(str(lab))}</text>')
        if sub is not None:
            parts.append(f'<text x="{x + bw / 2:.1f}" y="{h - 12}" class="lbl" text-anchor="middle">{html.escape(str(sub[i]))}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _heat(df: pd.DataFrame, fmt="{:.2f}") -> str:
    vals = df.to_numpy(dtype=float)
    lo, hi = np.nanmin(vals), np.nanmax(vals)
    head = "<tr><th></th>" + "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns) + "</tr>"
    rows = []
    for idx, r in df.iterrows():
        cells = []
        for v in r:
            a = 0 if hi == lo or np.isnan(v) else (v - lo) / (hi - lo)
            cells.append(f'<td style="background:color-mix(in srgb,var(--accent) {a * 70:.0f}%,transparent)">'
                         f'{"—" if np.isnan(v) else fmt.format(v)}</td>')
        rows.append(f"<tr><th>{html.escape(str(idx))}</th>{''.join(cells)}</tr>")
    return f'<div class="tw"><table>{head}{"".join(rows)}</table></div>'


def market_section(root: str) -> tuple[str, dict]:
    bars = data.load(root, "1h")
    b = prepare(bars)
    tz = home_tz(root)
    hz = by_hour(b, tz)
    d = daily(b)
    w, yr = by_weekday(d)
    ses = by_session(b)
    top_hours = list(np.argsort(-hz["range_pct"].to_numpy())[:3])
    top_days = list(np.argsort(-w["range_pct"].to_numpy())[:2])
    clock = "Tokyo" if tz == "Asia/Tokyo" else "New York"
    big = w.sort_values("range_pct", ascending=False)
    facts = {"root": root, "first": str(d.index[0].date()), "last": str(d.index[-1].date()),
             "top_days": list(big.index[:2]), "top_hours": [f"{int(h):02d}:00 {clock} ({s} Sydney)"
                                                            for h, s in zip(hz.index[top_hours], hz["sydney"].iloc[top_hours])],
             "weekday": w, "session": ses}
    wk_tab = w[["range_pct", "move_pct", "rel_vol", "range_vs_avg", "top2_years", "years"]].copy()
    rows = "".join(
        f"<tr><th>{k}</th><td>{r.range_pct:.2f}%</td><td>{r.range_vs_avg:+.0%}</td><td>{r.move_pct:.2f}%</td>"
        f"<td>{r.rel_vol:.2f}×</td><td><b>{int(r.top2_years)}</b> of {int(r.years)}</td></tr>"
        for k, r in wk_tab.iterrows())
    s = [f"<h2>{root} <span style='color:var(--muted);font-weight:400;font-size:14px'>{facts['first']} to {facts['last']} · "
         f"{len(d):,} trading days · times on the {clock} clock</span></h2>",
         "<h3>By weekday</h3>",
         _bars_svg(DAYS, w["range_pct"], [f"top-2 in {int(x)}/{int(n)} yrs" for x, n in zip(w["top2_years"], w["years"])],
                   fmt="{:.2f}%", hi=top_days, h=210),
         "<div class='tw'><table><tr><th>Day</th><th>Avg daily range</th><th>vs average day</th><th>Avg open→close move</th>"
         "<th>Volume vs normal</th><th>Top-2 range day</th></tr>" + rows + "</table></div>",
         "<p class='note'>“Top-2 range day” counts the years in which that weekday had one of the two biggest average ranges. "
         "A real weekday effect shows up in most years, not just on average.</p>",
         "<h3>By hour (average hourly range, % of price)</h3>",
         _bars_svg([f"{int(h):02d}" for h in hz.index], hz["range_pct"], list(hz["sydney"]), fmt="{:.2f}", hi=top_hours),
         f"<p class='note'>Top row: hour on the {clock} clock. Blue row: the same hour in Sydney right now (AEDT).</p>",
         "<h3>By hour (share of the day's volume, %)</h3>",
         _bars_svg([f"{int(h):02d}" for h in hz.index], hz["vol_share"], list(hz["sydney"]), fmt="{:.1f}",
                   hi=list(np.argsort(-hz["vol_share"].to_numpy())[:3])),
         "<h3>Session range by weekday (% of price)</h3>",
         _heat(ses, "{:.2f}%"),
         "<p class='note'>Asia = Tokyo 08:00–16:00 (10:00–18:00 Sydney), London = 08:00–13:00 London, "
         "New York = 08:00–16:00 New York. Darker = bigger moves.</p>",
         "<h3>Average daily range by year and weekday (% of price)</h3>",
         _heat(yr.round(3), "{:.2f}")]
    return "".join(s), facts


def render(roots: list[str]) -> tuple[str, list[dict]]:
    sections, facts = [], []
    for r in roots:
        sec, f = market_section(r)
        sections.append(sec)
        facts.append(f)
    summary = "".join(
        f"<li><b>{f['root']}</b>: biggest days {', '.join(f['top_days'])}; biggest hours {', '.join(f['top_hours'])}</li>"
        for f in facts)
    page = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Market profile · {', '.join(roots)} · Klaxton Strategy Lab</title><style>{CSS}
.barmid{{fill:color-mix(in srgb,var(--muted) 55%,transparent)}} .note{{color:var(--muted);font-size:13px;margin:4px 0 14px}}
h3{{font-size:15px;margin:22px 0 6px}}</style></head><body><div class="wrap">
<h1>Market profile <span>· {', '.join(roots)}</span></h1>
<p class="sub">When each market moves and trades, from hourly bars. Generated {datetime.now():%d %b %Y %H:%M}. Ranges are % of price;
volume is relative to the market's own recent average. This shows where the movement and liquidity are, not which way price goes.</p>
<div class="card"><b>At a glance</b><ul>{summary}</ul></div>
{''.join(sections)}
</div></body></html>"""
    return page, facts


def save(roots: list[str]) -> tuple[Path, list[dict]]:
    page, facts = render(roots)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    p = REPORT_DIR / f"profile_{'_'.join(roots)}_{datetime.now():%Y-%m-%d_%H%M%S}.html"
    p.write_text(page, encoding="utf-8")
    return p, facts
