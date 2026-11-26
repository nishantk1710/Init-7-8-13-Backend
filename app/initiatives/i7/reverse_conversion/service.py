"""Orchestration of the Min-Max -> OAR recommendation: fix the anchor, measure, decide, rank, count.

No SQL here and no HTTP: the data comes from a :class:`ReverseConversionSource` (the SQL one lives
beside the repository; tests hand in a fake), and the decision is :func:`evaluate`.

WHAT A REPORT HOLDS

Every in-scope material-plant exactly once, with its category, reason, metrics and stock figures, ranked
inside its category; the totals by category, plant and reason; and a reconciliation that says whether
the numbers add up to the population that was read. Stock value only RANKS rows inside a category -- it
never decides one, and a row with zero stock or no price is kept.

RANKING inside a category, best first: higher confidence first, then materials with stock and a known price by stock value, largest
first; then materials with stock but no price; then materials with no stock; ties by material and plant.

The report is a pure function of the data and the anchor, recalculated on request. Computing it reads the
staged consumption of every in-scope material-plant, so the finished report is kept in memory for a
few minutes, keyed by anchor, latest staged month, load time and policy version.
"""

import re
import threading
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy.orm import Session

from app.core.criticality import CriticalityTier
from app.initiatives.i7.kpi_formulas.currency import REPORTING_CURRENCY, TO_ZAR
from app.initiatives.i7.policy.reverse_conversion import ReverseConversionPolicy
from app.initiatives.i7.reverse_conversion import cooldown, repository
from app.shared.valuation_price import DEFAULT_TOKEN_MAX
from app.initiatives.i7.reverse_conversion.errors import AnchorError
from app.initiatives.i7.reverse_conversion.evaluate import evaluate
from app.initiatives.i7.reverse_conversion.metrics import (
    add_months,
    compute_consumption_metrics,
    first_of_month,
    resolve_anchor,
)
from app.initiatives.i7.reverse_conversion.repository import AttributeRow
from app.initiatives.i7.reverse_conversion.types import (
    CONFIDENCE_ORDER,
    FINAL_CATEGORIES,
    AnchorResolution,
    ConsumptionMetrics,
    MaterialPlantInputs,
    MonthlyMovement,
    ReverseCategory,
    ReverseConfidence,
    ReverseReason,
)

CATEGORY_CODES: dict[ReverseCategory, str] = {
    ReverseCategory.RECOMMENDED_FOR_OAR: "recommended_for_oar",
    ReverseCategory.NOT_ELIGIBLE: "not_eligible",
}
CATEGORY_BY_CODE: dict[str, ReverseCategory] = {code: category for category, code in CATEGORY_CODES.items()}

LEAD_TIME_BUCKETS = ("0-30d", "31-60d", "61-90d", ">90d", "unknown")
CRITICALITY_UNKNOWN = "unknown"

_KNOWN_TIERS = frozenset(tier.value for tier in CriticalityTier)
_ANCHOR_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_CACHE_TTL_SECONDS = 300.0


CONFIDENCE_CODES: dict[ReverseConfidence, str] = {level: level.name.lower() for level in ReverseConfidence}
CONFIDENCE_BY_CODE: dict[str, ReverseConfidence] = {code: level for level, code in CONFIDENCE_CODES.items()}


ELIGIBLE = "eligible"
NOT_ELIGIBLE = "not_eligible"


def conversion_eligibility(category: ReverseCategory) -> str:
    """Whether a row could move from Min-Max to OAR: ``eligible`` for Convert to OAR (whatever its
    confidence), ``not_eligible`` for Not Eligible."""
    return ELIGIBLE if category is ReverseCategory.RECOMMENDED_FOR_OAR else NOT_ELIGIBLE


def reason_code(reason: ReverseReason) -> str:
    return reason.name.lower()


REASON_BY_CODE: dict[str, ReverseReason] = {reason_code(reason): reason for reason in ReverseReason}


# --- The data source -----------------------------------------------------------------------


