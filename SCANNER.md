# Klaxton Desk Scanner

Scans every Topstep-tradable futures market through the ProjectX API every 15 minutes, ranks them by how far each one is from its own normal behaviour, runs the Five Gates, and posts to Discord.

## The desk (v2, Oct 2026)

| Agent | What it does | Where it runs |
|---|---|---|
| **Scanner** | Finds candidate ideas across 36 markets (range, displacement, volume, sweeps, room to target) | This service, every 15 min |
| **Technicals** | Daily trend regime per market: 200-day state and slope, ROC 50 and its percentile, stretched / flattening / reset flags, size ceiling (full, half, starter) | This service (`scanner/regime.py`) |
| **News** | Trump (trumpstruth.org archive), White House, Fed policy and speeches, Bank of Japan, CNBC headlines, tagged by market. Posts market-moving ones; morning Forex Factory digest at 8am Sydney | This service, every 5 min (`scanner/news.py`) |
| **Macro** | Daily direction call per market (bullish / bearish / neutral, conviction 1-3) from macro data, news and COT positioning | Scheduled Claude run, 8:24am Sydney weekdays; writes `desk/macro_call.json` on the `desk-data` branch |
| **ACD** (off: `ACD_ENABLED=true` to bring back) | Mark Fisher's opening-range method on each market's own open: A up / A down confirmed, failed A (a trap / sweep), C (the strong reversal), with the prior day's pivot range. Batched into one post per scan, plus an ACD line on every Watch idea | This service (`scanner/acd.py`), each scan |
| **Four Charts** | Daily bias read of Crown's four charts: oil curve (front vs next year's December), US 10-year box, RSP/SPY breadth, MAGS/SOXX inside AI. Each chart votes on NKD, NQ, ES, YM, RTY, CL and GC; post shows conviction HIGH / MEDIUM / LOW and the best-aligned markets | This service (`scanner/four_charts.py`), weekdays 9:50am Sydney to `#general` (after the 9:37 macro call, before the 10am NKD reopen) |
| **COT board** | Weekly ranking of every commodity by how stretched hedge funds are (3-year percentile), with commercials alongside and a flag when the two sit at opposite extremes | This service (`scanner/cot_board.py`), Saturday 9am Sydney, catches up if missed |
| **Daily setups** | After each daily close, every market's candle is checked for a **daily SFP** (took an untouched daily swing high/low or last week's high/low and closed back) and for **weekly frameworks**: classic expansion raid (Tuesday SFP of Monday), midweek reversal, Thursday reversal → Friday continuation, continuation days (Wednesday beyond 50% of Tuesday; small Monday and Tuesday closing outside it), consolidation weeks, weekly SFP on Fridays. Each gets a 0-100 score, stop beyond the SFP, target at the other end of the leg that made the level, OTE at 50% of the wick, and macro / COT / 200-day confluence (shown, never filtered). Every line also shows the **expected daily move** (1 STD = price × vol ÷ 100 ÷ 16, from the implied-vol index where there is one: OVX for crude, GVZ for gold, VIX/VXN for equities; otherwise 20-day realised vol), and the card ends with an expected-move table for every energy, metals and ags market. Setups to investigate on the hourly, not trades | This service (`scanner/daily_setups.py`), weekdays 17:15 New York (8:15am Sydney) to `DISCORD_DAILY_WEBHOOK`, else the desk channel |
| **Head of desk** | Checks every idea before you see it (below). Only verified ideas reach the desk channel | This service (`scanner/desk.py`) |

### Head of desk checks

Code rules only: fast, free and predictable. An idea is **verified** only if nothing fails.

1. **Data** is fresh (last bar under 90 min old, `DATA_STALE_MIN`)
2. **Levels** line up: stop, entry and target on the right sides, R:R at least 2 (`MIN_RR`) and adds up
3. **Stop size** is 0.1 to 1.5 daily ATR
4. **Entry** is a live price inside today's range
5. **Trend regime** sets the size ceiling. It flags counter-trend ideas but never rejects on its own
6. **Macro call**: rejected if today's call is the other way; neutral is a warning
7. **Positioning**: rejected if the idea joins a crowded COT position
8. **Catalyst**: rejected if a high-impact event is under 60 min away (`CATALYST_BLOCK_MIN`); a warning if it's inside the hold
9. **Headlines**: a warning if a high-priority headline hit this market in the last 30 min (`NEWS_BLOCK_MIN`)
10. **Conflict**: rejected if the opposite idea on the same market was verified today

