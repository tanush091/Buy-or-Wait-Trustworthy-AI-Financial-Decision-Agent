"""Daily 90-day ledger.

Within a day (default `income_first=True`), confirmed income lands with that day's outflows
and the minimum is checked on the end-of-day balance; a recommended payment on that day is
taken after income. With `income_first=False`, outflows are checked before income arrives.
Every candidate plan and every metric is evaluated through `Ledger.slack`.
"""
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from .config import CONFIG
from .models import Flow, Series, SpendingChange

EPS = 1e-6


class Ledger:
    def __init__(self, balance: float, minimum: float, request_date: date, flows: List[Flow],
                 series: List[Series], horizon: int = 90):
        self.balance = balance
        self.minimum = minimum
        self.rd = request_date
        self.H = horizon
        self.series = {s.last_event_id: s for s in series}
        self.out = [0.0] * (horizon + 1)
        self.inn = [0.0] * (horizon + 1)
        for f in flows:
            d = (f.day - request_date).days
            if 0 <= d <= horizon:
                if f.amount >= 0:
                    self.inn[d] += f.amount
                else:
                    self.out[d] -= f.amount

    def day(self, d: date) -> int:
        return (d - self.rd).days

    def savings(self, changes: Iterable[SpendingChange]) -> Tuple[List[float], float]:
        """Per-day reduction of outflows produced by spending changes, and the total saved."""
        delta = [0.0] * (self.H + 1)
        total = 0.0
        for ch in changes:
            s = self.series.get(ch.event_id)
            if not s:
                continue
            amounts = s.amounts or [s.amount] * len(s.occurrences)
            for t, amt in zip(s.occurrences, amounts):
                per = amt if ch.kind == "stop" else max(0.0, amt - (ch.new_amount or 0.0))
                d = self.day(t)
                if 0 <= d <= self.H:
                    delta[d] += per
                    total += per
        return delta, total

    def arrays(self, changes: Optional[Iterable[SpendingChange]] = None):
        if not changes:
            return self.out, self.inn
        delta, _ = self.savings(changes)
        return [o - s for o, s in zip(self.out, delta)], self.inn

    def slack(self, payments: Dict[int, float], changes=None) -> float:
        """Smallest (balance - minimum) over the horizon for a plan; >= 0 means safe."""
        out, inn = self.arrays(changes)
        bal = self.balance
        worst = float("inf")
        for d in range(self.H + 1):
            bal -= out[d]
            if not CONFIG["income_first"]:
                worst = min(worst, bal)
            bal += inn[d]
            worst = min(worst, bal)
            p = payments.get(d, 0.0)
            if p:
                bal -= p
                worst = min(worst, bal)
        return worst - self.minimum

    def lowest(self, payments: Optional[Dict[int, float]] = None, changes=None) -> Tuple[float, date]:
        """Lowest projected balance and its date for a plan (same ordering rules as slack)."""
        out, inn = self.arrays(changes)
        payments = payments or {}
        bal, worst, worst_day = self.balance, float("inf"), 0
        for d in range(self.H + 1):
            bal -= out[d]
            if not CONFIG["income_first"] and bal < worst:
                worst, worst_day = bal, d
            bal += inn[d]
            bal -= payments.get(d, 0.0)
            if bal < worst:
                worst, worst_day = bal, d
        return worst, self.rd + timedelta(days=worst_day)

    def _lows_ends(self, changes=None):
        out, inn = self.arrays(changes)
        lows, ends, bal = [], [], self.balance
        for d in range(self.H + 1):
            bal -= out[d]
            low = bal
            bal += inn[d]
            lows.append(bal if CONFIG["income_first"] else low)
            ends.append(bal)
        return lows, ends

    def capacity(self, day_index: int, changes=None) -> float:
        """Largest single payment possible on day_index (after that day's income)."""
        lows, ends = self._lows_ends(changes)
        if min(lows[: day_index + 1]) < self.minimum - EPS:
            return 0.0
        after = min([ends[day_index]] + lows[day_index + 1:])
        return after - self.minimum

    def earliest(self, amount: float, changes=None) -> Optional[date]:
        lows, ends = self._lows_ends(changes)
        suffix = [0.0] * (self.H + 2)
        suffix[self.H + 1] = float("inf")
        for d in range(self.H, -1, -1):
            suffix[d] = min(lows[d], suffix[d + 1])
        prefix_ok = True
        for d in range(self.H + 1):
            if lows[d] < self.minimum - EPS:
                prefix_ok = False
            if not prefix_ok:
                return None
            after = min(ends[d], suffix[d + 1])
            if after - amount >= self.minimum - EPS:
                return self.rd + timedelta(days=d)
        return None
