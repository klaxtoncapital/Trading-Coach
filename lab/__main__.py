"""Klaxton Strategy Lab: command line.

  python -m lab demo                                  # a tear sheet from made-up data, to see the format
  python -m lab pull NKD,NQ,ES --tf 1h --days 730     # download history into lab_data/
  python -m lab list                                  # what's in the data store
  python -m lab run retrace --root NKD --only "Bull WED,Bear THU" --risk 275 --limit 4500
  python -m lab run retrace --root NKD --sweep stop=40,50,60 target=150,185,220 --walk-forward
  python -m lab compare-retrace --root NKD            # Stage 0 gate: Lab vs the old backtest
  python -m lab check retrace --root NKD              # look-ahead test on its own

Strategy settings go in as --set key=value (or the shortcuts --only / --since).
Every run writes an HTML tear sheet to lab_reports/ and a CSV of the trades.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from . import data, engine, metrics, robust, tearsheet
from .strategies import get

# Round-trip commission estimates ($ per contract). Replace with your real fees:
# your ProjectX trade export shows them. Micros are cheaper.
COMMISSION_RT = {"default": 4.0, "micro": 1.5}
MICROS = {"MNQ", "MES", "M2K", "MYM", "MGC", "SIL", "MCL", "MHG", "M6E", "M6B", "M6A", "MBT", "MET", "MNG", "MNK"}


def _val(v: str):
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    return {"true": True, "false": False, "none": None}.get(v.lower(), v)


def _kv(items: list[str] | None) -> dict:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        out[k.strip()] = _val(v.strip())
    return out


def _grid(items: list[str] | None) -> dict[str, list]:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        out[k.strip()] = [_val(x.strip()) for x in v.split(",") if x.strip()]
    return out


def market_costs(root: str, meta: dict, args) -> tuple[engine.Costs, str]:
    tick = args.tick or meta.get("tick_size") or 0.25
    pv = args.point_value or meta.get("point_value") or 1.0
    if args.no_costs:
        return engine.Costs(0.0, 0.0, tick, pv), "none (--no-costs)"
    comm = args.commission if args.commission is not None else COMMISSION_RT["micro" if root in MICROS else "default"]
    c = engine.Costs(comm, args.slip_ticks, tick, pv)
    note = (f"${comm:.2f} commission a round trip + {args.slip_ticks:g} tick slippage per side on market fills, stops and "
            f"time exits (tick {tick:g} = ${tick * pv:,.2f}). Estimates: replace with your real fees.")
    return c, note


def load_bars(strategy, root: str, args) -> pd.DataFrame:
    return data.load(root, strategy.timeframe, adjusted=not args.raw)


def params_for(strategy, root: str, meta: dict, args, costs: engine.Costs) -> dict:
    p = {k: v for k, v in strategy.defaults.items()}
    p.update(_kv(args.set))
    if args.only:
        p["only"] = args.only
    if args.since:
        p["since"] = args.since
    if "tick" in p and not p["tick"]:
        p["tick"] = costs.tick
    return strategy.resolve(p) if hasattr(strategy, "resolve") else p


def cmd_run(args) -> None:
    strat = get(args.strategy)
    root = args.root.upper()
    meta = data.load_meta(root)
    bars = load_bars(strat, root, args)
    costs, note = market_costs(root, meta, args)
    params = params_for(strat, root, meta, args, costs)
    report_and_print(strat, f"{root} {strat.timeframe}", bars, costs, note, params, args)


def report_and_print(strat, market: str, bars: pd.DataFrame, costs: engine.Costs, note: str, params: dict, args,
                     extra_notes: list[str] | None = None) -> Path:
    gap = not params.get("pine_compat", False)
    trades = engine.simulate(bars, strat.setups(bars, **params), costs, gap)
    if trades.empty:
        print("No trades. Check the settings and the data range.")
        sys.exit(1)
    s = metrics.summary(trades)
    rep = tearsheet.Report(strat.name, market, params, strat.hypothesis, trades, note, notes=list(extra_notes or []))
    print(f"\n{strat.name} on {market}: {s['n']} trades, {s['first']:%d %b %Y} to {s['last']:%d %b %Y}")
    print(f"  expectancy {s['expectancy_r']:+.3f}R (after costs)  win {s['win_rate']:.0%}  total {s['total_r']:+.1f}R  "
          f"max DD {s['max_dd_r']:.1f}R  t-stat {s['t_stat']:.2f}")
    print("  looking for look-ahead...", end=" ", flush=True)
    rep.lookahead = engine.lookahead_check(strat, bars, params)
    print("clean" if rep.lookahead[0] else "PROBLEM: " + rep.lookahead[1][0])
    rep.stress = robust.cost_stress(strat, bars, costs, params, 1.5, gap)
    rep.mc = robust.monte_carlo(trades["r"], args.risk, args.limit, args.mc_trades)
    print(f"  Monte Carlo bad-case drawdown {rep.mc.dd_p95:.1f}R"
          + (f", {rep.mc.p_breach:.1%} chance of losing ${args.limit:,.0f}" if rep.mc.p_breach is not None else ""))
    if args.sweep:
        spec = _grid(args.sweep)
        print(f"  sweeping {len(robust.grid(spec))} combinations...")
        rep.sweep = robust.sweep(strat, bars, costs, spec, params, gap)
        rep.plateau = robust.plateau_score(rep.sweep, {k: params[k] for k in spec if k in params})
        if args.walk_forward:
            print("  walk-forward...")
            rep.wf_trades, rep.wf_windows = robust.walk_forward(strat, bars, costs, spec, params, args.train_days,
                                                                args.test_days, args.min_trades, gap_fills=gap)
            if len(rep.wf_trades):
                w = metrics.summary(rep.wf_trades)
                print(f"  walk-forward out-of-sample: {w['n']} trades, {w['expectancy_r']:+.3f}R a trade")
    elif args.walk_forward:
        print("  (walk-forward needs --sweep: it has to choose between settings)")
    p = tearsheet.save(rep)
    csv = p.with_suffix(".csv")
    trades.to_csv(csv, index=False)
    print(f"\nTear sheet: {p}\nTrades:     {csv}")
    return p


def cmd_check(args) -> None:
    strat = get(args.strategy)
    root = args.root.upper()
    meta = data.load_meta(root)
    bars = load_bars(strat, root, args)
    costs, _ = market_costs(root, meta, args)
    ok, probs = engine.lookahead_check(strat, bars, params_for(strat, root, meta, args, costs), samples=100)
    print("No look-ahead found." if ok else "LOOK-AHEAD:\n  " + "\n  ".join(probs))


def compare_retrace(bars: pd.DataFrame, root: str, tick: float, pv: float, params: dict) -> tuple[pd.DataFrame, dict]:
    """Stage 0 gate: the Lab's Retrace vs scanner/retrace_backtest.py on the same bars, no costs."""
    from scanner import retrace_backtest as rb
    from .strategies.retrace import Retrace

    s = rb.Settings(mode=params.get("mode", "ticks"), stop=params.get("stop"), target=params.get("target"),
                    entry=params.get("entry", 0.0) or 0.0, cont=params.get("cont", "close"),
                    atr_len=params.get("atr_len", 20))
    since = pd.Timestamp(params["since"]).date() if params.get("since") else None
    old = pd.DataFrame(rb.backtest_market(root, data.to_records(bars), tick, pv, s, since))
    lp = {**params, "pine_compat": True, "tick": tick}
    new = engine.simulate(bars, Retrace().setups(bars, **lp), engine.Costs(0, 0, tick, pv), gap_fills=False)
    new["date"] = [str(x) for x in data.trading_days(bars).reindex(new["entry_time"]).values] if len(new) else []
    o = old.set_index("date")[["result", "r"]] if len(old) else pd.DataFrame(columns=["result", "r"])
    nn = new.set_index("date")[["result", "r"]] if len(new) else pd.DataFrame(columns=["result", "r"])
    nn = nn.assign(result=nn["result"].replace({"time": "close"}))
    j = o.join(nn, how="outer", lsuffix="_old", rsuffix="_lab")
    j["match"] = (j["result_old"] == j["result_lab"]) & (np.isclose(j["r_old"].astype(float), j["r_lab"].astype(float), atol=1e-3))
    stats = {"old_trades": len(o), "lab_trades": len(nn), "matching": int(j["match"].sum()),
             "old_total_r": float(o["r"].sum()) if len(o) else 0.0, "lab_total_r": float(nn["r"].sum()) if len(nn) else 0.0}
    return j, stats


