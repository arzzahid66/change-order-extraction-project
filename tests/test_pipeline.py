import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from co_extract import pipeline
from co_extract.normalize import norm_id, parse_days, parse_money
from co_extract.schema import ChangeOrder, LineItem

DOCS = Path(__file__).resolve().parents[1] / "samples" / "docs"


@pytest.mark.parametrize("raw,expected", [
    ("$12,450.00", 12450.0), ("(1,200.00)", -1200.0), ("1,250.00 CR", -1250.0),
    ("$4.2k", 4200.0), ("T0TAL: $8,9l5.OO", 8915.0), ("credit of $500", -500.0),
    ("n/a", None), ("", None),
])
def test_parse_money(raw, expected):
    assert parse_money(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("2 calendar days", 2), ("No change to contract time", 0), ("1 week", 7), ("-3 days", -3), ("TBD", None),
])
def test_parse_days(raw, expected):
    assert parse_days(raw) == expected


def test_norm_id_equates_co_spellings():
    assert norm_id("CO #007") == norm_id("C.O. No. 7") == norm_id("7")
    assert norm_id("PCO-21") != norm_id("21")


def _co(**vals) -> ChangeOrder:
    co = ChangeOrder()
    for k, v in vals.items():
        if k == "line_items":
            co.line_items = [LineItem(**li) for li in v]
        else:
            getattr(co, k).value = v
            getattr(co, k).source = "llm"
    return co


def test_arithmetic_mismatch_forces_review(tmp_path):
    from co_extract.validate import validate
    co = _co(change_order_number="1", project_name="X", subtotal=5400.0, markup_amount=540.0,
             total_amount=5940.0, line_items=[{"amount": 3400.0}, {"amount": 1800.0}])
    issues, ok = validate(co, today=date(2026, 10, 1))
    assert ok is False
    assert any(i.code == "subtotal_mismatch" for i in issues)


def test_project_name_in_owner_slot_is_an_error():
    from co_extract.validate import validate
    co = _co(change_order_number="12", project_name="Harbor View Apartments",
             owner="Harbor View Apartments", total_amount=100.0)
    issues, _ = validate(co, today=date(2026, 10, 1))
    assert any(i.code == "party_equals_project" and i.severity == "error" for i in issues)


def test_derived_total_is_marked():
    from co_extract.validate import derive_missing
    co = _co(line_items=[{"amount": 100.0}, {"amount": 50.0}], markup_amount=15.0)
    derive_missing(co)
    assert co.subtotal.value == 150.0 and co.subtotal.source == "derived"
    assert co.total_amount.value == 165.0 and "derived_from_subtotal" in co.total_amount.flags


@pytest.mark.parametrize("name", ["co_01_clean_form.pdf", "co_03_credit.pdf", "co_08_ocr_noise.txt"])
def test_rules_mode_end_to_end(name):
    res = pipeline.run(DOCS / name, mode="rules")
    assert len(res.change_orders) == 1
    co = res.change_orders[0]
    assert co.total_amount.value is not None and co.total_amount.confidence >= 0.8


def test_scanned_pdf_routes_to_review_without_llm():
    res = pipeline.run(DOCS / "co_04_scanned.pdf", mode="rules")
    assert res.input_kind == "pdf_scanned"
    assert all(co.needs_review for co in res.change_orders)


def test_multi_co_document_is_split():
    res = pipeline.run(DOCS / "co_06_log.pdf", mode="rules")
    assert sorted(c.change_order_number.value for c in res.change_orders) == ["14", "15"]


# --- LLM path, with a fake client (no network) ----------------------------------

@pytest.fixture(autouse=True)
def _no_real_keys(monkeypatch):
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CO_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CO_PROVIDER", "openai")


def _fake_client(payload, refusal=None):
    """Mimics openai.OpenAI().chat.completions.create(...)."""
    calls = []

    def create(**kw):
        calls.append(kw)
        msg = SimpleNamespace(content=json.dumps(payload), refusal=refusal)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return client, calls


def _use(monkeypatch, client):
    import openai
    monkeypatch.setattr(openai, "OpenAI", lambda: client)


def _f(value, evidence=None, note=None):
    return {"value": value, "evidence": evidence, "uncertain_reason": note}


LLM_CO = {
    "change_order_number": _f("007", "CHANGE ORDER # 007"),
    "project_name": _f("Riverside Medical Office Building", "Project: Riverside Medical Office Building"),
    "project_number": _f("2024-118", "Project No: 2024-118"),
    "date_issued": _f("2026-03-14", "Date: March 14, 2026"),
    "contractor": _f("Apex Builders LLC", "Contractor: Apex Builders LLC"),
    "owner": _f("Riverside Health Partners", "Owner: Riverside Health Partners"),
    "description": _f("Add receptacles and circuits", "Add duplex receptacles"),
    "reason": _f("owner_request", None),
    "status": _f("approved", "Approved By: Dana Whitfield"),
    "subtotal": _f(2460.0, "Subtotal $2,460.00"),
    "markup_amount": _f(246.0, "Overhead & Profit (10%) $246.00"),
    "tax_amount": _f(None),
    "total_amount": _f(2706.0, "Total Change Order Amount $2,706.00"),
    "schedule_impact_days": _f(2, "2 calendar days"),
    "approved_by": _f("Dana Whitfield", "Approved By: Dana Whitfield"),
    "references": _f(["RFI-042"], "Reference: RFI-042"),
    "line_items": [
        {"description": "Duplex receptacles 20A", "quantity": 4, "unit": "EA",
         "unit_price": 185.0, "amount": 740.0, "cost_code": "26 05 19"},
        {"description": "Dedicated 20A circuit", "quantity": 2, "unit": "EA",
         "unit_price": 650.0, "amount": 1300.0, "cost_code": "26 05 33"},
        {"description": "Drywall patch and paint", "quantity": 1, "unit": "LS",
         "unit_price": 420.0, "amount": 420.0, "cost_code": "09 29 00"},
    ],
}


