"""Vision OCR fallback for pages with no text layer.

Runs only on the pages `ingest` queued, so typed PDFs cost nothing extra.
The transcript replaces the page's `[[ocr:N]]` marker, which lets the regex
extractor and the evidence-grounding check work on scans too.
"""
from __future__ import annotations

import base64
import re

from . import llm
from .ingest import Document, ocr_marker

PROMPT = """Transcribe this scanned construction document page exactly as written.
- Keep the reading order and one line per printed line; keep table rows on one line.
- Plain text only: no markdown, no | table pipes. Separate table columns with two spaces.
- Copy numbers, typos and spelling exactly. Do not correct, summarize or explain.
- Write [illegible] for anything you cannot read. Output only the transcription."""


def _openai(scan, client) -> str:
    url = f"data:{scan.mime};base64,{base64.b64encode(scan.image).decode()}"
    resp = client.chat.completions.create(
        model=llm.model_name(),
        temperature=0,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": url, "detail": "high"}},
        ]}],
    )
    return resp.choices[0].message.content or ""


def _anthropic(scan, client) -> str:
    resp = client.messages.create(
        model=llm.model_name(),
        max_tokens=8000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": scan.mime,
                                         "data": base64.b64encode(scan.image).decode()}},
            {"type": "text", "text": PROMPT},
        ]}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def _plain(text: str) -> str:
    """Models still emit markdown tables sometimes; flatten to the layout a PDF
    text layer would have so the regex extractor reads both the same way."""
    lines = []
    for line in text.strip().removeprefix("```").removesuffix("```").splitlines():
        if re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", line) and "-" in line:
            continue  # markdown separator row |---|---|
        lines.append(re.sub(r"\s*\|\s*", "  ", line).strip())
    return "\n".join(lines)


def transcribe(doc: Document, client=None) -> None:
    if not doc.scans:
        return
    if llm.provider() == "openai":
        import openai
        client, fn = client or openai.OpenAI(), _openai
    else:
        import anthropic
        client, fn = client or anthropic.Anthropic(), _anthropic
    for i, scan in enumerate(doc.scans):
        doc.text = doc.text.replace(ocr_marker(i), _plain(fn(scan, client)))
    doc.ocr_done = True
    doc.warnings.append(f"vision_ocr_used: {len(doc.scans)} page(s)")


def strip_markers(doc: Document) -> None:
    """No LLM available: leave a readable placeholder instead of the marker."""
    for i, scan in enumerate(doc.scans):
        doc.text = doc.text.replace(ocr_marker(i), f"[{scan.label}: scanned, no text layer]")
