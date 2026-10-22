"""LLM extractor using structured outputs (schema-constrained JSON).

Provider is picked by CO_PROVIDER (openai | anthropic); defaults to whichever
API key is present, OpenAI first.
"""
from __future__ import annotations

import base64
import json
import os

from .ingest import Document
from .schema import Reason, Status

DEFAULT_MODELS = {"openai": "gpt-4.1-mini", "anthropic": "claude-opus-5-5"}


def provider() -> str:
    p = os.environ.get("CO_PROVIDER")
    if p:
        return p
    return "openai" if os.environ.get("OPENAI_API_KEY") else "anthropic"


def model_name() -> str:
    return os.environ.get("CO_MODEL") or DEFAULT_MODELS[provider()]


def _nullable(t: dict) -> dict:
    return {"anyOf": [t, {"type": "null"}]}


def _field(t: dict) -> dict:
    return {
        "type": "object",
        "properties": {
            "value": _nullable(t),
            "evidence": _nullable({"type": "string"}),
            "uncertain_reason": _nullable({"type": "string"}),
        },
        "required": ["value", "evidence", "uncertain_reason"],
        "additionalProperties": False,
    }


_STR, _NUM, _INT = {"type": "string"}, {"type": "number"}, {"type": "integer"}
FIELD_TYPES = {
    "change_order_number": _STR, "project_name": _STR, "project_number": _STR,
    "date_issued": {"type": "string", "description": "ISO 8601 YYYY-MM-DD"},
    "contractor": _STR, "owner": _STR, "description": _STR,
    "reason": {"type": "string", "enum": [r.value for r in Reason]},
    "status": {"type": "string", "enum": [s.value for s in Status]},
    "subtotal": _NUM, "markup_amount": _NUM, "tax_amount": _NUM, "total_amount": _NUM,
    "schedule_impact_days": _INT, "approved_by": _STR,
    "references": {"type": "array", "items": _STR},
}
_LINE_ITEM = {
    "type": "object",
    "properties": {k: _nullable(t) for k, t in {
        "description": _STR, "quantity": _NUM, "unit": _STR, "unit_price": _NUM,
        "amount": _NUM, "cost_code": _STR,
    }.items()},
    "required": ["description", "quantity", "unit", "unit_price", "amount", "cost_code"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "change_orders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    **{k: _field(t) for k, t in FIELD_TYPES.items()},
                    "line_items": {"type": "array", "items": _LINE_ITEM},
                },
                "required": [*FIELD_TYPES, "line_items"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["change_orders"],
    "additionalProperties": False,
}

SYSTEM = """You extract change orders from construction documents (forms, emails, \
CO logs, scans) for a contractor's back office. Output feeds job costing, so a \
wrong number is worse than a missing one.

Rules:
- Return one entry per distinct change order in the input. A revised CO supersedes \
the earlier version of the same number; return only the latest revision.
- `value` must be stated in or directly read off the document. Do not compute \
totals or fill gaps by arithmetic; leave null and the pipeline will derive and flag it.
- `evidence` is a short verbatim quote (copied character-for-character, typos included) \
from the document that supports the value. null if value is null.
- Money is a signed number in dollars: credits/deducts/parenthesized amounts are negative.
- schedule_impact_days: calendar days added (+) or removed (-); 0 only if the document \
says there is no impact; null if not mentioned.
- status: "approved" only with an owner/architect signature or explicit approval; \
an unsigned contractor proposal is "proposed".
- references: RFI, ASI, bulletin, drawing or PCO numbers the CO cites.
- owner is the paying party (owner/client). Never reuse the project or building name \
as the owner; if no owner is named, null.
- reason: pick the best-fitting category from the description (unforeseen_condition for \
conditions found in the field, design_change for ASI/bulletin/architect/RFI-driven changes, \
owner_request when the owner asked for it, value_engineering for credits/substitutions). \
Use "other" only if none fits, and set uncertain_reason when it is a judgment call.
- line_items: only rows that are itemized in a table or list in the document. Do not \
turn prose into line items.
- Set `uncertain_reason` when the value is ambiguous, illegible, conflicting, or \
inferred (e.g. "two different totals on page 1 and 2"); otherwise null."""


def _text_part(doc: Document) -> str:
    note = (" Some pages had no text layer; their text below is an OCR transcript and may "
            "contain read errors - trust the page image over it." if doc.ocr_done else "")
    return (f"Document type: {doc.kind}.{note}\n\nExtracted text layer:\n"
            f"<document_text>\n{doc.text}\n</document_text>")


class LLMError(RuntimeError):
    pass


def extract(doc: Document, client=None) -> list[dict]:
    if provider() == "openai":
        return _extract_openai(doc, client)
    return _extract_anthropic(doc, client)


def _extract_openai(doc: Document, client=None) -> list[dict]:
    import openai

    client = client or openai.OpenAI()
    # PDFs go in as file parts so the model sees the page itself (tables, scans).
    content = [
        {"type": "file", "file": {"filename": f"document_{i + 1}.pdf",
                                  "file_data": "data:application/pdf;base64," + base64.b64encode(pdf).decode()}}
        for i, pdf in enumerate(doc.pdfs)
    ]
    content += [
        {"type": "image_url", "image_url": {"url": f"data:{s.mime};base64,{base64.b64encode(s.image).decode()}",
                                            "detail": "high"}}
        for s in doc.scans if not s.in_pdf
    ]
    content.append({"type": "text", "text": _text_part(doc)})
    resp = client.chat.completions.create(
        model=model_name(),
        temperature=0,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}],
        response_format={"type": "json_schema",
                         "json_schema": {"name": "change_orders", "strict": True, "schema": SCHEMA}},
    )
    choice = resp.choices[0]
    if choice.message.refusal:
        raise LLMError(f"model refused: {choice.message.refusal}")
    if choice.finish_reason == "length":
        raise LLMError("output truncated (finish_reason=length)")
    return json.loads(choice.message.content)["change_orders"]


def _extract_anthropic(doc: Document, client=None) -> list[dict]:
    import anthropic

    client = client or anthropic.Anthropic()
    content = [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                          "data": base64.b64encode(pdf).decode()}}
        for pdf in doc.pdfs
    ]
    content += [
        {"type": "image", "source": {"type": "base64", "media_type": s.mime,
                                     "data": base64.b64encode(s.image).decode()}}
        for s in doc.scans if not s.in_pdf
    ]
    content.append({"type": "text", "text": _text_part(doc)})
    with client.beta.messages.stream(
        model=model_name(),
        max_tokens=32000,
        system=SYSTEM,
        thinking={"type": "adaptive"},
        output_config={"effort": os.environ.get("CO_EFFORT", "medium"),
                       "format": {"type": "json_schema", "schema": SCHEMA}},
        # Server-side fallback: if a safety classifier declines, the API reruns on another model.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": content}],
    ) as stream:
        resp = stream.get_final_message()

    if resp.stop_reason == "refusal":
        raise LLMError(f"model refused: {resp.stop_details}")
    if resp.stop_reason == "max_tokens":
        raise LLMError("output truncated at max_tokens")
    text = "".join(b.text for b in resp.content if b.type == "text")
    return json.loads(text)["change_orders"]
