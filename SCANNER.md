# Klaxton Desk Scanner

Scans every Topstep-tradable futures market through the ProjectX API every 15 minutes, ranks them by how far each one is from its own normal behaviour, runs the Four Gates, and posts to Discord.

## What it posts

- **👀 WATCH ideas**: a market that is moving hard, has a direction from liquidity (a sweep of a prior day, week or Monday high/low) or displacement, has a liquidity target at least 2R away, and isn't fighting crowded positioning. One post per market, direction and trading day.
- **📊 Biggest movers**: the top markets by score, before the Asia, London and New York opens.

Each Watch post shows:

- **Thesis:** entry zone, stop beyond the sweep, target at the next untouched liquidity, and the R multiple.
- **Gate 2 · Positioning:** CFTC COT. Leveraged funds for financial markets, managed money for commodities, each as a 3-year percentile.
- **Gate 3 · Options:** CBOE implied vol against realised vol, plus the options structure to use. Covers VIX, VXN, RVX, VXD, OVX and GVZ.
- **Gate 4 · Catalysts:** high-impact events in the holding window, for the currencies that move that market, plus EIA reports for energy.

"Take" calls are switched off until the playbook entry signatures are added and backtested on ProjectX history.

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
4. Deploy. The logs show `Scanner running every 15 min`.

## Commands

```
python -m scanner.main --list-contracts   # what ProjectX offers and how it maps (✓ = scanned)
python -m scanner.main --once --dry-run   # one scan, print the posts instead of sending
python -m scanner.main --once             # one scan, post to Discord
python -m pytest -q tests                 # tests on synthetic data
```
