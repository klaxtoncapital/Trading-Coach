"""TopstepX contract names, TradingView symbols and contract sizes."""
from __future__ import annotations

import os
import re

# TradingView exchange prefix per contract root (full-size and micros)
TV_EXCHANGE = {
    **dict.fromkeys(["ES", "MES", "NQ", "MNQ", "RTY", "M2K"], "CME_MINI"),
    **dict.fromkeys(["YM", "MYM"], "CBOT_MINI"),
    **dict.fromkeys(["NKD", "6E", "M6E", "E7", "6B", "M6B", "6J", "6A", "M6A", "6C", "6S", "6M", "6N",
                     "HE", "LE", "MBT", "MET"], "CME"),
    **dict.fromkeys(["ZT", "ZF", "ZN", "TN", "ZB", "UB", "ZC", "ZW", "ZS", "ZM", "ZL"], "CBOT"),
    **dict.fromkeys(["CL", "QM", "MCL", "NG", "QG", "MNG", "RB", "HO", "PL"], "NYMEX"),
    **dict.fromkeys(["GC", "MGC", "SI", "SIL", "HG", "MHG"], "COMEX"),
}

RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "") or 275)

_NAME = re.compile(r"^([A-Z0-9]+?)([FGHJKMNQUVXZ])(\d{1,2})$")


def tradingview(root: str, contract_name: str | None) -> tuple[str, str | None]:
    """('CME_MINI:NQ1!', 'CME_MINI:NQZ2026') — continuous and the exact contract."""
    ex = TV_EXCHANGE.get(root, "CME")
    cont = f"{ex}:{root}1!"
    m = _NAME.match((contract_name or "").upper())
    if not m:
        return cont, None
    yy = m.group(3)
    year = 2020 + int(yy) if len(yy) == 1 else 2000 + int(yy)
    return cont, f"{ex}:{m.group(1)}{m.group(2)}{year}"


def point_value(contract: dict | None) -> float | None:
    """Dollars per 1.0 price move, from ProjectX tickValue / tickSize."""
    if not contract:
        return None
    try:
        ts, tv = float(contract.get("tickSize") or 0), float(contract.get("tickValue") or 0)
        return tv / ts if ts else None
    except (TypeError, ValueError):
        return None


def size_for_risk(stop_distance: float, pv: float | None, risk: float = RISK_PER_TRADE) -> tuple[int, float] | None:
    """(contracts, dollar risk) for a stop distance; contracts may be 0 if one is already too big."""
    if not pv or stop_distance <= 0:
        return None
    per = stop_distance * pv
    n = int(risk // per)
    return n, per
