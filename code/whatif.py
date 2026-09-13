"""What-if analysis: re-decide one request with a changed balance, price, deadline, minimum balance
or payment preferences, and show exactly which outputs change. Nothing is written; the dataset
files are untouched and the engine is restored afterwards.

    python code/whatif.py <request_id>--balance +5000
    python code/whatif.py <request_id>--amount 90000 --deadline 2024-12-01
    python code/whatif.py <request_id>--minimum -500 --methods full_payment,installments --allow-partial yes
    python code/whatif.py <request_id>--balance +5000 --json

Numbers starting with + or - are changes; plain numbers are new values. When the price changes,
the seller's installment options are scaled to the new price (same count, dates and frequency).
"""
import argparse
import dataclasses
import json
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from code.audit import audit_request
from code.loaders import load_requests, load_sample_requests
from code.main import Engine

METHODS = {"full_payment", "partial_payment", "installments"}
ROWS = [("amount_safe_to_pay", "Safe to pay today"), ("affordability_status", "Status"),
        ("recommended_payment_method", "Method"), ("payment_plan", "Plan"),
        ("earliest_date_for_full_payment", "Earliest full payment"), ("spending_changes_needed", "Spending changes"),
        ("risk", "Risk (cushion)"), ("lowest", "Lowest projected balance")]


def _apply(value: Optional[str], current: float) -> float:
    """'+500' / '-500' change the current value; '1200' replaces it."""
    if value is None:
        return current
    v = str(value).strip().replace(",", "")
    return current + float(v) if v[:1] in "+-" else float(v)


def scenario(engine: Engine, req, balance: Optional[str] = None, amount: Optional[str] = None,
             deadline: Optional[date] = None, minimum: Optional[str] = None,
             allow_partial: Optional[bool] = None, methods: Optional[List[str]] = None) -> Tuple[dict, dict, List[str]]:
    """Return (baseline audit, scenario audit, applied changes). The engine is left exactly as it was."""
    prof = engine.profiles[req.user_id]
    new_prof = dataclasses.replace(
        prof,
        available_balance=_apply(balance, prof.available_balance),
        minimum_balance_to_keep=_apply(minimum, prof.minimum_balance_to_keep),
        payment_methods_user_will_consider=set(methods) if methods is not None else prof.payment_methods_user_will_consider)
    new_req = dataclasses.replace(
        req,
        requested_amount=round(_apply(amount, req.requested_amount), 2),
        desired_completion_date=deadline or req.desired_completion_date,
        allows_partial_payment=req.allows_partial_payment if allow_partial is None else allow_partial)
    if new_req.requested_amount <= 0:
        raise ValueError("the requested amount must be positive")
    if new_prof.minimum_balance_to_keep < 0:
        raise ValueError("the minimum balance cannot be negative")
    if new_req.desired_completion_date < req.request_date:
        raise ValueError("the deadline cannot be before the request date")
    if methods is not None and not set(methods) <= METHODS:
        raise ValueError(f"payment methods must be among {sorted(METHODS)}")

    cur = prof.home_currency
    notes = []
    for label, old, new in (("balance", prof.available_balance, new_prof.available_balance),
                            ("minimum balance", prof.minimum_balance_to_keep, new_prof.minimum_balance_to_keep),
                            ("price", req.requested_amount, new_req.requested_amount)):
        if abs(old - new) > 0.005:
            notes.append(f"{label} {cur} {old:,.2f} -> {new:,.2f}")
    if new_req.desired_completion_date != req.desired_completion_date:
        notes.append(f"deadline {req.desired_completion_date} -> {new_req.desired_completion_date}")
    if new_req.allows_partial_payment != req.allows_partial_payment:
        notes.append(f"partial payment allowed {req.allows_partial_payment} -> {new_req.allows_partial_payment}")
    if new_prof.payment_methods_user_will_consider != prof.payment_methods_user_will_consider:
        notes.append(f"methods {sorted(prof.payment_methods_user_will_consider)} -> "
                     f"{sorted(new_prof.payment_methods_user_will_consider)}")

    base = audit_request(engine, req)
    opts = engine.options.get(req.request_id)
    engine.profiles[req.user_id] = new_prof
    if opts is not None and abs(new_req.requested_amount - req.requested_amount) > 0.005:
        k = new_req.requested_amount / req.requested_amount
        engine.options[req.request_id] = [dataclasses.replace(o, payment_amount=round(o.payment_amount * k, 2),
                                                              total_payable_amount=round(o.total_payable_amount * k, 2))
                                          for o in opts]
        notes.append("installment options scaled to the new price")
    try:
        alt = audit_request(engine, new_req)
    finally:
        engine.profiles[req.user_id] = prof
        if opts is not None:
            engine.options[req.request_id] = opts
    return base, alt, notes


