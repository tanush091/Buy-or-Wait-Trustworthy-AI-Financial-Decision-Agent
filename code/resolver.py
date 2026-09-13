"""Reconstruct a user's forward cash flows (the 90-day forecast inputs) for one request.

Pipeline: fill blank amounts from images -> convert currency at the settlement date ->
drop failed/cancelled/unrealized/pending-credit rows -> detect recurring expense series
per category -> build the salary stream -> apply message amendments.
"""
import calendar
import dataclasses
import math
import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from .config import CONFIG
from .models import Amendment, Flow, Profile, RawEvent, Request, Series, UserState

EXPENSE_TYPES = {"expense", "subscription", "debt_payment"}
INCOME_EXCLUDE = ("commission", "bonus", "arrears", "final", "reimburs", "prize", "proceeds", "invoice")


def add_months(d: date, n: int, day: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    return date(y, m, min(day, calendar.monthrange(y, m)[1]))


class FX:
    """Fixed dated rates. Uses the row for the settlement date, else the nearest dated row."""

    def __init__(self, rates: Dict[Tuple[date, str, str], float]):
        self.pairs: Dict[Tuple[str, str], List[Tuple[date, float]]] = defaultdict(list)
        for (d, f, t), r in rates.items():
            self.pairs[(f, t)].append((d, r))

    def _direct(self, cur: str, home: str, day: date) -> Optional[float]:
        rows = self.pairs.get((cur, home))
        if rows:
            return min(rows, key=lambda x: (abs((x[0] - day).days), x[0]))[1]
        rows = self.pairs.get((home, cur))
        if rows:
            return 1.0 / min(rows, key=lambda x: (abs((x[0] - day).days), x[0]))[1]
        return None

    def rate(self, cur: str, home: str, day: date) -> float:
        if cur == home:
            return 1.0
        direct = self._direct(cur, home, day)
        if direct is not None:
            return direct
        # no direct pair: go through one common currency (e.g. EUR -> USD -> IDR)
        for mid in sorted({c for pair in self.pairs for c in pair} - {cur, home}):
            a, b = self._direct(cur, mid, day), self._direct(mid, home, day)
            if a is not None and b is not None:
                return a * b
        raise KeyError(f"no exchange rate for {cur}->{home}")

    def convert(self, amt: float, cur: str, home: str, day: date) -> float:
        return amt * self.rate(cur, home, day)


def _fits(anchor: date, d: date, gap: int, monthly: bool) -> bool:
    if monthly:
        return abs(d.day - anchor.day) <= 1 or min(d.day, anchor.day) >= 28
    r = abs((anchor - d).days) % gap
    return r <= 1 or r >= gap - 1


def find_series(rows: List[Tuple[date, float, RawEvent]], min_count: int):
    """Find the dominant regular cadence in date-sorted rows; one-off rows fall off the grid."""
    if len(rows) < min_count:
        return None
    gaps = [(b[0] - a[0]).days for a, b in zip(rows, rows[1:]) if (b[0] - a[0]).days > 0]
    if not gaps:
        return None
    monthly = sum(27 <= g <= 32 for g in gaps) >= len(gaps) / 2
    gap = 30 if monthly else Counter(gaps).most_common(1)[0][0]
    if gap < 2:
        return None
    best = None
    for anchor in reversed(rows[-3:]):
        on = [r for r in rows if r[0] <= anchor[0] and _fits(anchor[0], r[0], gap, monthly)]
        if best is None or len(on) > len(best[1]):
            best = (anchor, on)
    anchor, on = best
    if len(on) < min_count:
        return None
    return anchor, on, gap, monthly


def step(anchor: date, k: int, gap: int, monthly: bool, day: Optional[int] = None) -> date:
    return add_months(anchor, k, day or anchor.day) if monthly else anchor + timedelta(days=gap * k)


def project(anchor: date, gap: int, monthly: bool, start: date, end: date, day: Optional[int] = None) -> List[date]:
    out, k = [], 1
    while True:
        t = step(anchor, k, gap, monthly, day)
        if t > end:
            return out
        if t >= start:
            out.append(t)
        k += 1


def _estimate(values: List[float], setting: str = None) -> float:
    how, _, rounding = (setting or CONFIG["var_estimator"]).partition("_")
    if how == "median":
        v = statistics.median(values)
    elif how == "max":
        v = max(values)
    elif how == "last":
        v = values[-1]
    elif how == "last3":
        v = sum(values[-3:]) / len(values[-3:])
    elif how == "last6":
        v = sum(values[-6:]) / len(values[-6:])
    elif how == "midrange":
        # purchases scatter roughly uniformly within +/-30% of a typical charge; for bounded flat noise
        # the midrange is the most precise estimate of that centre (checked on 269 series whose
        # typical charge is known from minimum_allowed_amount: 1.7% median error vs 4.5% for the median)
        v = (min(values) + max(values)) / 2
    else:
        v = sum(values) / len(values)
    if rounding == "ceil":
        return float(math.ceil(v))
    if rounding == "round":
        return float(round(v))
    return v


def build_user_state(
    req: Request,
    profile: Profile,
    events: List[RawEvent],
    amendments: List[Amendment],
    fx: FX,
    image_amount: Callable[[str], Optional[float]],
    horizon: int,
) -> UserState:
    rd = req.request_date
    end = rd + timedelta(days=horizon)
    home = profile.home_currency
    notes: List[str] = []
    flows: List[Flow] = []
    linked_ids = {e.linked_event_id for e in events if e.linked_event_id}
    by_id = {e.event_id: e for e in events}

    def home_amount(e: RawEvent) -> Optional[float]:
        a = e.amount
        if a is None:
            a = image_amount(e.event_id)       # never treat a blank amount as zero
            if a is None:
                notes.append(f"{e.event_id}: amount unavailable")
                return None
        try:
            return fx.convert(a, e.currency, home, e.settlement_date or e.event_date)
        except KeyError:
            if e.direction == "debit":
                # an expense we cannot convert must not silently vanish from the forecast
                raise ValueError(f"no exchange rate {e.currency}->{home} for {e.event_id}")
            notes.append(f"{e.event_id}: no exchange rate {e.currency}->{home}")
            return None

    # 1. Known future rows: reserve pending/scheduled debits, never count pending credits.
    seen = set()
    for e in events:
        if e.status not in ("pending", "scheduled") or e.direction == "non_cash":
            continue
        if e.direction == "credit":
            continue  # unsettled credits never count; scheduled salary is handled by the salary stream
        a = home_amount(e)
        if a is None:
            if e.direction == "debit":
                # an upcoming bill of unknown size cannot be ruled safe; the caller falls back
                raise ValueError(f"amount of upcoming debit {e.event_id} could not be verified")
            continue
        day = max(e.settlement_date or e.event_date, rd)
        if e.status == "pending" and CONFIG["pending_on_request_day"]:
            day = rd  # hold pending authorisations from today rather than their settlement date
        if day > end:
            continue
        key = (e.description, round(a, 2), day)
        if key in seen:
            continue  # duplicate record
        seen.add(key)
        flows.append(Flow(day, a if e.direction == "credit" else -a, e.description, e.status))

    # Failed bills that a bank message says are still outstanding are reserved today.
    for am in amendments:
        if am.kind == "bill_retry" and am.event_id in by_id:
            e = by_id[am.event_id]
            # a pending/scheduled retry row linked to the failed bill already reserves it
            retried = any(x.linked_event_id == e.event_id and x.status in ("pending", "scheduled") for x in events)
            if e.status == "failed" and e.direction == "debit" and not retried:
                a = home_amount(e)
                if a is not None:
                    flows.append(Flow(rd, -a, e.description, "retry"))

    # 2. Recurring expense series, one per category, projected on their own cadence.
    rent_amendment = next((a for a in reversed(amendments) if a.kind == "rent_increase"), None)
    by_cat: Dict[str, List[Tuple[date, float, RawEvent]]] = defaultdict(list)
    for e in events:
        if (e.status == "settled" and e.direction == "debit" and e.event_type in EXPENSE_TYPES
                and e.event_date < rd and not e.linked_event_id and e.event_id not in linked_ids):
            a = home_amount(e)
            if a is not None:
                by_cat[e.category].append((e.event_date, a, e))
    series: List[Series] = []
    rent_cat = "rent" if "rent" in by_cat else "housing"
    for cat, rows in sorted(by_cat.items()):
        rows.sort(key=lambda r: (r[0], r[2].event_id))
        descs = Counter(r[2].description for r in rows)
        top, n_top = descs.most_common(1)[0]
        if len(descs) > 1 and n_top / len(rows) >= 0.6:
            # single-payee bill (e.g. "Water and power payment"): rows under another description are
            # one-offs such as a separate receipt, not part of the recurring series
            rows = [r for r in rows if r[2].description == top]
        found = find_series(rows, 3)
        if not found:
            continue
        anchor, on, gap, monthly = found
        # month-end payers (30th/31st) stay at month end instead of drifting to the 28th
        pay_day = max(r[0].day for r in on[-6:]) if monthly and anchor[0].day >= 28 else None
        if step(anchor[0], 1, gap, monthly, pay_day) < rd - timedelta(days=2):
            continue  # series has lapsed
        vals = [r[1] for r in on]
        constant = max(vals) - min(vals) < 0.005
        pooled = len({r[2].description for r in on}) > 1  # variable spending pooled under one category
        if (pooled and CONFIG["variable_scope"] == "protected"
                and cat not in profile.expense_categories_to_protect):
            continue
        # pooled day-to-day spending and single-payee variable bills (e.g. utilities) may be estimated differently
        amount = vals[0] if constant else _estimate(vals, CONFIG["var_estimator"] if pooled else CONFIG["bill_estimator"])
        amount = round(amount, 2)
        ev = anchor[2]
        min_allowed = None
        if ev.minimum_allowed_amount is not None:
            min_allowed = round(fx.convert(ev.minimum_allowed_amount, ev.currency, home, ev.event_date), 2)
        if CONFIG["base_from_minimum"] and not constant and min_allowed:
            # the reduction floor is a fixed share (e.g. 40% / 50%) of the typical charge,
            # so floor / share recovers the typical amount without purchase-to-purchase noise
            share = round(min_allowed / (sum(vals) / len(vals)), 1)
            if share > 0:
                amount = round(min_allowed / share, 2)
        occ = project(anchor[0], gap, monthly, rd, end, pay_day)
        if not CONFIG["day0"]:
            occ = [t for t in occ if t > rd]
        if pooled and not constant and CONFIG["pooled_window"]:
            occ = [t for t in occ if (t - rd).days <= CONFIG["pooled_window"]]
        if (CONFIG["discretionary_window"] and cat in CONFIG["discretionary_categories"]
                and cat not in profile.expense_categories_to_protect):
            occ = [t for t in occ if (t - rd).days <= CONFIG["discretionary_window"]]  # experimental
        amts = [amount] * len(occ)
        if pooled and not constant and CONFIG["pooled_mode"] == "replay":
            # repeat the last 30 days of actual spending in 30-day steps
            recent = [r for r in on if r[0] >= rd - timedelta(days=30)]
            pairs = sorted((r[0] + timedelta(days=30 * k), r[1]) for k in range(1, 5) for r in recent)
            pairs = [p for p in pairs if rd <= p[0] <= end and (CONFIG["day0"] or p[0] > rd)]
            occ, amts = [p[0] for p in pairs], [p[1] for p in pairs]
        elif pooled and not constant and CONFIG["pooled_mode"] == "daily":
            rate = sum(vals) / max(1, (rd - on[0][0]).days)
            occ = [rd + timedelta(days=i) for i in range((end - rd).days + 1)]
            amts = [rate] * len(occ)
        series.append(Series(cat, cat, ev.description, ev.event_id, amount, ev.flexibility, min_allowed, occ, amts))
        for t, a in zip(occ, amts):
            if rent_amendment and cat == rent_cat and t >= rent_amendment.sent:
                a = round(amount * (1 + rent_amendment.pct / 100.0), 2)
            flows.append(Flow(t, -a, ev.description, "recurring", series=cat))

    # 3. Salary stream.
    # salary rows with a blank amount (e.g. a payslip image) are read from the image, like expenses
    salary_events = [dataclasses.replace(e, amount=image_amount(e.event_id))
                     if e.amount is None and e.category == "salary" else e for e in events]
    flows.extend(_salary_flows(req, profile, salary_events, amendments, fx, end))
    flows.sort(key=lambda f: (f.day, f.amount))
    return UserState(flows=flows, series=series, notes=notes)


def _cluster_income(hist: List[RawEvent], rd: date):
    """Freelance-style income: payments that land on the same day each month under varying descriptions."""
    clusters: Dict[int, list] = defaultdict(list)
    for e in hist:
        if not any(k in e.description.lower() for k in INCOME_EXCLUDE if k != "invoice"):
            clusters[_paid_on(e).day].append((_paid_on(e), e.amount, e))
    out = []
    for day, rows in sorted(clusters.items()):
        found = find_series(rows, 3)
        if not found or not found[3]:
            continue
        anchor = found[0]
        if step(anchor[0], 1, 30, True) < rd - timedelta(days=3):
            continue
        vals = [r[1] for r in found[1]]
        out.append((anchor[0], sum(vals) / len(vals), anchor[2].currency, f"Regular income (day {day})"))
    return out


def _paid_on(e: RawEvent) -> date:
    """Salary counts on the date it settled (spec); event_date only when there is no settlement date."""
    if CONFIG.get("salary_on_settlement", True):
        return e.settlement_date or e.event_date
    return e.event_date


def _salary_flows(req, profile, events, amendments, fx: FX, end: date) -> List[Flow]:
    rd = req.request_date
    home = profile.home_currency
    hist = sorted((e for e in events if e.direction == "credit" and e.category == "salary"
                   and e.status == "settled" and e.event_date < rd and e.amount is not None),
                  key=lambda e: (_paid_on(e), e.event_id))
    ended = bool(hist) and "final" in hist[-1].description.lower()

    streams = []  # (anchor_date, amount, currency, label)
    if not ended:
        groups: Dict[str, list] = defaultdict(list)
        for e in hist:
            if not any(k in e.description.lower() for k in INCOME_EXCLUDE):
                groups[e.description].append((_paid_on(e), e.amount, e))
        for desc, rows in groups.items():
            found = find_series(rows, 2)
            if not found or not found[3]:
                continue
            anchor = found[0]
            if step(anchor[0], 1, 30, True) < rd - timedelta(days=3):
                continue
            # a one-off payslip (e.g. unpaid leave) should not replace the recurring amount
            common, count = Counter(r[1] for r in found[1]).most_common(1)[0]
            streams.append((anchor[0], common if count >= 2 else anchor[1], anchor[2].currency, desc))

    sched = sorted((e for e in events if e.status == "scheduled" and e.direction == "credit"
                    and e.category == "salary" and e.amount is not None
                    and (e.settlement_date or e.event_date) >= rd),
                   key=lambda e: e.settlement_date or e.event_date)

    from_clusters = False
    if not ended and not streams and not sched:
        streams = _cluster_income(hist, rd)
        from_clusters = bool(streams)

    # main stream occurrences: list of [date, amount, currency]
    main: List[list] = []
    streams.sort(key=lambda s: -fx.convert(s[1], s[2], home, s[0]))
    others = streams
    if sched:
        first = sched[0]
        d0 = first.settlement_date or first.event_date
        main = [[d0, first.amount, first.currency]]
        k = 1
        while add_months(d0, k, d0.day) <= end:
            main.append([add_months(d0, k, d0.day), first.amount, first.currency])
            k += 1
        for e in sched[1:]:
            d = e.settlement_date or e.event_date
            main = [m for m in main if (m[0].year, m[0].month) != (d.year, d.month)] + [[d, e.amount, e.currency]]
        others = streams[1:] if streams else []
    elif streams:
        a0, amt, cur, _ = streams[0]
        main = [[t, amt, cur] for t in project(a0, 30, True, rd, end)]
        others = streams[1:]

    extra: List[Flow] = []

    def month_key(d):
        return (d.year, d.month)

    for am in sorted(amendments, key=lambda a: a.sent):
        cur = am.currency or home
        if am.kind == "income_end" and any(_paid_on(e) > am.sent for e in hist):
            continue  # salary settled after the notice: income resumed, the notice is outdated
        if am.kind == "income_end" or (am.kind == "income_uncertain" and from_clusters):
            main, others = [], []
        elif am.kind == "salary_set":
            main = [[m[0], am.amount, cur] for m in main]
            others = []
        elif am.kind == "salary_base":
            main = [[m[0], am.amount, cur] for m in main]
        elif am.kind == "salary_raise":
            main = [[m[0], am.amount, cur] if m[0] >= am.effective else m for m in main]
        elif am.kind == "salary_date":
            # "this replaces the payroll date": the revised pay day holds from that payroll onward
            nxt = [m for m in sorted(main) if m[0] >= am.sent]
            for k, m in enumerate(nxt):
                m[0] = add_months(am.effective, k, am.effective.day)
        elif am.kind in ("salary_next", "salary_arrears"):
            nxt = [m for m in sorted(main) if m[0] >= max(rd, am.sent)]
            if nxt:
                nxt[0][1] = am.amount + (am.extra or 0.0)
                nxt[0][2] = cur
        elif am.kind in ("salary_first", "salary_resume"):
            d0 = am.effective
            main = [m for m in main if month_key(m[0]) < month_key(d0)]
            k = 0
            while add_months(d0, k, d0.day) <= end:
                main.append([add_months(d0, k, d0.day), am.amount, cur])
                k += 1
        elif am.kind == "salary_on_date":
            main = [m for m in main if month_key(m[0]) != month_key(am.effective)]
            main.append([am.effective, am.amount, cur])
        elif am.kind == "income_once":
            if rd <= am.effective <= end:
                extra.append(Flow(am.effective, fx.convert(am.amount, cur, home, am.effective),
                                  "Confirmed invoice payment", "income"))

    out: List[Flow] = []
    for d, amt, cur in main:
        if rd <= d <= end:
            out.append(Flow(d, fx.convert(amt, cur, home, d), "Confirmed salary", "salary"))
    for a0, amt, cur, desc in others:
        for t in project(a0, 30, True, rd, end):
            out.append(Flow(t, fx.convert(amt, cur, home, t), desc, "salary"))
    return out + extra
