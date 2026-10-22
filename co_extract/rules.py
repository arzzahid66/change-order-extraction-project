"""Deterministic regex extractor.

Two jobs: (1) a zero-cost offline fallback, (2) an independent second opinion
whose agreement with the LLM is a confidence signal. It is deliberately
precision-oriented: it returns None rather than guessing.
"""
from __future__ import annotations

import re

from .normalize import parse_date, parse_days, parse_money

Raw = dict  # field -> {"value": ..., "evidence": str}

# Money must carry a "$" or cents, so "(10%)" or "Bldg 4" are never read as dollars.
_D = r"[0-9OolIS]"
_MONEY = rf"\(?-?(?:\$\s*{_D}[0-9OolIS,]*(?:\.{_D}{{2}})?|{_D}[0-9OolIS,]*\.{_D}{{2}})\)?(?:\s*CR\b)?"
_SEP = r"\s*[:#.\-]*\s*"
_L = r"(?im)^\s*"  # labels sit at the start of a line on forms
_TEXT = r"([^\n|]{2,80}?)(?:\s{2,}|\s*$)"  # stop at a column gap
_DATE = (
    r"([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}"   # March 14, 2026
    r"|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}"         # 7 Apr 2026
    r"|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"            # 03/14/2026, 6/2/26
    r"|\d{4}-\d{2}-\d{2})"                      # 2026-03-14
)

# Each label regex is tried in order; the first hit wins.
LABELS: dict[str, list[str]] = {
    "change_order_number": [
        r"(?i)(?:change\s*order|c\.\s*o\.|\bCO)\s*(?:no\.?|number|#)?\s*[:#]?\s*((?:PCO-?)?[A-Z]{0,3}\d{1,5}[A-Z]?)\b",
        r"(?i)\b(PCO-?\d{1,5})\b",
    ],
    "project_name": [rf"{_L}proj\s?ect(?:\s*name)?\s*:\s*{_TEXT}"],
    "project_number": [rf"(?i)\b(?:project|job)\s*(?:no\.?|number|#){_SEP}([A-Z0-9][A-Z0-9\-]{{2,20}})"],
    "date_issued": [
        rf"{_L}(?:date(?:\s*issued)?|issued){_SEP}(?:[A-Za-z]{{3}},?\s+)?{_DATE}",
        rf"(?i)\bdate{_SEP}{_DATE}",
    ],
    "contractor": [rf"{_L}(?:contractor|submitted\s*by)\s*:\s*{_TEXT}"],
    "owner": [rf"{_L}(?:owner|client|submitted\s*to)\s*:\s*{_TEXT}"],
    "subtotal": [rf"(?i)\bsub\s*-?\s*total{_SEP}({_MONEY})"],
    "markup_amount": [rf"(?i)(?:markup|overhead\s*(?:&|and)\s*profit|OH\s*&\s*P)[^\n$]{{0,200}}?({_MONEY})"],
    "tax_amount": [rf"(?im)^\s*(?:sales\s*)?tax\b[^\n$]{{0,200}}?({_MONEY})"],
    "total_amount": [
        rf"(?i)(?<![\w-])t[o0]tal(?:\s*change\s*order)?(?:\s*(?:amount|price|cost))?{_SEP}({_MONEY})",
        rf"(?i)contract\s*sum\s*(?:will\s*be\s*)?(?:increased|decreased)\s*by{_SEP}({_MONEY})",
        rf"(?i)({_MONEY})\s+total\b",
    ],
    "schedule_impact_days": [
        r"(?im)^\s*(?:schedule|time|contract\s*time)\s*(?:impact|extension|change|adjustment)?\s*:\s*([^\n]{1,40})",
        r"(?i)(?:push\w*|delay\w*|extend\w*|add\w*)[^\n.]{0,40}?(\d+\s*(?:calendar|working|business)?\s*days?)",
    ],
    "approved_by": [
        rf"(?im)^\s*(?:approved\s*by|accepted\s*by|owner\s*signature){_SEP}"
        r"([A-Z][A-Za-z.' ]{2,40}?)(?:,|\s+-|\s{2,}|$)",
    ],
}

