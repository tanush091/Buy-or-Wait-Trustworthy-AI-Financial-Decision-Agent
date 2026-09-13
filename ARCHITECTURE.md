# ARCHITECTURE.md — "Buy or Wait?" Financial Agent

> **Purpose of this document.** This is the single implementation blueprint for the
> HackerRank Orchestrate (September 2026) "Buy or Wait?" agent. It is written to be
> handed directly to an AI coding assistant. Every module has an explicit contract:
> inputs, outputs, invariants, and failure behaviour. Implement modules in the order
> given in §10. Do not deviate from the data contracts in §3.

---

## 1. Design principle (read this first)

**The decision engine is 100% deterministic Python. The LLM never makes a decision.**

Scoring compares numeric amounts, enum values, exact plan strings, and exact dates
against hidden ground truth. Therefore:

| Layer | Implemented by | Why |
|---|---|---|
| Reading CSVs, joins, currency conversion | Pure Python | Exactness |
| Extracting amounts from images | VLM (cached) | Unavoidable multimodal step |
| Interpreting messages into typed amendments | LLM (cached) | Natural-language → struct |
| Forecasting, plan generation, safety, ranking | Pure Python | Must be exact + auditable |
| Writing `decision_explanation` | LLM (batched, post-hoc) | Prose only, zero authority |

The LLM emits **structured facts only**. It is never asked "what should the user do?".

---

## 2. Top-level pipeline

```
                         dataset/*.csv  +  dataset/media/images/*.png
                                        │
              ┌─────────────────────────┴─────────────────────────┐
              ▼                                                   ▼
        STAGE 1: LOAD                                   STAGE 2: EXTRACT
        loaders.py                                      extractors.py (VLM)
        typed dataclasses                               amendments.py (LLM)
        no interpretation                               run ONCE per asset, cached
              │                                                   │
              └─────────────────────────┬─────────────────────────┘
                                        ▼
                              STAGE 3: RESOLVE
                              resolver.py
                              - apply amendments to events
                              - drop cancelled / failed / pending-credit
                              - de-duplicate
                              - fill blank amounts from images
                              - convert all → home_currency
                              output: List[ResolvedEvent] per user
                                        │
                                        ▼
                              STAGE 4: FORECAST
                              timeline.py
                              output: B[0..90] float vector
                                        │
                                        ▼
                              STAGE 5: CORE METRICS
                              metrics.py
                              - amount_safe_to_pay  (closed form)
                              - earliest_date_for_full_payment
                                        │
                                        ▼
                              STAGE 6: GENERATE CANDIDATES
                              candidates.py
                              full | installments | partial | wait | none
                              × spending-change subsets (size 0..3)
                                        │
                                        ▼
                              STAGE 7: VALIDATE
                              validator.py :: is_safe(B, plan, changes)
                              keep only candidates passing the 90-day check
                                        │
                                        ▼
                              STAGE 8: RANK
                              ranker.py — 6-key lexicographic sort
                              output: exactly one BestPlan
                                        │
                                        ▼
                              STAGE 9: EXPLAIN
                              explain.py (LLM, batched 25/call)
                                        │
                                        ▼
                              STAGE 10: WRITE + SELF-CHECK
                              writer.py → output.csv (REPO ROOT)
                              schema_check.py must pass 100%
```

---

## 3. Data contracts

All dataclasses live in `models.py`. Use `@dataclass(frozen=True)` where possible.