class ReverseConversionSource(Protocol):
    """Everything the report reads. The SQL implementation is :class:`SqlSource`."""

    def attributes(self) -> list[AttributeRow]: ...

    def consumption(self, anchor: date) -> Iterator[tuple[tuple[str, str], list[MonthlyMovement]]]: ...

    def latest_staged_month(self) -> date | None: ...

    def material_consumption(self, material: str, plant: str, anchor: date, first_month: date) -> list[MonthlyMovement]:
        """One material-plant's staged months from ``first_month`` to the anchor (for its detail chart)."""
        ...

    def group_changes(self) -> dict[tuple[str, str], date]:
        """The day each material-plant was seen to change between Min-Max and OAR (those that have)."""
        ...

    def data_loaded_at(self) -> datetime | None:
        """When the staged data was loaded from SAP, or None. Called only when the configured anchor
        source asks for it."""
        ...


class SqlSource:
    """The source over the real database (read-only)."""

    def __init__(self, session: Session, policy: ReverseConversionPolicy) -> None:
        self._session = session
        self._policy = policy

    def attributes(self) -> list[AttributeRow]:
        return repository.fetch_attributes(self._session, self._policy)

    def consumption(self, anchor: date) -> Iterator[tuple[tuple[str, str], list[MonthlyMovement]]]:
        return repository.iter_consumption(self._session, self._policy, anchor)

    def latest_staged_month(self) -> date | None:
        return repository.latest_staged_month(self._session)

    def material_consumption(self, material: str, plant: str, anchor: date, first_month: date) -> list[MonthlyMovement]:
        return repository.fetch_material_consumption(self._session, material, plant, anchor, first_month)

    def group_changes(self) -> dict[tuple[str, str], date]:
        return cooldown.changed_days(self._session)

    def data_loaded_at(self) -> datetime | None:
        return repository.latest_extract_request_time(self._session)


# --- The anchor ----------------------------------------------------------------------------


def parse_anchor_month(text: str | None) -> date | None:
    """``YYYY-MM`` -> the first of that month; empty or None -> None. Anything else is refused."""
    if text is None or not text.strip():
        return None
    match = _ANCHOR_MONTH.match(text.strip())
    if match is None:
        raise AnchorError(f"The anchor must be a month written YYYY-MM, got {text!r}.")
    return date(int(match.group(1)), int(match.group(2)), 1)


def resolve_report_anchor(
    source: ReverseConversionSource,
    *,
    override_month: str | None = None,
    configured_month: str | None = None,
    anchor_source: str = "configured",
) -> AnchorResolution:
    """The calculation anchor: an explicit month (request override, else the setting) or one derived from
    the data-load time. Refuses rather than guesses; see :func:`~metrics.resolve_anchor`."""
    configured = parse_anchor_month(override_month) or parse_anchor_month(configured_month)
    loaded_at = source.data_loaded_at() if anchor_source == "csv_extract" else None
    return resolve_anchor(
        latest_staged_month=source.latest_staged_month(),
        data_loaded_at=loaded_at,
        configured_anchor=configured,
    )


# --- The report -----------------------------------------------------------------------------


def lead_time_bucket(value: object) -> str:
    try:
        days = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "unknown"
    if days <= 0:
        return "unknown"
    if days <= 30:
        return "0-30d"
    if days <= 60:
        return "31-60d"
    if days <= 90:
        return "61-90d"
    return ">90d"


def value_currency(price: Decimal | None, currency: str | None) -> str | None:
    """``"ZAR"`` when this price can be counted in rand, else None -- the rule the OAR and Stock item money
    figures use: a price in ZAR as it is, in NAD at 1:1 (the rand peg), and a price above the placeholder
    ceiling. Another currency, or none stated, is left out -- never converted or guessed."""
    if price is None or price <= DEFAULT_TOKEN_MAX:
        return None
    return REPORTING_CURRENCY if currency in TO_ZAR else None


def stock_value(stock: Decimal | None, price: Decimal | None, currency: str | None) -> Decimal | None:
    """Stock x unit price in ZAR, or None when the stock is missing or the price cannot be counted in rand
    (see :func:`value_currency`)."""
    if stock is None or price is None or value_currency(price, currency) is None:
        return None
    return stock * price * TO_ZAR[currency]  # type: ignore[index]