def _view(a: dict) -> Dict[str, str]:
    d, f = a["decision"], a.get("forecast") or {}
    v = {k: str(d[k]) for k, _ in ROWS[:6]}
    v["amount_safe_to_pay"] = f"{d['amount_safe_to_pay']:,.2f}"
    buffer = f.get("buffer_days")
    v["risk"] = f.get("risk", "blocked") + ("" if buffer is None else f" ({buffer} days)")
    with_plan = f.get("lowest_with_plan") is not None
    low = f.get("lowest_with_plan") if with_plan else f.get("lowest_without_purchase")
    day = f.get("lowest_with_plan_date") if with_plan else f.get("lowest_without_purchase_date")
    v["lowest"] = "" if low is None else f"{low:,.2f} on {day}"
    return v


def report(base: dict, alt: dict, notes: List[str]) -> str:
    b, s = _view(base), _view(alt)
    width = min(max(max(len(b[k]) for k, _ in ROWS), 10), 44)
    lines = [f"WHAT-IF {base['request_id']} ({base['currency']}): " + ("; ".join(notes) or "no change"),
             f"{'':26s} {'baseline':<{width}s}  scenario"]
    for key, label in ROWS:
        mark = "   <- changed" if b[key] != s[key] else ""
        lines.append(f"{label:26s} {b[key]:<{width}s}  {s[key]}{mark}")
    lines.append("")
    lines.append("Scenario: " + alt["decision"]["decision_explanation"])
    return "\n".join(lines)


def find_request(request_id: str, requests_path: Optional[Path] = None):
    reqs = {r.request_id: r for r, _ in load_sample_requests()}
    reqs.update({r.request_id: r for r in load_requests(requests_path)})
    if request_id not in reqs:
        raise SystemExit(f"unknown request_id {request_id!r}")
    return reqs[request_id]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Re-decide one request with changed inputs.")
    ap.add_argument("request_id")
    ap.add_argument("--balance", help="new balance, or +/- change")
    ap.add_argument("--amount", help="new requested amount, or +/- change")
    ap.add_argument("--minimum", help="new minimum balance to keep, or +/- change")
    ap.add_argument("--deadline", type=date.fromisoformat, help="new desired completion date (YYYY-MM-DD)")
    ap.add_argument("--allow-partial", choices=["yes", "no"], help="whether the request allows partial payment")
    ap.add_argument("--methods", help="comma-separated methods the user will consider")
    ap.add_argument("--requests", type=Path, default=None, help="requests file (default dataset/requests.csv)")
    ap.add_argument("--json", action="store_true", help="print the baseline and scenario audits as JSON")
    args = ap.parse_args(argv)
    engine = Engine()
    req = find_request(args.request_id, args.requests)
    methods = [m.strip() for m in args.methods.split(",") if m.strip()] if args.methods else None
    partial = None if args.allow_partial is None else args.allow_partial == "yes"
    try:
        base, alt, notes = scenario(engine, req, args.balance, args.amount, args.deadline, args.minimum, partial, methods)
    except ValueError as exc:
        raise SystemExit(f"invalid scenario: {exc}")
    print(json.dumps({"changes": notes, "baseline": base, "scenario": alt}, indent=1) if args.json
          else report(base, alt, notes))


if __name__ == "__main__":
    main()
