"""Turn an input file into text (+ the raw PDF / page images when the model should see the page).

Pages with no usable text layer (scans, phone photos) are rendered to images
and left as `[[ocr:N]]` markers in the text; `ocr.transcribe` fills them in.
"""
from __future__ import annotations

import email
import unicodedata
from dataclasses import dataclass, field
from email import policy
from pathlib import Path

import pymupdf

# Below this many extractable characters per page we assume a scan/photo.
MIN_CHARS_PER_PAGE = 40
RENDER_DPI = 150
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


@dataclass
class ScanPage:
    label: str      # "page 2" / "photo.jpg" - for warnings and the OCR prompt
    image: bytes
    mime: str
    in_pdf: bool    # True if the model already sees it inside a PDF we send


@dataclass
class Document:
    path: Path
    kind: str  # "pdf_text" | "pdf_scanned" | "image" | "text" | "email"
    text: str
    pdfs: list[bytes] = field(default_factory=list)
    scans: list[ScanPage] = field(default_factory=list)
    ocr_done: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def is_scanned(self) -> bool:
        return bool(self.scans)


def ocr_marker(i: int) -> str:
    return f"[[ocr:{i}]]"


def _pdf_text(data: bytes, doc: Document, name: str = "") -> str:
    """Text layer per page; low-text pages are rendered and queued for OCR."""
    out = []
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        for n, page in enumerate(pdf, start=1):
            # NFKC folds ligatures ("ﬃ" -> "ffi") and odd spaces that break matching.
            text = unicodedata.normalize("NFKC", page.get_text("text", sort=True))
            if len(text.strip()) < MIN_CHARS_PER_PAGE:
                png = page.get_pixmap(dpi=RENDER_DPI).tobytes("png")
                doc.scans.append(ScanPage(f"{name} page {n}".strip(), png, "image/png", in_pdf=True))
                text = ocr_marker(len(doc.scans) - 1)
            out.append(f"--- page {n} ---\n{text}")
    return "\n".join(out)


def _add_image(data: bytes, mime: str, label: str, doc: Document) -> str:
    doc.scans.append(ScanPage(label, data, mime, in_pdf=False))
    return ocr_marker(len(doc.scans) - 1)


def load(path: str | Path) -> Document:
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        data = path.read_bytes()
        doc = Document(path, "pdf_text", "", [data])
        doc.text = _pdf_text(data, doc)
    elif suffix in IMAGE_TYPES:
        doc = Document(path, "image", "")
        doc.text = _add_image(path.read_bytes(), IMAGE_TYPES[suffix], path.name, doc)
    elif suffix == ".eml":
        msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
        body = msg.get_body(preferencelist=("plain", "html"))
        parts = [
            f"From: {msg['from']}\nTo: {msg['to']}\nDate: {msg['date']}\nSubject: {msg['subject']}\n",
            body.get_content() if body else "",
        ]
        doc = Document(path, "email", "")
        for att in msg.iter_attachments():
            ctype, name = att.get_content_type(), att.get_filename() or "attachment"
            data = att.get_payload(decode=True)
            if ctype == "application/pdf":
                doc.pdfs.append(data)
                parts.append(f"\n=== attachment: {name} ===\n{_pdf_text(data, doc, name)}")
            elif ctype in IMAGE_TYPES.values():
                parts.append(f"\n=== attachment: {name} ===\n{_add_image(data, ctype, name, doc)}")
        doc.text = "\n".join(parts)
    else:
        return Document(path, "text", path.read_text(encoding="utf-8", errors="replace"))

    if doc.scans:
        if doc.kind == "pdf_text":
            doc.kind = "pdf_scanned"
        labels = ", ".join(s.label for s in doc.scans)
        doc.warnings.append(f"no_text_layer: {labels} need OCR")
    return doc
