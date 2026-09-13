"""Self-contained HTML dashboard of the final run: accuracy, decision mix, risk and every decision.

    python code/evaluation/dashboard.py          # -> code/evaluation/dashboard.html
    python code/main.py --insights               # output.csv, then audit + risk flags + dashboard

It refreshes decision_audit.json and risk_flags.csv first. No external scripts, fonts or network
calls: open the file in any browser.
"""
import argparse
import json
import re
import sys
from collections import Counter
from html import escape
from pathlib import Path
from typing import Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from code.audit import RISK_LEVELS, risk_row
from code.config import EVALUATION_DIR
from code.evaluation import decision_audit
from code.security import sanitize

FIELD_LABELS = [("affordability_status_pct", "Affordability status"),
                ("recommended_payment_method_pct", "Payment method"),
                ("payment_plan_pct", "Payment plan"),
                ("earliest_date_pct", "Earliest full-payment date"),
                ("spending_changes_pct", "Spending changes"),
                ("amount_safe_to_pay_pct", "Exact safe amount")]
ACC_COLUMNS = [("in_sample", "Public samples (in-sample)"), ("5-fold", "5-fold cross-validation"),
               ("leave-one-out", "Leave-one-out")]
STATUS_ORDER = ["affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"]
METHOD_ORDER = ["full_payment", "partial_payment", "installments", "wait", "not_recommended"]

CSS = """
:root{--bg:#f6f6f3;--card:#ffffff;--ink:#1c1c1a;--muted:#686862;--line:#e2e1da;--accent:#2d6cdf;
--ok:#1e8449;--plan:#2d6cdf;--later:#7b5bc4;--bad:#c0392b;--warn:#b7791f}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#141413;--card:#1e1e1c;--ink:#ecebe5;
--muted:#a4a39c;--line:#353430;--accent:#6d9cff;--ok:#3fa56d;--plan:#4f82e8;--later:#8f74dc;--bad:#d4574d;--warn:#c98e2c}}
:root[data-theme="dark"]{--bg:#141413;--card:#1e1e1c;--ink:#ecebe5;--muted:#a4a39c;--line:#353430;--accent:#6d9cff;
--ok:#3fa56d;--plan:#4f82e8;--later:#8f74dc;--bad:#d4574d;--warn:#c98e2c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1200px;margin:0 auto;padding:28px 16px 56px}
h1{font-size:26px;margin:0 0 4px}
h2{font-size:17px;margin:0 0 12px}
.muted,small{color:var(--muted)}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-top:16px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-top:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.big{font-size:28px;font-weight:700}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr));gap:0 16px}
.bar-row{display:grid;grid-template-columns:minmax(90px,150px) 1fr 40px;align-items:center;gap:8px;margin:6px 0}
.bar-label{text-transform:capitalize}
.bar-track{background:var(--line);border-radius:6px;height:12px;overflow:hidden}
.bar{display:block;height:100%}
.st-affordable_now,.m-full_payment,.rk-comfortable{background:var(--ok)}
.st-affordable_with_plan,.m-installments,.m-partial_payment{background:var(--plan)}
.st-affordable_later,.m-wait,.rk-moderate{background:var(--later)}
.st-not_affordable,.m-not_recommended,.rk-blocked{background:var(--bad)}
.rk-tight{background:var(--warn)}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;color:#fff;font-size:12px;white-space:nowrap}
.table-wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%}
th,td{border-bottom:1px solid var(--line);padding:8px 6px;text-align:left;vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600}
td.num,th.num{text-align:right;white-space:nowrap}
td.id{white-space:nowrap}
details summary{cursor:pointer}
ul.opts{margin:6px 0;padding-left:18px}
ul.opts li.chosen{color:var(--ok);font-weight:600}
ul.opts li.no{color:var(--muted)}
.controls{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}
input,select{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink)}
input{flex:1;min-width:min(100%,180px)}
.flow{display:flex;flex-wrap:wrap;gap:8px}
.step{flex:1 1 150px;border:1px solid var(--line);border-radius:10px;padding:10px;background:var(--bg)}
.step b{display:block;margin-bottom:2px}
code{font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
"""

JS = """
var q=document.getElementById('q'),fs=document.getElementById('fs'),fr=document.getElementById('fr');
function applyFilter(){var t=q.value.trim().toLowerCase(),s=fs.value,r=fr.value,n=0;
document.querySelectorAll('#rows tr').forEach(function(tr){
var ok=(!t||tr.textContent.toLowerCase().indexOf(t)>=0)&&(!s||tr.dataset.status===s)&&(!r||tr.dataset.risk===r);
tr.hidden=!ok;if(ok){n++;}});
document.getElementById('shown').textContent=n;}
[q,fs,fr].forEach(function(e){e.addEventListener('input',applyFilter);});
"""