```python
# ---------- Raw inputs ----------

@dataclass
class Request:
    request_id: str
    user_id: str
    request_date: date
    request_type: str          # purchase|travel|education|family_transfer|
                               # debt_repayment|investment|housing|
                               # emergency_expense|other
    requested_amount: float    # already in user's home_currency
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str

@dataclass
class Profile:
    user_id: str
    home_currency: str                      # INR|ZAR|IDR|USD|EUR
    available_balance: float
    minimum_balance_to_keep: float
    financial_priorities: list[str]
    spending_preferences: list[str]
    payment_methods_user_will_consider: set[str]   # subset of
                               # {full_payment, partial_payment, installments}

@dataclass
class RawEvent:
    event_id: str
    user_id: str
    event_date: date
    event_type: str            # income|expense|transfer|refund|investment|...
    amount: float | None       # None = MUST be filled from a linked image
    currency: str
    is_recurring: bool
    recurrence_interval_days: int | None
    is_essential: bool
    is_flexible: bool          # only flexible+recurring may be changed
    status: str                # settled|pending|failed|cancelled|forecast|...
    linked_event_id: str | None

@dataclass
class PaymentOption:
    request_id: str
    payment_option_id: str
    start_date: date
    num_payments: int
    interval_days: int
    per_payment_amount: float
    financing_fee: float
    total_payable: float

@dataclass
class Message:
    message_id: str
    user_id: str | None
    request_id: str | None
    related_event_id: str | None
    message_date: date
    text: str                  # UNTRUSTED

@dataclass
class ImageRef:
    image_id: str              # file at dataset/media/images/<image_id>.png
    user_id: str | None
    request_id: str | None
    related_event_id: str | None

@dataclass
class ExchangeRate:
    rate_date: date
    from_currency: str
    to_currency: str
    rate: float

# ---------- Derived ----------

@dataclass
class Amendment:                 # produced by amendments.py (LLM)
    source_message_id: str
    action: str                  # cancel|amend_amount|delay|confirm|irrelevant
    target_event_id: str | None
    new_amount: float | None
    new_date: date | None
    confidence: float

@dataclass
class ResolvedEvent:             # produced by resolver.py
    event_id: str
    event_date: date
    amount_home: float           # signed: +income, -expense
    is_recurring: bool
    recurrence_interval_days: int | None
    is_essential: bool
    is_flexible: bool
    provenance: list[str]        # ["csv", "image:image_07", "msg:msg_12"]

@dataclass
class SpendingChange:
    kind: str                    # "stop" | "reduce_to"
    event_id: str
    new_amount: float | None     # required iff kind == "reduce_to"
    def render(self) -> str: ...  # "stop:event_14" / "reduce_to:event_21:100"

@dataclass
class Candidate:
    method: str                  # full_payment|partial_payment|installments|
                                 # wait|not_recommended
    plan: list[tuple[date, float]]
    changes: list[SpendingChange]
    payment_option_id: str | None
    total_paid: float

@dataclass
class Decision:
    request_id: str
    amount_safe_to_pay: float
    affordability_status: str
    recommended_payment_method: str
    payment_plan: str            # "2026-09-07:300|2026-10-07:300" or "none"
    earliest_date_for_full_payment: str   # "YYYY-MM-DD" or ""
    spending_changes_needed: str          # "stop:event_14|..." or "none"
    decision_explanation: str
```

---

## 4. STAGE 3 — `resolver.py` (the accuracy bottleneck)

Produces the clean event list. Apply rules in **this exact order**:

1. **Fill blank amounts.** For any `RawEvent` with `amount is None`, look up its
   `event_id` in `images.csv` as `related_event_id`, load
   `dataset/media/images/<image_id>.png`, call the VLM. **Never treat blank as 0.**
   If extraction fails, mark the event `unresolved` and use the financially safer
   interpretation (treat an unknown expense as its largest plausible value; treat an
   unknown income as absent).

2. **Apply amendments** from `amendments.py`, only where the amendment passes the
   trust gate in §6.

3. **Drop** these rows entirely:
   - `status` in `{failed, cancelled}`
   - pending **credits** (incoming money not yet settled)
   - duplicate representations of the same event (same user + date + amount +
     type, or linked via `linked_event_id` chains — keep the newest/most settled)
   - unrealised investment values and non-cash holdings

4. **Keep** pending **debits** as reserved outflows.

5. **Salary** counts only on its confirmed settlement date.

6. **Currency conversion.** Convert any event not in `home_currency` using
   `exchange_rates.csv` matched on (rate_date, from, to). Conversion policy is a
   config switch — see §8.

