import os
from pathlib import Path

# Paths
REPO_ROOT = Path(__file__).resolve().parent.parent
CODE_DIR = REPO_ROOT / "code"
DATASET_DIR = Path(os.environ.get("BUY_OR_WAIT_DATASET", REPO_ROOT / "dataset"))
MEDIA_DIR = DATASET_DIR / "media" / "images"
CACHE_DIR = CODE_DIR / "cache"
EVALUATION_DIR = CODE_DIR / "evaluation"
OUTPUT_PATH = REPO_ROOT / "output.csv"

CACHE_DIR.mkdir(parents=True, exist_ok=True)

HORIZON_DAYS = 90

AFFORDABILITY_STATUSES = {
    "affordable_now",
    "affordable_with_plan",
    "affordable_later",
    "not_affordable",
}

RECOMMENDED_PAYMENT_METHODS = {
    "full_payment",
    "partial_payment",
    "installments",
    "wait",
    "not_recommended",
}

# Spec disambiguation switches, chosen against dataset/sample_requests.csv (never per request).
CONFIG = {
    "var_estimator": "median",    # per-occurrence estimate for pooled variable spending (groceries, dining, transport)
    "bill_estimator": "last",     # single-payee bills whose amount varies (utilities): use the most recent bill
    "pending_on_request_day": False,  # reserve pending debits today instead of on their settlement date
    "horizon": 90,                # forecast length in days
    # Experimental switches evaluated against samples + cross-validation (off = spec-strict):
    "change_tolerance": 0.0,      # accept a spending-change plan short of the minimum by <= this share of the request
    "now_buffer": 0.0,            # require capacity >= request * (1 + buffer) for pay-now-in-full
    "discretionary_window": None, # days to forecast unprotected discretionary categories (None = like others)
    "discretionary_categories": ["dining", "entertainment", "shopping"],
    "explain_detail": True,       # add the forecast's lowest balance and date to each explanation
    "base_from_minimum": True,    # flexible expenses: typical charge = minimum_allowed_amount / its share
                                  # (the floor is exactly 40% or 50% of a typical charge across the dataset)
    "max_changes": 3,             # spending changes allowed per plan
    "income_first": True,         # same-day income is available before that day's bills are checked
    # Salary history dated by settlement_date (True) or event_date (False). Tested both ways: settlement
    # dating turns a solved sample from installments into not_affordable, contradicting its published
    # answer, so the samples indicate event-date timing (see README "Known design trade-offs").
    "salary_on_settlement": False,
    "variable_scope": "all",      # "all" | "protected": forecast pooled variable spending for all or protected categories
    "day0": True,                 # recurring occurrences falling on request_date are still owed
    "pooled_mode": "cadence",     # pooled variable spending: "cadence" | "replay" (last 30 days) | "daily" rate
    "pooled_window": 60,          # days of pooled variable spending (groceries, dining, transport) to forecast;
                                  # fixed bills always run the full horizon. 55-60 is a stable plateau on samples.
}


# LLM reading layer (Google AI Studio / Gemini). Reads unrecognised messages and new images; never decides.
# gemini-3.5-flash by default: on the free tier gemini-2.5-flash allows only 20 requests/day.
LLM_MODEL = os.environ.get("BUY_OR_WAIT_LLM_MODEL", "gemini-3.5-flash")
# Tried in order when the default model's daily quota runs out (free tier: 20 requests/day per model).
LLM_FALLBACK_MODELS = [m.strip() for m in os.environ.get(
    "BUY_OR_WAIT_LLM_FALLBACKS", "gemini-3.5-flash-lite,gemini-flash-lite-latest").split(",") if m.strip()]
LLM_BATCH = 25                     # messages per call (batching keeps calls and tokens low)
LLM_FREE_TIER = True               # Google AI Studio free tier: nothing is billed
# Local NLI verifier for LLM-extracted facts (multilingual: English + Indonesian messages).
NLI_MODEL = os.environ.get("BUY_OR_WAIT_NLI_MODEL", "MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7")
NLI_MIN_ENTAILMENT = float(os.environ.get("BUY_OR_WAIT_NLI_MIN", "0.6"))  # message must entail the fact
LLM_TIMEOUT = float(os.environ.get("BUY_OR_WAIT_LLM_TIMEOUT", "45"))          # seconds per call (batched calls take ~20 s)
LLM_TIME_BUDGET = float(os.environ.get("BUY_OR_WAIT_LLM_TIME_BUDGET", "180"))  # total LLM seconds per run
# USD paid-tier list prices per 1M text tokens (input, output), from
# https://ai.google.dev/gemini-api/docs/pricing (checked 2026-09-13); update if pricing changes.
LLM_PRICES = {
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-flash-lite-latest": (0.30, 2.50),  # alias, not listed: priced as the latest Flash-Lite (3.5)
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
}
