from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Set, Tuple
import pandas as pd

import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))  # allows `python code/schema_check.py`

from code.config import (
    AFFORDABILITY_STATUSES,
    RECOMMENDED_PAYMENT_METHODS,
    OUTPUT_PATH,
    DATASET_DIR,
)
from code.models import fmt_amount

def validate_output_csv(output_path: Path = OUTPUT_PATH, requests_path: Path = None) -> Tuple[bool, List[str]]:
    errors: List[str] = []
    
    if not output_path.exists():
        return False, [f"Output file {output_path} does not exist."]

    # Load output.csv
    try:
        df_out = pd.read_csv(output_path)
    except Exception as e:
        return False, [f"Failed to read CSV: {e}"]

    # Required columns
    expected_cols = [
        "request_id",
        "amount_safe_to_pay",
        "affordability_status",
        "recommended_payment_method",
        "payment_plan",
        "earliest_date_for_full_payment",
        "spending_changes_needed",
        "decision_explanation",
    ]
    if list(df_out.columns) != expected_cols:
        errors.append(f"Columns mismatch. Expected {expected_cols}, got {list(df_out.columns)}")

    # Check row count (must be 250 for requests.csv)
    df_req = pd.read_csv(requests_path or (DATASET_DIR / "requests.csv"))
    if len(df_out) != len(df_req):
        errors.append(f"Row count mismatch: output has {len(df_out)}, requests.csv has {len(df_req)}")

    req_map = {row["request_id"]: row for _, row in df_req.iterrows()}
    df_opt = pd.read_csv(DATASET_DIR / "request_payment_options.csv")

    # Validate each row
    for idx, row in df_out.iterrows():
        req_id = str(row["request_id"]).strip()
        req = req_map.get(req_id)
        if req is None:
            errors.append(f"Row {idx}: unknown request_id '{req_id}'")
            continue

        req_amount = float(req["requested_amount"])
        req_date = str(req["request_date"]).strip()

        # 1. amount_safe_to_pay bounds: 0 <= amount_safe_to_pay <= requested_amount
        try:
            safe = float(row["amount_safe_to_pay"])
            if safe < -1e-4 or safe > (req_amount + 1e-4):
                errors.append(f"Row {req_id}: safe amount {safe} outside [0, {req_amount}]")
        except Exception:
            errors.append(f"Row {req_id}: invalid amount_safe_to_pay '{row['amount_safe_to_pay']}'")

        # 2. affordability_status enum
        status = str(row["affordability_status"]).strip()
        if status not in AFFORDABILITY_STATUSES:
            errors.append(f"Row {req_id}: invalid status '{status}'")

        # 3. recommended_payment_method enum
        method = str(row["recommended_payment_method"]).strip()
        if method not in RECOMMENDED_PAYMENT_METHODS:
            errors.append(f"Row {req_id}: invalid method '{method}'")

        # 4. affordable_now invariant
        earliest = str(row["earliest_date_for_full_payment"]).strip() if pd.notna(row["earliest_date_for_full_payment"]) else ""
        if earliest.lower() == "nan":
            earliest = ""
        if status == "affordable_now" and earliest != req_date:
            errors.append(f"Row {req_id}: status is affordable_now but earliest_date '{earliest}' != req_date '{req_date}'")

        # 5. payment_plan format
        plan = str(row["payment_plan"]).strip()
        if plan != "none":
            items = plan.split("|")
            prev_dt = None
            for it in items:
                parts = it.split(":")
                if len(parts) != 2:
                    errors.append(f"Row {req_id}: invalid plan item format '{it}'")
                    break
                try:
                    p_dt = date.fromisoformat(parts[0])
                    p_amt = float(parts[1])
                    if prev_dt and p_dt < prev_dt:
                        errors.append(f"Row {req_id}: plan is not chronological: {p_dt} < {prev_dt}")
                    prev_dt = p_dt
                except Exception as e:
                    errors.append(f"Row {req_id}: failed parsing plan item '{it}': {e}")

        # 6. partial_payment invariant
        if method == "partial_payment":
            if status != "affordable_with_plan":
                errors.append(f"Row {req_id}: partial_payment must have affordable_with_plan status, got '{status}'")
            items = plan.split("|")
            if len(items) != 2:
                errors.append(f"Row {req_id}: partial_payment must have exactly 2 payments, got {len(items)}")
            else:
                try:
                    amt1 = float(items[0].split(":")[1])
                    amt2 = float(items[1].split(":")[1])
                    if abs((amt1 + amt2) - req_amount) > 0.5:
                        errors.append(f"Row {req_id}: partial payments sum {amt1 + amt2} != requested {req_amount}")
                except Exception:
                    pass

        # 6b. installments must reproduce a supplied payment option exactly
        if method == "installments":
            opts = df_opt[(df_opt.request_id == req_id) & (df_opt.payment_method == "installments")]
            schedules = set()
            for _, o in opts.iterrows():
                step = int(o.payment_frequency_days) if pd.notna(o.payment_frequency_days) else 30
                start = date.fromisoformat(str(o.first_payment_date))
                schedules.add("|".join(f"{(start + timedelta(days=step * i)).isoformat()}:{fmt_amount(o.payment_amount)}"
                                       for i in range(int(o.number_of_payments))))
            if plan not in schedules:
                errors.append(f"Row {req_id}: installment plan does not match any supplied payment option")

        # 7. spending_changes_needed
        changes = str(row["spending_changes_needed"]).strip()
        if changes != "none":
            ch_items = changes.split("|")
            if len(ch_items) > 3:
                errors.append(f"Row {req_id}: more than 3 spending changes ({len(ch_items)})")
            seen_events = set()
            for ch in ch_items:
                ch_parts = ch.split(":")
                if ch_parts[0] not in ("stop", "reduce_to"):
                    errors.append(f"Row {req_id}: invalid change prefix '{ch}'")
                ev_id = ch_parts[1] if len(ch_parts) > 1 else ""
                if ev_id in seen_events:
                    errors.append(f"Row {req_id}: event {ev_id} targeted multiple times")
                seen_events.add(ev_id)

        # 8. decision_explanation
        expl = str(row["decision_explanation"]).strip()
        if not expl or expl.lower() == "nan":
            errors.append(f"Row {req_id}: empty decision_explanation")

    is_valid = len(errors) == 0
    return is_valid, errors

if __name__ == "__main__":
    valid, errs = validate_output_csv()
    if valid:
        print("output.csv PASSED all validation checks! 100% compliant.")
    else:
        print(f"output.csv FAILED validation with {len(errs)} errors:")
        for e in errs[:20]:
            print(f"  - {e}")
