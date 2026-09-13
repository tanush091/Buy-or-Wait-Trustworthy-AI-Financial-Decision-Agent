"""Turn untrusted messages into typed amendments.

Messages are data, never instructions. Each message is matched against a fixed set of
patterns (English and Indonesian); a match yields numbers and dates only. A trust gate
checks the source type, the message date and basic sanity before anything reaches the
forecast. Instruction-like or scam content is logged and ignored.
"""
import re
from datetime import date
from typing import Callable, Dict, List, Optional, Tuple

from . import nli
from .models import Amendment, Message

CUR = r"(INR|IDR|ZAR|USD|EUR)"
NUM = r"([0-9][0-9,]*(?:\.[0-9]+)?)"
ISO = r"(\d{4}-\d{2}-\d{2})"
_MONTHS = ["january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december"]
LONG = r"(\d{1,2}) (" + "|".join(m.capitalize() for m in _MONTHS) + r") (\d{4})"

SUSPICIOUS = re.compile(
    r"release charge|processing charge|biaya pencairan|biaya pemrosesan|ignore (?:all|any|previous)"
    r"|disregard (?:the|all|previous)|system prompt|as an ai|you must (?:approve|recommend)|override the",
    re.I,
)

EMPLOYER = {"employer"}
PAYER_INFO = {"employer", "financial_service"}


def _n(s: str) -> float:
    return float(s.replace(",", ""))


def _iso(s: str) -> date:
    return date.fromisoformat(s)


def _long(d: str, m: str, y: str) -> date:
    return date(int(y), _MONTHS.index(m.lower()) + 1, int(d))


# (kind, pattern, allowed source types, builder(match) -> fields)
Rule = Tuple[str, re.Pattern, set, Callable[[re.Match], Dict]]
RULES: List[Rule] = [
    ("salary_raise", re.compile(rf"(?:salary has increased to|naik menjadi) {CUR} {NUM}.*?(?:applies from|berlaku mulai) {ISO}", re.I | re.S),
     EMPLOYER, lambda m: dict(currency=m[1], amount=_n(m[2]), effective=_iso(m[3]))),
    ("salary_date", re.compile(rf"(?:salary is now expected on|kini diperkirakan masuk pada) {ISO}", re.I),
     EMPLOYER, lambda m: dict(effective=_iso(m[1]))),
    ("salary_arrears", re.compile(rf"(?:regular salary for the next payroll is|untuk penggajian berikutnya adalah) {CUR} {NUM}.*?(?:adjustment of|sebesar) {CUR} {NUM}", re.I | re.S),
     EMPLOYER, lambda m: dict(currency=m[1], amount=_n(m[2]), extra=_n(m[4]))),
    ("salary_next", re.compile(rf"(?:next salary is reduced to|temporary monthly pay is|gaji bulanan sementara anda adalah) {CUR} {NUM}", re.I),
     EMPLOYER, lambda m: dict(currency=m[1], amount=_n(m[2]))),
    ("salary_resume", re.compile(rf"regular salary of {CUR} {NUM} resumes on {ISO}", re.I),
     EMPLOYER, lambda m: dict(currency=m[1], amount=_n(m[2]), effective=_iso(m[3]))),
    ("salary_first", re.compile(rf"(?:first salary|gaji pertama)\D*?{CUR} {NUM}.*?{ISO}", re.I | re.S),
     EMPLOYER, lambda m: dict(currency=m[1], amount=_n(m[2]), effective=_iso(m[3]))),
    ("salary_on_date", re.compile(rf"(?:your salary of|gaji sebesar) {CUR} {NUM} (?:is confirmed for|dikonfirmasi untuk) {ISO}", re.I),
     EMPLOYER, lambda m: dict(currency=m[1], amount=_n(m[2]), effective=_iso(m[3]))),
    ("salary_on_date", re.compile(rf"confirmed (?:a|the) {CUR} {NUM} salary (?:credit|payment) (?:for|on) (?:{ISO}|{LONG})", re.I),
     PAYER_INFO, lambda m: dict(currency=m[1], amount=_n(m[2]),
                                effective=_iso(m[3]) if m[3] else _long(m[4], m[5], m[6]))),
    ("salary_set", re.compile(rf"(?:remaining confirmed monthly salary is|sisa gaji bulanan yang dikonfirmasi adalah) {CUR} {NUM}", re.I),
     EMPLOYER, lambda m: dict(currency=m[1], amount=_n(m[2]))),
    ("salary_base", re.compile(rf"(?:confirmed base salary is|gaji pokok yang dikonfirmasi adalah) {CUR} {NUM}", re.I),
     EMPLOYER, lambda m: dict(currency=m[1], amount=_n(m[2]))),
    ("income_end", re.compile(r"employment has ended|hubungan kerja anda telah berakhir|seasonal contract has ended|kontrak musiman saat ini telah berakhir", re.I),
     EMPLOYER, lambda m: dict()),
    ("income_once", re.compile(rf"(?:approved an invoice payment of|menyetujui pembayaran faktur sebesar) {CUR} {NUM}.*?(?:expected on|diperkirakan pada) {ISO}", re.I | re.S),
     {"service_provider"}, lambda m: dict(currency=m[1], amount=_n(m[2]), effective=_iso(m[3]))),
    ("rent_increase", re.compile(rf"(?:increases monthly rent by|menaikkan biaya sewa bulanan sebesar) {NUM} ?%", re.I),
     {"service_provider"}, lambda m: dict(pct=_n(m[1]))),
    ("income_uncertain", re.compile(r"payout is still pending|pembayaran berikutnya dari \w+ masih tertunda", re.I),
     {"service_provider"}, lambda m: dict()),
    ("bill_retry", re.compile(r"previous debit attempt failed|bill is still outstanding", re.I),
     {"bank"}, lambda m: dict()),
]


