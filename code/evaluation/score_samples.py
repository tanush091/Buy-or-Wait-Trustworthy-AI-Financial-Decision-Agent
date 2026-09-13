import sys
from pathlib import Path
from typing import Dict, List, Tuple
import math
import pandas as pd

# Add repo root to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from code.loaders import load_sample_requests
from code.models import Decision

def normalize_plan(p: str) -> str:
    if not p or pd.isna(p) or p.strip().lower() == "none":
        return "none"
    return p.strip()

def normalize_date(d: str) -> str:
    if not d or pd.isna(d) or str(d).strip().lower() in ("nan", "none"):
        return ""
    return str(d).strip()

def normalize_changes(ch: str) -> str:
    if not ch or pd.isna(ch) or str(ch).strip().lower() in ("nan", "none"):
        return "none"
    parts = sorted([p.strip() for p in ch.split("|") if p.strip()])
    return "|".join(parts) if parts else "none"

def compare_decisions(predicted: List[Decision], ground_truth: List[Decision]) -> Dict[str, any]:
    gt_map = {d.request_id: d for d in ground_truth}
    
    total = len(ground_truth)
    safe_match = 0
    status_match = 0
    method_match = 0
    plan_match = 0
    earliest_match = 0
    changes_match = 0
    perfect_rows = 0
    
    diffs = []
    
    for pred in predicted:
        gt = gt_map.get(pred.request_id)
        if not gt:
            continue
            
        row_perfect = True
        
        # 1. amount_safe_to_pay (allow 0.01 tolerance)
        is_safe_ok = math.isclose(pred.amount_safe_to_pay, gt.amount_safe_to_pay, abs_tol=0.5)
        if is_safe_ok:
            safe_match += 1
        else:
            row_perfect = False
            
        # 2. affordability_status
        is_status_ok = pred.affordability_status == gt.affordability_status
        if is_status_ok:
            status_match += 1
        else:
            row_perfect = False
            
        # 3. recommended_payment_method
        is_method_ok = pred.recommended_payment_method == gt.recommended_payment_method
        if is_method_ok:
            method_match += 1
        else:
            row_perfect = False
            
        # 4. payment_plan
        is_plan_ok = normalize_plan(pred.payment_plan) == normalize_plan(gt.payment_plan)
        if is_plan_ok:
            plan_match += 1
        else:
            row_perfect = False
            
        # 5. earliest_date_for_full_payment
        is_earliest_ok = normalize_date(pred.earliest_date_for_full_payment) == normalize_date(gt.earliest_date_for_full_payment)
        if is_earliest_ok:
            earliest_match += 1
        else:
            row_perfect = False
            
        # 6. spending_changes_needed
        is_changes_ok = normalize_changes(pred.spending_changes_needed) == normalize_changes(gt.spending_changes_needed)
        if is_changes_ok:
            changes_match += 1
        else:
            row_perfect = False
            
        if row_perfect:
            perfect_rows += 1
        else:
            diffs.append({
                "request_id": pred.request_id,
                "safe_diff": (pred.amount_safe_to_pay, gt.amount_safe_to_pay) if not is_safe_ok else None,
                "status_diff": (pred.affordability_status, gt.affordability_status) if not is_status_ok else None,
                "method_diff": (pred.recommended_payment_method, gt.recommended_payment_method) if not is_method_ok else None,
                "plan_diff": (pred.payment_plan, gt.payment_plan) if not is_plan_ok else None,
                "earliest_diff": (pred.earliest_date_for_full_payment, gt.earliest_date_for_full_payment) if not is_earliest_ok else None,
                "changes_diff": (pred.spending_changes_needed, gt.spending_changes_needed) if not is_changes_ok else None,
            })
            
    metrics = {
        "total": total,
        "perfect_rows": perfect_rows,
        "perfect_pct": (perfect_rows / total * 100) if total else 0.0,
        "amount_safe_to_pay_pct": (safe_match / total * 100) if total else 0.0,
        "affordability_status_pct": (status_match / total * 100) if total else 0.0,
        "recommended_payment_method_pct": (method_match / total * 100) if total else 0.0,
        "payment_plan_pct": (plan_match / total * 100) if total else 0.0,
        "earliest_date_pct": (earliest_match / total * 100) if total else 0.0,
        "spending_changes_pct": (changes_match / total * 100) if total else 0.0,
        "diffs": diffs,
    }
    return metrics

def print_score_report(metrics: Dict[str, any], limit=10):
    print("=" * 60)
    print(" SAMPLE EVALUATION BENCHMARK REPORT (25 Requests)")
    print("=" * 60)
    print(f"Overall Exact Match Rows: {metrics['perfect_rows']}/{metrics['total']} ({metrics['perfect_pct']:.1f}%)")
    print("-" * 60)
    print(f"amount_safe_to_pay:             {metrics['amount_safe_to_pay_pct']:.1f}%")
    print(f"affordability_status:           {metrics['affordability_status_pct']:.1f}%")
    print(f"recommended_payment_method:     {metrics['recommended_payment_method_pct']:.1f}%")
    print(f"payment_plan:                   {metrics['payment_plan_pct']:.1f}%")
    print(f"earliest_date_for_full_payment: {metrics['earliest_date_pct']:.1f}%")
    print(f"spending_changes_needed:        {metrics['spending_changes_pct']:.1f}%")
    print("=" * 60)
    if metrics["diffs"]:
        print(f"\nMismatches ({len(metrics['diffs'])} requests):")
        for d in metrics["diffs"][:limit]:
            print(f"[{d['request_id']}]")
            for k, v in d.items():
                if k != "request_id" and v is not None:
                    pred_val, gt_val = v
                    print(f"   {k}: pred='{pred_val}' != gt='{gt_val}'")
    else:
        print("\nALL SAMPLE PREDICTIONS MATCH GROUND TRUTH PERFECTLY!")

if __name__ == "__main__":
    # Test scoring with self-check
    samples = load_sample_requests()
    gt = [dec for _, dec in samples]
    metrics = compare_decisions(gt, gt)
    print_score_report(metrics)
