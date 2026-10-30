# Klaxton Strategy Lab

Stage 0 of the Klaxton Quant pathway: one place to test any strategy honestly.
Research only. Nothing here places orders, and the live desk (`scanner/`, Railway) is untouched.

## What's in it

| File | What it does |
|---|---|
| `lab/data.py` | Data store. Pulls ProjectX history for every contract month, stitches a continuous series (volume roll on the Globex trading day), saves it to `lab_data/` as Parquet. Loads raw or back-adjusted (roll gaps removed). |
| `lab/engine.py` | Backtest engine. Strategies return **Setups** (decision time, side, entry, stop, target, time exit); the engine plays them through the bars that come after. Stop before target in the same bar; the target can't count on a limit order's fill bar; gaps fill at the open; costs (commission + slippage) on every trade. Includes the **look-ahead detector**. |
| `lab/metrics.py` | Expectancy, win rate, payoff, profit factor, drawdown, losing streak, Sharpe, Sortino, t-stat, bootstrap confidence range, breakdowns by setup/year/weekday. All in R after costs, plus $ per contract. |
| `lab/robust.py` | Monte Carlo (drawdown spread + chance of hitting an account limit), parameter sweep + plateau score, walk-forward (choose settings on the past, trade the future), cost stress (1.5× slippage). |
| `lab/tearsheet.py` | One self-contained HTML tear sheet per run: verdict, the 12-point validation checklist auto-ticked, headline numbers with plain-English notes, charts, robustness, trade list. |
| `lab/strategies/` | One file per strategy. `retrace.py` is the NKD Retrace Rule, the reference strategy. |

## Commands

```
python -m lab demo                                   # tear sheet from made-up data (no login needed)
python -m lab pull NKD,NQ,ES --tf 1h --days 730      # download history (uses the desk's ProjectX keys in .env)
python -m lab list                                   # what's in the store
python -m lab compare-retrace --root NKD             # STAGE 0 GATE: Lab vs the old Retrace backtest
python -m lab run retrace --root NKD --only "Bull WED,Bear THU" --risk 275 --limit 4500
python -m lab run retrace --root NKD --set mode=atr --sweep stop=0.18,0.22,0.26 target=0.6,0.8,1.0 --walk-forward
python -m lab check retrace --root NKD               # look-ahead test only
```

Tear sheets and trade CSVs land in `lab_reports/`. Costs default to an estimate ($4 round trip, $1.50 for micros, 1 tick slippage a side): set real fees with `--commission` and `--slip-ticks`.

## Stage 0 gate

`compare-retrace` runs the Lab's Retrace and `scanner/retrace_backtest.py` on the same bars with no costs. On test data they agree trade for trade. The one known difference: when the target is touched on the entry bar, the old study holds the trade to the day's close (ignoring the stop); the Lab keeps managing it. That can make the old study **overstate** big days.

## Adding a strategy

1. Write the hypothesis first: the rule, why the edge should exist, what would kill it.
2. Copy `lab/strategies/retrace.py`, change `setups()`. Each Setup may only use bars that start before its `decided_at`.
3. Register it in `lab/strategies/__init__.py`.
4. `python -m lab check <name> --root X` must say "No look-ahead found".
5. Run it with `--sweep ... --walk-forward --risk ... --limit ...` and read the checklist.

## Reading a tear sheet (short version)

- **Expectancy (R)** after costs is the number that pays. Win rate means nothing without payoff.
- **t-stat under 2**, or a **confidence range that includes 0**: luck could explain the result.
- **Walk-forward** is the honest out-of-sample test, because it includes the act of picking settings.
- **Plateau**: neighbouring settings should also work. One lucky cell is curve fitting.
- **Monte Carlo**: the backtest drawdown is one ordering of the trades. Plan for the bad case (95th percentile), not the backtest's own number.