7. **Conflict precedence** when two records disagree:
   1. explicit cancellation / settlement / amendment wins
   2. newer record from the same source wins
   3. settled event beats estimate/forecast
   4. otherwise choose the financially **safer** interpretation

**Invariant:** `resolver.py` never looks at the request. It produces a user-level
financial state that is reusable across all of that user's requests. Cache it.

---

## 5. STAGES 4–8 — the deterministic core

### 5.1 `timeline.py`

```python
def build_balance_vector(profile, events, request_date, horizon=90) -> list[float]:
    """
    B[i] = projected balance at end of day (request_date + i days).
    Includes ALL recurring expenses at full amount (flexible ones too) —
    spending changes are applied later, as deltas.
    Excludes the request payment itself.
    """
    B = [0.0] * (horizon + 1)
    B[0] = profile.available_balance + net_flows_on(request_date, events)
    for i in range(1, horizon + 1):
        B[i] = B[i-1] + net_flows_on(request_date + timedelta(days=i), events)
    return B
```

Expand recurring events across the horizon using `recurrence_interval_days`.

### 5.2 `validator.py` — the one function everything uses

A payment of `a` on day `d` subtracts `a` from `B[d:]`. A spending change adds
back its saved amount from the change's effective date onward. Therefore:

```python
def apply(B, plan, changes, request_date, events) -> list[float]:
    C = B.copy()
    for (pay_date, amt) in plan:
        d = (pay_date - request_date).days
        for i in range(d, len(C)):
            C[i] -= amt
    for ch in changes:
        for occ_day, saved in savings_schedule(ch, events, request_date):
            for i in range(occ_day, len(C)):
                C[i] += saved
    return C

def is_safe(B, plan, changes, min_balance, request_date, events) -> bool:
    return min(apply(B, plan, changes, request_date, events)) >= min_balance
```

Every candidate — full, partial, installments, wait — validates through `is_safe`.
Write this once and get it right; most of the score follows from it.

### 5.3 `metrics.py` — closed forms

```python
def amount_safe_to_pay(B, min_balance, requested_amount) -> float:
    """A payment today shifts the WHOLE curve down uniformly, so no search."""
    headroom = min(B) - min_balance
    return clamp(headroom, 0.0, requested_amount)

def earliest_date_for_full_payment(B, min_balance, requested_amount,
                                   request_date, horizon=90) -> date | None:
    """Capacity only. Ignores payment-method preferences AND spending changes."""
    for d in range(horizon + 1):
        if is_safe(B, [(request_date + timedelta(d), requested_amount)],
                   [], min_balance, request_date, events):
            return request_date + timedelta(d)
    return None      # → empty string in output
```

### 5.4 `candidates.py`

Generate every legal candidate, then filter. Do **not** write branching decision logic.

```python
def generate(request, profile, options, B, safe, earliest) -> list[Candidate]:
    out = []
    M = profile.payment_methods_user_will_consider

    if "full_payment" in M:
        out.append(Candidate("full_payment",
                             [(request.request_date, request.requested_amount)],
                             [], None, request.requested_amount))

    if "installments" in M:
        for opt in options:                      # one candidate per option
            out.append(Candidate("installments", schedule_of(opt), [],
                                 opt.payment_option_id, opt.total_payable))

    if ("partial_payment" in M and request.allows_partial_payment
            and 0 < safe < request.requested_amount
            and earliest is not None
            and earliest <= request.desired_completion_date):
        out.append(Candidate("partial_payment",
                             [(request.request_date, safe),
                              (earliest, request.requested_amount - safe)],
                             [], None, request.requested_amount))

    if "full_payment" in M and earliest is not None \
            and earliest > request.request_date:
        out.append(Candidate("wait",
                             [(earliest, request.requested_amount)],
                             [], None, request.requested_amount))

    # cross every above candidate with spending-change subsets of size 1..3
    out += with_spending_changes(out, flexible_recurring_events(events))

    out.append(Candidate("not_recommended", [], [], None, 0.0))
    return out
```

