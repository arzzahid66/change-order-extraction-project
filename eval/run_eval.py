"""Score the pipeline against samples/gold.

Reports, per extractor mode:
  * field accuracy (on fields the gold says are present)
  * false-fill rate (value invented where gold says null) - the dangerous error
  * calibration: accuracy per confidence bucket
  * auto-accept precision/coverage at the review threshold - the business metric:
    "of the fields we'd write to the ledger without a human, how many are right?"

usage: python eval/run_eval.py [--mode rules|llm] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from co_extract.confidence import same_value
from co_extract.normalize import norm_id, parse_date
from co_extract.pipeline import REVIEW_THRESHOLD, run

ROOT = Path(__file__).resolve().parents[1] / "samples"
META = {"n_line_items", "expect_review"}
BUCKETS = [(0.0, 0.5), (0.5, 0.8), (0.8, 0.9), (0.9, 1.01)]


def _norm(field: str, v):
    if v is None:
        return None
    if field == "date_issued":
        return parse_date(v)
    if field in ("reason", "status"):
        return getattr(v, "value", v)
    if field == "references":
        return sorted(r.upper().replace(" ", "") for r in v) or None
    return v


def evaluate(mode: str) -> dict:
    rows, doc_rows = [], []
    for gold_path in sorted((ROOT / "gold").glob("*.json")):
        doc = next((ROOT / "docs").glob(gold_path.stem + ".*"))
        result = run(doc, mode=mode)
        preds = {norm_id(c.change_order_number.value): c for c in result.change_orders}
        for g in json.loads(gold_path.read_text())["change_orders"]:
            co = preds.get(norm_id(g["change_order_number"]))
            if co is None and len(result.change_orders) == 1 and len(preds) == 1:
                co = result.change_orders[0]  # matched by position when the number itself was wrong
            doc_rows.append(dict(doc=doc.name, co=g["change_order_number"], found=co is not None,
                                 needs_review=co.needs_review if co else None,
                                 expect_review=g.get("expect_review", False),
                                 lines_ok=(len(co.line_items) == g["n_line_items"]) if co else False))
            for field, gv in g.items():
                if field in META:
                    continue
                fld = getattr(co, field) if co else None
                pv, conf = (_norm(field, fld.value), fld.confidence) if fld else (None, 0.0)
                gv = _norm(field, gv)
                if gv is None:
                    correct = pv is None
                elif pv is None:
                    correct = False
                else:
                    correct = bool(same_value(field, pv, gv))
                rows.append(dict(doc=doc.name, field=field, gold=gv, pred=pv, conf=conf,
                                 correct=correct, gold_present=gv is not None, pred_present=pv is not None))
    return summarize(rows, doc_rows, mode)


def summarize(rows: list[dict], doc_rows: list[dict], mode: str) -> dict:
    present = [r for r in rows if r["gold_present"]]
    absent = [r for r in rows if not r["gold_present"]]
    predicted = [r for r in rows if r["pred_present"]]
    auto = [r for r in predicted if r["conf"] >= REVIEW_THRESHOLD]

    buckets = []
    for lo, hi in BUCKETS:
        b = [r for r in predicted if lo <= r["conf"] < hi]
        buckets.append(dict(range=f"[{lo:.1f},{min(hi, 1):.1f})", n=len(b),
                            mean_conf=round(sum(r["conf"] for r in b) / len(b), 3) if b else None,
                            accuracy=round(sum(r["correct"] for r in b) / len(b), 3) if b else None))
    per_field = defaultdict(lambda: [0, 0])
    for r in present:
        per_field[r["field"]][0] += r["correct"]
        per_field[r["field"]][1] += 1

    return dict(
        mode=mode,
        field_accuracy=round(sum(r["correct"] for r in present) / len(present), 3),
        false_fill_rate=round(sum(r["pred_present"] for r in absent) / max(len(absent), 1), 3),
        auto_accept_precision=round(sum(r["correct"] for r in auto) / max(len(auto), 1), 3),
        auto_accept_coverage=round(len(auto) / max(len(present), 1), 3),
        review_flag_on_bad_docs=[d for d in doc_rows if d["expect_review"]],
        calibration=buckets,
        per_field={k: f"{c}/{n}" for k, (c, n) in sorted(per_field.items())},
        documents=doc_rows,
        errors=[r for r in rows if not r["correct"]],
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="rules", choices=["rules", "llm"])
    ap.add_argument("--json", type=Path)
    ap.add_argument("--errors", action="store_true", help="print every wrong field")
    args = ap.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    s = evaluate(args.mode)
    if args.json:
        args.json.write_text(json.dumps(s, indent=2, default=str))
    print(f"\n== mode: {s['mode']}  (review threshold {REVIEW_THRESHOLD})")
    for k in ("field_accuracy", "false_fill_rate", "auto_accept_precision", "auto_accept_coverage"):
        print(f"{k:<24}{s[k]}")
    print("\ncalibration (predicted fields):")
    for b in s["calibration"]:
        print(f"  conf {b['range']:<11} n={b['n']:<4} mean_conf={b['mean_conf']!s:<6} accuracy={b['accuracy']}")
    print("\nper field:", ", ".join(f"{k} {v}" for k, v in s["per_field"].items()))
    print("\ndocuments:")
    for d in s["documents"]:
        print(f"  {d['doc']:<30} CO {d['co']:<7} found={d['found']!s:<5} review={d['needs_review']!s:<5} "
              f"line_items_ok={d['lines_ok']}")
    if args.errors:
        print("\nerrors:")
        for r in s["errors"]:
            print(f"  {r['doc']:<30} {r['field']:<22} gold={r['gold']!s:<35} pred={r['pred']!s:<35} conf={r['conf']}")


if __name__ == "__main__":
    main()