ARCH = """<div class="flow">
<div class="step"><b>1 &middot; Load</b><span class="muted">Typed rows from every dataset CSV; malformed rows are skipped.</span></div>
<div class="step"><b>2 &middot; Read evidence</b><span class="muted">Rules first; unknown messages to Gemini; trust gate and local NLI check; images by SHA-256 cache.</span></div>
<div class="step"><b>3 &middot; Resolve</b><span class="muted">FX at settlement date; pending debits reserved; pending credits ignored; recurring expenses and salary detected.</span></div>
<div class="step"><b>4 &middot; 90-day ledger</b><span class="muted">Daily balance; safe amount today and earliest safe full-payment date.</span></div>
<div class="step"><b>5 &middot; Candidates</b><span class="muted">Full, supplied installments, partial, wait; up to three spending changes.</span></div>
<div class="step"><b>6 &middot; Rank</b><span class="muted">Safety filter, then the spec's six-step order.</span></div>
<div class="step"><b>7 &middot; Explain &amp; check</b><span class="muted">Grounded explanation; schema and consistency checks.</span></div>
</div>"""


def esc(x) -> str:
    return escape(str(x), quote=True)


def load_accuracy(eval_dir: Path = EVALUATION_DIR) -> Dict[str, dict]:
    """cv_scores.json from cross_validate.py; falls back to sample_scores.md for in-sample scores."""
    acc: Dict[str, dict] = {}
    cv = eval_dir / "cv_scores.json"
    if cv.exists():
        try:
            acc = json.loads(cv.read_text(encoding="utf-8"))
        except ValueError:
            acc = {}
    md = eval_dir / "sample_scores.md"
    if "in_sample" not in acc and md.exists():
        acc["in_sample"] = {k: float(v) for k, v in re.findall(r"- (\w+): ([\d.]+)%", md.read_text(encoding="utf-8"))}
    return acc


def _pct(v: Optional[float]) -> str:
    return "&ndash;" if v is None else f"{v:.0f}%"


def _money(x) -> str:
    return "" if x is None or x == "" else f"{float(x):,.2f}"


def _bars(counts: Counter, order: List[str], prefix: str) -> str:
    total = sum(counts.values()) or 1
    out = []
    for k in order + sorted(k for k in counts if k not in order):
        n = counts.get(k, 0)
        label, css, width = esc(k.replace("_", " ")), esc(f"{prefix}-{k}"), 100.0 * n / total
        out.append(f'<div class="bar-row"><span class="bar-label">{label}</span><span class="bar-track">'
                   f'<span class="bar {css}" style="width:{width:.1f}%"></span></span><span class="bar-n">{n}</span></div>')
    return "".join(out)


def _accuracy(acc: Dict[str, dict]) -> str:
    cols = [(k, label) for k, label in ACC_COLUMNS if k in acc]
    if not cols:
        return '<p class="muted">Run <code>python code/evaluation/cross_validate.py</code> to add accuracy.</p>'
    head = "".join(f"<th class=\"num\">{esc(label)}</th>" for _, label in cols)
    body = []
    for key, label in FIELD_LABELS:
        cells = "".join(f"<td class=\"num\">{_pct(acc[k].get(key))}</td>" for k, _ in cols)
        body.append(f"<tr><td>{esc(label)}</td>{cells}</tr>")
    rows = "".join(body)
    return f'<div class="table-wrap"><table><thead><tr><th>Field</th>{head}</tr></thead><tbody>{rows}</tbody></table></div>'


def _row(a: dict) -> str:
    d, risk = a["decision"], risk_row(a)
    status, method, level = d["affordability_status"], d["recommended_payment_method"], risk["risk_level"]
    items = []
    for o in a.get("options", []):
        css = "chosen" if o["chosen"] else ("ok" if o["eligible"] and o["safe"] else "no")
        name, kind, why = esc(o["option"]), esc(o["method"].replace("_", " ")), esc(o["reason"])
        items.append(f'<li class="{css}"><b>{name}</b> {kind}: {why}</li>')
    if a.get("fallback"):
        items.append(f'<li class="no">records could not be verified: {esc(a["fallback"])}</li>')
    facts = a.get("evidence", {}).get("facts_used", [])
    fact_text = ", ".join(f"{x['message_id']} ({x['kind']}, {x['source']})" for x in facts) or "none"
    expl = d["decision_explanation"]
    short = expl if len(expl) <= 90 else expl[:88].rstrip() + "..."
    opts = "".join(items)
    rid, rtype, cur = esc(a["request_id"]), esc(a.get("request_type", "")), esc(a.get("currency") or "")
    amount, safe = _money(a["requested_amount"]), _money(d["amount_safe_to_pay"])
    st_label, m_label = esc(status.replace("_", " ")), esc(method.replace("_", " "))
    low, low_date, buffer = _money(risk["lowest_projected_balance"]), esc(risk["lowest_balance_date"]), esc(risk["buffer_days"])
    plan, changes = esc(d["payment_plan"]), esc(d["spending_changes_needed"])
    return (f'<tr data-status="{esc(status)}" data-risk="{esc(level)}">'
            f'<td class="id">{rid}<br><small>{rtype}</small></td>'
            f'<td class="num">{cur} {amount}</td><td class="num">{safe}</td>'
            f'<td><span class="pill st-{esc(status)}">{st_label}</span></td><td>{m_label}</td>'
            f'<td><span class="pill rk-{esc(level)}">{esc(level)}</span></td><td class="num">{buffer}</td>'
            f'<td class="num">{low}<br><small>{low_date}</small></td>'
            f'<td><details><summary>{esc(short)}</summary><p>{esc(expl)}</p><ul class="opts">{opts}</ul>'
            f'<p class="muted">Plan: {plan} &middot; Changes: {changes} &middot; Message facts used: {esc(fact_text)}</p>'
            f'</details></td></tr>')


