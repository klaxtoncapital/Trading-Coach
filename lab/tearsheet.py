"""One self-contained HTML tear sheet per test: open it in any browser.

The tear sheet has five parts:
  1. Verdict: the validation checklist, auto-ticked where the Lab can check it
  2. Headline numbers, each with a one-line "what this means"
  3. Charts: equity curve and drawdown, R distribution, results by year
  4. Robustness: out-of-sample, walk-forward, parameter sweep, Monte Carlo, cost stress
  5. Breakdown tables (by setup, by year) and the full trade list
"""
from __future__ import annotations

import html
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import metrics
from .robust import MonteCarlo

REPORT_DIR = Path(__file__).resolve().parent.parent / "lab_reports"


@dataclass
class Report:
    strategy: str
    market: str
    params: dict
    hypothesis: str
    trades: pd.DataFrame
    costs_note: str
    lookahead: tuple[bool, list[str]] | None = None
    mc: MonteCarlo | None = None
    stress: dict | None = None
    sweep: pd.DataFrame | None = None
    plateau: float | None = None
    wf_trades: pd.DataFrame | None = None
    wf_windows: pd.DataFrame | None = None
    oos_frac: float = 0.3
    notes: list[str] = field(default_factory=list)


# ── formatting ───────────────────────────────────────────────────────────

def _f(x, nd=2, pct=False, sign=False):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—" if x is None or math.isnan(x) else "∞"
    if pct:
        return f"{x * 100:.{nd}f}%" if nd else f"{x * 100:.0f}%"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def _usd(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"-${abs(x):,.0f}" if x < 0 else f"${x:,.0f}"


def _e(s) -> str:
    return html.escape(str(s))


# ── charts (plain SVG, no libraries) ─────────────────────────────────────

def _equity_svg(trades: pd.DataFrame, oos_count: int = 0, w=900, h=300) -> str:
    eq = metrics.equity(trades)
    if eq.empty:
        return ""
    y = np.concatenate([[0.0], eq["equity_r"].to_numpy()])
    dd = np.concatenate([[0.0], eq["dd_r"].to_numpy()])
    n = len(y)
    pad_l, pad_r, top_h, gap, dd_h = 54, 12, int(h * 0.68), 14, int(h * 0.32) - 14 - 22
    ymin, ymax = min(y.min(), 0), max(y.max(), 0.5)
    dmin = min(dd.min(), -0.5)
    X = lambda i: pad_l + (w - pad_l - pad_r) * i / max(n - 1, 1)
    Y = lambda v: 8 + (top_h - 16) * (1 - (v - ymin) / (ymax - ymin))
    D = lambda v: top_h + gap + dd_h * (v / dmin)
    parts = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="Equity curve">']
    if oos_count:
        k = n - oos_count
        if 0 < k < n:
            parts.append(f'<rect x="{X(k):.1f}" y="0" width="{w - pad_r - X(k):.1f}" height="{h - 22}" class="oos"/>')
            parts.append(f'<text x="{X(k) + 6:.1f}" y="18" class="lbl">recent {oos_count} trades</text>')
    for v in np.linspace(ymin, ymax, 5):
        parts.append(f'<line x1="{pad_l}" x2="{w - pad_r}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" class="grid"/>'
                     f'<text x="{pad_l - 6}" y="{Y(v) + 4:.1f}" class="ax" text-anchor="end">{v:.0f}R</text>')
    parts.append(f'<line x1="{pad_l}" x2="{w - pad_r}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" class="zero"/>')
    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(y))
    parts.append(f'<polyline points="{pts}" class="eq"/>')
    area = f"{X(0):.1f},{D(0):.1f} " + " ".join(f"{X(i):.1f},{D(v):.1f}" for i, v in enumerate(dd)) + f" {X(n - 1):.1f},{D(0):.1f}"
    parts.append(f'<polygon points="{area}" class="dd"/>')
    parts.append(f'<text x="{pad_l - 6}" y="{D(dmin) + 4:.1f}" class="ax" text-anchor="end">{dmin:.0f}R</text>'
                 f'<text x="{pad_l + 4}" y="{top_h + gap + 12}" class="lbl">drawdown</text>')
    times = pd.to_datetime(eq["time"])
    for frac in (0, 0.25, 0.5, 0.75, 1):
        i = int(round(frac * (len(times) - 1)))
        anchor = "start" if frac == 0 else "end" if frac == 1 else "middle"
        parts.append(f'<text x="{X(i + 1):.1f}" y="{h - 4}" class="ax" text-anchor="{anchor}">{times.iloc[i]:%b %Y}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _hist_svg(r, w=440, h=220) -> str:
    r = np.asarray(r, dtype=float)
    if not len(r):
        return ""
    lo, hi = math.floor(min(r.min(), -1)), math.ceil(max(r.max(), 1))
    bins = np.arange(lo, hi + 0.5, 0.5)
    cnt, edges = np.histogram(r, bins=bins)
    pad_l, pad_b = 30, 24
    bw = (w - pad_l - 8) / len(cnt)
    mx = max(cnt.max(), 1)
    parts = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="Distribution of trade results">']
    for i, k in enumerate(cnt):
        bh = (h - pad_b - 10) * k / mx
        cls = "barpos" if edges[i] >= 0 else "barneg"
        parts.append(f'<rect x="{pad_l + i * bw + 1:.1f}" y="{h - pad_b - bh:.1f}" width="{bw - 2:.1f}" height="{bh:.1f}" class="{cls}"><title>{edges[i]:g} to {edges[i + 1]:g}R: {k} trades</title></rect>')
    for i, e in enumerate(edges):
        if float(e).is_integer():
            parts.append(f'<text x="{pad_l + i * bw:.1f}" y="{h - 6}" class="ax" text-anchor="middle">{e:g}R</text>')
    parts.append("</svg>")
    return "".join(parts)


def _years_svg(by_year: pd.DataFrame, w=440, h=220) -> str:
    if by_year.empty:
        return ""
    vals = by_year["total_r"].to_numpy(dtype=float)
    mx = max(abs(vals).max(), 1)
    pad_b, mid = 24, (h - 24) / 2
    bw = (w - 20) / len(vals)
    parts = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img" aria-label="Result by year">',
             f'<line x1="10" x2="{w - 10}" y1="{mid:.1f}" y2="{mid:.1f}" class="zero"/>']
    for i, (yr, v, n) in enumerate(zip(by_year["year"], vals, by_year["n"])):
        bh = (mid - 14) * abs(v) / mx
        y = mid - bh if v >= 0 else mid
        cls = "barpos" if v >= 0 else "barneg"
        x = 10 + i * bw
        parts.append(f'<rect x="{x + bw * 0.2:.1f}" y="{y:.1f}" width="{bw * 0.6:.1f}" height="{bh:.1f}" class="{cls}"/>'
                     f'<text x="{x + bw / 2:.1f}" y="{(y - 4) if v >= 0 else (y + bh + 12):.1f}" class="ax" text-anchor="middle">{v:+.1f}R</text>'
                     f'<text x="{x + bw / 2:.1f}" y="{h - 6}" class="ax" text-anchor="middle">{yr} ({n})</text>')
    parts.append("</svg>")
    return "".join(parts)


