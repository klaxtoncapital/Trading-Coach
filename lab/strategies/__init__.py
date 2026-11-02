"""Strategy registry. Each strategy is a module with a class exposing:

  name        short id used on the command line
  timeframe   which stored bars it runs on ("1h", "1m", ...)
  defaults    dict of parameters and their default values
  hypothesis  plain-English rule and why the edge should exist
  setups(bars, **params) -> list[engine.Setup]

setups() may only use bars that start before each Setup's decided_at.
`python -m lab check <name>` proves that with the look-ahead test.
"""
from __future__ import annotations

from . import atm_ny, nkd_expansion, nkd_signature, nkd_sweep_reclaim, retrace

REGISTRY = {s.name: s for s in (retrace.Retrace(), nkd_expansion.NKDExpansion(),
                                 nkd_signature.NKDSignature(),
                                 nkd_sweep_reclaim.NKDSweepReclaim(),
                                 atm_ny.ATMNewYork())}


def get(name: str):
    try:
        return REGISTRY[name]
    except KeyError:
        raise SystemExit(f"Unknown strategy '{name}'. Available: {', '.join(REGISTRY)}")
