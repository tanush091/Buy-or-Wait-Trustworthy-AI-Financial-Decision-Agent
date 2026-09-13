"""Model comparison: rule parser vs Gemini models on messages, Gemini models vs verified image amounts.

    python code/evaluation/model_comparison.py [model ...]

Writes code/evaluation/model_comparison.md. Messages: agreement with the rule parser on
kind, amount and date (215 messages, batched). Images: exact-amount accuracy against the 16
amounts verified by hand. Results are cached, so re-runs are free.
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from code import llm
from code.amendments import RULES, SUSPICIOUS
from code.config import CACHE_DIR, EVALUATION_DIR, LLM_PRICES, MEDIA_DIR
from code.loaders import load_messages

DEFAULT_MODELS = ["gemini-2.5-flash", "gemini-3.5-flash"]


def rule_label(text: str) -> dict:
    if SUSPICIOUS.search(text):
        return {"kind": "no_effect"}
    for kind, pat, _, build in RULES:
        m = pat.search(text)
        if m:
            f = build(m)
            return {"kind": kind, "amount": f.get("amount"), "date": str(f["effective"]) if f.get("effective") else None,
                    "pct": f.get("pct")}
    return {"kind": "no_effect"}


def compare(models):
    msgs = load_messages()
    truth = {m.message_id: rule_label(m.message_text) for m in msgs}
    images = json.loads((CACHE_DIR / "images.json").read_text(encoding="utf-8"))
    lines = ["# Model comparison", "",
             "Rule parser vs LLM on all messages; LLM vs hand-verified amounts on all images.", "",
             "| Model | Message kind agreement | Amount/date agreement (where rule found one) | Scam/info messages kept no-effect | Image amount accuracy | Calls | Input tokens | Output tokens | Est. cost (USD) |",
             "|---|---|---|---|---|---|---|---|---|"]
    details = []
    for model in models:
        before = dict(llm.USAGE["by_model"].get(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0}))
        facts = llm.classify_messages(msgs, model=model, batch=75)
        kind_ok = val_ok = val_n = noeff_ok = noeff_n = 0
        for m in msgs:
            t, f = truth[m.message_id], facts.get(m.message_id, {})
            if f.get("kind") == t["kind"]:
                kind_ok += 1
            else:
                details.append(f"- {model} {m.message_id}: rule={t['kind']} llm={f.get('kind')} ({(f.get('reason') or '')[:90]})")
            if t["kind"] == "no_effect":
                noeff_n += 1
                noeff_ok += f.get("kind") == "no_effect"
            elif t.get("amount") is not None or t.get("date") or t.get("pct") is not None:
                val_n += 1
                amt_ok = t.get("amount") is None or (isinstance(f.get("amount"), (int, float)) and abs(f["amount"] - t["amount"]) < 0.01)
                date_ok = not t.get("date") or str(f.get("effective_date"))[:10] == t["date"]
                pct_ok = t.get("pct") is None or f.get("pct") == t["pct"]
                val_ok += amt_ok and date_ok and pct_ok
        img_ok = 0
        readings = llm.extract_images_batch({i: MEDIA_DIR / f"{i}.png" for i in images}, model=model)
        for image_id, rec in images.items():
            got = (readings.get(image_id) or {}).get("amount")
            if isinstance(got, (int, float)) and abs(got - rec["amount"]) < 0.01:
                img_ok += 1
            else:
                details.append(f"- {model} {image_id}: verified={rec['amount']} llm={got}")
        after = llm.USAGE["by_model"].get(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        calls = after["calls"] - before["calls"]
        tin = after["input_tokens"] - before["input_tokens"]
        tout = after["output_tokens"] - before["output_tokens"]
        price = LLM_PRICES.get(model)
        cost = f"{tin / 1e6 * price[0] + tout / 1e6 * price[1]:.4f}" if price else "n/a"
        lines.append(f"| {model} | {kind_ok}/{len(msgs)} ({100 * kind_ok / len(msgs):.1f}%) | {val_ok}/{val_n} | "
                     f"{noeff_ok}/{noeff_n} | {img_ok}/{len(images)} | {calls} | {tin} | {tout} | {cost} |")
        print(lines[-1])
    lines += ["", "Calls/tokens are for this comparison run only (0 when answers came from the cache).", "",
              "## Disagreements", ""] + (details or ["- none"])
    (EVALUATION_DIR / "model_comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    compare(sys.argv[1:] or DEFAULT_MODELS)
