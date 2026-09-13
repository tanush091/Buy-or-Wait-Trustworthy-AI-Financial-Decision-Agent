# Model comparison

Rule parser vs LLM on all messages; LLM vs hand-verified amounts on all images.

| Model | Message kind agreement | Amount/date agreement (where rule found one) | Scam/info messages kept no-effect | Image amount accuracy | Calls | Input tokens | Output tokens | Est. cost (USD) |
|---|---|---|---|---|---|---|---|---|
| gemini-3.5-flash | 215/215 (100.0%) | 124/124 | 66/66 | 15/16 | 4 | 40507 | 56202 | n/a |
| gemini-2.5-flash | 191/215 (88.8%) | 114/124 | 55/66 | 0/16 | 0 | 0 | 0 | 0.0000 |

gemini-2.5-flash: cached answers only (its free-tier quota of 20 requests/day ran out after 12 calls); unanswered items count as disagreements.

Calls/tokens are for this comparison run only (0 when answers came from the cache).

## Disagreements

- gemini-3.5-flash image_07: verified=8528.0 llm=8528.1
