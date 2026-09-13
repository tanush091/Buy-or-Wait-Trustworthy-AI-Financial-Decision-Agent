"""Buy or Wait? financial decision agent.

Usage:
    python code/main.py                       # dataset/requests.csv -> output.csv
    python code/main.py --requests dataset/sample_requests.csv --output sample_output.csv
"""
import argparse
import dataclasses
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from code import llm, nli
from code.amendments import SUSPICIOUS, parse_messages, rule_matches
from code.config import CONFIG, EVALUATION_DIR, LLM_FREE_TIER, LLM_PRICES, OUTPUT_PATH
from code.explain import generate_explanation
from code.extractors import get_image_amount
from code.loaders import (load_exchange_rates, load_financial_events, load_images, load_messages,
                          load_payment_options, load_profiles, load_requests)
from code.metrics import amount_safe_to_pay, earliest_date_for_full_payment
from code.models import Decision, Request
from code.ranker import choose_plan, derive_affordability_status, format_changes_string, format_plan_string
from code.resolver import FX, build_user_state
from code.schema_check import validate_output_csv
from code.security import sanitize
from code.timeline import Ledger
from code.validator import plan_payments
from code.writer import write_decisions_csv


class Engine:
    """Loads the dataset once and decides requests deterministically."""

    def __init__(self):
        self.profiles = load_profiles()
        self.events = defaultdict(list)
        for e in load_financial_events():
            self.events[e.user_id].append(e)
        self.options = load_payment_options()
        self.fx = FX(load_exchange_rates())
        self.messages = load_messages()
        self.image_by_event = {i.related_event_id: i.image_id for i in load_images() if i.related_event_id}
        self.trust_log: List[str] = []
        self.llm_facts: Dict[str, dict] = {}  # LLM readings, filled lazily per request

    def image_amount(self, event_id: str) -> Optional[float]:
        image_id = self.image_by_event.get(event_id)
        return get_image_amount(image_id) if image_id else None

    def _unrecognised(self, msgs) -> list:
        """Messages the rule parser does not recognise (scam text never goes to a model)."""
        return [m for m in msgs if m.message_id not in self.llm_facts
                and not rule_matches(m.message_text) and not SUSPICIOUS.search(m.message_text or "")]

    def prefetch(self, requests: List[Request]):
        """Full runs: read all unrecognised messages of these requests in a few batched calls."""
        users = {r.user_id for r in requests}
        ids = {r.request_id for r in requests}
        msgs = [m for m in self.messages if m.user_id in users or m.request_id in ids]
        self.llm_facts.update(llm.classify_messages(self._unrecognised(msgs)))

    def state(self, req: Request):
        prof = self.profiles[req.user_id]
        msgs = [m for m in self.messages if m.user_id == req.user_id or m.request_id == req.request_id]
        unknown = self._unrecognised(msgs)
        if unknown:  # lazy: only this request's messages; cached answers cost nothing
            self.llm_facts.update(llm.classify_messages(unknown))
        amendments, log = parse_messages(msgs, req.request_date, self.llm_facts)
        self.trust_log.extend(f"{req.request_id} {line}" for line in log)
        state = build_user_state(req, prof, self.events[req.user_id], amendments, self.fx,
                                 self.image_amount, CONFIG["horizon"])
        ledger = Ledger(prof.available_balance, prof.minimum_balance_to_keep, req.request_date,
                        state.flows, state.series, CONFIG["horizon"])
        return prof, state, ledger

    def decide(self, req: Request) -> Decision:
        """Every entry point (batch, trace, tests, evaluation) gets the same safe fallback."""
        try:
            return self._decide(req)
        except (ValueError, KeyError) as exc:
            # unverifiable data, e.g. an expense in a currency with no exchange-rate path: never guess,
            # never drop it (that would overstate the balance) -> the financially safest answer
            reason = str(exc).strip("'\"")
            self.trust_log.append(f"{req.request_id} safe fallback: {reason}")
            return fallback_decision(req, self, reason)

    def _decide(self, req: Request) -> Decision:
        prof, state, ledger = self.state(req)
        safe = amount_safe_to_pay(ledger, req.requested_amount)
        earliest = earliest_date_for_full_payment(ledger, req.requested_amount)
        best = choose_plan(req, prof, self.options.get(req.request_id, []), ledger, safe, earliest, state.series)
        status = derive_affordability_status(best, req.request_date)
        if status == "affordable_now":
            earliest = req.request_date
        lowest = ledger.lowest(plan_payments(ledger, best.plan) if best.plan else {}, best.changes)
        return Decision(
            request_id=req.request_id,
            amount_safe_to_pay=safe,
            affordability_status=status,
            recommended_payment_method=best.method,
            payment_plan=format_plan_string(best.plan),
            earliest_date_for_full_payment=earliest.isoformat() if earliest else "",
            spending_changes_needed=format_changes_string(best.changes),
            decision_explanation=generate_explanation(req, prof, best, status, safe, earliest, state.series, lowest),
        )