Raw ideas still go to `#scanner`, each with the verdict. Verified ideas go to the desk channel with every check listed and the size ceiling applied. The daily idea cap (`MAX_IDEAS_PER_DAY`) now counts verified ideas. The leaderboard shows the 200-day state per market and the day's verified / rejected tally.

## What it posts

- **👀 WATCH ideas**: a market that is moving hard, has a direction from liquidity (a sweep of a prior day, week or Monday high/low) or displacement, has a liquidity target at least 2R away, and isn't fighting crowded positioning. One post per market, direction and trading day.
- **📏 Size**: contracts for every account in `RISK_ACCOUNTS` (default `Topstep:1000,Tradeify:275`). Information only: a stop too wide for an account never blocks an idea.
- **📊 Biggest movers**: the top markets by score, before the Asia, London and New York opens.

Each Watch post shows:

- **Thesis:** entry zone, stop beyond the sweep, target at the next untouched liquidity, and the R multiple.
- **Gate 2 · Positioning:** CFTC COT. Leveraged funds for financial markets, managed money for commodities, each as a 3-year percentile.
- **Gate 3 · Options:** CBOE implied vol against realised vol, plus the options structure to use. Covers VIX, VXN, RVX, VXD, OVX and GVZ.
- **Gate 4 · Catalysts:** high-impact events in the holding window, for the currencies that move that market, plus EIA reports for energy.

"Take" calls are switched off until the playbook entry signatures are added and backtested on ProjectX history.

## ACD (opening range)

| Market group | Opening range starts | Session ends |
|---|---|---|
| ES, NQ, YM, RTY | 9:30 ET | 15:05 CT (Topstep flat) |
| NKD | 9:00 Tokyo | 15:30 Tokyo |
| CL, NG, RB, HO | 9:00 ET | 15:05 CT |
| GC 8:20, SI 8:25, HG 8:10, PL 8:20 ET | | 15:05 CT |
| Rates and FX | 8:20 ET | 15:05 CT |
| Grains 8:30 CT (end 13:15), meats 8:30 CT (end 13:00) | | |

- Opening range: first `ACD_OR_MIN` minutes (15). A value: `ACD_A_ATR` x 20-day ATR (0.10).
- **A confirmed:** closes beyond the A level for half the OR time in a row.
- **Failed A:** touched the A level, then closed back inside the OR. Leans the other way; invalid beyond the extreme it reached.
- **C:** after a failed A, price holds the opposite A level. The strongest reversal in the method.
- Pivot range from the prior day ((H+L)/2 and (H+L+C)/3); a narrow one (bottom 20% of 20 days) is flagged as trend-day risk.
- Signals are context for your entry signature, never orders.

**Backtest it before trusting it:** `python -m scanner.main --acd-backtest GC,SI,CL,HG,NQ,ES --days 180` (add `--a-atr 0.1,0.15,0.2` to compare A values). It stitches the monthly contracts from ProjectX, runs the same code the live agent uses, and prints win %, average R and total R per market and signal. Takes a few minutes per market because of the API rate limit. No costs or slippage included.

## Earnings (Oct 2026)

Forex Factory has no company earnings, so `scanner/earnings.py` adds them.

- **US:** Nasdaq's public earnings calendar (no key), next 7 days, filtered to the
  names that move NQ/ES on their own: AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA, AVGO,
  NFLX, AMD, SPCX, ORCL, COST, TSM. Pre-market reports are timed at 7:00am New York,
  after-hours at 4:05pm, unconfirmed times as after the close.
- **Japan:** `earnings_jp.json` in the repo root (Advantest, Tokyo Electron, SoftBank
  Group). Nasdaq doesn't list Tokyo reports, so edit this file when a date is
  confirmed on the company's IR page. Default time is after the 15:30 Tokyo close;
  add `"time": "HH:MM"` (Tokyo) to set it exactly.