def cmd_compare_retrace(args) -> None:
    root = args.root.upper()
    meta = data.load_meta(root)
    bars = data.load(root, "1h")
    tick = args.tick or meta.get("tick_size")
    pv = args.point_value or meta.get("point_value") or 1.0
    params = {"mode": "ticks", **_kv(args.set)}
    if args.since:
        params["since"] = args.since
    j, st = compare_retrace(bars, root, tick, pv, params)
    print_compare(j, st)


def print_compare(j: pd.DataFrame, st: dict) -> None:
    print(f"Old backtest: {st['old_trades']} trades, {st['old_total_r']:+.2f}R")
    print(f"Strategy Lab: {st['lab_trades']} trades, {st['lab_total_r']:+.2f}R")
    print(f"Same day, same result, same R: {st['matching']} of {max(st['old_trades'], st['lab_trades'])}")
    diff = j[~j["match"]]
    if len(diff):
        print("\nDays that differ (the old study holds a trade to the close once the target is touched on the entry bar,"
              " even through the stop; the Lab keeps managing it):")
        print(diff.to_string())
    else:
        print("Identical. Stage 0 gate passed.")


def synthetic(days: int = 900, seed: int = 5, drift: float = 0.0) -> pd.DataFrame:
    """Made-up hourly bars on the Globex calendar (for the demo and the tests)."""
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2023-01-01 18:00", tz="America/New_York")
    idx = []
    d = start
    for _ in range(days):
        if d.weekday() in (4, 5):          # Friday and Saturday evenings: no session
            d += pd.Timedelta(days=1)
            continue
        idx += [(d + pd.Timedelta(hours=k)).tz_convert("UTC") for k in range(23)]
        d += pd.Timedelta(days=1)
    n = len(idx)
    ret = rng.standard_t(4, n) * 0.0022 + drift
    c = 30000 * np.exp(np.cumsum(ret))
    o = np.concatenate([[30000], c[:-1]])
    rng_hl = np.abs(rng.normal(0, 0.0015, n)) * c
    h = np.maximum(o, c) + rng_hl
    l = np.minimum(o, c) - rng_hl
    df = pd.DataFrame({"o": o, "h": h, "l": l, "c": c, "v": rng.integers(100, 2000, n).astype(float),
                       "contract": "SYNTH"}, index=pd.DatetimeIndex(idx, name="t"))
    for col in ("o", "h", "l", "c"):
        df[col] = (df[col] / 5).round() * 5     # NKD-style 5-point ticks
    df["h"] = df[["o", "h", "c"]].max(axis=1)
    df["l"] = df[["o", "l", "c"]].min(axis=1)
    return df


