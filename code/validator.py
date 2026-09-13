from datetime import date
from typing import Dict, List, Optional, Tuple

from .models import SpendingChange
from .timeline import EPS, Ledger


def plan_payments(ledger: Ledger, plan: List[Tuple[date, float]]) -> Dict[int, float]:
    pays: Dict[int, float] = {}
    for d, amt in plan:
        i = ledger.day(d)
        if i < 0:
            return {-1: amt}
        if i <= ledger.H:
            pays[i] = pays.get(i, 0.0) + amt
    return pays


def is_safe(ledger: Ledger, plan: List[Tuple[date, float]],
            changes: Optional[List[SpendingChange]] = None, allowance: float = 0.0) -> bool:
    """Plan keeps the balance >= minimum for the whole horizon (allowance > 0 relaxes that; off by default)."""
    pays = plan_payments(ledger, plan)
    if -1 in pays:
        return False
    return ledger.slack(pays, changes) >= -EPS - allowance