def test_llm_request_shape_and_merge(monkeypatch):
    client, calls = _fake_client({"change_orders": [LLM_CO]})
    _use(monkeypatch, client)
    res = pipeline.run(DOCS / "co_01_clean_form.pdf", mode="llm")

    kw = calls[0]
    assert kw["response_format"]["json_schema"]["strict"] is True
    assert kw["messages"][1]["content"][0]["type"] == "file"  # PDF sent natively
    co = res.change_orders[0]
    assert res.extractor.startswith("llm:")
    assert co.total_amount.value == 2706.0
    assert co.total_amount.confidence > 0.95  # grounded + regex agrees + arithmetic reconciles
    assert not co.needs_review


def test_hallucinated_evidence_is_penalised(monkeypatch):
    bad = json.loads(json.dumps(LLM_CO))
    bad["total_amount"] = _f(2760.0, "Total Change Order Amount $2,760.00")  # not in the document
    client, _ = _fake_client({"change_orders": [bad]})
    _use(monkeypatch, client)
    co = pipeline.run(DOCS / "co_01_clean_form.pdf", mode="llm").change_orders[0]
    assert co.total_amount.confidence < 0.5
    assert {"evidence_not_in_source", "extractors_disagree", "arithmetic_mismatch"} <= set(co.total_amount.flags)
    assert co.needs_review


def test_llm_failure_falls_back_to_rules_in_auto_mode(monkeypatch):
    client, _ = _fake_client({"change_orders": []}, refusal="no")
    _use(monkeypatch, client)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    res = pipeline.run(DOCS / "co_01_clean_form.pdf", mode="auto")
    assert res.extractor == "rules"
    assert any("llm_failed" in w for w in res.warnings)


# --- scanned pages: vision OCR fallback --------------------------------------

MIXED_PAGE2 = """PRICING SHEET - CO # 31
| Cost Code | Description | Qty | Unit | Unit Price | Amount |
|---|---|---|---|---|---|
| 05 52 00 | Stainless handrail | 320 | LF | $62.00 | $19,840.00 |
| 05 52 00 | Remove existing handrail | 320 | LF | $6.00 | $1,920.00 |
Subtotal $21,760.00
OH&P (8%) $1,740.80
Total Change Order Amount $23,500.80"""


def _ocr_and_extract_client(ocr_text, payload):
    """OCR calls have no response_format; the extraction call does."""
    calls = []

    def create(**kw):
        calls.append(kw)
        text = json.dumps(payload) if "response_format" in kw else ocr_text
        msg = SimpleNamespace(content=text, refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), calls


def test_only_textless_pages_are_queued_for_ocr():
    from co_extract.ingest import load
    doc = load(DOCS / "co_10_mixed.pdf")
    assert [s.label for s in doc.scans] == ["page 2"]
    assert "Bayview Senior Living" in doc.text  # page 1 text layer kept as-is


def test_mixed_pdf_ocr_feeds_rules_and_grounding(monkeypatch):
    co_json = {k: _f(None) for k in LLM_CO if k != "line_items"} | {"line_items": []}
    co_json |= {"change_order_number": _f("31", "CHANGE ORDER # 31"),
                "project_name": _f("Bayview Senior Living", "Project: Bayview Senior Living"),
                "subtotal": _f(21760.0, "Subtotal $21,760.00"),
                "markup_amount": _f(1740.8, "OH&P (8%) $1,740.80"),
                "total_amount": _f(23500.8, "Total Change Order Amount $23,500.80")}
    client, calls = _ocr_and_extract_client(MIXED_PAGE2, {"change_orders": [co_json]})
    _use(monkeypatch, client)
    res = pipeline.run(DOCS / "co_10_mixed.pdf", mode="llm")

    assert calls[0]["messages"][0]["content"][1]["type"] == "image_url"  # page image went to vision
    co = res.change_orders[0]
    assert co.total_amount.value == 23500.8
    assert "ocr_source" in co.total_amount.flags
    assert "evidence_not_in_source" not in co.total_amount.flags  # grounded against the OCR text
    assert "extractors_disagree" not in co.total_amount.flags     # regex read the OCR text too
    assert co.total_amount.confidence > 0.9


def test_phone_photo_is_sent_as_image(monkeypatch):
    client, calls = _ocr_and_extract_client("CHANGE ORDER # 41", {"change_orders": []})
    _use(monkeypatch, client)
    pipeline.run(DOCS / "co_09_phone_photo.jpg", mode="llm")
    extraction = next(c for c in calls if "response_format" in c)
    assert any(p["type"] == "image_url" for p in extraction["messages"][1]["content"])


def test_ocr_failure_degrades_to_review(monkeypatch):
    def boom(**kw):
        raise RuntimeError("vision down")
    _use(monkeypatch, SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=boom))))
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    res = pipeline.run(DOCS / "co_04_scanned.pdf", mode="auto")
    assert any("vision_ocr_failed" in w for w in res.warnings)
    assert all(co.needs_review for co in res.change_orders)
