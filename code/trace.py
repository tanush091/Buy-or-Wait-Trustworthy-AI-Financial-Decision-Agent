"""Explain one decision step by step.

    python code/trace.py <request_id>
    python code/trace.py <request_id> --requests path/to/new_requests.csv
"""
import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from code.amendments import parse_messages
from code.candidates import base_candidates
from code.loaders import load_requests, load_sample_requests
from code.main import Engine
from code.metrics import amount_safe_to_pay, earliest_date_for_full_payment
from code.models import fmt_amount
from code.ranker import choose_plan, completes_by_deadline, derive_affordability_status, format_plan_string
from code.validator import is_safe


def trace(request_id: str, requests_path: Path = None):
    eng = Engine()
    reqs = {r.request_id: r for r, _ in load_sample_requests()}
    reqs.update({r.request_id: r for r in load_requests(requests_path)})
    req = reqs[request_id]
    try:
        prof, state, ledger = eng.state(req)
    except (ValueError, KeyError) as exc:
        d = eng.decide(req)
        print(f"REQUEST {req.request_id}: data could not be verified ({str(exc).strip(chr(39))}) -> safe fallback")
        print(f"DECISION {d.affordability_status} / {d.recommended_payment_method}: {d.decision_explanation}")
        return
    cur = prof.home_currency

    print(f"REQUEST {req.request_id} ({req.request_type}) on {req.request_date}: {cur} {fmt_amount(req.requested_amount)}, "
          f"complete by {req.desired_completion_date}, partial allowed={req.allows_partial_payment}")
    print(f"PROFILE balance {fmt_amount(prof.available_balance)}, minimum {fmt_amount(prof.minimum_balance_to_keep)}, "
          f"accepts {sorted(prof.payment_methods_user_will_consider)}, max installments {prof.max_installment_months}")
    print(f"  protect {prof.expense_categories_to_protect}; reduce {prof.expense_categories_user_is_willing_to_reduce}; "
          f"stop {prof.expense_categories_user_is_willing_to_stop}")

    msgs = [m for m in eng.messages if m.user_id == req.user_id or m.request_id == req.request_id]
    amendments, log = parse_messages(msgs, req.request_date)
    print(f"\nMESSAGES {len(msgs)} -> facts used:")
    for a in amendments:
        print(f"  {a.message_id}: {a.kind} amount={a.amount} extra={a.extra} {a.currency or ''} date={a.effective} pct={a.pct}")
    for line in log:
        print(f"  (gate) {line}")

    print("\nRECURRING EXPENSES (projected over the forecast)")
    for s in state.series:
        print(f"  {s.category:20s} {fmt_amount(s.amount):>12s} x{len(s.occurrences):<3d} {s.flexibility:22s} last={s.last_event_id}")
    print("\nOTHER FUTURE FLOWS (pending/scheduled debits, salary, confirmed income)")
    for f in state.flows:
        if f.kind != "recurring":
            print(f"  {f.day} {f.amount:>+16,.2f}  {f.kind:9s} {f.label}")

    lows, _ = ledger._lows_ends()
    i = min(range(len(lows)), key=lambda d: lows[d])
    safe = amount_safe_to_pay(ledger, req.requested_amount)
    earliest = earliest_date_for_full_payment(ledger, req.requested_amount)
    print(f"\nFORECAST lowest balance {cur} {lows[i]:,.2f} on {ledger.rd + __import__('datetime').timedelta(days=i)} "
          f"(minimum {fmt_amount(prof.minimum_balance_to_keep)})")
    print(f"  amount_safe_to_pay = {fmt_amount(safe)}   earliest_date_for_full_payment = {earliest or '(none in 90 days)'}")

    print("\nCANDIDATES (before spending changes)")
    for c in base_candidates(req, prof, eng.options.get(req.request_id, []), safe, earliest):
        print(f"  {c.method:15s} {c.payment_option_id or '':18s} total {fmt_amount(c.total_paid):>12s} "
              f"safe={is_safe(ledger, c.plan)} on_time={completes_by_deadline(c, req)}  {format_plan_string(c.plan)}")
    best = choose_plan(req, prof, eng.options.get(req.request_id, []), ledger, safe, earliest, state.series)
    print(f"\nDECISION {derive_affordability_status(best, req.request_date)} / {best.method}: {format_plan_string(best.plan)} "
          f"changes={[c.render() for c in best.changes] or 'none'}")
    print(eng.decide(req).decision_explanation)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("request_id")
    ap.add_argument("--requests", type=Path, default=None)
    args = ap.parse_args()
    trace(args.request_id, args.requests)