**Spending-change rules (hard):**
- only events that are `is_recurring and is_flexible`
- maximum 3 changes
- an event may be `stop`ped **or** `reduce_to`d, never both
- for `reduce_to`, binary-search the **minimum** reduction that makes the plan safe

### 5.5 `ranker.py`

```python
def sort_key(c: Candidate, request) -> tuple:
    return (
        not completes_by(c, request.desired_completion_date),  # False first
        len(c.changes) > 0,
        c.total_paid,                       # financing fees penalised here
        c.plan[0][0] if c.plan else date.max,
        len(c.plan),
        int(c.payment_option_id.split("_")[-1]) if c.payment_option_id else 0,
    )

best = min(valid_candidates, key=lambda c: sort_key(c, request))
```

### 5.6 Status mapping

| Winning candidate | `affordability_status` |
|---|---|
| `full_payment` on `request_date`, zero changes | `affordable_now` |
| `installments` / `partial_payment` / anything with changes | `affordable_with_plan` |
| `wait` | `affordable_later` |
| `not_recommended` | `not_affordable` |

Enforced invariant: `affordable_now` ⟹ `earliest_date_for_full_payment == request_date`.

---

## 6. LLM layer contracts

### 6.1 `extractors.py` — VLM, one call per unique image, cached to disk

```
INPUT : dataset/media/images/<image_id>.png + doc hint
OUTPUT: {"amount": float|null, "currency": str|null, "date": "YYYY-MM-DD"|null,
         "doc_type": "payslip|statement|bill|receipt|other",
         "confidence": 0.0-1.0}
```

Cache file: `code/cache/images.json`, keyed by `image_id`. **Reuse across all 250
requests.** This is the largest token saving available.

### 6.2 `amendments.py` — LLM, one call per unique message, cached

```
INPUT : message text, wrapped in <untrusted_data> delimiters
OUTPUT: {"action": "cancel|amend_amount|delay|confirm|irrelevant",
         "target_event_id": str|null, "new_amount": float|null,
         "new_date": "YYYY-MM-DD"|null, "confidence": 0.0-1.0}
```

Cache file: `code/cache/messages.json`, keyed by `message_id`.

### 6.3 Trust gate (mandatory — the spec warns about injection)

Reject any amendment that fails **any** of these, and append the rejection to
`evaluation/injection_log.md`:

1. JSON does not validate against the schema
2. `target_event_id` does not exist for this user
3. amendment magnitude exceeds a sanity cap (e.g. >5× the original amount)
4. text contains instruction-like directives aimed at the agent
5. `confidence` below threshold

Never let message text reach the ranker, validator, or any decision path.

### 6.4 `explain.py` — post-hoc prose only

Runs **after** `best` is chosen. Input is computed facts only: balance, minimum,
headroom, the binding constraint date, the chosen plan. Batch 25 requests per call.
One or two sentences, citing concrete numbers and the binding date.

---

## 7. STAGE 10 — `writer.py` + `schema_check.py`

Write to **`output.csv` in the repository root** (not `dataset/`). Exact column
order:

```
request_id,amount_safe_to_pay,affordability_status,recommended_payment_method,
payment_plan,earliest_date_for_full_payment,spending_changes_needed,
decision_explanation
```

`schema_check.py` must pass 100% before submission. It asserts:

- 250 data rows + 1 header, one row per `request_id`
- `0 <= amount_safe_to_pay <= requested_amount` on every row
- `affordability_status` and `recommended_payment_method` in the allowed enums
- `affordable_now` ⟹ `earliest_date_for_full_payment == request_date`
- `payment_plan` chronological, `YYYY-MM-DD:<amount>` joined by `|`, or `none`
- `partial_payment` ⟹ exactly 2 payments summing to `requested_amount`,
  status is `affordable_with_plan`
