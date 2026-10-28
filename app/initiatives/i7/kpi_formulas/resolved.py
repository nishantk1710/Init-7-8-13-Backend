"""The formulas in force: validated, parsed, and ready to compile.

:func:`load_active` is what the Overview, the quarterly report and the information cards call. It never
raises: a missing table (the migration has not been applied), no saved version, or a saved version that
no longer validates all give the defaults -- the Overview must not go down because of a settings table.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.initiatives.i7.kpi_formulas import expression
from app.initiatives.i7.kpi_formulas import data_catalog
from app.initiatives.i7.kpi_formulas.definitions import DEFAULTS, FIELDS, FIELDS_BY_KEY, allowed_names
from app.initiatives.i7.kpi_formulas.expression import FormulaError, Parsed
from app.models.i7_kpi_formula import KpiFormulaVersion

logger = logging.getLogger(__name__)

ACTIVE = "active"


class FormulaValidationError(ValueError):
    """One or more fields are not usable. ``errors`` maps a field key to a sentence for the page."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


@dataclass(frozen=True)
class Formulas:
    version: int | None
    """The saved version these came from; ``None`` for the built-in defaults."""
    values: dict[str, str | int]
    parsed: dict[str, Parsed]
    window_days: int
    lookback_days: int

    @property
    def is_default(self) -> bool:
        return self.values == DEFAULTS


def validate(values: dict[str, object], *, version: int | None = None) -> Formulas:
    """Check every editable field. Missing fields take their default. Raises
    :class:`FormulaValidationError` listing *all* the problems, not just the first."""
    errors: dict[str, str] = {}
    unknown = sorted(set(values) - set(FIELDS_BY_KEY))
    for key in unknown:
        errors[key] = "This is not an editable field."

    resolved: dict[str, str | int] = {}
    parsed: dict[str, Parsed] = {}
    for field in FIELDS:
        raw = values.get(field.key, field.default)
        if field.kind == "setting":
            if isinstance(raw, bool) or not isinstance(raw, (int, str)):
                errors[field.key] = "Enter a whole number."
                continue
            try:
                number = int(str(raw).strip())
            except ValueError:
                errors[field.key] = "Enter a whole number."
                continue
            if field.minimum is not None and number < field.minimum:
                errors[field.key] = f"Must be at least {field.minimum}."
                continue
            if field.maximum is not None and number > field.maximum:
                errors[field.key] = f"Must be at most {field.maximum}."
                continue
            resolved[field.key] = number
            continue
        try:
            checked = expression.parse(str(raw), allowed_names(field), field.kind)
        except FormulaError as exc:
            errors[field.key] = str(exc)
            continue
        resolved[field.key] = checked.source
        parsed[field.key] = checked

    if errors:
        raise FormulaValidationError(errors)

    # The stages feed each other (average -> needed -> at risk), so a formula that runs for a typical row
    # proves the chain is coherent, not only each part. A failure here is a bug in the language, not in the
    # user's formula, but it must never reach the database.
    _dry_run(parsed)
    return Formulas(
        version=version,
        values=resolved,
        parsed=parsed,
        window_days=int(resolved["window_days"]),
        lookback_days=int(resolved["lookback_days"]),
    )


def _dry_run(parsed: dict[str, Parsed]) -> None:
    """Run every formula once over made-up numbers, so a chain that cannot work fails here and not in SQL."""
    sample: dict[str, Decimal | None] = {name: Decimal(10) for name in data_catalog.labels()}
    sample["window_days"] = Decimal(90)
    for key in ("avg_daily_use", "stock_needed"):
        sample[key] = expression.evaluate(parsed[key], sample)  # type: ignore[assignment]
    for item in parsed.values():
        expression.evaluate(item, sample)


def defaults() -> Formulas:
    return validate({}, version=None)


def from_document(document_json: str, version: int) -> Formulas:
    document = json.loads(document_json)
    return validate(dict(document.get("fields", {})), version=version)


def newest_active(session: Session) -> KpiFormulaVersion | None:
    return session.execute(
        select(KpiFormulaVersion)
        .where(KpiFormulaVersion.status == ACTIVE)
        .order_by(KpiFormulaVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()


def load_active(session: Session) -> Formulas:
    """The formulas the Overview is using now. Never raises -- see the module docstring."""
    try:
        row = newest_active(session)
    except SQLAlchemyError:
        session.rollback()  # the table is not there yet: the same "not set up" answer as an empty one
        return defaults()
    if row is None:
        return defaults()
    try:
        return from_document(row.document_json, row.version)
    except (FormulaValidationError, ValueError, TypeError):
        logger.exception("kpi formula version %s no longer validates; using the defaults", row.version)
        return defaults()