def fallback_decision(req: Request, engine: Engine, reason: str = "") -> Decision:
    """Financially safest answer when a request's data cannot be verified."""
    prof = engine.profiles.get(req.user_id)
    cur = prof.home_currency if prof else ""
    why = f" ({reason})" if reason else ""
    return Decision(
        request_id=req.request_id,
        amount_safe_to_pay=0.0,
        affordability_status="not_affordable",
        recommended_payment_method="not_recommended",
        payment_plan="none",
        earliest_date_for_full_payment="",
        spending_changes_needed="none",
        decision_explanation=(f"Do not proceed with the {cur} {req.requested_amount:,.2f} request. "
                              f"The financial records needed to confirm it is safe could not be verified{why}.").replace("  ", " "),
    )


def _numbers(text: str):
    return {x.replace(",", "") for x in re.findall(r"\d[\d,]*(?:\.\d+)?", text or "")}


def polish_explanations(decisions: List[Decision]) -> List[Decision]:
    """Optional (BUY_OR_WAIT_LLM_EXPLAIN=1): LLM rewrites the template explanation for readability.
    A rewrite is used only if it introduces no number that is not in the template draft."""
    drafts = {d.request_id: d.decision_explanation for d in decisions}
    rewrites = llm.polish_explanations(drafts)
    out = []
    for d in decisions:
        text = rewrites.get(d.request_id, "")
        ok = text and len(text) <= 320 and _numbers(text) <= _numbers(d.decision_explanation)
        out.append(dataclasses.replace(d, decision_explanation=text) if ok else d)
    return out


def write_trust_log(lines: List[str]):
    path = EVALUATION_DIR / "injection_log.md"
    body = ["# Message trust-gate log", "",
            "Messages are untrusted. Rejected or ignored messages from the last run:", ""]
    body += [f"- {line}" for line in lines] or ["- none"]
    path.write_text(sanitize("\n".join(body) + "\n"), encoding="utf-8")


def _cost(model: str, tin: int, tout: int) -> Optional[float]:
    price = LLM_PRICES.get(model)
    return None if price is None else tin / 1e6 * price[0] + tout / 1e6 * price[1]