- `installments` ⟹ plan matches a real `payment_option_id` exactly
- ≤3 spending changes, each targeting a flexible recurring event,
  no event both stopped and reduced
- no empty `decision_explanation`

---

## 8. `config.py` — the ambiguity switches

The spec is genuinely ambiguous in seven places. Encode them as switches and
grid-search against the 25 solved samples in `dataset/sample_requests.csv`.

```python
CONFIG = {
  "horizon_inclusive":        True,    # [request_date, +90] both ends?
  "event_before_payment":     True,    # same-day event lands before payment?
  "earliest_window":          "fixed", # "fixed" [req,+90] vs "sliding" [D,D+90]
  "rounding":                 0,       # decimal places — read from samples
  "salary_counts_on_req_date":True,
  "prefer_fewer_changes":     True,    # tie-break among change-requiring plans
  "fx_match":                 "exact", # "exact" vs "nearest_prior"
}
```

This is spec disambiguation, not label-fitting. Never branch on `request_id`.

---

## 9. Directory layout to ship

```
code/
├── main.py              # entry point: python3 code/main.py
├── README.md            # architecture + run instructions + design rationale
├── requirements.txt     # pinned
├── config.py
├── models.py
├── loaders.py
├── extractors.py
├── amendments.py
├── resolver.py
├── timeline.py
├── metrics.py
├── candidates.py
├── validator.py
├── ranker.py
├── explain.py
├── writer.py
├── schema_check.py
├── cache/
│   ├── images.json
│   └── messages.json
└── evaluation/
    ├── usage_report.md      # MANDATORY — real numbers from the final run
    ├── score_samples.py     # per-field accuracy on the 25 samples
    ├── sample_scores.md
    ├── ablations.md
    ├── injection_log.md
    └── error_analysis.md
output.csv                   # repo root
log.txt                      # repo root, gitignored, upload as transcript
```

---

## 10. Build order (do not reorder)

| # | Module | Gate before moving on |
|---|---|---|
| 1 | `loaders.py` + `models.py` | All 9 CSVs load into typed objects; schemas printed |
| 2 | `evaluation/score_samples.py` | Scorer exists **before** any engine code |
| 3 | `timeline.py` + `validator.py` | `is_safe` unit-tested on hand-built vectors |
| 4 | `metrics.py` | `amount_safe_to_pay` measured on 25 samples, no LLM |
| 5 | `candidates.py` + `ranker.py` | Full deterministic pipeline end-to-end |
| 6 | `schema_check.py` + `writer.py` | **A valid 250-row output.csv exists.** Ship-able. |
| 7 | `config.py` grid search | Sample accuracy maximised; config locked |
| 8 | `extractors.py` | Blank-amount events resolved from images |
| 9 | `amendments.py` + trust gate | `injection_log.md` populated |
| 10 | `explain.py` | Explanations cite real numbers |
| 11 | Full run + `usage_report.md` | Real instrumented token counters |
| 12 | `error_analysis.md`, `ablations.md`, zip | Submit with time to spare |

**Hard rule:** step 6 must be complete by the halfway point. A valid mediocre
submission beats a brilliant unfinished one.

---

## 11. Instrumentation for `usage_report.md`

Wrap every model call in a counter from hour one:

```python
USAGE = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "by_model": {}}
```

The report must cover: model providers and names, model calls, input and output
tokens, total and average tokens per request, estimated total and per-request cost.
Per-model **and** overall totals if you use more than one model. Numbers must
correspond to the final full-dataset run that produced `output.csv`.

Caching image and message extractions by ID is what makes these numbers good.

---

## 12. Non-negotiable rules

1. Never hardcode `request_id`-specific answers or test labels.
2. Never let untrusted message/image text influence a decision path.
3. Never invent income, expenses, payment options, or installment schedules.
4. Installment plans must match a supplied `payment_option_id` exactly.
5. Only flexible recurring expenses may be changed; maximum three.
6. Secrets from environment variables only; none in the zip.
7. Deterministic where possible; seed anything random.
8. `output.csv` goes in the repository root.