@dataclass(frozen=True)
class RecommendationRow:
    material: str
    description: str | None
    plant: str
    mrp_type: str | None
    criticality: str | None
    category: ReverseCategory
    reason: ReverseReason
    detail: str
    rule: int
    confidence: ReverseConfidence | None
    points: tuple[tuple[str, str], ...]
    """The reason as labelled lines: (label, text) pairs."""
    metrics: ConsumptionMetrics
    lead_time_days: Decimal | None
    lead_time_bucket: str
    current_stock: Decimal | None
    unit_price: Decimal | None
    price_currency: str | None
    """The currency SAP stated for the price, or None."""
    value_currency: str | None
    """``"ZAR"`` when the price is counted in rand (ZAR, or NAD at 1:1), else None."""
    stock_value: Decimal | None
    minbe: Decimal | None
    mabst: Decimal | None


@dataclass(frozen=True)
class CategoryTotals:
    count: int = 0
    with_stock: int = 0
    with_stock_missing_price: int = 0
    stock_value: Decimal = Decimal(0)
    """Sum, in ZAR, over rows whose price can be counted in rand; the rest are not in it."""


@dataclass(frozen=True)
class Reconciliation:
    source_population: int
    """Material-plants read for the scope."""
    excluded: int
    """Left out at evaluation: a deletion flag set since the feature build, or a price not in ZAR or NAD."""
    categorised: int
    """Rows carrying one of the categories."""
    duplicate_keys: int
    reconciled: bool
    """population = excluded + categorised, and no material-plant appears twice."""
    by_plant: dict[str, tuple[int, int]] = field(default_factory=dict)
    """plant -> (population, categorised)."""


@dataclass(frozen=True)
class ReverseReport:
    anchor: AnchorResolution
    policy: ReverseConversionPolicy
    rows: tuple[RecommendationRow, ...]
    totals_by_category: dict[ReverseCategory, CategoryTotals]
    count_by_plant_category: dict[tuple[str, ReverseCategory], int]
    count_by_category_reason: dict[tuple[ReverseCategory, ReverseReason], int]
    count_by_confidence: dict[ReverseConfidence, int]
    """Convert to OAR rows by confidence; every level is present, with 0 when none."""
    reconciliation: Reconciliation
    generated_at: datetime | None = None
    """When the pipeline stage that stored this report finished; None for one calculated on the spot."""
    run_id: int | None = None
    """The stored run it was read from; None for one calculated on the spot."""


def _rank_key(row: RecommendationRow) -> tuple:
    has_stock = row.current_stock is not None and row.current_stock > 0
    if has_stock and row.stock_value is not None:
        group, value = 0, -row.stock_value
    elif has_stock:
        group, value = 1, Decimal(0)
    else:
        group, value = 2, Decimal(0)
    confidence = CONFIDENCE_ORDER.index(row.confidence) if row.confidence is not None else len(CONFIDENCE_ORDER)
    return (FINAL_CATEGORIES.index(row.category), confidence, group, value, row.material, row.plant)


