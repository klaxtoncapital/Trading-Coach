"""The Lab's data store: one clean copy of every market's history, on disk.

  python -m lab pull NKD,NQ,ES --tf 1h --days 730
  python -m lab pull NKD --tf 1m --days 365

History comes from ProjectX (the same login the desk uses). Each contract
month is downloaded separately and stitched into one continuous series: on each
day the contract with the most volume wins (a volume roll). Every bar keeps the
contract it came from, so the roll dates are known.

Files live in lab_data/ (not committed to git):
  lab_data/NKD_1h.parquet   bars: t (UTC bar start), o, h, l, c, v, contract
  lab_data/NKD.json         contract facts: tick size, tick value, point value

load() gives either the raw stitched prices (what you actually traded) or a
back-adjusted series (roll gaps removed, so multi-day indicators and
day-over-day comparisons don't see a fake jump on roll day).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("lab.data")

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("LAB_DATA_DIR", ROOT_DIR / "lab_data"))

# timeframe -> (ProjectX unit, unitNumber). 2 = minute, 3 = hour, 4 = day.
TIMEFRAMES = {"1m": (2, 1), "1h": (3, 1), "1d": (4, 1)}
CHUNK = {"1m": timedelta(days=12), "1h": timedelta(days=365), "1d": timedelta(days=3650)}
COLS = ["o", "h", "l", "c", "v"]


# ── files ────────────────────────────────────────────────────────────────

def path(root: str, tf: str) -> Path:
    return DATA_DIR / f"{root.upper()}_{tf}.parquet"


def meta_path(root: str) -> Path:
    return DATA_DIR / f"{root.upper()}.json"


def save(root: str, tf: str, bars: pd.DataFrame, meta: dict | None = None) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df = _clean(bars)
    p = path(root, tf)
    df.to_parquet(p)
    if meta:
        old = load_meta(root)
        old.update(meta)
        meta_path(root).write_text(json.dumps(old, indent=2, default=str))
    return p


def load_meta(root: str) -> dict:
    p = meta_path(root)
    return json.loads(p.read_text()) if p.exists() else {}


def available() -> list[tuple[str, str, int, str, str]]:
    """(root, tf, bars, first, last) for everything in the store."""
    out = []
    if not DATA_DIR.exists():
        return out
    for p in sorted(DATA_DIR.glob("*_*.parquet")):
        root, tf = p.stem.rsplit("_", 1)
        df = pd.read_parquet(p)
        if len(df):
            out.append((root, tf, len(df), str(df.index[0].date()), str(df.index[-1].date())))
    return out


def load(root: str, tf: str = "1h", adjusted: bool = False, since: str | date | None = None) -> pd.DataFrame:
    """Bars indexed by UTC bar-start time. adjusted=True removes roll gaps (back-adjusted)."""
    p = path(root, tf)
    if not p.exists():
        raise FileNotFoundError(f"No {tf} data for {root}. Pull it first: python -m lab pull {root} --tf {tf}")
    df = pd.read_parquet(p)
    if adjusted:
        df = back_adjust(df)
    if since is not None:
        df = df[df.index >= pd.Timestamp(since, tz="UTC")]
    return df


def _clean(bars: pd.DataFrame) -> pd.DataFrame:
    df = bars.copy()
    if "t" in df.columns:
        df = df.set_index("t")
    df.index = pd.to_datetime(df.index, utc=True)
    df.index.name = "t"
    df = df[~df.index.duplicated(keep="last")].sort_index()
    for c in COLS:
        df[c] = df[c].astype(float)
    bad = (df["h"] < df[["o", "c", "l"]].max(axis=1)) | (df["l"] > df[["o", "c", "h"]].min(axis=1))
    if bad.any():
        log.warning("%d bars with high/low inconsistent with open/close were dropped", int(bad.sum()))
        df = df[~bad]
    if "contract" not in df.columns:
        df["contract"] = ""
    return df[COLS + ["contract"]]


def from_records(records: list[dict], contract: str = "") -> pd.DataFrame:
    """The scanner's bar dicts {t, o, h, l, c, v} as a Lab DataFrame."""
    df = pd.DataFrame(records)
    if df.empty:
        return pd.DataFrame(columns=COLS + ["contract"], index=pd.DatetimeIndex([], tz="UTC", name="t"))
    if "contract" not in df.columns:
        df["contract"] = contract
    return _clean(df)


def to_records(df: pd.DataFrame) -> list[dict]:
    """Lab DataFrame back to the scanner's bar dicts (used to cross-check the old backtests)."""
    return [{"t": t.to_pydatetime(), "o": r.o, "h": r.h, "l": r.l, "c": r.c, "v": r.v}
            for t, r in zip(df.index, df.itertuples(index=False))]


# ── continuous contracts ─────────────────────────────────────────────────