# ── the checklist ────────────────────────────────────────────────────────

def checklist(rep: Report, s: dict, by_year: pd.DataFrame, oos_s: dict, is_s: dict) -> list[tuple[str, str, str]]:
    """(status, check, detail). status: PASS, CAUTION, FAIL, MANUAL, NOT RUN."""
    out = []
    out.append(("MANUAL", "Written rule and a reason the edge exists",
                f"Hypothesis on file: “{rep.hypothesis}” Is it convincing? Who is on the other side of the trade?"))
    if rep.lookahead is None:
        out.append(("NOT RUN", "No look-ahead", "Run with --check-lookahead."))
    else:
        ok, probs = rep.lookahead
        out.append(("PASS" if ok else "FAIL", "No look-ahead",
                    "Re-ran the strategy with the future hidden at sampled decision points: identical setups."
                    if ok else "Setups changed when the future was hidden: " + "; ".join(probs[:3])))
    out.append(("PASS", "Survivorship-free data", "Futures continuous contract: every contract month is in the data."))
    if rep.stress:
        e = rep.stress.get("expectancy_r", float("nan"))
        out.append(("PASS" if e > 0 else "FAIL", "Profitable after costs, even at 1.5× slippage",
                    f"Expectancy {_f(s['expectancy_r'], 3, sign=True)}R with costs; {_f(e, 3, sign=True)}R at 1.5× slippage. "
                    f"Costs eat {_f(s['cost_per_trade_r'], 3)}R a trade. {rep.costs_note}"))
    else:
        out.append(("NOT RUN", "Profitable after costs, even at 1.5× slippage", rep.costs_note))
    n = s["n"]
    out.append(("PASS" if n >= 100 else "CAUTION" if n >= 30 else "FAIL", "Enough trades",
                f"{n} trades. Under 100, a few lucky trades can make the whole result; under 30 it's an anecdote."))
    t = s.get("t_stat", float("nan"))
    lo, hi = s["expectancy_ci"]
    out.append(("PASS" if t >= 2 else "CAUTION" if t >= 1 else "FAIL", "Edge is bigger than the noise",
                f"t-stat {_f(t)} (2+ is the usual bar). The true expectancy is probably between "
                f"{_f(lo, 3, sign=True)}R and {_f(hi, 3, sign=True)}R; if that range includes 0, the edge may not be real."))
    if rep.wf_trades is not None and len(rep.wf_trades):
        wf = metrics.summary(rep.wf_trades)
        ok = wf["expectancy_r"] > 0
        out.append(("PASS" if ok else "FAIL", "Out-of-sample (walk-forward)",
                    f"Settings chosen only on past data, then traded forward: {wf['n']} trades, "
                    f"{_f(wf['expectancy_r'], 3, sign=True)}R a trade, {_f(wf['total_r'], 1, sign=True)}R total."))
    elif oos_s.get("n"):
        ratio = oos_s["expectancy_r"] / is_s["expectancy_r"] if is_s.get("expectancy_r", 0) > 0 else float("nan")
        st = "PASS" if oos_s["expectancy_r"] > 0 and (ratio >= 0.5) else "CAUTION" if oos_s["expectancy_r"] > 0 else "FAIL"
        out.append((st, "Out-of-sample (recent 30% held back)",
                    f"Older trades {_f(is_s['expectancy_r'], 3, sign=True)}R, recent {_f(oos_s['expectancy_r'], 3, sign=True)}R a trade. "
                    "Only a true test if the recent period wasn't looked at when picking the settings. "
                    "Run --walk-forward for the honest version."))
    else:
        out.append(("NOT RUN", "Out-of-sample", "Not enough trades to split."))
    if rep.plateau is None:
        out.append(("NOT RUN", "Settings sit on a plateau, not a spike", "Run with --sweep to test neighbouring settings."))
    else:
        out.append(("PASS" if rep.plateau >= 0.6 else "CAUTION" if rep.plateau >= 0.4 else "FAIL",
                    "Settings sit on a plateau, not a spike",
                    f"{rep.plateau * 100:.0f}% of neighbouring settings are also profitable."))
    big = by_year[by_year["n"] >= 10] if not by_year.empty else by_year
    if len(big) >= 2:
        pos = (big["total_r"] > 0).mean()
        if s["total_r"] > 0:
            share = big["total_r"].max() / s["total_r"]
            share_txt = f"the best year is {share * 100:.0f}% of the total."
        else:
            share, share_txt = float("inf"), "losing overall."
        st = "PASS" if pos >= 0.75 and share < 0.6 else "CAUTION" if pos >= 0.5 and s["total_r"] > 0 else "FAIL"
        out.append((st, "Works across years, not carried by one",
                    f"Profitable in {int((big['total_r'] > 0).sum())} of {len(big)} years with 10+ trades; " + share_txt))
    else:
        out.append(("CAUTION", "Works across years, not carried by one", "Less than two full years of trades to compare."))
    if rep.mc:
        mc = rep.mc
        if mc.p_breach is not None:
            st = "PASS" if mc.p_breach < 0.05 else "CAUTION" if mc.p_breach < 0.15 else "FAIL"
            out.append((st, "Drawdown fits inside the account limit",
                        f"At {_usd(mc.risk_usd)} a trade, {mc.p_breach * 100:.1f}% chance of a {_usd(mc.limit_usd)} drawdown "
                        f"within {mc.trades} trades. Bad-case (95th percentile) drawdown {_f(mc.dd_p95, 1)}R = "
                        f"{_usd(mc.dd_p95 * mc.risk_usd)}."))
        else:
            out.append(("MANUAL", "Drawdown fits inside the account limit",
                        f"Bad-case (95th percentile) drawdown {_f(mc.dd_p95, 1)}R. Add --risk and --limit to get the account breach chance."))
    else:
        out.append(("NOT RUN", "Drawdown fits inside the account limit", ""))
    changed = {k: v for k, v in rep.params.items() if v not in (None, "", 0, 0.0, False)}
    out.append(("MANUAL", "Few knobs", f"{len(changed)} settings in use: " +
                ", ".join(f"{k}={v}" for k, v in changed.items()) + ". Every extra rule needs a reason."))
    out.append(("MANUAL", "Forward test before real size", "Paper or small-size live results must land inside the expected range."))
    if rep.mc:
        out.append(("MANUAL", "Kill criteria written down before going live",
                    f"Suggested from the Monte Carlo: pause if the live drawdown passes {_f(rep.mc.dd_p95, 1)}R "
                    f"or a losing streak passes {rep.mc.streak_p95} trades, then review."))
    return out