def cmd_demo(args) -> None:
    strat = get("retrace")
    bars = synthetic()
    costs = engine.Costs(4.0, 1.0, 5.0, 5.0)
    params = strat.resolve({"tick": 5.0})
    args.sweep = ["stop=40,50,60", "target=150,185,220"]
    args.walk_forward = True
    report_and_print(strat, "DEMO (made-up data)", bars, costs,
                     "$4 commission + 1 tick slippage per side (demo values).", params, args,
                     ["DEMO: random made-up prices with no edge in them. Every check should come out unconvincing, "
                      "which is the point: this is what 'no edge' looks like."])


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m lab", description="Klaxton Strategy Lab")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--root", required=True, help="market, e.g. NKD")
        p.add_argument("--set", nargs="*", metavar="KEY=VALUE", help="strategy settings, e.g. mode=atr stop=0.22")
        p.add_argument("--only", help='setups to keep, e.g. "Bull WED,Bear THU"')
        p.add_argument("--since", help="first trade date, e.g. 2025-01-01")
        p.add_argument("--raw", action="store_true",
                       help="use raw stitched prices instead of back-adjusted (roll gaps left in)")
        p.add_argument("--tick", type=float, help="tick size (default: from the data store)")
        p.add_argument("--point-value", type=float, help="$ per point (default: from the data store)")
        p.add_argument("--commission", type=float, help="$ round trip per contract")
        p.add_argument("--slip-ticks", type=float, default=1.0, help="slippage ticks per side (default 1)")
        p.add_argument("--no-costs", action="store_true")

    def robust_opts(p):
        p.add_argument("--risk", type=float, help="$ risked per trade, for the account check (e.g. 275)")
        p.add_argument("--limit", type=float, help="account drawdown limit in $ (e.g. 4500)")
        p.add_argument("--mc-trades", type=int, help="trades per Monte Carlo path (default: same as the backtest)")
        p.add_argument("--sweep", nargs="*", metavar="KEY=V1,V2", help="settings grid, e.g. stop=40,50,60")
        p.add_argument("--walk-forward", action="store_true")
        p.add_argument("--train-days", type=int, default=365)
        p.add_argument("--test-days", type=int, default=90)
        p.add_argument("--min-trades", type=int, default=20)

    p = sub.add_parser("pull", help="download history from ProjectX")
    p.add_argument("roots")
    p.add_argument("--tf", default="1h", help="1m, 1h, 1d (comma-separated for several)")
    p.add_argument("--days", type=int, default=730)
    sub.add_parser("list", help="show the data store")
    p = sub.add_parser("run", help="backtest a strategy and write a tear sheet")
    p.add_argument("strategy")
    common(p)
    robust_opts(p)
    p = sub.add_parser("check", help="look-ahead test")
    p.add_argument("strategy")
    common(p)
    p = sub.add_parser("compare-retrace", help="Stage 0 gate: Lab vs the old Retrace backtest")
    common(p)
    p = sub.add_parser("demo", help="tear sheet from made-up data")
    robust_opts(p)
    args = ap.parse_args(argv)

    if args.cmd == "pull":
        from dotenv import load_dotenv
        load_dotenv()
        roots = [r.strip().upper() for r in args.roots.split(",") if r.strip()]
        asyncio.run(data.pull_and_save(roots, [t.strip() for t in args.tf.split(",")], args.days))
    elif args.cmd == "list":
        rows = data.available()
        if not rows:
            print(f"Data store is empty ({data.DATA_DIR}). Start with: python -m lab pull NKD --tf 1h")
        for r in rows:
            print(f"{r[0]:6} {r[1]:3} {r[2]:>8,} bars  {r[3]} to {r[4]}")
    elif args.cmd == "run":
        cmd_run(args)
    elif args.cmd == "check":
        cmd_check(args)
    elif args.cmd == "compare-retrace":
        cmd_compare_retrace(args)
    elif args.cmd == "demo":
        args.risk = args.risk or 275.0
        args.limit = args.limit or 4500.0
        cmd_demo(args)


if __name__ == "__main__":
    main()
