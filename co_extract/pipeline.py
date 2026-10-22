"""ingest -> extract (LLM primary, rules secondary) -> normalize -> derive ->
validate -> score -> route (auto-accept vs human review)."""
from __future__ import annotations

import os
from pathlib import Path

from . import confidence, ocr, rules
from .ingest import Document, load
from .normalize import norm_id, parse_date, parse_days, parse_money
from .schema import (CRITICAL_FIELDS, MONEY_FIELDS, SCALAR_FIELDS, ChangeOrder,
                     ExtractionResult, LineItem, Reason, Status)
from .validate import derive_missing, validate

REVIEW_THRESHOLD = float(os.environ.get("CO_REVIEW_THRESHOLD", "0.80"))


def _coerce(field: str, v):
    if v is None or v == "":
        return None
    if field in MONEY_FIELDS:
        return parse_money(v)
    if field == "date_issued":
        return parse_date(v)
    if field == "schedule_impact_days":
        return parse_days(v)
    if field == "reason":
        return Reason(v) if v in Reason._value2member_map_ else Reason.other
    if field == "status":
        return Status(v) if v in Status._value2member_map_ else Status.unknown
    if field == "references":
        return list(v) if isinstance(v, list) else [str(v)]
    return str(v).strip()


def _pair(primary: list[dict], secondary: list[dict]) -> list[tuple[dict, dict | None]]:
    """Match each primary CO to the secondary CO with the same number."""
    if len(primary) == 1 and len(secondary) == 1:
        return [(primary[0], secondary[0])]
    by_num = {norm_id((s.get("change_order_number") or {}).get("value")): s for s in secondary}
    return [(p, by_num.get(norm_id((p.get("change_order_number") or {}).get("value")))) for p in primary]


def _build(raw: dict, source: str) -> ChangeOrder:
    co = ChangeOrder()
    for f in SCALAR_FIELDS:
        r = raw.get(f) or {}
        fld = getattr(co, f)
        fld.value = _coerce(f, r.get("value"))
        fld.evidence = r.get("evidence")
        fld.source = source if fld.value is not None else None
        if r.get("uncertain_reason"):
            fld.flags.append(f"model_note: {r['uncertain_reason']}")
    co.line_items = [LineItem(**{k: li.get(k) for k in LineItem.model_fields if k in li})
                     for li in raw.get("line_items") or []]
    for li in co.line_items:
        li.amount = parse_money(li.amount) if li.amount is not None else None
    return co


def _finalize(co: ChangeOrder, other: dict | None, doc: Document) -> ChangeOrder:
    derive_missing(co)
    co.issues, arithmetic_ok = validate(co)
    issue_count = {f: sum(f in i.fields for i in co.issues if i.code not in
                          ("subtotal_mismatch", "total_mismatch")) for f in SCALAR_FIELDS}

    for f in SCALAR_FIELDS:
        fld = getattr(co, f)
        other_val = _coerce(f, ((other or {}).get(f) or {}).get("value")) if other else None
        fld.confidence, flags = confidence.score(
            field=f, value=fld.value, source=fld.source or "llm", evidence=fld.evidence,
            source_text=doc.text, other_value=other_val, has_other=other is not None and other_val is not None,
            arithmetic_ok=arithmetic_ok if f in MONEY_FIELDS else None,
            uncertain=any(x.startswith("model_note") for x in fld.flags),
            n_issues=issue_count[f], scanned=doc.is_scanned, ocr=doc.ocr_done,
        )
        fld.flags += [x for x in flags if x not in fld.flags]

    for li in co.line_items:
        base = 0.85 if arithmetic_ok else 0.6 if arithmetic_ok is None else 0.4
        li.confidence = round(base - 0.3 * bool(li.flags) - 0.15 * doc.is_scanned, 3)

    crit = [getattr(co, f).confidence for f in CRITICAL_FIELDS]
    co.overall_confidence = round(min(crit), 3)
    reasons = [f"{i.code}: {i.message}" for i in co.issues if i.severity == "error"]
    reasons += [f"low_confidence: {f} ({getattr(co, f).confidence})"
                for f in sorted(CRITICAL_FIELDS) if 0 < getattr(co, f).confidence < REVIEW_THRESHOLD]
    if co.total_amount.source == "derived":
        reasons.append("total_amount was computed, not stated")
    co.review_reasons = reasons
    co.needs_review = bool(reasons)
    return co


def run(path: str | Path, mode: str = "auto") -> ExtractionResult:
    """mode: 'llm' | 'rules' | 'auto' (llm when OPENAI_API_KEY or ANTHROPIC_API_KEY is set)."""
    doc = load(path)
    has_key = bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"))
    use_llm = mode == "llm" or (mode == "auto" and has_key)
    ocr_error = None
    if doc.scans and use_llm:
        try:
            ocr.transcribe(doc)  # before the regex pass, so rules + grounding see scanned pages
        except Exception as e:
            if mode == "llm":
                raise
            ocr_error = f"vision_ocr_failed: {e}"
    ocr.strip_markers(doc)
    rule_raw = rules.extract(doc.text)
    warnings = list(doc.warnings) + ([ocr_error] if ocr_error else [])

    if use_llm:
        from . import llm
        try:
            primary, secondary, extractor = llm.extract(doc), rule_raw, f"llm:{llm.provider()}/{llm.model_name()}+rules"
        except Exception as e:  # network, refusal, truncation -> degrade, don't drop the document
            if mode == "llm":
                raise
            warnings.append(f"llm_failed_fell_back_to_rules: {e}")
            primary, secondary, extractor = rule_raw, None, "rules"
    else:
        primary, secondary, extractor = rule_raw, None, "rules"

    source = "llm" if extractor.startswith("llm") else "rules"
    pairs = _pair(primary, secondary) if secondary else [(p, None) for p in primary]
    cos = [_finalize(_build(p, source), s, doc) for p, s in pairs]

    nums = [norm_id(c.change_order_number.value) for c in cos if c.change_order_number.value]
    if len(nums) != len(set(nums)):
        warnings.append("duplicate_change_order_numbers_in_document")
    return ExtractionResult(source_file=str(path), extractor=extractor, input_kind=doc.kind,
                            change_orders=cos, warnings=warnings)