# ── page ─────────────────────────────────────────────────────────────────

EXPLAIN = {
    "expectancy_r": "Average result per trade after costs, in R. The single number that pays you.",
    "win_rate": "Share of trades that made money. Meaningless on its own: pair it with the payoff.",
    "payoff": "Average win divided by average loss.",
    "profit_factor": "Total won ÷ total lost. Under 1.2 after costs is thin.",
    "total_r": "Sum of all trades in R. At $275 risk a trade, 10R = $2,750.",
    "max_dd_r": "Worst peak-to-trough fall in the backtest. Expect worse live: see the Monte Carlo.",
    "worst_streak": "Most losses in a row. Know this number before it happens to you.",
    "sharpe": "Return per unit of day-to-day swing, yearly. Above 1 is good for one strategy; above 2 is rare.",
    "sortino": "Like Sharpe but only counts downside swings.",
    "t_stat": "How far the average is from zero, in noise units. Below 2, luck could explain it.",
    "trades_per_year": "How often it trades. Low numbers mean you need many years to trust it.",
    "recovery": "Total profit ÷ worst drawdown. How much you earn for the pain.",
}

CSS = """
:root{--bg:#0f1216;--panel:#161b21;--panel2:#1c232b;--line:#2a333d;--text:#e6e9ed;--muted:#9aa5b1;--accent:#f2b134;--good:#4cc38a;--bad:#ef6461;--warn:#e9a23b;--info:#5aa9e6}
@media (prefers-color-scheme: light){:root{--bg:#f6f7f9;--panel:#fff;--panel2:#f0f2f5;--line:#dde2e8;--text:#161a1f;--muted:#5b6672;--accent:#b97a00;--good:#18864f;--bad:#c23b38;--warn:#b26a00;--info:#1f6fb2}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14.5px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:28px 20px 60px}h1{margin:0;font-size:26px}h1 span{color:var(--accent)}
h2{font-size:18px;margin:34px 0 8px;padding-top:10px;border-top:1px solid var(--line)}.sub{color:var(--muted);margin:4px 0 18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:10px 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpi b{display:block;font-size:22px;font-variant-numeric:tabular-nums}.kpi small{color:var(--muted);display:block;font-size:12px;line-height:1.35;margin-top:4px}
.kpi .k{font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:13.5px;font-variant-numeric:tabular-nums}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.05em}.tw{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:4px 8px}
.st{font:700 11px/1 ui-monospace,monospace;padding:4px 7px;border-radius:5px;white-space:nowrap}
.PASS{color:var(--good);background:color-mix(in srgb,var(--good) 16%,transparent)}.FAIL{color:var(--bad);background:color-mix(in srgb,var(--bad) 16%,transparent)}
.CAUTION{color:var(--warn);background:color-mix(in srgb,var(--warn) 18%,transparent)}.MANUAL,.NOTRUN{color:var(--info);background:color-mix(in srgb,var(--info) 16%,transparent)}
.verdict{font-size:17px;font-weight:700}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:760px){.grid2{grid-template-columns:1fr}}
svg.chart{width:100%;height:auto;display:block}.grid{stroke:var(--line);stroke-width:1}.zero{stroke:var(--muted);stroke-width:1;stroke-dasharray:3 3}
.eq{fill:none;stroke:var(--accent);stroke-width:2}.dd{fill:color-mix(in srgb,var(--bad) 45%,transparent);stroke:none}.oos{fill:color-mix(in srgb,var(--info) 10%,transparent)}
.ax{fill:var(--muted);font-size:11px}.lbl{fill:var(--info);font-size:11px}.barpos{fill:var(--good)}.barneg{fill:var(--bad)}
details summary{cursor:pointer;color:var(--info)}.neg{color:var(--bad)}.pos{color:var(--good)}code{background:var(--panel2);padding:1px 5px;border-radius:4px}
"""