- **Where it shows:** the catalyst gate warns only the markets a report moves (US
  names → NQ, ES; Japanese → NKD), the head of desk blocks entries in the last
  `CATALYST_BLOCK_MIN` before one, and the 8am digest gets a 💼 Earnings section
  (next 24h, plus the rest of the week).
- **Env vars (optional):** `EARNINGS_WATCH_US` (comma list of tickers),
  `EARNINGS_US_ROOTS` (default `NQ,ES`), `EARNINGS_LOOKAHEAD_DAYS` (default 7),
  `EARNINGS_JP_FILE`.
- If Nasdaq is unreachable the log shows `Nasdaq earnings <date> failed` and
  everything else carries on without earnings.

## Guardrails (from the Oct 2026 trade-history autopsy)

- **No Watch ideas 3pm-9pm Sydney** (London: 1 win in 5) and no London leaderboard.
- **At most 2 Watch ideas per trading day** (`MAX_IDEAS_PER_DAY`).
- **Account watch, read-only, every minute**, on every active account the ProjectX login can see:
  - 🛑 a 30-minute cool-down after any losing trade (`COOLDOWN_MIN`)
  - ⏱️ a reminder when a trade closes inside 10 minutes (`QUICK_EXIT_MIN`)
  - ⚠️ a warning when size goes up after a loss
  - ✅ "that's the day" after 2 round trips (`MAX_TRADES_PER_DAY`)
  - ⛔ a personal daily stop message when `PERSONAL_DAILY_STOP` (dollars) is set
  - 🏁 a consistency-cap message when the day's profit reaches `DAILY_PROFIT_CAP` (dollars)
- The scanner never places, changes or cancels orders.

## Testing desk v2 without touching the live scanner

1. In the Railway project, add a **second service** from this repo on the **`desk-v2`** branch, start command `python -m scanner.main`.
2. Give it the same ProjectX variables, but point all three webhooks at **test channels** (e.g. `#desk-test`, `#news-test`) so the live `#scanner` is untouched.
3. Watch it for a few sessions. When you're happy, merge `desk-v2` into `scanner` and delete the test service.

## Score (0-100)

| Part | Points | Full marks at |
|---|---|---|
| Day range vs 20-day ATR | 20 | 2.0x |
| Week range vs average week | 10 | 1.6x |
| Displacement (largest recent 1h body vs average) | 15 | 4x |
| Volume vs same hours on the last 20 days | 15 | 2.5x |
| Liquidity event (sweep: week 20, day 12, Monday 10; break and hold: half) | up to 20 | |
| Room to target | 15 at 3R, 8 at 2R | |
| Weekly profile agrees (C2 or the Wednesday 50% continuation rule) | 5 | |

## Markets

ES, NQ, YM, RTY, NKD · 6E, 6B, 6J, 6A, 6C, 6S, 6M, 6N · ZT, ZF, ZN, TN, ZB, UB · CL, NG, RB, HO · GC, SI, HG, PL · ZC, ZW, ZS, ZM, ZL, HE, LE · MBT, MET.
Data is pulled for the full-size contract; each idea lists the micro you can trade (MES, MNQ, MGC, MCL…).

## Deploy on Railway

