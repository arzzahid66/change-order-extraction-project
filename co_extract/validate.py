"""Business-rule validation. Arithmetic is the strongest signal we have:
when line items, subtotal, markup, tax and total all reconcile, the money
fields cross-check each other independently of any model."""
from __future__ import annotations

from datetime import date

from .schema import CRITICAL_FIELDS, ChangeOrder, Issue, Reason, Status

MONEY_TOL = 1.00  # dollars; rounding on paper forms is common


def _close(a: float, b: float, tol: float = MONEY_TOL) -> bool:
    return abs(a - b) <= tol


def derive_missing(co: ChangeOrder) -> None:
    """Fill subtotal/total by arithmetic when absent, marked source='derived'."""
    lines = [li.amount for li in co.line_items if li.amount is not None]
    if co.subtotal.value is None and lines:
        co.subtotal.value, co.subtotal.source = round(sum(lines), 2), "derived"
        co.subtotal.flags.append("derived_from_line_items")
    if co.total_amount.value is None and co.subtotal.value is not None:
        total = co.subtotal.value + (co.markup_amount.value or 0) + (co.tax_amount.value or 0)
        co.total_amount.value, co.total_amount.source = round(total, 2), "derived"
        co.total_amount.flags.append("derived_from_subtotal")


def validate(co: ChangeOrder, today: date | None = None) -> tuple[list[Issue], bool | None]:
    """Returns (issues, arithmetic_ok). arithmetic_ok is None when there is
    nothing to reconcile (e.g. a one-line email with only a total)."""
    today = today or date.today()
    issues: list[Issue] = []
    arithmetic_ok: bool | None = None

    for li in co.line_items:
        if None not in (li.quantity, li.unit_price, li.amount):
            if not _close(li.quantity * li.unit_price, li.amount, max(0.01, abs(li.amount) * 0.01)):
                li.flags.append("qty_x_price_mismatch")
                issues.append(Issue(severity="warning", code="line_math",
                                    message=f"{li.description!r}: {li.quantity} x {li.unit_price} != {li.amount}",
                                    fields=["line_items"]))

    amounts = [li.amount for li in co.line_items if li.amount is not None]
    sub, total = co.subtotal.value, co.total_amount.value
    checks: list[bool] = []
    if amounts and sub is not None and co.subtotal.source != "derived":
        ok = _close(sum(amounts), sub)
        checks.append(ok)
        if not ok:
            issues.append(Issue(severity="error", code="subtotal_mismatch",
                                message=f"line items sum to {sum(amounts):,.2f} but subtotal is {sub:,.2f}",
                                fields=["subtotal", "line_items"]))
    base = sub if sub is not None else (sum(amounts) if amounts else None)
    if base is not None and total is not None and co.total_amount.source != "derived":
        expected = base + (co.markup_amount.value or 0) + (co.tax_amount.value or 0)
        ok = _close(expected, total)
        checks.append(ok)
        if not ok:
            issues.append(Issue(severity="error", code="total_mismatch",
                                message=f"subtotal+markup+tax = {expected:,.2f} but total is {total:,.2f}",
                                fields=["total_amount", "subtotal", "markup_amount", "tax_amount"]))
    if checks:
        arithmetic_ok = all(checks)

    for f in CRITICAL_FIELDS:
        if getattr(co, f).value in (None, ""):
            issues.append(Issue(severity="error", code="missing_critical",
                                message=f"{f} not found", fields=[f]))

    d = co.date_issued.value
    if d and not (date(2000, 1, 1) <= d <= date(today.year + 1, 12, 31)):
        issues.append(Issue(severity="warning", code="implausible_date", message=f"date {d}",
                            fields=["date_issued"]))
    if co.reason.value == Reason.value_engineering and total is not None and total > 0:
        issues.append(Issue(severity="warning", code="ve_not_credit",
                            message="value-engineering CO with a positive total; sign may be wrong",
                            fields=["total_amount", "reason"]))
    if co.status.value == Status.approved and not co.approved_by.value:
        issues.append(Issue(severity="warning", code="approved_unsigned",
                            message="status approved but no approver found", fields=["status"]))
    project = (co.project_name.value or "").strip().lower()
    for party in ("owner", "contractor"):
        if project and (getattr(co, party).value or "").strip().lower() == project:
            # Who pays is material; a project name in the owner slot is a classic LLM slip.
            issues.append(Issue(severity="error", code="party_equals_project",
                                message=f"{party} is the same as the project name", fields=[party]))
    days = co.schedule_impact_days.value
    if days is not None and abs(days) > 365:
        issues.append(Issue(severity="warning", code="implausible_days",
                            message=f"{days} schedule days", fields=["schedule_impact_days"]))
    return issues, arithmetic_ok