def build_report(
    source: ReverseConversionSource,
    policy: ReverseConversionPolicy,
    anchor: AnchorResolution,
    *,
    as_of: date | None = None,
) -> ReverseReport:
    """Measure and decide every in-scope material-plant, up to the anchor.

    ``as_of`` is today, for the conversion cooldown; left out, no cooldown applies."""
    attributes = source.attributes()
    changed_on = source.group_changes() if as_of is not None else {}

    metrics_by_key = {
        key: compute_consumption_metrics(months, anchor.month, policy)
        for key, months in source.consumption(anchor.month)
    }
    never_moved = compute_consumption_metrics([], anchor.month, policy)

    rows: list[RecommendationRow] = []
    excluded = 0
    seen: set[tuple[str, str]] = set()
    duplicates = 0
    population_by_plant: Counter[str] = Counter()
    for attribute in attributes:
        key = (attribute.material, attribute.plant)
        if key in seen:
            duplicates += 1
        seen.add(key)
        population_by_plant[attribute.plant] += 1

        decision = evaluate(
            MaterialPlantInputs(
                material=attribute.material,
                plant=attribute.plant,
                mrp_type=attribute.mrp_type,
                deletion_flag=attribute.deletion_flag,
                criticality=attribute.criticality,
                lead_time_days=attribute.lead_time_days,
                metrics=metrics_by_key.get(key, never_moved),
                current_stock=attribute.current_stock,
                unit_price=attribute.unit_price,
                group_changed_on=changed_on.get(key),
                as_of=as_of,
                price_currency=attribute.currency,
                minbe=attribute.minbe,
                mabst=attribute.mabst,
            ),
            policy,
        )
        if decision.category is ReverseCategory.EXCLUDED:
            excluded += 1
            continue
        rows.append(
            RecommendationRow(
                material=attribute.material,
                description=attribute.description,
                plant=attribute.plant,
                mrp_type=attribute.mrp_type,
                criticality=attribute.criticality,
                category=decision.category,
                reason=decision.reason,
                detail=decision.detail,
                rule=decision.rule,
                confidence=decision.confidence,
                points=decision.points,
                metrics=metrics_by_key.get(key, never_moved),
                lead_time_days=attribute.lead_time_days,
                lead_time_bucket=lead_time_bucket(attribute.lead_time_days),
                current_stock=attribute.current_stock,
                unit_price=attribute.unit_price,
                price_currency=attribute.currency,
                value_currency=value_currency(attribute.unit_price, attribute.currency),
                stock_value=stock_value(attribute.current_stock, attribute.unit_price, attribute.currency),
                minbe=attribute.minbe,
                mabst=attribute.mabst,
            )
        )
    return assemble_report(
        rows,
        anchor,
        policy,
        source_population=len(attributes),
        excluded=excluded,
        duplicate_keys=duplicates,
        population_by_plant=dict(population_by_plant),
    )


def assemble_report(
    rows: list[RecommendationRow],
    anchor: AnchorResolution,
    policy: ReverseConversionPolicy,
    *,
    source_population: int,
    excluded: int,
    duplicate_keys: int,
    population_by_plant: dict[str, int],
    generated_at: datetime | None = None,
    run_id: int | None = None,
) -> ReverseReport:
    """Rank the rows and count them by category, plant, reason and confidence, with the reconciliation.

    Shared by the calculation (:func:`build_report`) and by reading a stored run back
    (``snapshot.load_latest_report``), so a report built either way carries the same totals."""
    rows = sorted(rows, key=_rank_key)

    totals: dict[ReverseCategory, CategoryTotals] = {}
    for category in FINAL_CATEGORIES:
        chosen = [row for row in rows if row.category is category]
        stocked = [row for row in chosen if row.current_stock is not None and row.current_stock > 0]
        totals[category] = CategoryTotals(
            count=len(chosen),
            with_stock=len(stocked),
            with_stock_missing_price=sum(1 for row in stocked if row.stock_value is None),
            stock_value=sum((row.stock_value for row in chosen if row.stock_value is not None), Decimal(0)),
        )

    categorised_by_plant: Counter[str] = Counter(row.plant for row in rows)
    categorised = len(rows)
    reconciliation = Reconciliation(
        source_population=source_population,
        excluded=excluded,
        categorised=categorised,
        duplicate_keys=duplicate_keys,
        reconciled=source_population == excluded + categorised and duplicate_keys == 0,
        by_plant={
            plant: (population_by_plant[plant], categorised_by_plant[plant]) for plant in sorted(population_by_plant)
        },
    )
    return ReverseReport(
        anchor=anchor,
        policy=policy,
        rows=tuple(rows),
        totals_by_category=totals,
        count_by_plant_category=dict(Counter((row.plant, row.category) for row in rows)),
        count_by_category_reason=dict(Counter((row.category, row.reason) for row in rows)),
        count_by_confidence={
            level: sum(1 for row in rows if row.confidence is level) for level in CONFIDENCE_ORDER
        },
        reconciliation=reconciliation,
        generated_at=generated_at,
        run_id=run_id,
    )


# --- A short-lived cache ---------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: dict[tuple, tuple[float, ReverseReport]] = {}


def clear_report_cache() -> None:
    with _cache_lock:
        _cache.clear()