SOURCES_BY_KIND: Dict[str, set] = {}
for _kind, _pat, _src, _build in RULES:
    SOURCES_BY_KIND.setdefault(_kind, set()).update(_src)
NEEDS_AMOUNT = {"salary_raise", "salary_next", "salary_arrears", "salary_resume", "salary_first",
                "salary_on_date", "salary_set", "salary_base", "income_once"}
NEEDS_DATE = {"salary_raise", "salary_date", "salary_resume", "salary_first", "salary_on_date", "income_once"}
CURRENCIES = {"INR", "IDR", "ZAR", "USD", "EUR"}
# LLM facts get a stricter sender list: salary facts only from employers, never third parties.
LLM_SOURCES: Dict[str, set] = {k: (v - {"financial_service"}) or v for k, v in SOURCES_BY_KIND.items()}
# Facts that would add money are accepted from the LLM only when the income is confirmed.
INCOME_UP = {"salary_raise", "salary_resume", "salary_first", "salary_on_date", "salary_arrears", "income_once"}
UNCONFIRMED = re.compile(
    r"\b(?:pending|submitted|awaiting|under review|to be reviewed|estimated?|estimate|claim(?:ed)?|processing|"
    r"unapproved|not (?:yet )?(?:approved|confirmed|credited)|provisional|expected to be approved|"
    r"menunggu|tertunda|belum|diproses|pengajuan|perkiraan)\b", re.I)
# Windfalls the spec says never to count before they settle (prizes, lotteries, refunds, bonuses...).
WINDFALL = re.compile(
    r"\b(?:prize|lottery|jackpot|lucky ?draw|sweepstakes?|giveaway|reward|cashback|refund|bonus|commission|"
    r"hadiah|undian|lotre|lotere|pengembalian dana|komisi)\b", re.I)
# One-off income from a non-employer is accepted only when it is a client / invoice payment.
INVOICE_WORDS = re.compile(r"\b(?:invoice|faktur|client|klien)\b", re.I)


def rule_matches(text: str) -> bool:
    return any(p.search(text or "") for _, p, _, _ in RULES)


