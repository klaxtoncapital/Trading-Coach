"""Settings and the instrument registry.

Every value that can change between deployments comes from environment
variables. Secrets (the ProjectX API key, the Discord webhook) are only ever
read from the environment and never written anywhere else.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# ─────────────────────────────────────────────
# ENVIRONMENT
# ─────────────────────────────────────────────
PROJECTX_BASE_URL = os.getenv("PROJECTX_BASE_URL", "https://api.topstepx.com").rstrip("/")
PROJECTX_USERNAME = os.getenv("PROJECTX_USERNAME", "")
PROJECTX_API_KEY = os.getenv("PROJECTX_API_KEY", "")
# True = live market data subscription, False = sim data.
PROJECTX_LIVE_DATA = os.getenv("PROJECTX_LIVE_DATA", "false").lower() in ("1", "true", "yes")

DISCORD_SCANNER_WEBHOOK = os.getenv("DISCORD_SCANNER_WEBHOOK", "")
# Verified ideas only (the head of desk's channel). Unset = verified ideas aren't posted anywhere extra.
DISCORD_DESK_WEBHOOK = os.getenv("DISCORD_DESK_WEBHOOK", "")

# How often to scan, in minutes.
SCAN_INTERVAL_MIN = int(os.getenv("SCAN_INTERVAL_MIN", "15"))
# Minimum score (0-100) for a market to be posted as a Watch idea.
WATCH_MIN_SCORE = int(os.getenv("WATCH_MIN_SCORE", "55"))
# How many markets the leaderboard post lists.
LEADERBOARD_SIZE = int(os.getenv("LEADERBOARD_SIZE", "6"))
# Default holding window for the catalyst gate, in hours.
HOLD_WINDOW_HOURS = int(os.getenv("HOLD_WINDOW_HOURS", "24"))
# Comma-separated roots to skip, e.g. "HE,LE".
EXCLUDE_ROOTS = {s.strip().upper() for s in os.getenv("EXCLUDE_ROOTS", "").split(",") if s.strip()}

# Public data sources (no keys needed).
CFTC_BASE = "https://publicreporting.cftc.gov/resource"
CFTC_TFF = "gpe5-46if"            # Traders in Financial Futures (financials)
CFTC_DISAGG = "72hh-3qpy"         # Disaggregated (commodities)
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FF_CALENDAR = os.getenv(
    "FF_CALENDAR_URL", "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
)


# ─────────────────────────────────────────────
# INSTRUMENT REGISTRY
# ─────────────────────────────────────────────
@dataclass(frozen=True)
class Market:
    root: str                 # contract root the scanner pulls data for
    name: str
    asset_class: str          # equity / fx / rates / energy / metals / ags / crypto
    cot_report: str | None    # "tff" or "disagg"
    cot_code: str | None      # CFTC contract market code (None = match by name)
    cot_name_hint: str | None = None
    iv_series: str | None = None   # FRED implied-vol series
    micros: tuple[str, ...] = field(default_factory=tuple)  # tradable smaller contracts
    currencies: tuple[str, ...] = ("USD",)                # calendar currencies that move it


# Data is pulled for the full-size contract only (same price, better volume
# profile); the micro is listed so the idea says what can be traded small.
MARKETS: tuple[Market, ...] = (
    # Equity indices
    Market("ES", "S&P 500", "equity", "tff", "13874A", iv_series="VIXCLS", micros=("MES",)),
    Market("NQ", "Nasdaq-100", "equity", "tff", "209742", iv_series="VXNCLS", micros=("MNQ",)),
    Market("YM", "Dow", "equity", "tff", "124603", iv_series="VXDCLS", micros=("MYM",)),
    Market("RTY", "Russell 2000", "equity", "tff", "239742", iv_series="RVXCLS", micros=("M2K",)),
    Market("NKD", "Nikkei 225", "equity", "tff", "240743", currencies=("USD", "JPY")),
    # FX
    Market("6E", "Euro", "fx", "tff", "099741", micros=("M6E", "E7"), currencies=("USD", "EUR")),
    Market("6B", "British pound", "fx", "tff", "096742", micros=("M6B",), currencies=("USD", "GBP")),
    Market("6J", "Japanese yen", "fx", "tff", "097741", currencies=("USD", "JPY")),
    Market("6A", "Australian dollar", "fx", "tff", "232741", micros=("M6A",), currencies=("USD", "AUD")),
    Market("6C", "Canadian dollar", "fx", "tff", "090741", currencies=("USD", "CAD")),
    Market("6S", "Swiss franc", "fx", "tff", "092741", currencies=("USD", "CHF")),
    Market("6M", "Mexican peso", "fx", "tff", "095741", currencies=("USD",)),
    Market("6N", "New Zealand dollar", "fx", "tff", "112741", currencies=("USD", "NZD")),
    # Rates
    Market("ZT", "2-year note", "rates", "tff", "042601"),
    Market("ZF", "5-year note", "rates", "tff", "044601"),
    Market("ZN", "10-year note", "rates", "tff", "043602"),
    Market("TN", "Ultra 10-year", "rates", "tff", "043607"),
    Market("ZB", "30-year bond", "rates", "tff", "020601"),
    Market("UB", "Ultra bond", "rates", "tff", "020604"),
    # Energy
    # CFTC now lists NYMEX light sweet crude (CL) as "WTI-PHYSICAL".
    Market("CL", "Crude oil", "energy", "disagg", "067651", iv_series="OVXCLS", micros=("QM", "MCL")),
    Market("NG", "Natural gas", "energy", "disagg", "023651", micros=("QG", "MNG")),
    Market("RB", "RBOB gasoline", "energy", "disagg", "111659"),
    Market("HO", "Heating oil", "energy", "disagg", "022651"),
    # Metals
    Market("GC", "Gold", "metals", "disagg", "088691", iv_series="GVZCLS", micros=("MGC",)),
    Market("SI", "Silver", "metals", "disagg", "084691", micros=("SIL",)),
    Market("HG", "Copper", "metals", "disagg", "085692", micros=("MHG",)),
    Market("PL", "Platinum", "metals", "disagg", "076651"),
    # Agriculture
    Market("ZC", "Corn", "ags", "disagg", "002602"),
    Market("ZW", "Wheat", "ags", "disagg", "001602"),
    Market("ZS", "Soybeans", "ags", "disagg", "005602"),
    Market("ZM", "Soybean meal", "ags", "disagg", "026603"),
    Market("ZL", "Soybean oil", "ags", "disagg", "007601"),
    Market("HE", "Lean hogs", "ags", "disagg", "054642"),
    Market("LE", "Live cattle", "ags", "disagg", "057642"),
    # Crypto (only micros are tradable on Topstep)
    Market("MBT", "Bitcoin (micro)", "crypto", "tff", "133741"),
    Market("MET", "Ether (micro)", "crypto", "tff", "146021"),
)

MARKETS_BY_ROOT = {m.root: m for m in MARKETS}

# Recurring scheduled reports that move single asset classes (US Eastern time).
# weekday: Monday=0. These sit beside the macro calendar feed.
RECURRING_EVENTS = (
    {"roots": ("CL", "RB", "HO"), "weekday": 2, "hour": 10, "minute": 30, "name": "EIA crude inventories"},
    {"roots": ("NG",), "weekday": 3, "hour": 10, "minute": 30, "name": "EIA natural gas storage"},
)
