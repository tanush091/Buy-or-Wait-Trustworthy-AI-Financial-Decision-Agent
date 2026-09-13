"""Risk-adjusted safe amounts: pessimistic / expected / optimistic scenarios.

output.csv must keep one amount_safe_to_pay per request (the expected scenario). This report
adds a range for each request by re-running the same deterministic engine under a cautious
and a relaxed forecast of everyday spending, and measures on the solved samples how often
the true answer falls inside the range (calibration).

    python code/evaluation/safe_ranges.py
Writes code/evaluation/safe_amount_ranges.csv and code/evaluation/safe_ranges.md.
"""
import csv
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from code.config import CONFIG, EVALUATION_DIR
from code.loaders import load_requests, load_sample_requests
from code.main import Engine

SCENARIOS = {
    "pessimistic": dict(var_estimator="max", bill_estimator="max", pooled_window=None),
    "expected": {},  # the submitted configuration
    "optimistic": dict(var_estimator="median", bill_estimator="median", pooled_window=30),
}


def main():
    saved = dict(CONFIG)
    engine = Engine()
    samples = load_sample_requests()
    requests = load_requests() + [r for r, _ in samples]
    amounts = {}
    for name, overrides in SCENARIOS.items():
        CONFIG.clear()
        CONFIG.update(saved, **overrides)
        for r in requests:
            try:
                amounts.setdefault(r.request_id, {})[name] = engine.decide(r).amount_safe_to_pay
            except Exception:
                amounts.setdefault(r.request_id, {})[name] = 0.0
    CONFIG.clear()
    CONFIG.update(saved)

    with open(EVALUATION_DIR / "safe_amount_ranges.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["request_id", "safe_pessimistic", "safe_expected", "safe_optimistic"])
        for r in load_requests():
            a = amounts[r.request_id]
            w.writerow([r.request_id, a["pessimistic"], a["expected"], a["optimistic"]])

    inside = total = 0
    rows = []
    for r, g in samples:
        a = amounts[r.request_id]
        lo, hi = min(a.values()), max(a.values())
        ok = lo - 0.01 <= g.amount_safe_to_pay <= hi + 0.01
        inside += ok
        total += 1
        rows.append(f"| {r.request_id} | {lo:,.2f} | {a['expected']:,.2f} | {hi:,.2f} | {g.amount_safe_to_pay:,.2f} | {'yes' if ok else 'no'} |")
    report = ["# Safe-amount ranges (scenario-based)", "",
              f"True sample answer inside [pessimistic, optimistic]: **{inside}/{total}** "
              f"({100 * inside / total:.0f}%). The expected value is what output.csv submits.", "",
              "| Request | Pessimistic | Expected | Optimistic | True answer | Inside |", "|---|---|---|---|---|---|"] + rows
    (EVALUATION_DIR / "safe_ranges.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(report[2])


if __name__ == "__main__":
    main()
