"""Integrity tests: sample independence, metamorphic relations, differential simulation, secrets.

    python -m unittest discover -s code/tests -v        (from the repository root)
"""
import dataclasses
import os
import pathlib
import re
import sys
import unittest

os.environ["BUY_OR_WAIT_LLM"] = "0"
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd

from code.config import CONFIG, DATASET_DIR
from code.loaders import load_requests
from code.main import Engine
from code.models import SpendingChange
from code.security import sanitize

ENGINE = Engine()
REQUESTS = load_requests()


class SampleIndependence(unittest.TestCase):
    def test_production_code_has_no_sample_ids_or_answers(self):
        s = pd.read_csv(DATASET_DIR / "sample_requests.csv")
        answers = {str(v) for v in s.amount_safe_to_pay if v > 0}
        answers |= {x.split(":")[1] for p in s.payment_plan if p != "none" for x in p.split("|")}
        hits = []
        for p in (REPO_ROOT / "code").rglob("*.py"):
            if "tests" in p.parts:
                continue
            text = p.read_text(encoding="utf-8")
            hits += [(p.name, rid) for rid in s.request_id if re.search(rf"\b{rid}\b", text)]
            hits += [(p.name, a) for a in answers if len(a.replace(".", "")) >= 4
                     and re.search(rf"(?<![\d.]){re.escape(a)}(?![\d.])", text)]
        self.assertEqual(hits, [])


class Metamorphic(unittest.TestCase):
    """Relations that must hold whatever the forecast: changing an input in a known way changes the
    output in a known way. They catch logic errors without needing any answer labels."""

    SUBSET = REQUESTS[::10]

    def test_more_balance_raises_safe_amount_by_exactly_that_much(self):
        extra = 1000.0
        for req in self.SUBSET:
            d0 = ENGINE.decide(req)
            prof = ENGINE.profiles[req.user_id]
            ENGINE.profiles[req.user_id] = dataclasses.replace(prof, available_balance=prof.available_balance + extra)
            try:
                d1 = ENGINE.decide(req)
            finally:
                ENGINE.profiles[req.user_id] = prof
            self.assertGreaterEqual(d1.amount_safe_to_pay + 1e-6, d0.amount_safe_to_pay, req.request_id)
            if 0 < d0.amount_safe_to_pay < req.requested_amount:
                self.assertAlmostEqual(d1.amount_safe_to_pay, min(req.requested_amount, d0.amount_safe_to_pay + extra),
                                       places=2, msg=req.request_id)

    def test_higher_minimum_never_raises_safe_amount(self):
        for req in self.SUBSET:
            d0 = ENGINE.decide(req)
            prof = ENGINE.profiles[req.user_id]
            ENGINE.profiles[req.user_id] = dataclasses.replace(prof, minimum_balance_to_keep=prof.minimum_balance_to_keep + 500)
            try:
                d1 = ENGINE.decide(req)
            finally:
                ENGINE.profiles[req.user_id] = prof
            self.assertLessEqual(d1.amount_safe_to_pay, d0.amount_safe_to_pay + 1e-6, req.request_id)

    def test_renaming_the_request_changes_nothing(self):
        for req in self.SUBSET:
            d0 = ENGINE.decide(req)
            twin = dataclasses.replace(req, request_id="request_999999")
            ENGINE.options[twin.request_id] = ENGINE.options.get(req.request_id, [])
            d1 = ENGINE.decide(twin)
            self.assertEqual(dataclasses.replace(d1, request_id=req.request_id), d0, req.request_id)

    def test_payment_option_order_does_not_matter(self):
        for req in self.SUBSET:
            d0 = ENGINE.decide(req)
            opts = ENGINE.options.get(req.request_id, [])
            ENGINE.options[req.request_id] = list(reversed(opts))
            try:
                d1 = ENGINE.decide(req)
            finally:
                ENGINE.options[req.request_id] = opts
            self.assertEqual(d1, d0, req.request_id)


def naive_min_slack(req, plan, changes):
    """Independent reference: sum each day's flows from the raw flow list (no ledger arrays),
    apply same-day income before the check, then the payment; return lowest balance - minimum."""
    prof, state, _ = ENGINE.state(req)
    horizon = CONFIG["horizon"]
    series = {s.last_event_id: s for s in state.series}
    changed = {}
    for ch in changes:
        s = series[ch.event_id]
        changed[s.key] = ch
    per_day = {}
    for f in state.flows:
        d = (f.day - req.request_date).days
        if not 0 <= d <= horizon:
            continue
        amount = f.amount
        ch = changed.get(f.series)
        if ch is not None and f.kind == "recurring":
            amount = 0.0 if ch.kind == "stop" else -min(-f.amount, ch.new_amount)
        per_day[d] = per_day.get(d, 0.0) + amount
    pays = {}
    for day, amt in plan:
        d = (day - req.request_date).days
        if 0 <= d <= horizon:
            pays[d] = pays.get(d, 0.0) + amt
    bal, low = prof.available_balance, float("inf")
    for d in range(horizon + 1):
        bal += per_day.get(d, 0.0)
        low = min(low, bal)
        if d in pays:
            bal -= pays[d]
            low = min(low, bal)
    return low - prof.minimum_balance_to_keep


class Differential(unittest.TestCase):
    """The ledger and an independent reference simulator must agree on every recommended plan."""

    def test_every_plan_is_safe_under_the_reference_simulator(self):
        self.assertTrue(CONFIG["income_first"], "reference simulator assumes income-first ordering")
        from datetime import date
        for req in REQUESTS:
            d = ENGINE.decide(req)
            if d.payment_plan == "none":
                continue
            plan = [(date.fromisoformat(p.split(":")[0]), float(p.split(":")[1])) for p in d.payment_plan.split("|")]
            changes = [] if d.spending_changes_needed == "none" else [
                SpendingChange(c.split(":")[0], c.split(":")[1], float(c.split(":")[2]) if c.startswith("reduce_to") else None)
                for c in d.spending_changes_needed.split("|")]
            self.assertGreaterEqual(naive_min_slack(req, plan, changes), -0.01, req.request_id)

    def test_safe_amount_is_the_exact_boundary(self):
        for req in REQUESTS:
            d = ENGINE.decide(req)
            if not 0 < d.amount_safe_to_pay < req.requested_amount:
                continue
            today = req.request_date
            self.assertGreaterEqual(naive_min_slack(req, [(today, d.amount_safe_to_pay)], []), -0.01, req.request_id)
            self.assertLess(naive_min_slack(req, [(today, d.amount_safe_to_pay + 0.05)], []), 0, req.request_id)


class Secrets(unittest.TestCase):
    def test_keys_are_redacted(self):
        # clearly fake values (never derived from a real key)
        text = "key AQ.FAKEtokenForTestsOnly0123456789 and AIzaFAKEkeyForTestsOnly0123456789 and api_key=abc123"
        out = sanitize(text)
        self.assertNotIn("FAKEtokenForTestsOnly", out)
        self.assertNotIn("FAKEkeyForTestsOnly", out)
        self.assertNotIn("abc123", out)

    def test_generated_reports_contain_no_key(self):
        for name in ("usage_report.md", "injection_log.md", "model_comparison.md"):
            p = REPO_ROOT / "code" / "evaluation" / name
            if p.exists():
                self.assertIsNone(re.search(r"AQ\.[0-9A-Za-z_\-.]{20,}|AIza[0-9A-Za-z_\-]{20,}", p.read_text(encoding="utf-8")), name)


if __name__ == "__main__":
    unittest.main()
