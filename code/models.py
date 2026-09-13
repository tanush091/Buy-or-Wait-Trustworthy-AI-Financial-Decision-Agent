from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, List, Set, Tuple


def fmt_amount(x: float) -> str:
    """Plain amount for CSV fields: integers without decimals, otherwise two decimals."""
    if abs(x - round(x)) < 0.005:
        return str(int(round(x)))
    return f"{x:.2f}"


# ---------- Raw inputs ----------

@dataclass(frozen=True)
class Request:
    request_id: str
    user_id: str
    request_date: date
    request_type: str          # purchase|travel|education|family_transfer|debt_repayment|investment|housing|emergency_expense|other
    requested_amount: float    # in user's home_currency
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str

@dataclass(frozen=True)
class Profile:
    user_id: str
    home_currency: str                      # INR|ZAR|IDR|USD|EUR
    available_balance: float
    minimum_balance_to_keep: float
    financial_priorities: List[str]
    expense_categories_to_protect: List[str]
    expense_categories_user_is_willing_to_reduce: List[str]
    expense_categories_user_is_willing_to_stop: List[str]
    payment_methods_user_will_consider: Set[str]   # subset of {full_payment, partial_payment, installments}
    max_installment_months: Optional[int]

@dataclass(frozen=True)
class RawEvent:
    event_id: str
    user_id: str
    event_type: str            # expense|debt_payment|subscription|income|refund|investment_...
    description: str
    category: str
    direction: str             # debit|credit|non_cash
    amount: Optional[float]    # None = MUST be filled from a linked image
    currency: str
    event_date: date
    settlement_date: Optional[date]
    status: str                # settled|pending|failed|cancelled|scheduled|unrealized
    linked_event_id: Optional[str]
    flexibility: str           # fixed|reducible|stoppable|reducible_or_stoppable
    minimum_allowed_amount: Optional[float]

@dataclass(frozen=True)
class PaymentOption:
    payment_option_id: str
    request_id: str
    payment_method: str
    payment_amount: float
    number_of_payments: int
    first_payment_date: date
    payment_frequency_days: Optional[int]
    financing_fee: float
    total_payable_amount: float

@dataclass(frozen=True)
class Message:
    message_id: str
    user_id: Optional[str]
    request_id: Optional[str]
    related_event_id: Optional[str]
    sent_at: str
    source_type: str
    message_text: str

@dataclass(frozen=True)
class ImageRef:
    image_id: str              # file at dataset/media/images/<image_id>.png
    user_id: Optional[str]
    request_id: Optional[str]
    related_event_id: Optional[str]

@dataclass(frozen=True)
class ExchangeRate:
    rate_date: date
    from_currency: str
    to_currency: str
    rate: float

# ---------- Derived ----------

@dataclass
class Amendment:
    """A typed fact extracted from an untrusted message. Only these fields reach the engine."""
    message_id: str
    kind: str                    # salary_raise|salary_date|salary_next|salary_arrears|salary_first|salary_resume|
                                 # salary_on_date|salary_set|salary_base|income_end|income_once|rent_increase|bill_retry
    sent: date
    amount: Optional[float] = None
    extra: Optional[float] = None
    currency: Optional[str] = None
    effective: Optional[date] = None
    pct: Optional[float] = None
    event_id: Optional[str] = None

@dataclass
class Flow:
    day: date
    amount: float                # signed home currency: + inflow, - outflow
    label: str
    kind: str                    # pending|scheduled|recurring|salary|income|retry
    series: Optional[str] = None  # expense-series key, used by spending changes

@dataclass
class Series:
    """A recurring expense detected from history and projected over the horizon."""
    key: str
    category: str
    description: str
    last_event_id: str
    amount: float
    flexibility: str
    minimum_allowed: Optional[float]
    occurrences: List[date] = field(default_factory=list)
    amounts: List[float] = field(default_factory=list)   # per-occurrence amounts (empty = `amount` each time)

@dataclass
class UserState:
    flows: List[Flow]
    series: List[Series]
    notes: List[str] = field(default_factory=list)

@dataclass(frozen=True)
class SpendingChange:
    kind: str                    # "stop" | "reduce_to"
    event_id: str
    new_amount: Optional[float] = None     # required if kind == "reduce_to"

    def render(self) -> str:
        if self.kind == "reduce_to":
            return f"reduce_to:{self.event_id}:{fmt_amount(self.new_amount or 0.0)}"
        return f"stop:{self.event_id}"

@dataclass
class Candidate:
    method: str                  # full_payment|partial_payment|installments|wait|not_recommended
    plan: List[Tuple[date, float]]
    changes: List[SpendingChange]
    payment_option_id: Optional[str]
    total_paid: float
    savings: float = 0.0

@dataclass(frozen=True)
class Decision:
    request_id: str
    amount_safe_to_pay: float
    affordability_status: str
    recommended_payment_method: str
    payment_plan: str            # "YYYY-MM-DD:amount|..." or "none"
    earliest_date_for_full_payment: str   # "YYYY-MM-DD" or ""
    spending_changes_needed: str          # "stop:...|..." or "none"
    decision_explanation: str
