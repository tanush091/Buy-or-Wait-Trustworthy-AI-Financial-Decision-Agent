"""Decision audit and risk flags, built from the engine's own intermediate results.

For one request it records the forecast's lowest point with and without the recommended plan,
how many days of normal spending the cushion above the minimum covers (the risk level), every
payment approach the user could take with the reason it was chosen or rejected, and the evidence
behind the forecast (message facts, images, recurring expenses). Read-only: it never changes a
decision; `consistent` confirms the audit re-derived exactly the decision Engine.decide made.
"""
from collections import defaultdict
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from .amendments import parse_messages, rule_matches
from .candidates import eligible_installment_options, installment_schedule
from .metrics import amount_safe_to_pay, earliest_date_for_full_payment
from .models import Candidate
from .ranker import _option_order, choose_plan, format_plan_string, sort_key
from .validator import is_safe, plan_payments

# Cushion left above the minimum at the forecast's lowest point, in days of the user's average
# forecast outflow: under a week of normal spending is tight, under a month moderate.
TIGHT_DAYS, MODERATE_DAYS = 7, 30
RISK_LEVELS = ("comfortable", "moderate", "tight", "blocked")
RISK_COLUMNS = ["request_id", "affordability_status", "recommended_payment_method", "currency",
                "lowest_projected_balance", "lowest_balance_date", "minimum_balance_to_keep",
                "margin_above_minimum", "buffer_days", "risk_level"]
# the spec's ranking order (ranker.sort_key positions 0-5), phrased as the reason a plan lost
_RANK_REASONS = ["finishes after the desired completion date", "needs spending changes",
                 "costs more in total", "starts later", "uses more payments", "has a higher payment option id"]


def risk_level(method: str, buffer_days: Optional[float]) -> str:
    if method == "not_recommended":
        return "blocked"
    if buffer_days is None:  # no forecast outflows, so the cushion cannot shrink
        return "comfortable"
    if buffer_days < TIGHT_DAYS:
        return "tight"
    return "moderate" if buffer_days < MODERATE_DAYS else "comfortable"


def _r(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 2)


def _on_time(plan, req) -> bool:
    return bool(plan) and max(d for d, _ in plan) <= req.desired_completion_date


