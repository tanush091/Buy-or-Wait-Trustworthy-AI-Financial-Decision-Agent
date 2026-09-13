# Scaling Buy or Wait? to production

## What already scales

| Property | Why it matters | Where |
|---|---|---|
| Each decision depends only on one user's data | Requests can be split by user across processes or machines | `main.py --workers N` |
| Deterministic engine, no model in the decision path | Same input gives the same answer; results can be cached and audited | `resolver.py`, `timeline.py`, `ranker.py` |
| Model answers cached per message / per image content | Re-reading costs nothing; the provided dataset runs with 0 model calls | `code/cache/llm_cache.json` |
| Fast-fail LLM layer with model fallback | No hang offline or when quota runs out; falls back to rules + cache | `llm.py` |
| Bad records fall back to the safest answer | One corrupt row never stops a batch | `main.py`, `loaders.py` |
| Regression tests + consistency check | Fixed bugs cannot silently return | `tests/`, `evaluation/consistency_check.py` |

## Production architecture (next steps)

1. **Service layer.** Wrap `Engine.decide(request)` behind an HTTP API (FastAPI) with one
   endpoint per decision and one for `trace` (the step-by-step explanation already in
   `trace.py`). Load each user's data on demand from a database instead of CSV files.
2. **Storage.** Events, profiles and payment options in Postgres (indexed by user); the
   LLM/image cache in Redis or an object store keyed by content hash, shared by all workers.
3. **Asynchronous perception.** Receipt uploads and new messages go on a queue (SQS, Pub/Sub or
   Celery). Workers call Gemini, run the same trust gate, and store typed facts. The decision
   API only reads stored facts, so users never wait on a model call. Low-confidence image
   readings go to the user for a one-tap confirmation before they count.
4. **Horizontal scale.** Stateless decision workers behind a load balancer, partitioned by
   user; the engine decides ~100 requests per second per core, so cost is dominated by
   storage, not compute.
5. **Observability.** Log every decision with its inputs hash, config version, model versions
   and trust-gate outcomes; alert on fallback rate, LLM error rate, quota and latency.
6. **Safety and compliance.** Keep the hard rule (never below the minimum balance) as an
   invariant check on every response; version the config; keep secrets in a secret manager.

## Closing the accuracy gap

The remaining error is the forecast of everyday spending (groceries, dining, transport):
matching the answer key would need corrections from x0.64 to x2.55 depending on the user,
so no single rule fits 25 samples. In production this is solved with data, not tuning:

- **Learn per-user spending models** from months of history (seasonality, pay-cycle effects)
  and back-test them: forecast day d, compare with what actually happened by day d+90.
- **Report a range** (pessimistic / expected / optimistic, `evaluation/safe_ranges.py`) and
  calibrate it until the true outcome falls inside the range at the promised rate.
- **Close the loop:** record whether users who were told "affordable" stayed above their
  minimum, and retrain on those outcomes.
