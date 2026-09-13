# Token usage report (final full-dataset run)

Requests processed: 250

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
| Google AI Studio | gemini-3.5-flash | 0 | 0 | 0 | 0 | 0.0000 |
| **All models** | | **0** | **0** | **0** | **0** | **0.0000** |

| Metric | Value |
|---|---|
| Model calls in this run | 0 |
| Messages sent to the LLM in this run | 0 |
| LLM answers reused from cache | 57 |
| Tokens originally spent producing the cached answers used (input / output) | 6759 / 16275 |
| LLM status during run | available |
| NLI fact verifier (local, 0 API tokens) | not loaded; checks 0, rejected 0 |
| Time spent in LLM calls (s) | 0.0 |
| Image files read and fingerprinted | 11 |
| Verified image cache hits | 11 |
| Total tokens this run | 0 |
| Average tokens per request (this run) | 0.00 |
| Average tokens per request incl. original cost of cached answers | 92.14 |
| Billed cost (USD) | 0.00 (Google AI Studio free tier) |
| Estimated cost per request at list price (USD) | 0.000000 |

List prices per 1M tokens are in `code/config.py` (`LLM_PRICES`). Model comparison
(rule parser vs Gemini models on all messages and images) is in `model_comparison.md`.

Image cache provenance: the 16 image amounts were first read during development (Claude,
multimodal, in the Claude Code session that built this solution) and checked by hand
against each image; Gemini 3.5 Flash independently matched 15 of 16.
