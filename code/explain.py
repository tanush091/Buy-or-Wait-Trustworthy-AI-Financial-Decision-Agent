"""Grounded, template-based explanations built only from computed facts (no model authority)."""
from datetime import date
from typing import Dict, List, Optional, Tuple

from .config import CONFIG
from .models import Candidate, Profile, Request, Series

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def money(amt: float, cur: str) -> str:
    if abs(amt - round(amt)) < 0.005:
        return f"{cur} {int(round(amt)):,}"
    return f"{cur} {amt:,.2f}"


def long_date(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def _changes_text(best: Candidate, series: List[Series], cur: str) -> str:
    names: Dict[str, str] = {s.last_event_id: s.description.lower() for s in series}
    parts = []
    for i, ch in enumerate(best.changes):
        verb = "stop" if ch.kind == "stop" else "reduce"
        text = f"{verb} the {names.get(ch.event_id, ch.event_id)}"
        if ch.kind == "reduce_to":
            text += f" to {money(ch.new_amount or 0.0, cur)}"
        parts.append(text)
    text = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]
    return text[0].upper() + text[1:]


def generate_explanation(req: Request, profile: Profile, best: Candidate, status: str, safe: float,
                         earliest: Optional[date], series: List[Series],
                         lowest: Optional[Tuple[float, date]] = None) -> str:
    """Decision sentence, plus (explain_detail) the tightest point of the forecast that drives it."""
    text = _base_explanation(req, profile, best, status, safe, earliest, series)
    if lowest is None or not CONFIG.get("explain_detail", True):
        return text
    amount, day = lowest
    cur = profile.home_currency
    if best.method == "not_recommended":
        if amount < profile.minimum_balance_to_keep:
            # existing commitments already break the minimum: say so, it is the real reason
            return (f"{text} Even without this purchase, existing commitments take the balance below the minimum, "
                    f"to {money(amount, cur)} on {long_date(day)}.")
        return f"{text} Without this purchase, the lowest projected balance is {money(amount, cur)} on {long_date(day)}."
    return f"{text} The lowest projected balance with this plan is {money(amount, cur)} on {long_date(day)}."


def _base_explanation(req: Request, profile: Profile, best: Candidate, status: str, safe: float,
                      earliest: Optional[date], series: List[Series]) -> str:
    cur = profile.home_currency
    minimum = money(profile.minimum_balance_to_keep, cur)
    amount = money(req.requested_amount, cur)

    if status == "affordable_now":
        return f"Pay {amount} today. This leaves at least {minimum} available over the next 90 days."

    if best.method == "installments":
        n, per = len(best.plan), money(best.plan[0][1], cur)
        core = f"use {n} installments of {per}, starting {long_date(best.plan[0][0])}"
        if best.changes:
            return f"{_changes_text(best, series, cur)}, then {core}. This leaves at least {minimum} available."
        return f"{core[0].upper() + core[1:]}. This leaves at least {minimum} available."

    if best.method == "partial_payment":
        (_, a1), (d2, a2) = best.plan
        return (f"Pay {money(a1, cur)} today and the remaining {money(a2, cur)} on {long_date(d2)}. "
                f"This completes the full request and keeps the {minimum} minimum protected.")

    if best.method == "full_payment" and best.changes:
        return (f"{_changes_text(best, series, cur)}, then pay {amount} today. "
                f"This leaves at least {minimum} available.")

    if best.method == "wait":
        when = long_date(best.plan[0][0])
        if best.changes:
            return (f"{_changes_text(best, series, cur)}, then pay {amount} in full on {when}. "
                    f"This keeps the {minimum} minimum protected.")
        return f"Pay {amount} in full on {when}. Paying earlier would take the balance below the {minimum} minimum."

    accepted = profile.payment_methods_user_will_consider
    if earliest is not None and "full_payment" not in accepted:
        # capacity exists; the blocker is the user's own method preferences / option limits
        when = "today" if earliest == req.request_date else f"from {long_date(earliest)}"
        methods = ", ".join(m.replace("_", " ") for m in sorted(accepted)) or "none"
        return (f"Do not proceed with the {amount} request. A single full payment would be safe {when}, "
                f"but the user does not consider full payment, and none of the methods they accept "
                f"({methods}) gives a safe plan within their limits.")
    if earliest is None:
        return (f"Do not make this payment by {long_date(req.desired_completion_date)}. The full amount does not "
                f"become safe within the 90-day forecast, and none of the available options keeps the "
                f"{minimum} minimum protected.")
    if safe > 0 and accepted == {"partial_payment"}:
        return (f"Do not proceed with the {amount} request. Although {money(safe, cur)} is available today, "
                f"the full amount cannot be completed safely within 90 days.")
    return (f"Do not make this payment by {long_date(req.desired_completion_date)}. "
            f"None of the available options keeps the {minimum} minimum protected.")
