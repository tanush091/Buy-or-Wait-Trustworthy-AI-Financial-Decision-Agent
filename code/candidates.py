import itertools
from datetime import date, timedelta
from typing import List, Optional, Tuple

from .config import CONFIG
from .models import Candidate, PaymentOption, Profile, Request, Series, SpendingChange


def installment_schedule(opt: PaymentOption) -> List[Tuple[date, float]]:
    step = opt.payment_frequency_days or 30
    return [(opt.first_payment_date + timedelta(days=step * i), opt.payment_amount)
            for i in range(opt.number_of_payments)]


def eligible_installment_options(profile: Profile, options: List[PaymentOption]) -> List[PaymentOption]:
    if "installments" not in profile.payment_methods_user_will_consider:
        return []
    if profile.max_installment_months is None:
        return []  # blank means the user will not consider installments
    # months the plan runs, so weekly or 60-day schedules are judged by length, not payment count
    return [o for o in options
            if o.payment_method == "installments"
            and round(o.number_of_payments * (o.payment_frequency_days or 30) / 30.44) <= profile.max_installment_months]


def change_options(profile: Profile, series: List[Series]) -> List[SpendingChange]:
    """Flexible recurring expenses in categories the user allows changing (never protected ones)."""
    out: List[SpendingChange] = []
    for s in series:
        if not s.occurrences or s.category in profile.expense_categories_to_protect:
            continue
        if s.flexibility in ("stoppable", "reducible_or_stoppable") \
                and s.category in profile.expense_categories_user_is_willing_to_stop:
            out.append(SpendingChange("stop", s.last_event_id))
        if s.flexibility in ("reducible", "reducible_or_stoppable") \
                and s.category in profile.expense_categories_user_is_willing_to_reduce \
                and s.minimum_allowed is not None and s.minimum_allowed < s.amount:
            out.append(SpendingChange("reduce_to", s.last_event_id, s.minimum_allowed))
    return out


def change_sets(options: List[SpendingChange]) -> List[List[SpendingChange]]:
    sets = []
    for k in range(1, CONFIG["max_changes"] + 1):
        for combo in itertools.combinations(options, k):
            ids = [c.event_id for c in combo]
            if len(ids) == len(set(ids)):  # stop and reduce of one event are mutually exclusive
                sets.append(list(combo))
    return sets


def base_candidates(req: Request, profile: Profile, options: List[PaymentOption],
                    safe: float, earliest: Optional[date]) -> List[Candidate]:
    """Every payment approach the user and request permit, before safety filtering."""
    methods = profile.payment_methods_user_will_consider
    amt = req.requested_amount
    out: List[Candidate] = []
    if "full_payment" in methods:
        out.append(Candidate("full_payment", [(req.request_date, amt)], [], None, amt))
    for opt in eligible_installment_options(profile, options):
        out.append(Candidate("installments", installment_schedule(opt), [], opt.payment_option_id,
                             round(opt.total_payable_amount, 2)))
    if ("partial_payment" in methods and req.allows_partial_payment and 0 < safe < amt
            and earliest is not None and earliest <= req.desired_completion_date):
        out.append(Candidate("partial_payment",
                             [(req.request_date, safe), (earliest, round(amt - safe, 2))], [], None, amt))
    if "full_payment" in methods and earliest is not None and earliest > req.request_date:
        out.append(Candidate("wait", [(earliest, amt)], [], None, amt))
    return out