def write_usage_report(n_requests: int):
    U = llm.USAGE
    tin, tout = U["input_tokens"], U["output_tokens"]
    cin, cout = U["cached_input_tokens"], U["cached_output_tokens"]
    total = tin + tout
    costs = {m: _cost(m, v["input_tokens"], v["output_tokens"]) for m, v in U["by_model"].items()}
    rows = "\n".join(
        f"| Google AI Studio | {m} | {v['calls']} | {v['input_tokens']} | {v['output_tokens']} | "
        f"{v['input_tokens'] + v['output_tokens']} | {'n/a' if costs[m] is None else f'{costs[m]:.4f}'} |"
        for m, v in U["by_model"].items()) or f"| Google AI Studio | {llm.LLM_MODEL} | 0 | 0 | 0 | 0 | 0.0000 |"
    known = [c for c in costs.values() if c is not None]
    cost_total = sum(known)
    cost_note = "" if len(known) == len(costs) else " (list price not configured for every model)"
    status = "available" if not U["unavailable"] else f"unavailable: {U['unavailable']} (rules + cache used)"
    if not llm.api_key():
        status = "not configured (no key): rules + cache only, no network"
    elif not llm.enabled():
        status = "disabled (BUY_OR_WAIT_LLM=0): rules + cache only, no network"
    (EVALUATION_DIR / "usage_report.md").write_text(sanitize(f"""# Token usage report (final full-dataset run)

Requests processed: {n_requests}

**Where models are used.** The decision engine (forecast, plan generation, 90-day safety
check, ranking, explanations) is deterministic Python and makes no model calls. A single
provider, Gemini via Google AI Studio (`code/llm.py`), only *reads* unstructured inputs:

- **Messages:** the rule parser handles known wording. Messages it does not recognise are
  sent to Gemini (batched, up to 25 per call) and come back as typed facts that must pass the
  trust gate: sender allow-list, date sanity, every number must appear in the text, and new
  income only when the message says it is confirmed.
- **Images:** each image file is read and fingerprinted (SHA-256); the hand-verified amount is
  reused when the file is unchanged, otherwise Gemini vision reads it.
- **Cache:** every Gemini answer is cached per message / per image content, so re-running the
  provided dataset makes no calls and needs no key or network.

| Provider | Model | Calls this run | Input tokens | Output tokens | Total tokens | Est. cost at list price (USD) |
|---|---|---|---|---|---|---|
{rows}
| **All models** | | **{U['calls']}** | **{tin}** | **{tout}** | **{total}** | **{cost_total:.4f}{cost_note}** |

| Metric | Value |
|---|---|
| Model calls in this run | {U['calls']} |
| Messages sent to the LLM in this run | {U['messages_sent']} |
| LLM answers reused from cache | {U['cache_hits']} |
| Tokens originally spent producing the cached answers used (input / output) | {cin} / {cout} |
| LLM status during run | {status} |
| NLI fact verifier (local, 0 API tokens) | {nli.STATS['status']}; checks {nli.STATS['checks']}, rejected {nli.STATS['rejected']} |
| Time spent in LLM calls (s) | {U['llm_seconds']:.1f} |
| Image files read and fingerprinted | {U['images_read']} |
| Verified image cache hits | {U['image_cache_hits']} |
| Total tokens this run | {total} |
| Average tokens per request (this run) | {total / max(n_requests, 1):.2f} |
| Average tokens per request incl. original cost of cached answers | {(total + cin + cout) / max(n_requests, 1):.2f} |
| Billed cost (USD) | {'0.00 (Google AI Studio free tier)' if LLM_FREE_TIER else 'see estimate'} |
| Estimated cost per request at list price (USD) | {cost_total / max(n_requests, 1):.6f}{cost_note} |

List prices per 1M tokens are in `code/config.py` (`LLM_PRICES`). Model comparison
(rule parser vs Gemini models on all messages and images) is in `model_comparison.md`.

Image cache provenance: the 16 image amounts were first read during development (Claude,
multimodal, in the Claude Code session that built this solution) and checked by hand
against each image; Gemini 3.5 Flash independently matched 15 of 16.
"""), encoding="utf-8")


_WORKER_ENGINE: Optional[Engine] = None
# summed across workers; cache reuse is merged per cache entry instead (see _merge_usage)
_COUNTERS = ("calls", "input_tokens", "output_tokens", "messages_sent", "images_read",
             "image_cache_hits", "missing_images", "errors")


def _init_worker():
    global _WORKER_ENGINE
    _WORKER_ENGINE = Engine()


def _decide_batch(reqs: List[Request]):
    """Worker: decide one user-partitioned batch; return decisions, trust-gate lines and usage deltas."""
    eng = _WORKER_ENGINE
    out = []
    for r in reqs:
        try:
            out.append(eng.decide(r))
        except Exception as exc:
            print(f"  ! {r.request_id}: {type(exc).__name__}: {exc} -> not_recommended fallback")
            out.append(fallback_decision(r, eng))
    logs = list(eng.trust_log)
    eng.trust_log.clear()
    usage = {k: llm.USAGE[k] for k in _COUNTERS}
    usage.update(by_model=llm.USAGE["by_model"], llm_seconds=llm.USAGE["llm_seconds"],
                 unavailable=llm.USAGE["unavailable"], hit_keys=dict(llm.HIT_KEYS),
                 nli_checks=nli.STATS["checks"], nli_rejected=nli.STATS["rejected"], nli_status=nli.STATS["status"])
    for k in _COUNTERS:
        llm.USAGE[k] = 0
    llm.USAGE.update(by_model={}, llm_seconds=0.0)
    llm.HIT_KEYS.clear()
    nli.STATS.update(checks=0, rejected=0)
    return out, logs, usage