def _table(df: pd.DataFrame, fmt: dict | None = None) -> str:
    fmt = fmt or {}
    head = "".join(f"<th>{_e(c)}</th>" for c in df.columns)
    rows = []
    for _, r in df.iterrows():
        cells = []
        for c in df.columns:
            v = r[c]
            txt = fmt[c](v) if c in fmt else (f"{v:.3f}" if isinstance(v, float) else str(v))
            cls = ' class="neg"' if isinstance(v, (int, float)) and c in ("total_r", "expectancy_r", "r", "test_r", "net_usd", "total_usd") and v < 0 else ""
            cells.append(f"<td{cls}>{_e(txt)}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return f'<div class="tw"><table><tr>{head}</tr>{"".join(rows)}</table></div>'


def render(rep: Report) -> str:
    t = rep.trades.sort_values("entry_time").reset_index(drop=True)
    s = metrics.summary(t)
    if not s["n"]:
        return f"<!DOCTYPE html><html><body><h1>{_e(rep.strategy)} · {_e(rep.market)}</h1><p>No trades.</p></body></html>"
    is_t, oos_t = metrics.split(t, rep.oos_frac)
    is_s, oos_s = metrics.summary(is_t), metrics.summary(oos_t)
    by_year = metrics.by(t, "year")
    checks = checklist(rep, s, by_year, oos_s, is_s)
    fails = sum(1 for c in checks if c[0] == "FAIL")
    cautions = sum(1 for c in checks if c[0] == "CAUTION")
    not_run = sum(1 for c in checks if c[0] == "NOT RUN")
    if fails:
        verdict, vcls = f"NOT READY: {fails} check(s) failed", "FAIL"
    elif cautions or not_run:
        verdict, vcls = f"PROMISING, NOT PROVEN: {cautions} caution(s), {not_run} test(s) not run", "CAUTION"
    else:
        verdict, vcls = "PASSES THE AUTOMATIC CHECKS: now the manual ones", "PASS"

    def kpi(key, label, val):
        return f'<div class="kpi"><span class="k">{label}</span><b>{val}</b><small>{_e(EXPLAIN.get(key, ""))}</small></div>'

    kpis = "".join([
        kpi("expectancy_r", "Expectancy", f"{_f(s['expectancy_r'], 3, sign=True)}R"),
        kpi("win_rate", "Win rate", _f(s["win_rate"], 0, pct=True)),
        kpi("payoff", "Payoff", _f(s["payoff"])),
        kpi("profit_factor", "Profit factor", _f(s["profit_factor"])),
        kpi("total_r", "Total", f"{_f(s['total_r'], 1, sign=True)}R"),
        kpi("max_dd_r", "Max drawdown", f"{_f(s['max_dd_r'], 1)}R"),
        kpi("worst_streak", "Worst losing streak", str(s["worst_streak"])),
        kpi("sharpe", "Sharpe", _f(s["sharpe"])),
        kpi("t_stat", "t-stat", _f(s["t_stat"])),
        kpi("trades_per_year", "Trades a year", _f(s["trades_per_year"], 0)),
        kpi("recovery", "Recovery", _f(s["recovery"])),
        f'<div class="kpi"><span class="k">Per contract</span><b>{_usd(s["total_usd"])}</b><small>Net of costs, one contract, {_usd(s["avg_usd"])} a trade.</small></div>',
    ])
    rows = "".join(f'<tr><td><span class="st {c[0].replace(" ", "")}">{c[0]}</span></td><td><b>{_e(c[1])}</b><br>'
                   f'<span style="color:var(--muted)">{_e(c[2])}</span></td></tr>' for c in checks)
    parts = [f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(rep.strategy)} · {_e(rep.market)} · Klaxton Strategy Lab</title><style>{CSS}</style></head><body><div class="wrap">
<h1>{_e(rep.strategy)} <span>· {_e(rep.market)}</span></h1>
<p class="sub">Klaxton Strategy Lab tear sheet · {s['first']:%d %b %Y} to {s['last']:%d %b %Y} · generated {datetime.now():%d %b %Y %H:%M}<br>
Settings: <code>{_e(', '.join(f'{k}={v}' for k, v in rep.params.items() if v not in (None, '')))}</code><br>Costs: {_e(rep.costs_note)}</p>
<div class="card"><div class="verdict"><span class="st {vcls}">{_e(verdict)}</span></div>
<p style="margin:8px 0 0;color:var(--muted)">Results below are after costs, in R (1R = the risk on each trade). Nothing goes live until every row is PASS or a ticked MANUAL.</p></div>
<h2>1 · Validation checklist</h2><div class="tw"><table>{rows}</table></div>
<h2>2 · Headline numbers</h2><div class="kpis">{kpis}</div>
<h2>3 · Equity curve and drawdown</h2><div class="card">{_equity_svg(t, len(oos_t))}</div>
<div class="grid2"><div><h2>Trade results (R)</h2><div class="card">{_hist_svg(t['r'])}</div></div>
<div><h2>By year</h2><div class="card">{_years_svg(by_year)}</div></div></div>
<h2>4 · Robustness</h2>"""]
    rob = []
    rob.append(f"<p><b>Older vs recent trades.</b> First {100 - rep.oos_frac * 100:.0f}%: {is_s['n']} trades, "
               f"{_f(is_s['expectancy_r'], 3, sign=True)}R a trade. Most recent {rep.oos_frac * 100:.0f}%: {oos_s.get('n', 0)} trades, "
               f"{_f(oos_s.get('expectancy_r'), 3, sign=True)}R a trade. A real edge holds up in the recent period.</p>")
    if rep.stress:
        rob.append(f"<p><b>Cost stress.</b> At 1.5× slippage: {_f(rep.stress.get('expectancy_r'), 3, sign=True)}R a trade, "
                   f"{_f(rep.stress.get('total_r'), 1, sign=True)}R total.</p>")
    if rep.mc:
        mc = rep.mc
        rob.append(f"<p><b>Monte Carlo</b> ({mc.sims:,} reshuffles of {mc.trades} trades). Typical drawdown {_f(mc.dd_p50, 1)}R; "
                   f"bad case (1 in 20) {_f(mc.dd_p95, 1)}R; very bad (1 in 100) {_f(mc.dd_p99, 1)}R. "
                   f"Losing streak, bad case: {mc.streak_p95}. Total after {mc.trades} trades: median {_f(mc.total_p50, 1)}R, "
                   f"bad case {_f(mc.total_p05, 1)}R."
                   + (f" <b>Account check:</b> at {_usd(mc.risk_usd)} a trade, {mc.p_breach * 100:.1f}% of paths lose "
                      f"{_usd(mc.limit_usd)} from a peak." if mc.p_breach is not None else "") + "</p>")
    if rep.wf_windows is not None and len(rep.wf_windows):
        rob.append("<p><b>Walk-forward.</b> Each row: settings picked on the year before, then traded on the next window.</p>"
                   + _table(rep.wf_windows))
    if rep.sweep is not None and len(rep.sweep):
        rob.append(f"<p><b>Parameter sweep.</b> Every combination tested. Look for a broad area of green, not one lucky cell."
                   + (f" Plateau score: {rep.plateau * 100:.0f}% of neighbours profitable." if rep.plateau is not None else "")
                   + "</p>" + _table(rep.sweep.sort_values("expectancy_r", ascending=False)))
    parts.append('<div class="card">' + "".join(rob) + "</div>")
    if t["tag"].nunique() > 1:
        parts.append("<h2>5 · By setup</h2>" + _table(metrics.by(t, "tag"), {"win_rate": lambda v: _f(v, 0, pct=True),
                                                                              "total_usd": _usd}))
    parts.append("<h2>By year</h2>" + _table(by_year, {"win_rate": lambda v: _f(v, 0, pct=True), "total_usd": _usd}))
    if rep.notes:
        parts.append("<h2>Notes</h2><div class=\"card\"><ul>" + "".join(f"<li>{_e(n)}</li>" for n in rep.notes) + "</ul></div>")
    cols = ["tag", "side", "entry_time", "exit_time", "entry", "stop", "target", "exit", "result", "r", "net_usd"]
    tl = t[cols].copy()
    for c in ("entry_time", "exit_time"):
        tl[c] = pd.to_datetime(tl[c]).dt.tz_convert("Australia/Sydney").dt.strftime("%a %d %b %y %H:%M")
    parts.append(f"<h2>All trades ({len(t)}), Sydney time</h2><details><summary>Show the trade list</summary>"
                 + _table(tl, {"r": lambda v: f"{v:+.2f}", "net_usd": _usd, "target": lambda v: "—" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:g}"})
                 + "</details>")
    parts.append("</div></body></html>")
    return "".join(parts)


def save(rep: Report, name: str | None = None) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    import re
    fname = name or re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{rep.strategy}_{rep.market}_{stamp}").strip("_") + ".html"
    p = REPORT_DIR / fname
    p.write_text(render(rep), encoding="utf-8")
    return p