def stitch(by_contract: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Volume roll: for each Globex trading day keep the contract that traded the most that day,
    and never roll backwards to an older contract. Rolling on the trading day (not the UTC
    date) means a session never mixes two contracts."""
    frames = []
    for cid, df in by_contract.items():
        if len(df):
            d = df.copy()
            d["contract"] = cid
            frames.append(d)
    if not frames:
        return _clean(pd.DataFrame(columns=["t"] + COLS))
    allb = pd.concat(frames)
    allb["day"] = trading_days(allb).values
    vol = allb.groupby(["day", "contract"])["v"].sum().reset_index()
    order = {cid: i for i, cid in enumerate(by_contract)}      # caller passes contracts oldest -> newest
    best, current = {}, -1
    for day, g in vol.groupby("day"):
        cid = g.loc[g["v"].idxmax(), "contract"]
        current = max(current, order[cid])
        best[day] = current
    allb = allb[[order[c] == best[d] for c, d in zip(allb["contract"], allb["day"])]]
    return _clean(allb.drop(columns="day"))


def roll_points(df: pd.DataFrame) -> list[tuple[pd.Timestamp, str, str]]:
    """(first bar of the new contract, old contract, new contract)."""
    c = df["contract"].to_numpy()
    idx = np.flatnonzero(c[1:] != c[:-1]) + 1
    return [(df.index[i], c[i - 1], c[i]) for i in idx]


def back_adjust(df: pd.DataFrame, gaps: dict[pd.Timestamp, float] | None = None) -> pd.DataFrame:
    """Additive (Panama) back-adjustment: shift everything before each roll by the roll gap,
    so the newest prices are real and older prices line up with them.

    The gap is measured as new contract open minus old contract close across the switch,
    unless exact gaps (measured on the same bar in both contracts) are passed in."""
    out = df.copy()
    rolls = roll_points(df)
    if not rolls:
        return out
    adj = np.zeros(len(df))
    for t, _old, _new in rolls:
        i = df.index.get_loc(t)
        gap = gaps.get(t) if gaps and t in gaps else df["o"].iloc[i] - df["c"].iloc[i - 1]
        adj[:i] += gap
    for col in ("o", "h", "l", "c"):
        out[col] = out[col] + adj
    return out


# ── pulling from ProjectX ────────────────────────────────────────────────

async def _pull_contract(px, cid: str, tf: str, start: datetime, end: datetime) -> pd.DataFrame:
    from scanner.acd_backtest import _bars_between
    unit, _ = TIMEFRAMES[tf]
    bars, t = [], start
    while t < end:
        bars += await _bars_between(px, cid, unit, t, min(t + CHUNK[tf], end))
        t += CHUNK[tf]
    return from_records(bars, cid)


async def pull(px, root: str, tf: str, days: int) -> tuple[pd.DataFrame, dict]:
    """Download every contract month that overlaps the window and stitch them."""
    from scanner.acd_backtest import candidate_ids
    from scanner.projectx import ProjectXError, pick_front_contracts
    from scanner.symbols import point_value

    contracts = pick_front_contracts(await px.available_contracts(), {root})
    c = contracts.get(root)
    if not c:
        raise RuntimeError(f"{root} isn't offered by ProjectX right now")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    by_contract: dict[str, pd.DataFrame] = {}
    for cid, month in candidate_ids(c["id"], start.date(), end.date()):
        c_start = max(start, datetime.combine(month - timedelta(days=100), datetime.min.time(), tzinfo=timezone.utc))
        c_end = min(end, datetime.combine(month + timedelta(days=31), datetime.min.time(), tzinfo=timezone.utc))
        if c_start >= c_end:
            continue
        try:
            df = await _pull_contract(px, cid, tf, c_start, c_end)
        except ProjectXError as e:
            log.info("%s: %s not available (%s)", root, cid, str(e)[:80])
            continue
        if len(df):
            by_contract[cid] = df
            print(f"  {cid}: {len(df)} bars")
    meta = {"root": root, "tick_size": float(c.get("tickSize") or 0), "tick_value": float(c.get("tickValue") or 0),
            "point_value": point_value(c), "description": c.get("description", ""),
            "pulled_at": datetime.now(timezone.utc).isoformat(), "source": "ProjectX"}
    return stitch(by_contract), meta


async def pull_and_save(roots: list[str], tfs: list[str], days: int) -> None:
    from scanner import config
    from scanner.projectx import ProjectXClient

    async with ProjectXClient(config.PROJECTX_BASE_URL, config.PROJECTX_USERNAME,
                              config.PROJECTX_API_KEY, config.PROJECTX_LIVE_DATA) as px:
        for root in roots:
            for tf in tfs:
                print(f"{root} {tf}: pulling {days} days...")
                try:
                    df, meta = await pull(px, root, tf, days)
                except Exception as e:      # one bad market shouldn't stop the rest
                    print(f"{root} {tf}: failed ({e})")
                    continue
                if not len(df):
                    print(f"{root} {tf}: no data")
                    continue
                # merge with anything already stored, so the store only grows
                if path(root, tf).exists():
                    old = load(root, tf)
                    df = _clean(pd.concat([old[~old.index.isin(df.index)], df]))
                p = save(root, tf, df, meta)
                print(f"{root} {tf}: {len(df)} bars, {df.index[0].date()} to {df.index[-1].date()}, "
                      f"{len(roll_points(df))} rolls -> {p.name}")


# ── helpers for strategies ───────────────────────────────────────────────

def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """e.g. resample(one_minute, '15min'). Bars are labelled by their start time."""
    agg = df.resample(rule, label="left", closed="left").agg(
        {"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum", "contract": "last"})
    return agg.dropna(subset=["o"])


def trading_days(df: pd.DataFrame) -> pd.Series:
    """CME Globex trading day for each bar (rolls at 18:00 New York, so Sunday night is Monday)."""
    ny = df.index.tz_convert("America/New_York") + pd.Timedelta(hours=6)
    return pd.Series(ny.date, index=df.index, name="day")


def daily(df: pd.DataFrame) -> pd.DataFrame:
    """Intraday bars rolled up into Globex trading days (weekend prints dropped)."""
    d = df.assign(day=trading_days(df).values)
    d = d[pd.to_datetime(d["day"]).dt.weekday < 5]
    g = d.groupby("day")
    out = pd.DataFrame({"o": g["o"].first(), "h": g["h"].max(), "l": g["l"].min(), "c": g["c"].last(),
                        "v": g["v"].sum(), "first_bar": g.apply(lambda x: x.index[0], include_groups=False),
                        "last_bar": g.apply(lambda x: x.index[-1], include_groups=False)})
    return out
