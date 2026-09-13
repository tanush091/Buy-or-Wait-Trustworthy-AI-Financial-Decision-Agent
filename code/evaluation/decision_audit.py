"""Decision audit (JSON) and risk flags (CSV) for every request, from the same engine as output.csv.

    python code/evaluation/decision_audit.py                    # dataset/requests.csv
    python code/evaluation/decision_audit.py --requests path    # another requests file

Writes code/evaluation/decision_audit.json and code/evaluation/risk_flags.csv, then cross-checks
every audited decision against output.csv and reports any disagreement.
"""
import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd

from code.audit import RISK_COLUMNS, RISK_LEVELS, audit_request, risk_row
from code.config import EVALUATION_DIR, OUTPUT_PATH
from code.loaders import load_requests
from code.main import Engine
from code.security import sanitize

DECISION_FIELDS = ["affordability_status", "recommended_payment_method", "payment_plan",
                   "earliest_date_for_full_payment", "spending_changes_needed"]


def build(requests_path: Optional[Path] = None, out_dir: Path = EVALUATION_DIR) -> List[dict]:
    engine = Engine()
    requests = load_requests(requests_path)
    try:
        engine.prefetch(requests)
    except Exception as exc:  # the reading layer never costs the run; rules + cache carry on
        print(f"  ! LLM prefetch failed ({type(exc).__name__}); continuing with rules + cache")
    audits = [audit_request(engine, r) for r in requests]
    (out_dir / "decision_audit.json").write_text(sanitize(json.dumps(audits, indent=1, ensure_ascii=False)),
                                                 encoding="utf-8")
    with open(out_dir / "risk_flags.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RISK_COLUMNS)
        writer.writeheader()
        writer.writerows(risk_row(a) for a in audits)
    return audits


def mismatches(audits: List[dict], output_path: Path = OUTPUT_PATH) -> Optional[List[str]]:
    """Request ids whose audited decision differs from output.csv (None when output.csv is absent)."""
    if not output_path.exists():
        return None
    rows = pd.read_csv(output_path, dtype=str, keep_default_na=False).set_index("request_id")
    bad = []
    for a in audits:
        rid, d = a["request_id"], a["decision"]
        if rid not in rows.index:
            bad.append(rid)
            continue
        row = rows.loc[rid]
        if (any(str(d[f]) != row[f] for f in DECISION_FIELDS)
                or abs(float(row["amount_safe_to_pay"]) - d["amount_safe_to_pay"]) > 0.005):
            bad.append(rid)
    return bad


def summarise(audits: List[dict], requests_path: Optional[Path] = None):
    risk = Counter(risk_row(a)["risk_level"] for a in audits)
    print(f"Audited {len(audits)} decisions -> {EVALUATION_DIR / 'decision_audit.json'} and risk_flags.csv")
    print("Risk levels: " + ", ".join(f"{k} {risk.get(k, 0)}" for k in RISK_LEVELS))
    same = sum(a["consistent"] for a in audits)
    print(f"Audit re-derives the engine's decision: {same}/{len(audits)}")
    if requests_path is None:
        bad = mismatches(audits)
        if bad is not None:
            print(f"Audited decisions matching output.csv: {len(audits) - len(bad)}/{len(audits)}"
                  + (f" (differ: {bad[:5]})" if bad else ""))


def main(argv=None) -> List[dict]:
    ap = argparse.ArgumentParser(description="Write decision_audit.json and risk_flags.csv.")
    ap.add_argument("--requests", type=Path, default=None)
    args = ap.parse_args(argv)
    audits = build(args.requests)
    summarise(audits, args.requests)
    return audits


if __name__ == "__main__":
    main()