def option_verdicts(req, prof, options, ledger, safe, earliest, best: Candidate) -> List[Dict[str, Any]]:
    """Every approach the user could take: eligible, safe (unchanged spending), on time, and why it lost."""
    methods = prof.payment_methods_user_will_consider
    amt = req.requested_amount
    eligible_ids = {o.payment_option_id for o in eligible_installment_options(prof, options)}
    best_key = sort_key(best, req) if best.plan else None
    rows: List[Dict[str, Any]] = []
    chosen_done = False

    def add(method, option_id, plan, total, blocked: Optional[str]):
        nonlocal chosen_done
        chosen = (not chosen_done and blocked is None and best.method == method and best.plan == plan
                  and (method != "installments" or best.payment_option_id == option_id))
        chosen_done = chosen_done or chosen
        safe_now = is_safe(ledger, plan) if blocked is None and plan else None
        if chosen:
            reason = "recommended" + (" with spending changes" if best.changes else "")
        elif blocked is not None:
            reason = blocked
        elif not safe_now:
            reason = "would take the balance below the minimum within the 90-day forecast"
        else:
            key = sort_key(Candidate(method, plan, [], option_id, total), req)
            i = next((j for j in range(len(_RANK_REASONS)) if best_key and key[j] != best_key[j]), None)
            reason = f"safe, but ranked lower: {_RANK_REASONS[i]}" if i is not None else "safe, but ranked lower"
        rows.append({"option": option_id or method, "method": method, "plan": format_plan_string(plan),
                     "total_paid": _r(total), "eligible": blocked is None, "safe": safe_now,
                     "on_time": _on_time(plan, req), "chosen": chosen, "reason": reason})

    no_full = "the user does not consider full payment"
    has_full_row = False
    for o in sorted(options, key=lambda o: _option_order(o.payment_option_id)):
        if o.payment_method == "full_payment":
            has_full_row = True
            add("full_payment", o.payment_option_id, [(req.request_date, amt)], amt,
                None if "full_payment" in methods else no_full)
        elif o.payment_method == "installments":
            if "installments" not in methods:
                why = "the user does not consider installments"
            elif prof.max_installment_months is None:
                why = "max_installment_months is blank: the user will not consider installments"
            elif o.payment_option_id not in eligible_ids:
                months = round(o.number_of_payments * (o.payment_frequency_days or 30) / 30.44)
                why = f"runs about {months} months, above the user's {prof.max_installment_months}-month limit"
            else:
                why = None
            add("installments", o.payment_option_id, installment_schedule(o), o.total_payable_amount, why)
        else:
            add(o.payment_method, o.payment_option_id, installment_schedule(o), o.total_payable_amount,
                f"not used: {o.payment_method.replace('_', ' ')} plans follow the spec's own rules")
    if not has_full_row:
        add("full_payment", None, [(req.request_date, amt)], amt, None if "full_payment" in methods else no_full)

    if "partial_payment" not in methods:
        why = "the user does not consider partial payment"
    elif not req.allows_partial_payment:
        why = "the request does not allow partial payment"
    elif safe <= 0:
        why = "nothing is safe to pay today"
    elif safe >= amt:
        why = "the full amount is already safe today"
    elif earliest is None:
        why = "the rest never becomes safe within the 90-day forecast"
    elif earliest > req.desired_completion_date:
        why = f"the rest only becomes safe on {earliest.isoformat()}, after the desired completion date"
    else:
        why = None
    add("partial_payment", None, [(req.request_date, safe), (earliest, round(amt - safe, 2))] if why is None else [],
        amt, why)

    if "full_payment" not in methods:
        why = "waiting means paying in full later, and " + no_full
    elif earliest is None:
        why = "full payment never becomes safe within the 90-day forecast"
    elif earliest <= req.request_date:
        why = "full payment is already safe today"
    else:
        why = None
    add("wait", None, [(earliest, amt)] if why is None else [], amt, why)
    return rows


