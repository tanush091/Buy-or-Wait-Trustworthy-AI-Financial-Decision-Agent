"""Independent re-verification of every output row against the challenge rules.

Where schema_check validates format, this re-derives each row from the data and checks
the decision itself: the plan is safe for 90 days, the method is one the user accepts,
status/method/plan/earliest agree, installments match a supplied option within the user's
limit, partial payments follow the two-payment rule, and spending changes only touch
flexible, non-protected recurring expenses in allowed categories.

    python code/evaluation/consistency_check.py [output.csv] [requests.csv]
"""
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd

from code.candidates import eligible_installment_options, installment_schedule
from code.config import OUTPUT_PATH
from code.loaders import load_requests
from code.main import Engine
from code.metrics import amount_safe_to_pay, earliest_date_for_full_payment
from code.models import SpendingChange
from code.ranker import format_plan_string
from code.validator import is_safe


def parse_plan(s: str):
    if not isinstance(s, str) or s == "none":
        return []
    return [(date.fromisoformat(p.split(":")[0]), float(p.split(":")[1])) for p in s.split("|")]


def parse_changes(s: str):
    if not isinstance(s, str) or s == "none":
        return []
    out = []
    for p in s.split("|"):
        parts = p.split(":")
        out.append(SpendingChange(parts[0], parts[1], float(parts[2]) if parts[0] == "reduce_to" else None))
    return out


def check(output_path: Path = OUTPUT_PATH, requests_path: Path = None):
    engine = Engine()
    reqs = {r.request_id: r for r in load_requests(requests_path)}
    out = pd.read_csv(output_path, dtype=str, keep_default_na=False)
    problems = []
    for _, row in out.iterrows():
        rid = row.request_id
        req = reqs[rid]
        try:
            prof, state, ledger = engine.state(req)
        except (ValueError, KeyError) as exc:
            # unverifiable data: the only consistent answer is the safe fallback
            if not (row.recommended_payment_method == "not_recommended" and row.payment_plan == "none"):
                problems.append((rid, [f"unverifiable data ({exc}) but a plan was recommended"]))
            continue
        methods = prof.payment_methods_user_will_consider
        status, method = row.affordability_status, row.recommended_payment_method
        plan, changes = parse_plan(row.payment_plan), parse_changes(row.spending_changes_needed)
        safe = float(row.amount_safe_to_pay)
        earliest = row.earliest_date_for_full_payment
        bad = []

        if abs(safe - amount_safe_to_pay(ledger, req.requested_amount)) > 0.01:
            bad.append("safe amount differs from re-computed forecast")
        if not 0 <= safe <= req.requested_amount + 1e-6:
            bad.append("safe amount out of bounds")
        cap = earliest_date_for_full_payment(ledger, req.requested_amount)
        if status != "affordable_now" and earliest != (cap.isoformat() if cap else ""):
            bad.append("earliest date differs from re-computed capacity")

        pairs = {"affordable_now": {"full_payment"}, "affordable_later": {"wait"},
                 "not_affordable": {"not_recommended"},
                 "affordable_with_plan": {"full_payment", "partial_payment", "installments"}}
        if method not in pairs.get(status, set()):
            bad.append(f"status {status} inconsistent with method {method}")
        if method in ("full_payment", "partial_payment", "installments") and method not in methods:
            bad.append("method not accepted by user")
        if method == "wait" and "full_payment" not in methods:
            bad.append("wait without full_payment acceptance")
        if method == "not_recommended" and (plan or changes):
            bad.append("not_recommended must have no plan or changes")
        if status == "affordable_now" and (earliest != req.request_date.isoformat() or changes
                                           or plan != [(req.request_date, req.requested_amount)]):
            bad.append("affordable_now must pay in full today with no changes")
        if method == "wait" and (len(plan) != 1 or plan[0][0].isoformat() != earliest or changes):
            bad.append("wait must be one full payment on the earliest date")
        if method == "partial_payment":
            if not req.allows_partial_payment or len(plan) != 2 or plan[0][0] != req.request_date \
                    or abs(plan[0][1] - safe) > 0.01 or plan[1][0].isoformat() != earliest \
                    or plan[1][0] > req.desired_completion_date \
                    or abs(plan[0][1] + plan[1][1] - req.requested_amount) > 0.01 or not 0 < safe < req.requested_amount:
                bad.append("partial payment rule violated")
        if method == "installments":
            opts = eligible_installment_options(prof, engine.options.get(rid, []))
            if row.payment_plan not in {format_plan_string(installment_schedule(o)) for o in opts}:
                bad.append("installments do not match an eligible supplied option")
        if method in ("full_payment", "partial_payment", "installments", "wait") and not is_safe(ledger, plan, changes):
            bad.append("plan breaks the minimum balance within 90 days")

        eligible = {s.last_event_id: s for s in state.series}
        if len(changes) > 3 or len({c.event_id for c in changes}) != len(changes):
            bad.append("too many or duplicate spending changes")
        for c in changes:
            s = eligible.get(c.event_id)
            if s is None or s.category in prof.expense_categories_to_protect:
                bad.append(f"{c.event_id} is not a changeable recurring expense")
            elif c.kind == "stop" and (s.flexibility not in ("stoppable", "reducible_or_stoppable")
                                       or s.category not in prof.expense_categories_user_is_willing_to_stop):
                bad.append(f"{c.event_id} may not be stopped")
            elif c.kind == "reduce_to" and (s.flexibility not in ("reducible", "reducible_or_stoppable")
                                            or s.category not in prof.expense_categories_user_is_willing_to_reduce
                                            or s.minimum_allowed is None or c.new_amount + 1e-6 < s.minimum_allowed):
                bad.append(f"{c.event_id} may not be reduced to {c.new_amount}")
        if not row.decision_explanation.strip():
            bad.append("empty explanation")
        if bad:
            problems.append((rid, bad))

    print(f"Checked {len(out)} rows: {len(out) - len(problems)} consistent, {len(problems)} with problems")
    for rid, bad in problems[:25]:
        print(f"  {rid}: {'; '.join(bad)}")
    return problems


if __name__ == "__main__":
    args = [Path(a) for a in sys.argv[1:]]
    check(*(args or [OUTPUT_PATH]))