def _merge_usage(u: dict):
    """Fold one worker's usage into this process; a reused cache entry is counted once per run."""
    for k in _COUNTERS:
        llm.USAGE[k] += u[k]
    for m, v in u["by_model"].items():
        tgt = llm.USAGE["by_model"].setdefault(m, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        for f in tgt:
            tgt[f] += v[f]
    llm.USAGE["llm_seconds"] += u["llm_seconds"]
    llm.USAGE["unavailable"] = llm.USAGE["unavailable"] or u["unavailable"]
    for key, (tin, tout) in u["hit_keys"].items():
        llm.note_cache_hit(key, tin, tout)
    nli.STATS["checks"] += u["nli_checks"]
    nli.STATS["rejected"] += u["nli_rejected"]
    if nli.STATS["status"] == "not loaded":
        nli.STATS["status"] = u["nli_status"]


def _decide_parallel(requests: List[Request], workers: int):
    """Requests are independent per user: partition by user and decide on several processes."""
    from concurrent.futures import ProcessPoolExecutor
    by_user = defaultdict(list)
    for r in requests:
        by_user[r.user_id].append(r)
    users = sorted(by_user)
    n_batches = max(1, workers * 4)
    batches = [[r for u in users[i::n_batches] for r in by_user[u]] for i in range(n_batches)]
    decided, logs = {}, []
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker) as pool:
        for out, batch_logs, usage in pool.map(_decide_batch, [b for b in batches if b]):
            decided.update((d.request_id, d) for d in out)
            logs.extend(batch_logs)
            _merge_usage(usage)
    return [decided[r.request_id] for r in requests], logs  # original request order


def run_pipeline(requests_path: Optional[Path] = None, output_path: Path = OUTPUT_PATH,
                 workers: int = 1) -> List[Decision]:
    start = time.time()
    engine = Engine()
    requests = load_requests(requests_path)
    try:
        engine.prefetch(requests)  # batch the LLM readings for the whole run (cached for the workers)
    except Exception as exc:  # the reading layer must never cost the run; rules + cache carry on
        print(f"  ! LLM prefetch failed ({type(exc).__name__}); continuing with rules + cache")
    decisions = []
    if workers > 1:
        decisions, logs = _decide_parallel(requests, workers)
        engine.trust_log.extend(logs)
    for r in ([] if workers > 1 else requests):
        try:
            decisions.append(engine.decide(r))
        except Exception as exc:  # one bad record must not stop the run; fall back to the safe answer
            print(f"  ! {r.request_id}: {type(exc).__name__}: {exc} -> not_recommended fallback")
            decisions.append(fallback_decision(r, engine))
    if llm.enabled() and os.environ.get("BUY_OR_WAIT_LLM_EXPLAIN") == "1":
        decisions = polish_explanations(decisions)
    write_decisions_csv(decisions, output_path)
    write_trust_log(engine.trust_log)
    if requests_path is None:
        write_usage_report(len(requests))
    print(f"Evaluated {len(decisions)} requests in {time.time() - start:.2f}s -> {output_path}")
    print(f"LLM: {llm.USAGE['calls']} calls, {llm.USAGE['cache_hits']} cached answers, "
          f"status={'ok' if not llm.USAGE['unavailable'] else llm.USAGE['unavailable']}")
    ok, errors = validate_output_csv(output_path, requests_path)
    print("Schema validation: PASSED" if ok else f"Schema validation FAILED ({len(errors)} issues)")
    for err in errors[:10]:
        print("  -", err)
    return decisions


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests", type=Path, default=None)
    ap.add_argument("--output", type=Path, default=OUTPUT_PATH)
    ap.add_argument("--workers", type=int, default=1, help="processes; requests are partitioned by user")
    ap.add_argument("--insights", action="store_true",
                    help="also write decision_audit.json, risk_flags.csv and dashboard.html (code/evaluation/)")
    args = ap.parse_args()
    run_pipeline(args.requests, args.output, args.workers)
    if args.insights:  # additive: reads the same engine, never changes output.csv
        from code.evaluation import dashboard
        dashboard.main(["--requests", str(args.requests)] if args.requests else [])