def audit_request(engine, req) -> Dict[str, Any]:
    """Full audit of one decision, re-derived with the same calls Engine.decide makes."""
    decision = engine.decide(req)
    prof = engine.profiles.get(req.user_id)
    out: Dict[str, Any] = {
        "request_id": req.request_id, "user_id": req.user_id, "request_type": req.request_type,
        "request_date": req.request_date.isoformat(), "requested_amount": req.requested_amount,
        "currency": prof.home_currency if prof else None,
        "desired_completion_date": req.desired_completion_date.isoformat(),
        "allows_partial_payment": req.allows_partial_payment,
        "decision": {k: v for k, v in asdict(decision).items() if k != "request_id"},
        "fallback": None, "consistent": True,
        "forecast": {"minimum": _r(prof.minimum_balance_to_keep) if prof else None, "risk": "blocked"},
        "options": [], "spending_changes": [], "evidence": {}, "recurring_expenses": [], "upcoming": {},
    }
    try:
        prof, state, ledger = engine.state(req)
    except (ValueError, KeyError) as exc:  # the decision is the safe fallback; say why
        out["fallback"] = str(exc).strip("'\"")
        out["consistent"] = decision.recommended_payment_method == "not_recommended"
        return out

    options = engine.options.get(req.request_id, [])
    safe = amount_safe_to_pay(ledger, req.requested_amount)
    earliest = earliest_date_for_full_payment(ledger, req.requested_amount)
    best = choose_plan(req, prof, options, ledger, safe, earliest, state.series)
    out["consistent"] = (format_plan_string(best.plan) == decision.payment_plan
                         and best.method == decision.recommended_payment_method
                         and abs(safe - decision.amount_safe_to_pay) < 0.005)

    low0, day0 = ledger.lowest()
    low1, day1 = ledger.lowest(plan_payments(ledger, best.plan), best.changes) if best.plan else (low0, day0)
    daily_out = sum(ledger.out) / len(ledger.out)
    minimum = prof.minimum_balance_to_keep
    margin = low1 - minimum
    buffer_days = margin / daily_out if daily_out > 0 else None
    out["forecast"] = {
        "starting_balance": _r(prof.available_balance), "minimum": _r(minimum), "horizon_days": ledger.H,
        "lowest_without_purchase": _r(low0), "lowest_without_purchase_date": day0.isoformat(),
        "lowest_with_plan": _r(low1) if best.plan else None,
        "lowest_with_plan_date": day1.isoformat() if best.plan else None,
        "margin_above_minimum": _r(margin), "avg_daily_outflow": _r(daily_out),
        "buffer_days": None if buffer_days is None else round(buffer_days, 1),
        "risk": risk_level(best.method, buffer_days),
    }
    out["options"] = option_verdicts(req, prof, options, ledger, safe, earliest, best)
    out["spending_changes"] = [c.render() for c in best.changes]

    msgs = [m for m in engine.messages if m.user_id == req.user_id or m.request_id == req.request_id]
    texts = {m.message_id: m.message_text for m in msgs}
    amendments, gate_log = parse_messages(msgs, req.request_date, engine.llm_facts)
    out["evidence"] = {
        "messages_considered": len(msgs),
        "facts_used": [{"message_id": a.message_id, "kind": a.kind,
                        "source": "rules" if rule_matches(texts.get(a.message_id, "")) else "gemini",
                        "amount": a.amount, "extra": a.extra, "currency": a.currency, "pct": a.pct,
                        "effective": a.effective.isoformat() if a.effective else None} for a in amendments],
        "gate_log": gate_log,
        "images_used": [{"event_id": e.event_id, "image_id": engine.image_by_event[e.event_id],
                         "amount_read": engine.image_amount(e.event_id)}
                        for e in engine.events[req.user_id] if e.amount is None and e.event_id in engine.image_by_event],
    }
    out["recurring_expenses"] = [{"event_id": s.last_event_id, "category": s.category, "description": s.description,
                                  "amount": _r(s.amount), "occurrences_in_forecast": len(s.occurrences),
                                  "flexibility": s.flexibility} for s in state.series]
    upcoming: Dict[str, Dict[str, float]] = defaultdict(lambda: {"count": 0, "total": 0.0})
    for f in state.flows:
        if f.kind != "recurring" and 0 <= (f.day - req.request_date).days <= ledger.H:
            upcoming[f.kind]["count"] += 1
            upcoming[f.kind]["total"] = round(upcoming[f.kind]["total"] + f.amount, 2)
    out["upcoming"] = dict(upcoming)
    return out


def risk_row(a: Dict[str, Any]) -> Dict[str, Any]:
    """One risk_flags.csv row; the lowest balance is with the plan, or without the purchase if none."""
    f = a.get("forecast") or {}
    with_plan = f.get("lowest_with_plan") is not None
    blank = lambda v: "" if v is None else v  # noqa: E731
    return {
        "request_id": a["request_id"],
        "affordability_status": a["decision"]["affordability_status"],
        "recommended_payment_method": a["decision"]["recommended_payment_method"],
        "currency": blank(a.get("currency")),
        "lowest_projected_balance": blank(f.get("lowest_with_plan") if with_plan else f.get("lowest_without_purchase")),
        "lowest_balance_date": blank(f.get("lowest_with_plan_date") if with_plan else f.get("lowest_without_purchase_date")),
        "minimum_balance_to_keep": blank(f.get("minimum")),
        "margin_above_minimum": blank(f.get("margin_above_minimum")),
        "buffer_days": blank(f.get("buffer_days")),
        "risk_level": f.get("risk", "blocked"),
    }
