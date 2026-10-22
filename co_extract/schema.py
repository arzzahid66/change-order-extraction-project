"""Output contract for an extracted change order.

Every scalar is wrapped in `Field[T]` so downstream systems (job costing,
AP, the PM's review queue) can decide per-field whether to auto-accept.
"""
from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field as PField

T = TypeVar("T")


class Reason(str, Enum):
    owner_request = "owner_request"
    design_change = "design_change"
    unforeseen_condition = "unforeseen_condition"
    code_requirement = "code_requirement"
    error_omission = "error_omission"
    value_engineering = "value_engineering"
    other = "other"


class Status(str, Enum):
    proposed = "proposed"
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    unknown = "unknown"


class Field(BaseModel, Generic[T]):
    value: T | None = None
    confidence: float = PField(0.0, ge=0.0, le=1.0)
    evidence: str | None = None
    # Where the value came from: "llm", "rules", "derived" (computed from other fields)
    source: str | None = None
    flags: list[str] = PField(default_factory=list)


class LineItem(BaseModel):
    description: str | None = None
    quantity: float | None = None
    unit: str | None = None
    unit_price: float | None = None
    amount: float | None = None
    cost_code: str | None = None
    confidence: float = 0.0
    flags: list[str] = PField(default_factory=list)


class Issue(BaseModel):
    severity: Literal["error", "warning", "info"]
    code: str
    message: str
    fields: list[str] = PField(default_factory=list)


class ChangeOrder(BaseModel):
    change_order_number: Field[str] = Field[str]()
    project_name: Field[str] = Field[str]()
    project_number: Field[str] = Field[str]()
    date_issued: Field[date] = Field[date]()
    contractor: Field[str] = Field[str]()
    owner: Field[str] = Field[str]()
    description: Field[str] = Field[str]()
    reason: Field[Reason] = Field[Reason]()
    status: Field[Status] = Field[Status]()
    subtotal: Field[float] = Field[float]()
    markup_amount: Field[float] = Field[float]()
    tax_amount: Field[float] = Field[float]()
    total_amount: Field[float] = Field[float]()
    schedule_impact_days: Field[int] = Field[int]()
    approved_by: Field[str] = Field[str]()
    references: Field[list[str]] = Field[list[str]]()
    line_items: list[LineItem] = PField(default_factory=list)

    issues: list[Issue] = PField(default_factory=list)
    overall_confidence: float = 0.0
    needs_review: bool = True
    review_reasons: list[str] = PField(default_factory=list)


SCALAR_FIELDS = [
    "change_order_number", "project_name", "project_number", "date_issued",
    "contractor", "owner", "description", "reason", "status", "subtotal",
    "markup_amount", "tax_amount", "total_amount", "schedule_impact_days",
    "approved_by", "references",
]
MONEY_FIELDS = {"subtotal", "markup_amount", "tax_amount", "total_amount"}
# A change order is useless to job costing without these.
CRITICAL_FIELDS = {"change_order_number", "total_amount", "project_name"}


class ExtractionResult(BaseModel):
    source_file: str
    extractor: str
    input_kind: str
    change_orders: list[ChangeOrder]
    warnings: list[str] = PField(default_factory=list)
