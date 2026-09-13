# Buy or Wait? — financial decision agent

For every request in `dataset/requests.csv` the agent rebuilds the user's cash position,
forecasts 90 days, and chooses pay-now / partial / installments / wait / not recommended.

## Run

```bash
pip install -r code/requirements.txt        # pandas (the Gemini client uses only the standard library)
pip install -r code/requirements-nli.txt    # optional: torch + transformers for the NLI fact check
python code/main.py                          # dataset/requests.csv -> output.csv (repo root)
python code/evaluation/main.py               # score on the 25 solved samples
python code/evaluation/cross_validate.py     # held-out accuracy (5-fold + leave-one-out, all tuned switches)
python code/evaluation/consistency_check.py  # re-verify every output row against the rules
python code/schema_check.py                  # format check of output.csv
python code/trace.py request_19              # step-by-step explanation of one decision
python code/main.py --workers 4              # parallel: requests partitioned by user
python code/main.py --insights               # also: decision audit, risk flags, HTML dashboard
python code/whatif.py request_19 --balance +5000   # what-if: re-decide one request with changed inputs
python -m unittest discover -s code/tests -v # regression tests (no network)
```

See `SCALING.md` for the production scaling approach.

A request whose data cannot be verified (e.g. a missing exchange rate) falls back to the
safest answer (`not_affordable` / `not_recommended`) instead of stopping the run.

Run from the repository root: unzip `code.zip` so that `code/` sits next to `dataset/`
(the challenge repo layout). Set `BUY_OR_WAIT_DATASET` to point at another dataset folder.
No secrets are needed for the provided dataset (every message and image reading is cached);
`GEMINI_API_KEY` is only used for messages or images that are not in `code/cache/`.

Outputs: `output.csv`, `code/evaluation/usage_report.md`, `code/evaluation/injection_log.md`,
`code/evaluation/sample_scores.md`.

## Design

The decision engine is deterministic Python; models never decide. Multimodal reading is
limited to extracting amounts from the 16 receipt/payslip images, cached by `image_id`.

| Stage | Module | What it does |
|---|---|---|
| Load | `loaders.py` | Typed rows from every CSV |
| Images | `extractors.py` | Blank event amounts from `media/images/<image_id>.png` (SHA-256 verified cache, Gemini vision for new images) |
| Messages | `amendments.py` | Regex parser (English + Indonesian templates) → typed facts; trust gate by source type and date; scam / instruction-like text rejected and logged |
| Resolve | `resolver.py` | FX at settlement date; drops failed, cancelled, unrealized, pending credits; reserves pending/scheduled debits; detects recurring expense series per category by cadence (monthly or every N days, one-offs fall off the grid); builds the salary stream (scheduled salary continues monthly, final payroll / ended contracts stop it, freelance day-of-month clusters) and applies message facts (raises, delays, reduced or arrears pay, first salary, confirmed invoices, rent increases, failed bills still owed) |
| Forecast | `timeline.py` | Daily ledger over `request_date … +90`; a payment on a day is taken after that day's income |
| Metrics | `metrics.py` | `amount_safe_to_pay` (largest payment today, before changes), `earliest_date_for_full_payment` (capacity, preference-independent) |
| Plans | `candidates.py` | Full, supplied installment options (within `max_installment_months`), partial (two payments ending on the earliest date), wait |
| Rank | `ranker.py` | Safety filter, then: finish by deadline → no spending changes → lowest total paid → earlier start → fewer payments → lowest option id. Spending changes (≤3, flexible non-protected recurring expenses the user allows) are only tried when no unchanged plan finishes on time; the least disruptive safe set wins |
| Explain | `explain.py` | Short explanation from computed facts only |
| Check | `schema_check.py` | Column order, enums, bounds, plan format, partial rules, installment plans match a supplied option, change rules |

Spec ambiguities are resolved with a few global switches in `config.py`
(estimator for variable amounts, same-day ordering, request-day occurrences, and how far
ahead pooled day-to-day spending such as groceries, dining and transport is forecast),
chosen by scoring the public samples. Fixed bills are always forecast for the full 90 days;
pooled variable spending is forecast for 60 days, which sat on a stable plateau (55-75 days)
in the sample search. Nothing branches on a request id or label.

## LLM reading layer (Gemini, Google AI Studio)

The deterministic engine is unchanged; Gemini only *reads* inputs the rules cannot:

- **Unrecognised messages** (wording outside the rule parser's English / Indonesian patterns)
  are batch-classified by `gemini-3.5-flash` into the same typed facts the rule parser
  produces. (If the free-tier quota runs out mid-run the client falls back to lighter models,
  then stops calling and the rules + cache carry on.) Each fact passes the same trust gate
  (sender allow-list, date sanity) plus anti-hallucination checks: every number and the date
  the model returns must appear in the message text, windfalls (prizes, refunds, bonuses,
  commissions) are never counted, one-off income needs invoice / client wording, and the NLI
  model must judge that the message entails the fact. Scam/instruction text is rejected
  before any model call.
- **New or changed images** (SHA-256 does not match the verified cache) are read with
  Gemini vision.
- Answers are cached in `code/cache/llm_cache.json` (temperature 0), so re-runs are
  deterministic and work without a key. Usage and cost go to `evaluation/usage_report.md`.

Setup: put `GEMINI_API_KEY=...` in a `.env` file at the repository root (never commit it).
`BUY_OR_WAIT_LLM=0` disables the layer; `BUY_OR_WAIT_LLM_MODEL` picks another Gemini model.
`python code/evaluation/model_comparison.py` compares the rule parser with Gemini models on
all messages and images (`evaluation/model_comparison.md`).

### Known design trade-offs

- Pooled day-to-day spending (groceries, dining, transport) is forecast for 60 days and fixed
  bills for the full 90; the typical flexible charge comes from `minimum_allowed_amount`; variable
  bills use the latest bill. These six settings were chosen on the solved samples;
  `cross_validate.py` repeats that whole selection (all six switches, 160 combinations) inside
  5-fold and leave-one-out cross-validation to estimate held-out accuracy. They are less
  conservative than a literal full-horizon forecast of every category.
- Data that cannot be verified (e.g. an expense in a currency with no exchange-rate path, even
  through a common currency) is never dropped from the forecast; `Engine.decide` returns the
  safe fallback (`not_recommended`, safe amount 0) and states the reason.
- The safety check covers the spec's 90-day forecast; installment payments after day 90 are
  not simulated.
- Salary history is dated by `event_date` (`salary_on_settlement=False`). Both readings were
  tested: dating payroll by its settlement date (8 payroll rows settle a week later) turns solved
  sample request_07 from installments into not_affordable, contradicting its published answer.
- `wait` stays eligible when full payment only becomes safe after `desired_completion_date`:
  the spec makes `wait` eligible "when full payment becomes safe later" and treats the deadline
  as the first ranking preference, not as an eligibility rule.

### Reliability of the LLM layer

- **One provider:** all message and image reading goes through `code/llm.py` (Gemini) with a
  single usage tracker feeding `evaluation/usage_report.md`.
- **Never hangs offline:** a 3-second reachability probe before the first call; network errors
  are not retried; a rate limit or server error gets one short retry; a per-run time budget
  (`BUY_OR_WAIT_LLM_TIME_BUDGET`, default 180 s). On any failure the layer switches off and the
  run continues on rules + cache. Without a key there is no network access at all.
- **Model fallback:** if the default model's daily quota runs out (HTTP 429), the same call is
  retried on the next model in `LLM_FALLBACK_MODELS` (default: gemini-3.5-flash-lite, then
  gemini-flash-lite-latest); only when every model is exhausted does the layer switch off.
- **Lazy:** each request reads only its own unrecognised messages; full runs prefetch them in
  batches. Answers are cached per message, so a single request, a trace and a full run share them.
- **NLI entailment check (anti-hallucination):** every fact read by Gemini is restated as a plain
  sentence ("The monthly salary will be 3,150 EUR starting 2025-09-01.") and a local multilingual
  NLI model (`MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7`, English + Indonesian)
  must judge that the source message entails it (probability >= 0.6). This rejects readings whose
  numbers appear in the text but whose meaning does not ("pay *might* rise if approved", a number
  that belongs to something else, a misread amount). Runs offline after the first download, no
  API quota; if torch/transformers are missing the other gate checks still apply (`BUY_OR_WAIT_NLI=0`
  disables it).
- **Confirmed income only:** an LLM fact that adds money is rejected unless the model marks it
  confirmed and the message has no pending / submitted / under-review / estimated wording
  (English or Indonesian). Salary facts from the LLM are accepted only from employers.
- **Offline cache:** all current messages and images are cached, so the provided dataset runs
  with 0 model calls.

## Decision insights (additive; never change output.csv)

- **Decision audit** (`code/audit.py`, `python code/evaluation/decision_audit.py` →
  `evaluation/decision_audit.json`): for every request, the forecast's lowest balance with and
  without the recommended plan, every payment option with whether it was eligible, safe and on time
  and the exact reason it was chosen or rejected (e.g. "runs about 15 months, above the user's
  11-month limit", "safe, but ranked lower: costs more in total"), and the evidence used (message
  facts and their source, images, recurring expenses). Each audit re-derives the decision and is
  cross-checked against `output.csv`.
- **Risk flags** (`evaluation/risk_flags.csv`): the cushion left above the minimum at the lowest
  point, in days of the user's normal forecast spending: `tight` (< 7 days), `moderate` (< 30),
  `comfortable`, or `blocked` (no safe plan).
- **What-if** (`python code/whatif.py <request_id> --balance +5000 --amount 900 --deadline YYYY-MM-DD
  --minimum -500 --methods full_payment,installments --allow-partial yes [--json]`): re-decides one
  request with changed inputs and marks every output that changes; nothing is written.
- **Dashboard** (`python code/evaluation/dashboard.py` → `evaluation/dashboard.html`): one
  self-contained page (no external scripts or network) with the accuracy table, decision mix, risk
  levels, the decision pipeline and every decision with its option-by-option reasoning.

## Evaluation workflow

`code/evaluation/main.py` runs the engine on `dataset/sample_requests.csv` and reports
per-field match rates (`score_samples.py`). The grid of config switches was compared the
same way; the chosen setting favours forecasts whose safe amounts sit closest to the
samples while keeping plan choice accurate.
