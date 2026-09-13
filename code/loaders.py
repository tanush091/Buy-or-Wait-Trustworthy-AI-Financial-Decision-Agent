import csv
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional
import pandas as pd

from .config import DATASET_DIR
from .models import (
    Request,
    Profile,
    RawEvent,
    PaymentOption,
    Message,
    ImageRef,
    ExchangeRate,
    Decision,
)

def parse_date(d_str: Optional[str]) -> Optional[date]:
    if not d_str or pd.isna(d_str):
        return None
    d_str = str(d_str).strip()
    if not d_str:
        return None
    return date.fromisoformat(d_str[:10])

def parse_pipe_list(s: Optional[str]) -> List[str]:
    if not s or pd.isna(s):
        return []
    s = str(s).strip()
    if not s or s.lower() == "none":
        return []
    return [item.strip() for item in s.split("|") if item.strip()]

def load_requests(csv_path: Optional[Path] = None) -> List[Request]:
    path = csv_path or (DATASET_DIR / "requests.csv")
    df = pd.read_csv(path)
    requests = []
    for _, row in df.iterrows():
        req = Request(
            request_id=str(row["request_id"]).strip(),
            user_id=str(row["user_id"]).strip(),
            request_date=parse_date(row["request_date"]),
            request_type=str(row["request_type"]).strip(),
            requested_amount=float(row["requested_amount"]),
            desired_completion_date=parse_date(row["desired_completion_date"]),
            allows_partial_payment=str(row["allows_partial_payment"]).strip().lower() in ("true", "1"),
            request_text=str(row["request_text"]) if not pd.isna(row["request_text"]) else "",
        )
        requests.append(req)
    return requests

def load_sample_requests(csv_path: Optional[Path] = None) -> List[tuple[Request, Decision]]:
    path = csv_path or (DATASET_DIR / "sample_requests.csv")
    df = pd.read_csv(path)
    pairs = []
    for _, row in df.iterrows():
        req = Request(
            request_id=str(row["request_id"]).strip(),
            user_id=str(row["user_id"]).strip(),
            request_date=parse_date(row["request_date"]),
            request_type=str(row["request_type"]).strip(),
            requested_amount=float(row["requested_amount"]),
            desired_completion_date=parse_date(row["desired_completion_date"]),
            allows_partial_payment=str(row["allows_partial_payment"]).strip().lower() in ("true", "1"),
            request_text=str(row["request_text"]) if not pd.isna(row["request_text"]) else "",
        )
        earliest = str(row["earliest_date_for_full_payment"]).strip() if not pd.isna(row["earliest_date_for_full_payment"]) else ""
        if earliest.lower() == "nan":
            earliest = ""
        dec = Decision(
            request_id=str(row["request_id"]).strip(),
            amount_safe_to_pay=float(row["amount_safe_to_pay"]),
            affordability_status=str(row["affordability_status"]).strip(),
            recommended_payment_method=str(row["recommended_payment_method"]).strip(),
            payment_plan=str(row["payment_plan"]).strip(),
            earliest_date_for_full_payment=earliest,
            spending_changes_needed=str(row["spending_changes_needed"]).strip(),
            decision_explanation=str(row["decision_explanation"]).strip() if not pd.isna(row["decision_explanation"]) else "",
        )
        pairs.append((req, dec))
    return pairs

def load_profiles(csv_path: Optional[Path] = None) -> Dict[str, Profile]:
    path = csv_path or (DATASET_DIR / "financial_profiles.csv")
    df = pd.read_csv(path)
    profiles = {}
    for _, row in df.iterrows():
        user_id = str(row["user_id"]).strip()
        max_inst = row.get("max_installment_months")
        max_inst_val = int(max_inst) if pd.notna(max_inst) and str(max_inst).strip() != "" else None
        
        methods = parse_pipe_list(row.get("payment_methods_user_will_consider"))
        
        prof = Profile(
            user_id=user_id,
            home_currency=str(row["home_currency"]).strip(),
            available_balance=float(row["current_available_balance"]),
            minimum_balance_to_keep=float(row["minimum_balance_to_keep"]),
            financial_priorities=parse_pipe_list(row.get("financial_priorities")),
            expense_categories_to_protect=parse_pipe_list(row.get("expense_categories_to_protect")),
            expense_categories_user_is_willing_to_reduce=parse_pipe_list(row.get("expense_categories_user_is_willing_to_reduce")),
            expense_categories_user_is_willing_to_stop=parse_pipe_list(row.get("expense_categories_user_is_willing_to_stop")),
            payment_methods_user_will_consider=set(methods),
            max_installment_months=max_inst_val,
        )
        profiles[user_id] = prof
    return profiles