def _numbers_in(text: str) -> List[float]:
    text = text or ""
    nums = [_n(x) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]
    # Indonesian / European thousands separators: 9.000.000 or 1.234,50
    for x in re.findall(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", text):
        nums.append(float(x.replace(".", "").replace(",", ".")))
    return nums


def gate_llm_fact(msg: Message, fact: dict, sent: date) -> Tuple[Optional[Amendment], str]:
    """Same trust gate as rule facts, plus: the model may read numbers, never invent them."""
    kind = fact.get("kind")
    if kind in (None, "no_effect"):
        return None, "no financial effect"
    if kind not in LLM_SOURCES:
        return None, f"unknown kind {kind}"
    if msg.source_type not in LLM_SOURCES[kind]:
        return None, f"untrusted source '{msg.source_type}' for {kind}"
    if kind in INCOME_UP and (fact.get("confirmed") is not True or UNCONFIRMED.search(msg.message_text or "")):
        # never count pending / submitted / estimated money until it is confirmed
        return None, "income not confirmed (pending, submitted or under-review wording)"
    text = msg.message_text or ""
    if kind in INCOME_UP and WINDFALL.search(text):
        return None, "windfall income (prize, refund, bonus or commission) is not counted until it settles"
    if kind == "income_once" and not INVOICE_WORDS.search(text):
        return None, "one-off income without invoice / client wording"
    amount, extra, cur, pct = fact.get("amount"), fact.get("extra"), fact.get("currency"), fact.get("pct")
    if kind in NEEDS_AMOUNT and (not isinstance(amount, (int, float)) or amount <= 0):
        return None, "missing amount"
    if cur is not None and cur not in CURRENCIES:
        return None, f"unknown currency {cur}"
    numbers = _numbers_in(msg.message_text)
    for v in (amount, extra, pct):
        if isinstance(v, (int, float)) and not any(abs(v - x) < 0.005 for x in numbers):
            return None, f"value {v} does not appear in the message"
    eff = None
    if kind in NEEDS_DATE:
        try:
            eff = date.fromisoformat(str(fact.get("effective_date"))[:10])
        except ValueError:
            return None, "missing date"
        if not -120 <= (eff - sent).days <= 200:
            return None, f"implausible date {eff}"
        # the date must be stated in this message (guards against a fact leaking from another
        # message in the same batch, even when the NLI model is not installed)
        if eff.isoformat() not in text and not any(abs(x - eff.day) < 0.005 for x in numbers):
            return None, f"date {eff} does not appear in the message"
    if kind == "rent_increase" and not (isinstance(pct, (int, float)) and 0 < pct <= 50):
        return None, "implausible rent change"
    if kind == "bill_retry" and not msg.related_event_id:
        return None, "no related event"
    # last check: the message must ENTAIL the fact (local NLI model), not merely contain its numbers
    entailed, why = nli.verify(msg.message_text, fact)
    if not entailed:
        return None, why
    return Amendment(message_id=msg.message_id, kind=kind, sent=sent, amount=amount, extra=extra,
                     currency=cur, effective=eff, pct=pct, event_id=msg.related_event_id), "accepted"


def parse_message(msg: Message, request_date: date, llm_fact: Optional[dict] = None) -> Tuple[List[Amendment], List[str]]:
    """Return gated amendments and trust-gate log lines for one message."""
    log: List[str] = []
    sent = date.fromisoformat(msg.sent_at[:10]) if msg.sent_at else request_date
    text = msg.message_text or ""
    if sent > request_date:
        return [], [f"{msg.message_id}: ignored, sent after request date"]
    if SUSPICIOUS.search(text):
        return [], [f"{msg.message_id}: rejected, instruction-like or scam content ({msg.source_type})"]
    out: List[Amendment] = []
    matched = False
    for kind, pat, sources, build in RULES:
        m = pat.search(text)
        if not m:
            continue
        matched = True
        if msg.source_type not in sources:
            log.append(f"{msg.message_id}: rejected {kind}, untrusted source '{msg.source_type}'")
            continue
        fields = build(m)
        if fields.get("amount") is not None and fields["amount"] <= 0:
            log.append(f"{msg.message_id}: rejected {kind}, non-positive amount")
            continue
        eff: Optional[date] = fields.get("effective")
        if eff is not None and not (-120 <= (eff - sent).days <= 200):
            log.append(f"{msg.message_id}: rejected {kind}, implausible date {eff}")
            continue
        if kind == "bill_retry" and not msg.related_event_id:
            continue
        out.append(Amendment(message_id=msg.message_id, kind=kind, sent=sent,
                             event_id=msg.related_event_id, **fields))
        break  # one fact per message
    if not matched and llm_fact is not None:
        # wording the rules do not know: use the LLM reading, through the same gate
        a, why = gate_llm_fact(msg, llm_fact, sent)
        if a is not None:
            out.append(a)
            log.append(f"{msg.message_id}: LLM fact '{a.kind}' accepted ({msg.source_type})")
        elif why != "no financial effect":
            log.append(f"{msg.message_id}: LLM fact rejected, {why}")
    return out, log


def parse_messages(messages: List[Message], request_date: date,
                   llm_facts: Optional[Dict[str, dict]] = None) -> Tuple[List[Amendment], List[str]]:
    amendments: List[Amendment] = []
    log: List[str] = []
    for msg in sorted(messages, key=lambda m: (m.sent_at, m.message_id)):
        a, l = parse_message(msg, request_date, (llm_facts or {}).get(msg.message_id))
        amendments.extend(a)
        log.extend(l)
    return amendments, log
