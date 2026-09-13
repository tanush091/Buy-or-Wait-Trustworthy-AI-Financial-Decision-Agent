"""Tests for the decision-insight features: audit, risk flags, what-if and dashboard.

    python -m unittest discover -s code/tests -v        (from the repository root)
"""
import os
import pathlib
import re
import sys
import tempfile
import unittest
from datetime import timedelta

os.environ.setdefault("BUY_OR_WAIT_LLM", "0")
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import json

from code.audit import RISK_LEVELS, audit_request, risk_row
from code.evaluation import decision_audit
from code.evaluation.dashboard import render
from code.loaders import load_requests
from code.main import Engine
from code.whatif import scenario

ENGINE = Engine()
REQUESTS = load_requests()
SUBSET = REQUESTS[::7]


class Audit(unittest.TestCase):
    def test_audit_re_derives_the_engine_decision(self):
        for req in SUBSET:
            a = audit_request(ENGINE, req)
            d = ENGINE.decide(req)
            self.assertTrue(a["consistent"], req.request_id)
            self.assertEqual(a["decision"]["payment_plan"], d.payment_plan, req.request_id)
            json.dumps(a)  # fully serialisable

    def test_recommended_plan_is_listed_once_keeps_the_minimum(self):
        for req in SUBSET:
            a = audit_request(ENGINE, req)
            if a["fallback"] or a["decision"]["recommended_payment_method"] == "not_recommended":
                self.assertEqual(risk_row(a)["risk_level"], "blocked")
                self.assertFalse(any(o["chosen"] for o in a["options"]), req.request_id)
                continue
            chosen = [o for o in a["options"] if o["chosen"]]
            self.assertEqual(len(chosen), 1, req.request_id)
            self.assertEqual(chosen[0]["plan"], a["decision"]["payment_plan"], req.request_id)
            self.assertGreaterEqual(a["forecast"]["margin_above_minimum"], -0.01, req.request_id)

    def test_every_other_option_has_a_reason(self):
        for req in SUBSET:
            for o in audit_request(ENGINE, req)["options"]:
                self.assertTrue(o["reason"], (req.request_id, o))
                if not o["eligible"]:
                    self.assertIsNone(o["safe"])

    def test_risk_levels_are_known(self):
        for req in SUBSET:
            self.assertIn(risk_row(audit_request(ENGINE, req))["risk_level"], RISK_LEVELS)


class WhatIf(unittest.TestCase):
    def test_no_change_equals_baseline(self):
        for req in SUBSET[:12]:
            base, alt, notes = scenario(ENGINE, req)
            self.assertEqual(base["decision"], alt["decision"], req.request_id)
            self.assertEqual(notes, [])

    def test_more_balance_never_lowers_the_safe_amount_and_engine_is_restored(self):
        for req in SUBSET[:12]:
            prof = ENGINE.profiles[req.user_id]
            base, alt, _ = scenario(ENGINE, req, balance="+1000")
            self.assertIs(ENGINE.profiles[req.user_id], prof)
            self.assertGreaterEqual(alt["decision"]["amount_safe_to_pay"] + 1e-6,
                                    base["decision"]["amount_safe_to_pay"], req.request_id)

    def test_price_change_scales_options_only_temporarily(self):
        req = next(r for r in REQUESTS
                   if any(o.payment_method == "installments" for o in ENGINE.options.get(r.request_id, [])))
        opts = ENGINE.options[req.request_id]
        _, alt, notes = scenario(ENGINE, req, amount=str(req.requested_amount * 2))
        self.assertIs(ENGINE.options[req.request_id], opts)
        self.assertAlmostEqual(alt["requested_amount"], round(req.requested_amount * 2, 2))
        self.assertIn("installment options scaled to the new price", notes)

    def test_invalid_scenarios_are_rejected(self):
        req = REQUESTS[0]
        with self.assertRaises(ValueError):
            scenario(ENGINE, req, deadline=req.request_date - timedelta(days=1))
        with self.assertRaises(ValueError):
            scenario(ENGINE, req, amount="0")
        with self.assertRaises(ValueError):
            scenario(ENGINE, req, methods=["bitcoin"])


class Artifacts(unittest.TestCase):
    def test_full_audit_matches_output_csv(self):
        out = REPO_ROOT / "output.csv"
        if not out.exists():
            self.skipTest("output.csv not generated yet")
        with tempfile.TemporaryDirectory() as tmp:
            audits = decision_audit.build(None, pathlib.Path(tmp))
            self.assertEqual(decision_audit.mismatches(audits, out), [])
            self.assertTrue(all(a["consistent"] for a in audits))
            self.assertTrue((pathlib.Path(tmp) / "risk_flags.csv").exists())

    def test_dashboard_is_self_contained(self):
        audits = [audit_request(ENGINE, r) for r in REQUESTS[:15]]
        html = render(audits, {"in_sample": {"affordability_status_pct": 88.0}})
        self.assertIn("<table", html)
        self.assertIn("88%", html)
        self.assertIsNone(re.search(r"<(?:script|link|img)[^>]+(?:src|href)=[\"']?https?:", html))


if __name__ == "__main__":
    unittest.main()
