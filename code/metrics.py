import math
from datetime import date
from typing import Optional

from .timeline import Ledger


def amount_safe_to_pay(ledger: Ledger, requested_amount: float) -> float:
    """Largest payment today, before spending changes, that keeps the 90-day forecast above the minimum.
    Rounded down to the cent (never above the true capacity); the tiny epsilon absorbs float noise
    such as 7840260.039999995 so it stays 7840260.04."""
    cap = max(0.0, min(requested_amount, ledger.capacity(0)))
    return math.floor(cap * 100 + 1e-6) / 100


def earliest_date_for_full_payment(ledger: Ledger, requested_amount: float) -> Optional[date]:
    """First date a single full payment passes the safety check, ignoring payment preferences."""
    return ledger.earliest(requested_amount)
