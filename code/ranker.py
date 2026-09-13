import re
from datetime import date
from typing import List, Optional, Tuple

from .candidates import base_candidates, change_options, change_sets
from .config import CONFIG
from .models import Candidate, PaymentOption, Profile, Request, Series, fmt_amount
from .timeline import Ledger
from .validator import is_safe

NOT_RECOMMENDED = Candidate("not_recommended", [], [], None, 0.0)


def format_plan_string(plan: List[Tuple[date, float]]) -> str:
    if not plan:
        return "none"
    return "|".join(f"{d.isoformat()}:{fmt_amount(a)}" for d, a in plan)


def format_changes_string(changes: list) -> str:
    return "|".join(c.render() for c in changes) if changes else "none"


def completes_by_deadline(c: Candidate, req: Request) -> bool:
    return bool(c.plan) and max(d for d, _ in c.plan) <= req.desired_completion_date


def _option_order(option_id: Optional[str]) -> tuple:
    """Lowest payment_option_id: numeric suffix when present (p2 < p10), otherwise the id text."""
    if not option_id:
        return (0, "")
    digits = re.search(r"(\d+)$", option_id)
    return (int(digits[1]) if digits else float("inf"), option_id)


def sort_key(c: Candidate, req: Request) -> tuple:
    opt_num = _option_order(c.payment_option_id)
    return (
        0 if completes_by_deadline(c, req) else 1,   # 1. complete by desired_completion_date
        1 if c.changes else 0,                        # 2. no spending changes
        round(c.total_paid, 2),                       # 3. lowest total paid
        c.plan[0][0] if c.plan else date.max,         # 4. start earlier
        len(c.plan),                                  # 5. fewer payments
        opt_num,                                      # 6. lowest payment_option_id
        round(c.savings, 2), len(c.changes),          # least disruptive spending changes
        "|".join(ch.render() for ch in c.changes),
    )


def choose_plan(req: Request, profile: Profile, options: List[PaymentOption], ledger: Ledger,
                safe: float, earliest: Optional[date], series: List[Series]) -> Candidate:
    bases = base_candidates(req, profile, options, safe, earliest)
    valid = [c for c in bases if is_safe(ledger, c.plan)]
    buffer = CONFIG.get("now_buffer", 0.0)
    if buffer:  # experimental: pay-now-in-full only with a cushion above the minimum
        cap = ledger.capacity(0)
        valid = [c for c in valid if not (c.method == "full_payment" and c.plan[0][0] == req.request_date
                                          and cap < req.requested_amount * (1 + buffer))]
    allowance = CONFIG.get("change_tolerance", 0.0) * req.requested_amount  # experimental, 0 = strict

    if not any(completes_by_deadline(c, req) for c in valid):
        # Only reach for spending changes when no unchanged plan finishes on time.
        for chset in change_sets(change_options(profile, series)):
            _, saved = ledger.savings(chset)
            for b in bases:
                if b.method in ("partial_payment", "wait"):
                    continue
                if is_safe(ledger, b.plan, chset, allowance):
                    valid.append(Candidate(b.method, b.plan, chset, b.payment_option_id, b.total_paid, saved))
            # `wait` depends on unchanged capacity (earliest_date_for_full_payment), so it never takes changes

    if not valid:
        return NOT_RECOMMENDED
    return min(valid, key=lambda c: sort_key(c, req))


def derive_affordability_status(best: Candidate, request_date: date) -> str:
    if best.method == "not_recommended":
        return "not_affordable"
    if best.method == "full_payment" and not best.changes and best.plan[0][0] == request_date:
        return "affordable_now"
    if best.method == "wait" and not best.changes:
        return "affordable_later"
    return "affordable_with_plan"
