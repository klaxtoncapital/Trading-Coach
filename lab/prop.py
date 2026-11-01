"""Prop firm Combine simulator: put a strategy through the evaluation rules thousands of times.

  python -m lab prop retrace --root NKD --only "Bull WED,Bear THU" --contracts 1,2,3

For each attempt it trades the strategy day by day under the firm's rules and
records how the attempt ended:
  PASS     profit target reached AND the consistency target met
  FAIL     the account touched the Maximum Loss Limit (checked at each trade's
           worst point, not just at its exit, because the limit counts intraday)
  TIMEOUT  neither within `max_days` trading days (you're still paying fees)

Two ways to replay history:
  historical  start an attempt on every week of real history and play the real
              days forward in order (keeps real streaks and regimes)
  bootstrap   build thousands of attempts from randomly drawn real days
              (more attempts, but treats days as independent)

From the pass rate and the monthly fee it works out what a funded account
costs you on average, which is the number that matters for the business.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class Rules:
    name: str = "Topstep 150K"
    start: float = 150_000.0
    target: float = 9_000.0
    mll: float = 4_500.0              # Maximum Loss Limit, trails the end-of-day balance high
    mll_locks_at_start: bool = True   # stops trailing once it reaches the starting balance
    consistency: float | None = 0.50  # best day must be at or below this share of total profit
    dll: float | None = None          # optional Daily Loss Limit: stops trading for the day
    max_contracts: int = 15
    monthly_fee: float = 200.0        # $ per month per account (set yours)
    max_days: int = 60                # give up after this many trading days


PRESETS = {
    "topstep150": Rules(),
    "topstep100": Rules("Topstep 100K", 100_000, 6_000, 3_000, max_contracts=10),
    "topstep50": Rules("Topstep 50K", 50_000, 3_000, 2_000, max_contracts=5),
}


@dataclass
class Attempt:
    outcome: str          # PASS / FAIL / TIMEOUT
    days: int             # trading days used
    trades: int
    profit: float
    best_day: float


@dataclass
class PropResult:
    rules: Rules
    sizing: str
    method: str
    attempts: list[Attempt] = field(default_factory=list)
    skipped_trades: int = 0

    def _share(self, o):
        return sum(a.outcome == o for a in self.attempts) / len(self.attempts) if self.attempts else float("nan")

    @property
    def pass_rate(self):
        return self._share("PASS")

    @property
    def fail_rate(self):
        return self._share("FAIL")

    @property
    def timeout_rate(self):
        return self._share("TIMEOUT")

    def median_days(self, outcome="PASS"):
        d = [a.days for a in self.attempts if a.outcome == outcome]
        return float(np.median(d)) if d else float("nan")

    def cost_per_pass(self) -> float:
        """Average fees paid per funded account: total months paid across attempts / passes."""
        if not self.attempts or self.pass_rate == 0:
            return float("inf")
        months = sum(max(1, math.ceil(a.days / 21)) for a in self.attempts)
        passes = sum(a.outcome == "PASS" for a in self.attempts)
        return months * self.rules.monthly_fee / passes

    def row(self) -> dict:
        return {"sizing": self.sizing, "attempts": len(self.attempts), "pass": self.pass_rate,
                "fail": self.fail_rate, "timeout": self.timeout_rate,
                "days_to_pass": self.median_days("PASS"), "days_to_fail": self.median_days("FAIL"),
                "cost_per_pass": self.cost_per_pass()}


# ── turning trades into trading days ─────────────────────────────────────

def daily_book(trades: pd.DataFrame) -> list[list[tuple[float, float]]]:
    """Every weekday from the first to the last trade, each a list of (net $, worst $ during the
    trade) per trade for ONE contract, in time order. Days with no trade are empty lists."""
    if trades.empty:
        return []
    t = trades.sort_values("entry_time")
    day = pd.to_datetime(t["exit_time"]).dt.tz_convert("America/New_York").dt.tz_localize(None).dt.normalize()
    worst = -(t["mae_r"].astype(float) * t["risk_usd"].astype(float)) - t["cost_usd"].astype(float)
    worst = np.minimum(worst, t["net_usd"].astype(float))
    per: dict = {}
    for d, net, w in zip(day, t["net_usd"].astype(float), worst):
        per.setdefault(d, []).append((net, w))
    idx = pd.bdate_range(min(per), max(per))
    return [per.get(d, []) for d in idx]


def _size(trades: pd.DataFrame, contracts: int | None, risk: float | None, max_c: int) -> tuple[pd.DataFrame, int]:
    """Scale each trade to the position size. Fixed contracts, or as many as fit `risk` dollars."""
    t = trades.copy()
    if contracts is not None:
        n = np.full(len(t), min(contracts, max_c))
    else:
        n = np.minimum(np.floor(risk / t["risk_usd"].astype(float)), max_c)
    skipped = int((n < 1).sum())
    for col in ("net_usd", "cost_usd", "risk_usd"):
        t[col] = t[col].astype(float) * n
    return t[n >= 1], skipped


# ── one attempt ──────────────────────────────────────────────────────────

def run_attempt(days: list[list[tuple[float, float]]], r: Rules) -> Attempt:
    bal = r.start
    floor = r.start - r.mll
    day_pnls = []
    trades = 0
    for k, day in enumerate(days[: r.max_days], start=1):
        pnl = 0.0
        for net, worst in day:
            if r.dll is not None and pnl <= -r.dll:
                break                                   # daily loss limit: done for the day
            if bal + worst <= floor:                     # touched the limit during the trade
                return Attempt("FAIL", k, trades + 1, bal + worst - r.start, max(day_pnls or [0]))
            bal += net
            pnl += net
            trades += 1
        day_pnls.append(pnl)
        # end of day: the loss limit trails the balance high (but never above the start, if locked)
        new_floor = bal - r.mll
        if r.mll_locks_at_start:
            new_floor = min(new_floor, r.start)
        floor = max(floor, new_floor)
        profit = bal - r.start
        best = max(day_pnls)
        consistent = r.consistency is None or profit <= 0 or best <= r.consistency * profit
        if profit >= r.target and consistent:
            return Attempt("PASS", k, trades, profit, best)
    return Attempt("TIMEOUT", min(len(days), r.max_days), trades, bal - r.start, max(day_pnls or [0]))


# ── many attempts ────────────────────────────────────────────────────────

def simulate(trades: pd.DataFrame, rules: Rules, contracts: int | None = 1, risk: float | None = None,
             method: str = "historical", sims: int = 5000, step: int = 5, seed: int = 9) -> PropResult:
    sized, skipped = _size(trades, contracts, risk, rules.max_contracts)
    days = daily_book(sized)
    label = f"{contracts} contract{'s' if contracts != 1 else ''}" if contracts is not None else f"${risk:,.0f} risk"
    res = PropResult(rules, label, method, skipped_trades=skipped)
    if not days:
        return res
    if method == "historical":
        # an attempt starting each `step` days; attempts that run off the end of history are dropped
        for s in range(0, len(days), step):
            a = run_attempt(days[s:], rules)
            if a.outcome == "TIMEOUT" and s + rules.max_days > len(days):
                continue
            res.attempts.append(a)
    else:
        rng = np.random.default_rng(seed)
        n = len(days)
        for _ in range(sims):
            res.attempts.append(run_attempt([days[i] for i in rng.integers(0, n, rules.max_days)], rules))
    return res


def table(results: list[PropResult]) -> pd.DataFrame:
    return pd.DataFrame([r.row() for r in results])


def print_table(results: list[PropResult]) -> None:
    if not results:
        return
    r0 = results[0]
    print(f"\n{r0.rules.name} Combine ({r0.method}): target ${r0.rules.target:,.0f}, "
          f"max loss ${r0.rules.mll:,.0f} trailing, consistency "
          f"{'off' if r0.rules.consistency is None else f'{r0.rules.consistency:.0%}'}, "
          f"{r0.rules.max_days} trading days, ${r0.rules.monthly_fee:,.0f}/month")
    print(f"{'size':>14} {'attempts':>8} {'PASS':>6} {'FAIL':>6} {'TIMEOUT':>8} {'days→pass':>10} {'days→fail':>10} {'$/funded acct':>14}")
    for r in results:
        w = r.row()
        cpp = "never" if math.isinf(w["cost_per_pass"]) else f"${w['cost_per_pass']:,.0f}"
        dp = "—" if math.isnan(w["days_to_pass"]) else f"{w['days_to_pass']:.0f}"
        df = "—" if math.isnan(w["days_to_fail"]) else f"{w['days_to_fail']:.0f}"
        print(f"{w['sizing']:>14} {w['attempts']:>8} {w['pass']:>6.0%} {w['fail']:>6.0%} {w['timeout']:>8.0%} "
              f"{dp:>10} {df:>10} {cpp:>14}" + (f"   ({r.skipped_trades} trades too big to take)" if r.skipped_trades else ""))
