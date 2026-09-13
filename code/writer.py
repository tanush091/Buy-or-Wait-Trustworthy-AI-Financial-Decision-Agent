import csv
from pathlib import Path
from typing import List

from .config import OUTPUT_PATH
from .models import Decision, fmt_amount

FIELDNAMES = [
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
]


def write_decisions_csv(decisions: List[Decision], dest_path: Path = OUTPUT_PATH):
    with open(dest_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for d in decisions:
            writer.writerow({
                "request_id": d.request_id,
                "amount_safe_to_pay": fmt_amount(d.amount_safe_to_pay),
                "affordability_status": d.affordability_status,
                "recommended_payment_method": d.recommended_payment_method,
                "payment_plan": d.payment_plan,
                "earliest_date_for_full_payment": d.earliest_date_for_full_payment,
                "spending_changes_needed": d.spending_changes_needed,
                "decision_explanation": d.decision_explanation,
            })
