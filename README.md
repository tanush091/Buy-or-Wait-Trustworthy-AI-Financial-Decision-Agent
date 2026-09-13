# Buy or Wait? — Trustworthy AI Financial Decision Agent

An AI financial agent built for **HackerRank Orchestrate (September 2026)**. For every purchase or
payment request it decides whether the user should **pay in full, pay partially, use installments,
wait, or not proceed**. Every recommendation keeps the user above their minimum balance for 90 days
and comes with a grounded explanation.

> **Design principle:** the decision engine is deterministic Python and makes every decision.
> AI models only *read* unstructured evidence (messages, images), and every fact they read must pass
> a trust gate before the engine sees it.

## Results

| Field | Public samples | 5-fold cross-validation | Leave-one-out |
|---|---|---|---|
| Affordability status | 88% | 84% | 88% |
| Payment method | 92% | 84% | 88% |
| Payment plan | 88% | 72% | 88% |
| Earliest full-payment date | 92% | 68% | 84% |
| Spending changes | 88% | 88% | 88% |
| Exact safe amount | 16% | 16% | 16% |

- 250/250 output rows pass the schema check and a separate rule-consistency check.
- 45 automated tests pass, including metamorphic tests, a differential check against an
  independent reference simulator, and a scan proving no sample answers are hardcoded.
- The full run takes about 2 seconds and makes 0 model calls, because all readings are cached.

## How a decision is made

```
dataset/*.csv ──► Load ──► Read evidence ──► Resolve ──► 90-day ledger ──► Candidates ──► Rank ──► Explain & check ──► output.csv
                            │                                                    │
               rules → Gemini → trust gate → NLI                  full · installments · partial · wait
```

1. **Load** typed rows from every CSV; malformed rows are skipped, never guessed.
2. **Read evidence.** A rule parser (English and Indonesian) reads known message wording. Unknown
   messages go to Gemini, and each fact it returns must pass a trust gate:
   - an allowed sender;
   - every number and date must appear in the message;
   - only confirmed income counts, never prizes, refunds or bonuses;
   - a local multilingual NLI model (mDeBERTa) must judge that the message entails the fact.

   Image amounts are cached by SHA-256 of the image file.
3. **Resolve.**
   - Currency is converted at the settlement-date rate.
   - Pending debits are reserved; pending credits are never counted.
   - Recurring expenses are detected from their cadence, and the salary stream is projected.
4. **90-day ledger:** a daily balance forecast that gives the safe amount today and the earliest safe
   full-payment date.
5. **Candidates:** full payment, the seller's installment options, partial payment and wait, plus up
   to three permitted spending changes.
6. **Rank:** unsafe plans are removed, then the spec's six-step order picks the winner.
7. **Explain & check:** a template explanation built from computed facts only, followed by schema
   and consistency checks.

## Run

```bash
pip install -r code/requirements.txt              # pandas (Gemini client uses the standard library)
pip install -r code/requirements-nli.txt          # optional: torch + transformers for the NLI check
python code/main.py                               # dataset/requests.csv -> output.csv
python code/main.py --insights                    # + decision audit, risk flags, HTML dashboard
python code/whatif.py <request_id> --balance +5000   # re-decide one request with changed inputs
python code/trace.py <request_id>                 # step-by-step explanation of one decision
python code/evaluation/cross_validate.py          # held-out accuracy
python -m unittest discover -s code/tests -v      # tests (no network)
```

No API key is needed for the provided dataset. For new messages or images, put
`GEMINI_API_KEY=...` in a `.env` file at the repository root; `.env` is gitignored.

## Decision insights

- **Decision audit** (`code/evaluation/decision_audit.json`). For every request it records:
  - every payment option, and why it was chosen or rejected (e.g. "runs about 15 months, above the
    user's 11-month limit");
  - the forecast's lowest balance;
  - the evidence used.
- **Risk flags** (`code/evaluation/risk_flags.csv`): the cushion above the minimum, in days of normal
  spending (tight, moderate, comfortable or blocked).
- **What-if tool** (`code/whatif.py`): changes the balance, price, deadline, minimum or accepted
  methods, and shows exactly which outputs change.
- **Dashboard** (`code/evaluation/dashboard.html`): a self-contained page with the accuracy table,
  decision mix, risk levels and every decision's reasoning.

## Repository layout

```
code/                 solution (entry point code/main.py; details in code/README.md)
code/evaluation/      scoring, cross-validation, consistency check, usage report, audit, dashboard
code/tests/           engine, integrity and feature tests
code/cache/           cached model readings (so reruns are deterministic and offline)
dataset/              challenge input data
problem_statement.md  full challenge specification
output.csv            final predictions for dataset/requests.csv
```

More detail: [code/README.md](code/README.md) (design and trade-offs) and
[code/SCALING.md](code/SCALING.md) (production scaling).