_STATUS = [
    (r"(?i)\b(?:approved|accepted)\s*by\s*:\s*[A-Z]", "approved"),
    (r"(?i)\bstatus\s*:\s*(approved|rejected|pending|proposed)", None),
    (r"(?i)\b(rejected|declined)\b", "rejected"),
    (r"(?i)\b(pending|awaiting)\b", "pending"),
]
# Keyword classifier: cheap, transparent, and deliberately abstains when unsure.
_REASON = [
    (r"(?i)value\s*engineering|\bVE\b|substitut", "value_engineering"),
    (r"(?i)unforeseen|concealed|discovered|found\s+\w+|rotted|ledge\s*rock|differing\s*site", "unforeseen_condition"),
    (r"(?i)building\s*code|code\s*(?:requirement|compliance)|inspector|fire\s*marshal|\bAHJ\b", "code_requirement"),
    (r"(?i)owner[- ]request|per\s+owner|owner\s+(?:wants|asked)|owner-requested", "owner_request"),
    (r"(?i)\b(?:ASI|bulletin|SB|architect|structural|revised\s*drawing)", "design_change"),
    (r"(?i)\berror|omission|missed", "error_omission"),
]
_REF = re.compile(r"(?i)\b(RFI|ASI|SB|CCD|bulletin)[\s#-]*(\d{1,4})\b")

_PARSERS = {
    "subtotal": parse_money, "markup_amount": parse_money, "tax_amount": parse_money,
    "total_amount": parse_money, "date_issued": parse_date, "schedule_impact_days": parse_days,
}

_LINE = re.compile(
    r"^\s*(?:(?P<code>\d{2}[\s-]?\d{2}[\s-]?\d{2})\s+)?(?P<desc>[A-Za-z][^\n$]{3,80}?)\s+"
    r"(?P<qty>\d+(?:\.\d+)?)\s+(?P<unit>[A-Za-z]{1,4})\s+"
    r"(?P<price>\$?[\d,]+(?:\.\d{2})?)\s+(?P<amt>\(?-?\$?[\d,]+(?:\.\d{2})?\)?)\s*$",
    re.M,
)


def _first(text: str, field: str) -> dict | None:
    for pat in LABELS[field]:
        m = re.search(pat, text)
        if m:
            raw = m.group(1).strip(" .,;|")
            # "(1,200.00)" at the end of a table row is an accounting negative;
            # "markup ($577.50), so ..." mid-sentence is just prose punctuation.
            if raw.startswith("(") and not re.match(r"[ \t]*$", text[m.end(1):].split("\n", 1)[0]):
                raw = raw.strip("()")
            value = _PARSERS.get(field, lambda x: x)(raw)
            if value is not None and value != "":
                return {"value": value, "evidence": re.sub(r"\s{2,}", " ", m.group(0).strip())}
    return None


def split_change_orders(text: str) -> list[str]:
    """Split a CO log / multi-CO document on repeated 'CHANGE ORDER #n' headers."""
    starts = [m.start() for m in re.finditer(r"(?im)^\s*change\s*order\s*(?:no\.?|#)\s*\S+", text)]
    if len(starts) < 2:
        return [text]
    return [text[a:b] for a, b in zip(starts, [*starts[1:], len(text)], strict=True)]


def extract(text: str) -> list[Raw]:
    results = []
    for chunk in split_change_orders(text):
        raw: Raw = {}
        for field in LABELS:
            hit = _first(chunk, field)
            if hit:
                raw[field] = hit
        for pat, value in _STATUS:
            if m := re.search(pat, chunk):
                raw["status"] = {"value": value or m.group(1).lower(), "evidence": m.group(0)}
                break
        for pat, value in _REASON:
            if m := re.search(pat, chunk):
                raw["reason"] = {"value": value, "evidence": m.group(0)}
                break
        refs = sorted({f"{m.group(1).upper()}-{m.group(2)}" for m in _REF.finditer(chunk)})
        if refs:
            raw["references"] = {"value": refs, "evidence": ", ".join(refs)}
        raw["line_items"] = [
            {
                "description": m["desc"].strip(),
                "quantity": float(m["qty"]),
                "unit": m["unit"],
                "unit_price": parse_money(m["price"]),
                "amount": parse_money(m["amt"]),
                "cost_code": m["code"],
            }
            for m in _LINE.finditer(chunk)
        ]
        results.append(raw)
    return results
