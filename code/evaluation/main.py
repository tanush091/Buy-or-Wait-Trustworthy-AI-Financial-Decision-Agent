"""Evaluation workflow: score the engine on the 25 solved public samples.

    python code/evaluation/main.py            # summary + mismatches
    python code/evaluation/main.py --all      # print every mismatch
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from code.config import EVALUATION_DIR
from code.evaluation.score_samples import compare_decisions, print_score_report
from code.loaders import load_sample_requests
from code.main import Engine


def evaluate_sample_benchmark(show_all: bool = False):
    engine = Engine()
    samples = load_sample_requests()
    predicted = [engine.decide(req) for req, _ in samples]
    truth = [gt for _, gt in samples]
    metrics = compare_decisions(predicted, truth)
    print_score_report(metrics, limit=None if show_all else 10)
    lines = ["# Sample scores", "", f"Exact rows: {metrics['perfect_rows']}/{metrics['total']}", ""]
    for k in ("amount_safe_to_pay_pct", "affordability_status_pct", "recommended_payment_method_pct",
              "payment_plan_pct", "earliest_date_pct", "spending_changes_pct"):
        lines.append(f"- {k}: {metrics[k]:.1f}%")
    (EVALUATION_DIR / "sample_scores.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return metrics


if __name__ == "__main__":
    evaluate_sample_benchmark("--all" in sys.argv)