def load_financial_events(csv_path: Optional[Path] = None) -> List[RawEvent]:
    path = csv_path or (DATASET_DIR / "financial_events.csv")
    df = pd.read_csv(path)
    events = []
    for _, row in df.iterrows():
        amt = row.get("amount")
        amt_val = float(amt) if pd.notna(amt) else None
        
        min_amt = row.get("minimum_allowed_amount")
        min_amt_val = float(min_amt) if pd.notna(min_amt) else None
        
        linked = row.get("linked_event_id")
        linked_val = str(linked).strip() if pd.notna(linked) and str(linked).strip() != "" else None
        
        try:
            ev = RawEvent(
                event_id=str(row["event_id"]).strip(),
                user_id=str(row["user_id"]).strip(),
                event_type=str(row["event_type"]).strip(),
                description=str(row["description"]).strip(),
                category=str(row["category"]).strip(),
                direction=str(row["direction"]).strip(),
                amount=amt_val,
                currency=str(row["currency"]).strip(),
                event_date=parse_date(row["event_date"]),
                settlement_date=parse_date(row.get("settlement_date")),
                status=str(row["status"]).strip(),
                linked_event_id=linked_val,
                flexibility=str(row.get("flexibility", "fixed")).strip() if pd.notna(row.get("flexibility")) else "fixed",
                minimum_allowed_amount=min_amt_val,
            )
        except (ValueError, TypeError, KeyError) as exc:
            print(f"  ! skipped malformed event row {row.get('event_id')}: {exc}")
            continue
        if ev.event_date is None:
            print(f"  ! skipped event {ev.event_id}: no event_date")
            continue
        events.append(ev)
    return events

def load_payment_options(csv_path: Optional[Path] = None) -> Dict[str, List[PaymentOption]]:
    path = csv_path or (DATASET_DIR / "request_payment_options.csv")
    df = pd.read_csv(path)
    options: Dict[str, List[PaymentOption]] = {}
    for _, row in df.iterrows():
        req_id = str(row["request_id"]).strip()
        freq = row.get("payment_frequency_days")
        freq_val = int(freq) if pd.notna(freq) and str(freq).strip() != "" else None
        
        try:
            n = int(row["number_of_payments"])
            amount = float(row["payment_amount"])
            total = row.get("total_payable_amount")
            fee = row.get("financing_fee")
            opt = PaymentOption(
                payment_option_id=str(row["payment_option_id"]).strip(),
                request_id=req_id,
                payment_method=str(row["payment_method"]).strip(),
                payment_amount=amount,
                number_of_payments=n,
                first_payment_date=parse_date(row["first_payment_date"]),
                payment_frequency_days=freq_val,
                financing_fee=float(fee) if pd.notna(fee) else 0.0,
                total_payable_amount=float(total) if pd.notna(total) else round(amount * n, 2),
            )
        except (ValueError, TypeError, KeyError) as exc:
            print(f"  ! skipped malformed payment option {row.get('payment_option_id')}: {exc}")
            continue
        if opt.first_payment_date is None:
            print(f"  ! skipped payment option {opt.payment_option_id}: no first_payment_date")
            continue
        options.setdefault(req_id, []).append(opt)
    return options

def load_exchange_rates(csv_path: Optional[Path] = None) -> Dict[tuple[date, str, str], float]:
    path = csv_path or (DATASET_DIR / "exchange_rates.csv")
    df = pd.read_csv(path)
    rates = {}
    for _, row in df.iterrows():
        d = parse_date(row["rate_date"])
        f_cur = str(row["from_currency"]).strip()
        t_cur = str(row["to_currency"]).strip()
        r = float(row["rate"])
        rates[(d, f_cur, t_cur)] = r
    return rates

def load_messages(csv_path: Optional[Path] = None) -> List[Message]:
    path = csv_path or (DATASET_DIR / "messages.csv")
    df = pd.read_csv(path)
    messages = []
    for _, row in df.iterrows():
        u = row.get("user_id")
        r = row.get("request_id")
        e = row.get("related_event_id")
        msg = Message(
            message_id=str(row["message_id"]).strip(),
            user_id=str(u).strip() if pd.notna(u) else None,
            request_id=str(r).strip() if pd.notna(r) else None,
            related_event_id=str(e).strip() if pd.notna(e) else None,
            sent_at=str(row["sent_at"]).strip() if pd.notna(row["sent_at"]) else "",
            source_type=str(row["source_type"]).strip(),
            message_text=str(row["message_text"]) if pd.notna(row["message_text"]) else "",
        )
        messages.append(msg)
    return messages

def load_images(csv_path: Optional[Path] = None) -> List[ImageRef]:
    path = csv_path or (DATASET_DIR / "images.csv")
    df = pd.read_csv(path)
    images = []
    for _, row in df.iterrows():
        u = row.get("user_id")
        r = row.get("request_id")
        e = row.get("related_event_id")
        img = ImageRef(
            image_id=str(row["image_id"]).strip(),
            user_id=str(u).strip() if pd.notna(u) else None,
            request_id=str(r).strip() if pd.notna(r) else None,
            related_event_id=str(e).strip() if pd.notna(e) else None,
        )
        images.append(img)
    return images
