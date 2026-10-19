# Desk data

Files the Klaxton desk's scheduled agents write for the scanner to read.

- `macro_call.json`: the macro agent's daily direction call, written at 8:24am Sydney on weekdays.
  The scanner reads it from
  `https://raw.githubusercontent.com/klaxtoncapital/trading-coach/desk-data/desk/macro_call.json`
  and ignores it once it's more than 26 hours old.

Format:

```json
{
  "generated_at": "2026-10-20T08:24:00+11:00",
  "trading_day": "2026-10-20",
  "summary": "One or two sentences on the overnight picture.",
  "markets": {
    "NQ": {"bias": "bearish", "conviction": 2, "why": "One sentence.", "risk": "What would flip it."}
  }
}
```

`bias` is `bullish`, `bearish` or `neutral`; `conviction` is 1 to 3.