1. In the Railway project, add a **new service** from this GitHub repo.
2. **Start command:** `python -m scanner.main`
3. **Variables** (Settings → Variables). You enter these yourself; they never go in the code:
   - `PROJECTX_USERNAME`: your TopstepX username
   - `PROJECTX_API_KEY`: your ProjectX API key
   - `DISCORD_SCANNER_WEBHOOK`: webhook URL of the Discord channel for scanner posts
   - optional: `PROJECTX_LIVE_DATA=true` if your API plan includes live data (default uses sim data)
   - optional: `SCAN_INTERVAL_MIN` (15), `WATCH_MIN_SCORE` (55), `HOLD_WINDOW_HOURS` (24), `EXCLUDE_ROOTS` (e.g. `HE,LE`)
   - desk v2: `DISCORD_DESK_WEBHOOK` (verified ideas), `DISCORD_NEWS_WEBHOOK` (headlines and the morning calendar)
   - Four Charts: `DISCORD_GENERAL_WEBHOOK` (the #general channel; falls back to the desk channel). Optional: `FOUR_CHARTS_HOUR_SYD` (8), `FOUR_CHARTS_MINUTE_SYD` (40), `CURVE_BACK_SYMBOL` (default December of next year, e.g. `CLZ27.NYM`)
   - ACD and COT board, optional: `DISCORD_ACD_WEBHOOK` (defaults to the scanner channel), `DISCORD_COT_WEBHOOK` (defaults to the desk channel, then scanner), `ACD_ROOTS` (e.g. `GC,SI,CL,HG,NQ,ES`; empty = all), `ACD_OR_MIN` (15), `ACD_A_ATR` (0.10), `COT_BOARD_HOUR_SYD` (9), `COT_EXTREME_PCT` (90)
   - desk v2, optional: `NEWS_INTERVAL_MIN` (5), `NEWS_BLOCK_MIN` (30), `CATALYST_BLOCK_MIN` (60), `DATA_STALE_MIN` (90), `MIN_RR` (2), `REGIME_EXHAUSTED_PCT` (90), `REGIME_RESET_BAND` (2.0), `MACRO_CALL_URL`, `MACRO_MAX_AGE_HOURS` (26), `GITHUB_TOKEN` (only if the repo is made private), `NEWS_FEEDS_OFF` (e.g. `cnbc_world,whitehouse`)
4. Deploy. The logs show `Scanner running every 15 min`.

## Commands

```
python -m scanner.main --list-contracts   # what ProjectX offers and how it maps (✓ = scanned)
python -m scanner.main --once --dry-run   # one scan, print the posts instead of sending
python -m scanner.main --regime           # 200-day regime for every market
python -m scanner.main --news             # latest market-moving headlines
python -m scanner.main --four-charts      # today's Four Charts post, printed
python -m scanner.main --cot-board        # this week's commodity COT extremes board
python -m scanner.main --acd-backtest GC,SI,CL,NQ --days 180   # ACD backtest on ProjectX history
python -m scanner.main --retrace-backtest NKD,ES,CL,GC --days 2500   # weekday Retrace Rule backtest
python -m scanner.main --once             # one scan, post to Discord
python -m pytest -q tests                 # tests on synthetic data
```


## Daily setups backtest

    python -m scanner.main --daily-backtest CL,GC,SI,HG,6J,6E,NKD,NQ,ES --days 365

Finds every daily SFP in the stitched hourly history and trades it two ways: entry at the next 6PM open, and a limit at the OTE (50% of the SFP wick) live for the next day only. Stop beyond the SFP, target 1, or out after 5 days. Results by score band, weekly framework, market and weekday, saved to `daily_sfp_backtest_<date>.csv`. `--daily-setups` prints today's post without sending it.


## Retrace Rule backtest (weekday continuation, Oct 2026)

    # 1. Check it against the TradingView study (same rules: 50t stop, 185t target, entry at the open)
    python -m scanner.main --retrace-backtest NKD --rr-mode ticks --since 2025-01-01 --days 700
    # 2. Go further back and across markets, with stop/target scaled to each market's daily range
    python -m scanner.main --retrace-backtest NKD,ES,NQ,YM,RTY,CL,GC,SI,HG,6J,6E,ZN --days 2500

A Python copy of the Pine "6PM Retrace Rule Study". Bull day = yesterday closed above the day before (bear = below); enter at the 6PM New York open, stop past the open, target from the open, hourly bars with the adverse side checked first, otherwise out at the day's close. Every weekday, bull and bear, for every market.

`--rr-mode` sets the units: `ticks` (fixed, like the Pine study), `pct` (percent of price) or `atr` (multiples of the 20-day ATR, the default: 0.22 stop / 0.80 target, about NKD's 50 / 185 ticks against its recent daily range). Use `pct` or `atr` to compare years and markets fairly. Change them with `--rr-stop`, `--rr-target`, `--rr-entry`; `--rr-cont lowhigh|half` switches the bull/bear definition.

Prints, per market and setup: trades, % that hit target, average and total R, $ per trade on one contract, worst losing streak, deepest drawdown in R, total R per year, and whether every year with 10+ trades was positive. The first line for each market says how far back ProjectX history actually went. Saves `retrace_trades_<date>.csv` and `retrace_summary_<date>.csv`. No costs or slippage.
