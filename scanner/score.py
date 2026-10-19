"""Score each market's reading (0-100) and decide what gets posted."""
from __future__ import annotations

from .metrics import Reading


def _ramp(x: float, lo: float, hi: float) -> float:
    """0 at lo, 1 at hi, clamped."""
    if hi == lo:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def score(r: Reading) -> tuple[int, list[str]]:
    """How far this market is moving away from normal, and why."""
    parts: list[tuple[float, str]] = [
        (20 * _ramp(r.range_ratio, 0.6, 2.0), f"day range {r.range_ratio}x ATR"),
        (10 * _ramp(r.week_range_ratio, 0.6, 1.6), f"week range {r.week_range_ratio}x normal"),
        (15 * _ramp(r.displacement, 1.5, 4.0), f"displacement {r.displacement}x"),
        (15 * _ramp(r.relvol, 1.0, 2.5), f"volume {r.relvol}x normal"),
    ]
    if r.events:
        e = max(r.events, key=lambda e: e["weight"])
        parts.append((e["weight"], e["text"]))
    t = r.thesis
    if t and t["rr"] >= 3:
        parts.append((15, f"room to target {t['rr']}R"))
    elif t and t["rr"] >= 2:
        parts.append((8, f"room to target {t['rr']}R"))
    prof_dir = None
    if r.profile.get("continuation"):
        prof_dir = r.profile["continuation"]["dir"]
    elif r.profile.get("c2"):
        prof_dir = r.profile["c2"]["dir"]
    if t and prof_dir == t["dir"]:
        parts.append((5, "weekly profile agrees"))
    total = int(round(sum(p for p, _ in parts)))
    reasons = [txt for p, txt in sorted(parts, key=lambda x: -x[0]) if p >= 4]
    return min(total, 100), reasons


def classify(sc: int, r: Reading, gates: dict, watch_min: int) -> str | None:
    """'watch' when the market is moving, has a thesis and positioning doesn't fight it.

    'take' is switched off until your playbook entry signatures are added and
    backtested on ProjectX history.
    """
    if not r.thesis or sc < watch_min:
        return None
    if gates["positioning"]["status"] == "fail":
        return None
    # Needs a liquidity target at least 2R away (1:1 ideas are never worth posting).
    if r.thesis["rr"] < 2:
        return None
    return "watch"