def render(audits: List[dict], accuracy: Dict[str, dict]) -> str:
    n = len(audits)
    status = Counter(a["decision"]["affordability_status"] for a in audits)
    method = Counter(a["decision"]["recommended_payment_method"] for a in audits)
    flags = [risk_row(a) for a in audits]
    risk = Counter(f["risk_level"] for f in flags)
    completable = n - status.get("not_affordable", 0)
    tight = sorted((f for f in flags if f["risk_level"] == "tight"), key=lambda f: f["buffer_days"])
    tight_items = "".join(
        f"<li><b>{esc(f['request_id'])}</b> {esc(f['recommended_payment_method'].replace('_', ' '))}: "
        f"{esc(f['buffer_days'])} days of spending above the minimum</li>" for f in tight[:10]) or "<li>none</li>"
    cards = "".join(f'<div class="card"><div class="big">{v}</div><div class="muted">{esc(t)}</div></div>' for v, t in (
        (n, "requests decided"), (completable, "can be completed (now, with a plan, or later)"),
        (status.get("not_affordable", 0), "not affordable within 90 days"),
        (risk.get("tight", 0), "tight plans (under 7 days of spending cushion)")))
    status_opts = "".join(f'<option value="{esc(s)}">{esc(s.replace("_", " "))}</option>' for s in STATUS_ORDER)
    risk_opts = "".join(f'<option value="{esc(r)}">{esc(r)}</option>' for r in RISK_LEVELS)
    acc_html = _accuracy(accuracy)
    status_bars = _bars(status, STATUS_ORDER, "st")
    method_bars = _bars(method, METHOD_ORDER, "m")
    risk_bars = _bars(risk, list(RISK_LEVELS), "rk")
    rows = "".join(_row(a) for a in audits)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Buy or Wait? Decision Dashboard</title><style>{CSS}</style></head>
<body><main>
<h1>Buy or Wait? Decision Dashboard</h1>
<p class="muted">{n} requests, decided by the deterministic engine that wrote output.csv. Risk measures the cushion
left above the minimum balance at the forecast's lowest point, in days of the user's normal forecast spending.</p>
<div class="cards">{cards}</div>
<section><h2>Accuracy against the published sample answers</h2>{acc_html}
<p class="muted">In-sample scores use the 25 public samples the settings were chosen on. The cross-validated columns
choose the settings on part of the samples and score the rest: the honest estimate for unseen requests.</p></section>
<div class="grid2">
<section><h2>Affordability status</h2>{status_bars}</section>
<section><h2>Recommended method</h2>{method_bars}</section>
<section><h2>Risk level</h2>{risk_bars}
<p class="muted">Tight: under 7 days of normal spending above the minimum; moderate: under 30 days; blocked: no safe plan.</p></section>
<section><h2>Tightest recommended plans</h2><ul>{tight_items}</ul></section>
</div>
<section><h2>How a decision is made</h2>{ARCH}</section>
<section><h2>Every decision</h2>
<div class="controls"><input id="q" placeholder="Search request, method, status, explanation" aria-label="Search">
<select id="fs" aria-label="Status"><option value="">All statuses</option>{status_opts}</select>
<select id="fr" aria-label="Risk"><option value="">All risk levels</option>{risk_opts}</select></div>
<p class="muted"><span id="shown">{n}</span> shown. Open an explanation to see every option and why it was chosen or rejected.</p>
<div class="table-wrap"><table><thead><tr><th>Request</th><th class="num">Amount</th><th class="num">Safe today</th>
<th>Status</th><th>Method</th><th>Risk</th><th class="num">Cushion (days)</th><th class="num">Lowest balance</th>
<th>Why</th></tr></thead><tbody id="rows">{rows}</tbody></table></div></section>
</main><script>{JS}</script></body></html>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the decision dashboard (refreshes the audit first).")
    ap.add_argument("--requests", type=Path, default=None)
    ap.add_argument("--output", type=Path, default=EVALUATION_DIR / "dashboard.html")
    args = ap.parse_args(argv)
    audits = decision_audit.build(args.requests)
    decision_audit.summarise(audits, args.requests)
    args.output.write_text(sanitize(render(audits, load_accuracy())), encoding="utf-8")
    print(f"Dashboard -> {args.output}")


if __name__ == "__main__":
    main()
