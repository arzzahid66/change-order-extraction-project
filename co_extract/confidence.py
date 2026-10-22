"""Field-level confidence from observable signals, not model self-report.

LLM-stated confidence is poorly calibrated, so each field's score is a
logistic combination of things we can check:

  grounded      the evidence quote really occurs in the source text
  value_in_ev   the normalized value can be re-read from that quote
  agreement     an independent extractor (regex) found the same value
  arithmetic    money fields reconcile (lines -> subtotal -> total)
  flagged       the model said it was unsure / a validation rule fired
  scanned       no reliable text layer to verify against (smaller penalty once
                vision OCR has produced a transcript we can ground against)

Weights are hand-set priors; with labelled production data they would be
refit by logistic regression (that is what eval/run_eval.py measures).
"""
from __future__ import annotations

import math
import re
from difflib import SequenceMatcher
from typing import Any

from .normalize import norm_id, norm_text, parse_date, parse_money

BASE = {"llm": 0.80, "rules": 0.72, "derived": 0.62}
W = {
    "grounded": 1.0, "ungrounded": -2.5,
    "value_in_ev": 0.8, "value_not_in_ev": -1.2,
    "agree": 1.3, "disagree": -1.6,
    "arith_ok": 1.5, "arith_bad": -2.0,
    "uncertain": -1.4, "issue": -1.0, "scanned": -0.6, "ocr": -0.3,
}
UNCHECKABLE = {"reason", "status", "description"}  # classifications / summaries


def _logit(p: float) -> float:
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def grounded(evidence: str | None, source_text: str) -> bool | None:
    if not evidence or not source_text.strip():
        return None
    ev, src = norm_text(evidence), norm_text(source_text)
    if ev in src:
        return True
    # Fuzzy text match tolerates whitespace/OCR drift in words, but every number
    # in the quote must appear verbatim: "$2,760" vs "$2,706" is a 1-char diff.
    src_nums = set(_numbers(source_text))
    if any(n not in src_nums for n in _numbers(evidence)):
        return False
    m = SequenceMatcher(None, ev, src, autojunk=False).find_longest_match(0, len(ev), 0, len(src))
    return m.size / max(len(ev), 1) >= 0.8


def _numbers(s: str) -> list[str]:
    return [n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", s)]


def same_value(field: str, a: Any, b: Any) -> bool | None:
    if a is None or b is None:
        return None
    if isinstance(a, float) or isinstance(b, float):
        return abs(float(a) - float(b)) <= 0.01
    if field == "change_order_number":
        return norm_id(a) == norm_id(b)
    if isinstance(a, (int, list)) or hasattr(a, "isoformat"):
        return a == b
    a, b = norm_text(str(a)), norm_text(str(b))
    return a in b or b in a or SequenceMatcher(None, a, b).ratio() >= 0.85


def value_in_evidence(field: str, value: Any, evidence: str | None) -> bool | None:
    if value is None or not evidence or field in UNCHECKABLE:
        return None
    if isinstance(value, float):
        nums = [parse_money(t) for t in re.findall(r"\(?-?\$?[\dOolIS][\dOolIS,]*(?:\.\d+)?\)?(?:\s*CR)?", evidence)]
        return any(n is not None and abs(abs(n) - abs(value)) <= 0.01 for n in nums)
    if hasattr(value, "isoformat"):
        return parse_date(evidence) == value
    if isinstance(value, int):
        return str(abs(value)) in evidence or (value == 0 and "no" in evidence.lower())
    if isinstance(value, list):
        return all(norm_text(v) in norm_text(evidence) for v in value)
    if field == "change_order_number":
        return norm_id(value) in (norm_id(evidence) or "")
    return same_value(field, value, evidence) or norm_text(str(value)) in norm_text(evidence)


def score(
    *, field: str, value: Any, source: str, evidence: str | None, source_text: str,
    other_value: Any = None, has_other: bool = False, arithmetic_ok: bool | None = None,
    uncertain: bool = False, n_issues: int = 0, scanned: bool = False, ocr: bool = False,
) -> tuple[float, list[str]]:
    if value is None or value == [] or value == "":
        return 0.0, []
    x, flags = _logit(BASE[source]), []
    if source == "rules" and field in UNCHECKABLE:
        # A regex keyword hit is weak evidence for a category.
        x -= 1.0
        flags.append("keyword_classification")

    if source != "derived":
        g = grounded(evidence, source_text)
        if g is True:
            x += W["grounded"]
        elif g is False and (ocr or not scanned):
            x += W["ungrounded"]
            flags.append("evidence_not_in_source")
        v = value_in_evidence(field, value, evidence)
        if v is True:
            x += W["value_in_ev"]
        elif v is False:
            x += W["value_not_in_ev"]
            flags.append("value_not_in_evidence")

    if has_other:
        agree = same_value(field, value, other_value)
        if agree is True:
            x += W["agree"]
        elif agree is False:
            x += W["disagree"]
            flags.append("extractors_disagree")

    if arithmetic_ok is True:
        x += W["arith_ok"]
    elif arithmetic_ok is False:
        x += W["arith_bad"]
        flags.append("arithmetic_mismatch")
    if uncertain:
        x += W["uncertain"]
        flags.append("model_uncertain")
    x += W["issue"] * n_issues
    if scanned and ocr:
        x += W["ocr"]
        flags.append("ocr_source")
    elif scanned:
        x += W["scanned"]
        flags.append("scanned_source")
    return round(_sigmoid(x), 3), flags
