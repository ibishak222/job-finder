"""Helpers for presenting jobs to humans (used by the web UI and Excel export)."""
from __future__ import annotations

from typing import Optional

SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "INR": "₹", "JPY": "¥", "CAD": "CA$", "AUD": "A$", "NZD": "NZ$"}
PERIODS = {"year": "year", "month": "month", "week": "week", "day": "day", "hour": "hour"}


def _amount(v: float, sym: str, code: Optional[str]) -> str:
    if v >= 1000:
        k = v / 1000
        txt = f"{k:.0f}k" if abs(k - round(k)) < 0.05 else f"{k:.1f}k"
    elif v == int(v):
        txt = f"{int(v)}"
    else:
        txt = f"{v:.2f}"
    return f"{sym}{txt}" if sym else (f"{txt} {code}" if code else txt)


def format_salary(lo: Optional[float], hi: Optional[float], currency: Optional[str] = None,
                  period: Optional[str] = None) -> str:
    """e.g. (40000, 50000, 'GBP', 'year') -> '£40k–£50k / year'. NaN/None-safe (pandas rows)."""
    def clean(v):
        return None if v is None or (isinstance(v, float) and v != v) or v == "" else v
    lo, hi, currency, period = clean(lo), clean(hi), clean(currency), clean(period)
    currency = str(currency) if currency is not None else None
    period = str(period) if period is not None else None
    if lo in (None, "", 0) and hi in (None, "", 0):
        return ""
    lo = float(lo) if lo not in (None, "") else None
    hi = float(hi) if hi not in (None, "") else None
    sym = SYMBOLS.get((currency or "").upper(), "")
    code = None if sym else currency
    if lo and hi and lo != hi:
        s = f"{_amount(lo, sym, None)}–{_amount(hi, sym, code)}"
    elif lo and hi:
        s = _amount(lo, sym, code)
    elif hi:
        s = f"up to {_amount(hi, sym, code)}"
    else:
        s = f"from {_amount(lo, sym, code)}"
    if period:
        s += f" / {PERIODS.get(period, period)}"
    return s
