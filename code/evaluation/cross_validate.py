"""Honest estimate of accuracy on unseen requests.

Six forecast switches were chosen on the 25 public samples (pooled window, pooled estimator,
variable-bill estimator, typical amount from minimum_allowed_amount, same-day ordering,
request-day bills). Scoring on those same samples is optimistic, so this script repeats the
WHOLE selection inside cross-validation: for each fold it picks the best of all 160
combinations using the training samples only, then scores the held-out samples.
It reports 5-fold and leave-one-out results and how often the shipped setting wins.

    python code/evaluation/cross_validate.py
"""
import itertools
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from code.config import CONFIG, EVALUATION_DIR
from code.evaluation.score_samples import compare_decisions
from code.loaders import load_sample_requests
from code.main import Engine

FIELDS = ["affordability_status_pct", "recommended_payment_method_pct", "payment_plan_pct",
          "earliest_date_pct", "spending_changes_pct", "amount_safe_to_pay_pct"]
KEYS = ["pooled_window", "var_estimator", "bill_estimator", "base_from_minimum", "income_first", "day0"]
GRID = [dict(zip(KEYS, v)) for v in itertools.product(
    [None, 30, 45, 60, 75], ["mean", "median"], ["median", "last"], [True, False], [True, False], [True, False])]


def categorical(m):
    return sum(m[f] for f in FIELDS[:5]) / 5


def _cv(table, truth, k, shipped_idx):
    """Neutral selection (first best in grid order); also counts folds where the shipped setting ties for best."""
    n = len(truth)
    held_pred, held_truth, tied = [], [], 0
    for f in range(k):
        test = [i for i in range(n) if i % k == f]
        train = [i for i in range(n) if i % k != f]
        scores = [categorical(compare_decisions([p[i] for i in train], [truth[i] for i in train])) for p in table]
        best = max(range(len(GRID)), key=lambda j: scores[j])
        tied += scores[shipped_idx] >= scores[best] - 1e-9
        held_pred += [table[best][i] for i in test]
        held_truth += [truth[i] for i in test]
    return compare_decisions(held_pred, held_truth), tied


def main():
    saved = dict(CONFIG)
    shipped = {k: saved[k] for k in KEYS}
    engine = Engine()
    samples = load_sample_requests()
    truth = [g for _, g in samples]
    table = []
    for cfg in GRID:
        CONFIG.clear()
        CONFIG.update(saved, **cfg)
        table.append([engine.decide(r) for r, _ in samples])
    CONFIG.clear()
    CONFIG.update(saved)
    shipped_idx = GRID.index(shipped)
    in_sample = compare_decisions(table[shipped_idx], truth)
    print(f"Tuned switches: {KEYS} ({len(GRID)} combinations); shipped: {shipped}")
    print("In-sample (all 25, optimistic):   " + "  ".join(f"{in_sample[f]:.0f}%" for f in FIELDS))
    results = {"shipped": shipped, "in_sample": {f: in_sample[f] for f in FIELDS}}
    for label, k in (("5-fold", 5), ("leave-one-out", len(truth))):
        m, tied = _cv(table, truth, k, shipped_idx)
        results[label] = {**{f: m[f] for f in FIELDS}, "folds": k, "shipped_tied_for_best": tied}
        print(f"{label:14s} held-out:        " + "  ".join(f"{m[f]:.0f}%" for f in FIELDS)
              + f"   | shipped setting tied for best on the training folds in {tied}/{k}")
    (EVALUATION_DIR / "cv_scores.json").write_text(json.dumps(results, indent=1), encoding="utf-8")  # for the dashboard
    ties = sum(abs(categorical(compare_decisions(p, truth)) - categorical(in_sample)) < 1e-9 for p in table)
    print(f"{ties} of {len(GRID)} combinations tie with the shipped setting on all 25 samples "
          f"(many switches change only the safe amount, not the decision fields)")
    print("Columns: status, method, plan, earliest date, spending changes, exact safe amount")


if __name__ == "__main__":
    main()
