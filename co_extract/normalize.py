"""Parsing for the messy values construction paperwork actually contains."""
from __future__ import annotations

import re
from datetime import date

from dateutil import parser as dateparser

# Common OCR confusions inside numbers: O->0, l/I->1, S->5, B->8
_OCR_DIGIT = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "S": "5", "B": "8"})
_CREDIT_WORDS = re.compile(r"\b(credit|deduct(?:ion|ive)?|CR)\b", re.I)


def parse_money(raw) -> float | None:
    """'$12,450.00' -> 12450.0, '(1,200)' -> -1200.0, '$4.2k' -> 4200.0,
    '1,250.00 CR' -> -1250.0, 'T0TAL $4,2OO.OO' -> 4200.0."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if not s:
        return None
    negative = s.startswith("(") and s.rstrip(".").endswith(")")
    negative |= bool(re.search(r"^\s*-|-\s*\$", s)) or bool(_CREDIT_WORDS.search(s))
    m = re.search(r"\$?\s*([0-9OolISB][0-9OolISB,]*(?:\.[0-9OolISB]{1,2})?)\s*([kKmM])?\b", s)
    if not m:
        return None
    num = m.group(1).translate(_OCR_DIGIT).replace(",", "")
    if not re.fullmatch(r"\d+(\.\d+)?", num):
        return None
    val = float(num)
    if m.group(2):
        val *= 1_000 if m.group(2).lower() == "k" else 1_000_000
    return -val if negative else val


def parse_date(raw) -> date | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, date):
        return raw
    try:
        return dateparser.parse(str(raw), fuzzy=True, dayfirst=False).date()
    except (ValueError, OverflowError):
        return None


def parse_days(raw) -> int | None:
    """'5 working days' -> 5, 'no schedule impact' -> 0, '1 week' -> 7."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).lower()
    if re.search(r"\b(no|none|zero|n/a)\b.*(impact|change|days?)|^\s*(none|n/a|0)\s*$", s):
        return 0
    m = re.search(r"([+-]?\d+)\s*(calendar|working|business|work)?\s*(day|week)", s)
    if m:
        n = int(m.group(1))
        return n * 7 if m.group(3) == "week" else n
    m = re.fullmatch(r"\s*([+-]?\d+)\s*", s)
    return int(m.group(1)) if m else None


def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w$.,()/-]+", " ", s.lower())).strip()


def norm_id(s: str | None) -> str | None:
    """'CO #007' / 'C.O. No. 7' / 'co-7' all compare equal -> '7'.
    'PCO-21' (a *potential* CO) stays distinct from CO 21."""
    if s is None:
        return None
    s = re.sub(r"(?i)\b(change\s*order|c\.?\s*o\.?(?=[\s#:.\-\d]|$)|no\.?|number)|#", " ", str(s))
    s = re.sub(r"[\s#:.]+", "", s).lstrip("-")
    return s.lstrip("0").lower() or "0"
