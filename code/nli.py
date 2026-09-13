"""Natural Language Inference (NLI) check for LLM-extracted facts — an anti-hallucination layer.

A fact read by Gemini is accepted only if a local multilingual NLI model agrees that the source
message ENTAILS a plain-language statement of that fact (e.g. "The monthly salary will be
3,150 EUR starting 2025-09-01."). This catches readings whose numbers appear in the text but
whose meaning does not ("pay *might* rise if approved", "invoice *submitted* for approval").

The model runs locally (no API quota, deterministic in eval mode) and works across English and
Indonesian. If it cannot be loaded (no download, no torch), the layer reports itself unavailable
and the other trust-gate checks still apply. Disable with BUY_OR_WAIT_NLI=0.
"""
import os
from functools import lru_cache
from typing import Dict, Optional, Tuple

from .config import NLI_MIN_ENTAILMENT, NLI_MODEL

STATS = {"checks": 0, "rejected": 0, "status": "not loaded"}
_model = None


def _import_ml():
    """Import torch/transformers safely. This package is named `code` (the submission folder must be
    code/), which shadows the standard-library `code` module that torch -> pdb imports. Lend pdb the
    real stdlib module while torch loads, then restore this package."""
    import importlib.util
    import sys
    import sysconfig
    ours = sys.modules.get("code")
    std_path = os.path.join(sysconfig.get_paths()["stdlib"], "code.py")
    spec = importlib.util.spec_from_file_location("code", std_path)
    std = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(std)
    sys.modules["code"] = std
    try:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
    finally:
        if ours is not None:
            sys.modules["code"] = ours
    return torch, AutoModelForSequenceClassification, AutoTokenizer


def _load():
    global _model
    if _model is not None:
        return _model
    if os.environ.get("BUY_OR_WAIT_NLI", "1") == "0":
        STATS["status"] = "disabled"
        _model = False
        return _model
    try:
        torch, AutoModelForSequenceClassification, AutoTokenizer = _import_ml()
        tok = AutoTokenizer.from_pretrained(NLI_MODEL)
        model = AutoModelForSequenceClassification.from_pretrained(NLI_MODEL).eval()
        labels = {i: str(l).lower() for i, l in model.config.id2label.items()}
        _model = (tok, model, labels, torch)
        STATS["status"] = f"loaded {NLI_MODEL}"
    except Exception as e:  # offline sandbox, missing packages, ...
        STATS["status"] = f"unavailable ({type(e).__name__})"
        _model = False
    return _model


def available() -> bool:
    return bool(_load())


@lru_cache(maxsize=4096)
def entailment(premise: str, hypothesis: str) -> Optional[Dict[str, float]]:
    m = _load()
    if not m:
        return None
    tok, model, labels, torch = m
    with torch.no_grad():
        enc = tok(premise, hypothesis, return_tensors="pt", truncation=True, max_length=512)
        probs = torch.softmax(model(**enc).logits[0], dim=-1).tolist()
    return {labels[i]: p for i, p in enumerate(probs)}


def _num(x) -> str:
    x = float(x)
    return f"{int(round(x)):,}" if abs(x - round(x)) < 0.005 else f"{x:,.2f}"


HYPOTHESES = {
    "salary_raise": "The monthly salary will be {amount} {currency} starting {date}.",
    "salary_date": "The salary will be paid on {date}.",
    "salary_next": "The next salary will be {amount} {currency}.",
    "salary_arrears": "The next salary will be {amount} {currency} plus a one-time payment of {extra} {currency}.",
    "salary_resume": "A regular salary of {amount} {currency} resumes on {date}.",
    "salary_first": "A first salary of {amount} {currency} will be paid on {date}.",
    "salary_on_date": "A salary of {amount} {currency} is confirmed for {date}.",
    "salary_set": "The confirmed monthly salary is {amount} {currency}.",
    "salary_base": "The confirmed base salary is {amount} {currency}.",
    "income_end": "The employment or contract has ended.",
    "income_once": "A payment of {amount} {currency} has been approved and will be paid on {date}.",
    "rent_increase": "The rent will increase by {pct} percent.",
    "bill_retry": "The bill is still unpaid and will be charged again.",
    "income_uncertain": "The payout is not confirmed yet.",
}


def hypothesis(fact: dict) -> Optional[str]:
    template = HYPOTHESES.get(fact.get("kind"))
    if not template:
        return None
    return template.format(
        amount=_num(fact["amount"]) if fact.get("amount") is not None else "",
        extra=_num(fact["extra"]) if fact.get("extra") is not None else "",
        currency=fact.get("currency") or "",
        date=str(fact.get("effective_date") or "")[:10],
        pct=_num(fact["pct"]) if fact.get("pct") is not None else "",
    ).replace("  ", " ")


def verify(premise: str, fact: dict) -> Tuple[bool, str]:
    """(accepted, reason). Unavailable model -> accepted here; the other gate checks still apply."""
    h = hypothesis(fact)
    if h is None:
        return True, "no NLI statement for this kind"
    r = entailment(premise or "", h)
    if r is None:
        return True, f"NLI {STATS['status']}"
    STATS["checks"] += 1
    ent = r.get("entailment", 0.0)
    if ent < NLI_MIN_ENTAILMENT:
        STATS["rejected"] += 1
        return False, f"NLI: the message does not entail '{h}' (entailment {ent:.2f})"
    return True, f"NLI entailment {ent:.2f}"