def get_report(
    source: ReverseConversionSource,
    policy: ReverseConversionPolicy,
    *,
    override_month: str | None = None,
    configured_month: str | None = None,
    anchor_source: str = "configured",
) -> ReverseReport:
    """The report for the resolved anchor, from the cache when a recent one exists for the same anchor,
    latest staged month, load time and policy version."""
    anchor = resolve_report_anchor(
        source, override_month=override_month, configured_month=configured_month, anchor_source=anchor_source
    )
    key = (anchor.month, anchor.latest_staged_month, anchor.data_loaded_at, policy.policy_version)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < _CACHE_TTL_SECONDS:
            return hit[1]
    report = build_report(source, policy, anchor)
    with _cache_lock:
        _cache[key] = (time.monotonic(), report)
        for stale in [k for k, (stamp, _) in _cache.items() if time.monotonic() - stamp >= _CACHE_TTL_SECONDS]:
            _cache.pop(stale, None)
    return report


# --- One material-plant's consumption, for the detail chart --------------------------------------------

CHART_MONTHS = 24
"""Months shown on the detail chart: the latest window and the one before it."""


@dataclass(frozen=True)
class ConsumptionPoint:
    month: date
    net_issued_quantity: Decimal
    issue_count: int
    reversal_count: int


def consumption_series(
    source: ReverseConversionSource, material: str, plant: str, anchor: date, months: int = CHART_MONTHS
) -> list[ConsumptionPoint]:
    """The last ``months`` months up to and including the anchor, oldest first, one point per month.
    A month with no staged movement is a zero point, so the chart shows the gaps the rule counts."""
    first = add_months(anchor, -(months - 1))
    staged = {
        first_of_month(m.period): m for m in source.material_consumption(material, plant, anchor, first)
    }
    points: list[ConsumptionPoint] = []
    for offset in range(months):
        month = add_months(first, offset)
        movement = staged.get(month)
        points.append(
            ConsumptionPoint(
                month=month,
                net_issued_quantity=movement.net_issued_quantity if movement else Decimal(0),
                issue_count=movement.issue_count if movement else 0,
                reversal_count=movement.reversal_count if movement else 0,
            )
        )
    return points


# --- Filtering and paging ----------------------------------------------------------------------


@dataclass(frozen=True)
class ListFilters:
    category: ReverseCategory | None = None
    plant: str | None = None
    reason: ReverseReason | None = None
    confidence: ReverseConfidence | None = None
    eligibility: str | None = None
    """``eligible`` (Convert to OAR) or ``not_eligible`` (Not Eligible)."""
    criticality: str | None = None
    """A tier name (upper-cased), or ``"unknown"`` for none recorded."""
    lead_time_bucket: str | None = None
    search: str | None = None
    """Part of a material number or description, case-insensitive."""


def apply_filters(rows: tuple[RecommendationRow, ...] | list[RecommendationRow], filters: ListFilters) -> list[RecommendationRow]:
    """The rows matching every filter that is set, in the report's ranked order."""
    needle = filters.search.strip().lower() if filters.search and filters.search.strip() else None
    wanted_tier = filters.criticality.strip().upper() if filters.criticality else None
    chosen: list[RecommendationRow] = []
    for row in rows:
        if filters.category is not None and row.category is not filters.category:
            continue
        if filters.plant is not None and row.plant != filters.plant:
            continue
        if filters.reason is not None and row.reason is not filters.reason:
            continue
        if filters.confidence is not None and row.confidence is not filters.confidence:
            continue
        if filters.eligibility is not None and conversion_eligibility(row.category) != filters.eligibility:
            continue
        if filters.lead_time_bucket is not None and row.lead_time_bucket != filters.lead_time_bucket:
            continue
        if wanted_tier is not None:
            tier = (row.criticality or "").strip().upper()
            if wanted_tier == CRITICALITY_UNKNOWN.upper():
                if tier in _KNOWN_TIERS:
                    continue
            elif tier != wanted_tier:
                continue
        if needle is not None and needle not in f"{row.material} {row.description or ''}".lower():
            continue
        chosen.append(row)
    return chosen


def paginate(rows: list[RecommendationRow], page: int, page_size: int) -> list[RecommendationRow]:
    start = (page - 1) * page_size
    return rows[start : start + page_size]
