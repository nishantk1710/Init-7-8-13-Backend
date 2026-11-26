"""CSV export of the Min-Max -> OAR recommendation.

UTF-8 with a byte-order mark (so Excel reads it as UTF-8), CRLF line ends, one header row, and the
calculation anchor and policy version on every row -- a file on its own says how it was produced.

FORMULA INJECTION. A spreadsheet runs a cell that starts with ``=``, ``+``, ``-`` or ``@`` (or a tab or
carriage return) as a formula. A material description is free text from SAP, so every text cell that
starts with one of those characters gets a leading apostrophe, which a spreadsheet shows as plain text.
Numbers are written as numbers and are never altered.

CURRENCY. ``stock_value`` is in ZAR by the rule the OAR and Stock item figures use (a price in ZAR as it is, in
NAD at 1:1; any other or no currency is left out, so the value is empty). ``currency`` says ``ZAR`` when a price is
counted, and ``price_currency`` is what SAP stated.
"""

import csv
import io
from collections.abc import Iterable
from decimal import Decimal

from app.initiatives.i7.policy.reverse_conversion import ReverseConversionPolicy
from app.initiatives.i7.reverse_conversion.service import RecommendationRow
from app.initiatives.i7.reverse_conversion.types import AnchorResolution, ReverseCategory

BOM = "﻿"
_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

COLUMNS = (
    "material",
    "description",
    "plant",
    "mrp_type",
    "criticality",
    "category",
    "oar_conversion",
    "reason",
    "confidence",
    "explanation",
    "decision_rule",
    "issues_latest_12m",
    "months_with_consumption_12m",
    "issues_prior_12m",
    "first_valid_movement_month",
    "last_valid_movement_month",
    "months_since_last_movement",
    "history_span_months",
    "lifetime_months_with_use",
    "lead_time_days",
    "lead_time_bucket",
    "current_stock",
    "minbe",
    "mabst",
    "unit_price",
    "stock_value",
    "currency",
    "price_currency",
    "anchor_month",
    "policy_version",
)


def csv_safe(value: str | None) -> str:
    """A text cell that a spreadsheet cannot run as a formula."""
    if value is None:
        return ""
    return "'" + value if value.startswith(_DANGEROUS_PREFIXES) else value


def _month(value) -> str:
    return f"{value:%Y-%m}" if value is not None else ""


def _number(value: Decimal | int | float | None) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        # No exponent, no trailing zeros: 1500.000 -> 1500, 12.5000 -> 12.5.
        return format(value.normalize(), "f")
    return str(value)


def _row(row: RecommendationRow, anchor: AnchorResolution, policy: ReverseConversionPolicy) -> list[str]:
    m = row.metrics
    return [
        csv_safe(row.material),
        csv_safe(row.description),
        csv_safe(row.plant),
        csv_safe(row.mrp_type),
        csv_safe(row.criticality),
        row.category.value,
        "Eligible" if row.category is ReverseCategory.RECOMMENDED_FOR_OAR else "Not eligible",
        row.reason.value,
        row.confidence.value if row.confidence else "",
        csv_safe(row.detail),
        str(row.rule),
        str(m.issues_latest_window),
        str(m.months_with_use_latest_window),
        str(m.issues_prior_window),
        _month(m.first_valid_month),
        _month(m.last_valid_month),
        _number(m.months_since_last_movement),
        _number(m.history_span_months),
        str(m.lifetime_months_with_use),
        _number(row.lead_time_days),
        row.lead_time_bucket,
        _number(row.current_stock),
        _number(row.minbe),
        _number(row.mabst),
        _number(row.unit_price),
        _number(row.stock_value),
        row.value_currency or "",
        row.price_currency or "",
        _month(anchor.month),
        policy.policy_version,
    ]


def rows_to_csv(rows: Iterable[RecommendationRow], anchor: AnchorResolution, policy: ReverseConversionPolicy) -> bytes:
    """The recommendations as one UTF-8 CSV document (with BOM)."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow(_row(row, anchor, policy))
    return (BOM + buffer.getvalue()).encode("utf-8")
