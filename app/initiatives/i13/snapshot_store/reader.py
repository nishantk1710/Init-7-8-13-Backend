"""Read the I13 snapshot from Azure SQL, the way the routes read it from memory.

:class:`SqlSnapshot` is one ready version of the store. Its methods answer what
the routes used to compute by filtering the in-memory snapshot's tuples --
same filters, same order, same items -- but filter, count and page in SQL and
decode only the rows on the page. Each method returns ``(items, total)``,
``total`` being what the route reports in ``X-Total-Count``.

Plan-dependent results are the one thing not simply read back: the stored
legacy exception queue was computed with the generated reference plans only,
and :meth:`SqlSnapshot.exception_queue` re-evaluates just the material-plants
that captured plans touch, exactly as ``snapshot.exception_queue`` evaluates
the whole tenant with every plan.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from functools import cached_property
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.initiatives.i13.config import get_i13_config
from app.initiatives.i13.exceptions import exception_queue_from
from app.initiatives.i13.ledger_compat import LegacyLedgerEntry
from app.initiatives.i13.models import (
    AgingBand,
    ConsumptionAttribution,
    ExceptionQueueItem,
    ExceptionType,
    MovementMetrics,
    PartialLedgerEntry,
    ProcurementChainDiagnostics,
    ReclassificationCandidate,
    ReservationLedgerEntry,
    WatchMetric,
)
from app.initiatives.i13.plans import (
    ConsumptionPlan,
    PlanSource,
    load_captured_plans,
    load_reference_plans,
)
from app.initiatives.i13.snapshot import GrniEntry, MonthlyConsumption
from app.initiatives.i13.snapshot_store import codec, kinds
from app.initiatives.i13.summary import I13Summary
from app.models.i13_snapshot_store import I13SnapshotRecord, I13SnapshotRun

T = TypeVar("T")
Key = tuple[str, str]
R = I13SnapshotRecord


def _items(cls: type[T], payloads: Iterable[str]) -> list[T]:
    return [codec.decode(cls, json.loads(p)) for p in payloads]


@dataclass
class SqlSnapshot:
    """One ready version of the stored snapshot, read through ``db``."""

    db: Session
    run: I13SnapshotRun
    _meta: dict[str, Any] | None = field(default=None, repr=False)

    # --- what the run itself carries ---------------------------------------

    @property
    def version(self) -> int:
        return self.run.version

    @property
    def reference_date(self) -> date:
        return self.run.reference_date

    @property
    def built_at(self) -> datetime | None:
        return self.run.built_at

    @property
    def meta(self) -> dict[str, Any]:
        if self._meta is None:
            self._meta = json.loads(self.run.meta or "{}")
        return self._meta

    @property
    def history_months(self) -> tuple[str, ...]:
        return tuple(self.meta.get("history_months") or ())

    @property
    def chain_diagnostics(self) -> ProcurementChainDiagnostics:
        d = dict(self.meta.get("chain_diagnostics") or {})
        d["duplicate_pr_keys"] = [tuple(k) for k in d.get("duplicate_pr_keys", [])]
        d["duplicate_po_keys"] = [tuple(k) for k in d.get("duplicate_po_keys", [])]
        return ProcurementChainDiagnostics(**d)

    @cached_property
    def reference_plans(self) -> list[ConsumptionPlan]:
        return load_reference_plans(Path(get_settings().i13_data_dir))

    # --- the query every list shares ---------------------------------------

    def _where(
        self,
        kind: str,
        *,
        material: str | None = None,
        plant: str | None = None,
        oar_only: bool = False,
        f1: str | None = None,
        f2: str | None = None,
        f3: str | None = None,
        extra: Iterable = (),
    ) -> list:
        clauses = [R.version == self.version, R.kind == kind]
        if material:
            clauses.append(R.material == material)
        if plant:
            clauses.append(R.plant == plant)
        if oar_only:
            clauses.append(R.oar == True)
        if f1 is not None:
            clauses.append(R.f1 == f1)
        if f2 is not None:
            clauses.append(R.f2 == f2)
        if f3 is not None:
            clauses.append(R.f3 == f3)
        clauses.extend(extra)
        return clauses

    def _page(self, clauses: list, order: list, limit: int | None, offset: int, columns=(R.payload,)):
        total = self.db.execute(select(func.count()).select_from(R).where(*clauses)).scalar_one()
        stmt = select(*columns).where(*clauses).order_by(*order).offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return self.db.execute(stmt).all(), total

    def _list(self, cls: type[T], kind: str, *, order=None, limit=None, offset=0, **filters) -> tuple[list[T], int]:
        rows, total = self._page(self._where(kind, **filters), order or [R.seq], limit, offset)
        return _items(cls, (r.payload for r in rows)), total

    def _one(self, cls: type[T], kind: str, **filters) -> T | None:
        row = self.db.execute(select(R.payload).where(*self._where(kind, **filters)).limit(1)).first()
        return codec.decode(cls, json.loads(row.payload)) if row else None

    def _for_keys(self, cls: type[T], kind: str, keys: Iterable[Key]) -> list[T]:
        keys = list(dict.fromkeys(keys))
        if not keys:
            return []
        found: list[T] = []
        for start in range(0, len(keys), 200):
            chunk = keys[start : start + 200]
            match = or_(*(and_(R.material == m, R.plant == p) for m, p in chunk))
            rows = self.db.execute(
                select(R.payload).where(R.version == self.version, R.kind == kind, match).order_by(R.seq)
            ).scalars()
            found.extend(_items(cls, rows))
        return found

    # --- WATCH --------------------------------------------------------------

    def watch(
        self,
        *,
        plant=None,
        material=None,
        aging_band=None,
        grni: bool | None = None,
        acquired_vs_plan_status=None,
        oar_only=False,
        limit=None,
        offset=0,
    ) -> tuple[list[WatchMetric], int]:
        return self._list(
            WatchMetric, kinds.WATCH,
            order=[R.material, R.plant], limit=limit, offset=offset,
            plant=plant, material=material, oar_only=oar_only,
            f1=aging_band.upper() if aging_band else None,
            f2=acquired_vs_plan_status.upper() if acquired_vs_plan_status else None,
            f3=None if grni is None else ("Y" if grni else "N"),
        )

    def watch_row(self, material: str, plant: str) -> WatchMetric | None:
        return self._one(WatchMetric, kinds.WATCH, material=material, plant=plant)

    # --- movement metrics -------------------------------------------------------

    def movement_metrics(self, *, plant=None, material=None, aging_band=None, limit=None, offset=0):
        return self._list(
            MovementMetrics, kinds.MOVEMENT,
            order=[R.material, R.plant], limit=limit, offset=offset,
            plant=plant, material=material, f1=aging_band.upper() if aging_band else None,
        )

    # --- reclassification -------------------------------------------------------

    def reclassification(self, *, plant=None, material=None, candidates_only=False, limit=None, offset=0):
        return self._list(
            ReclassificationCandidate, kinds.RECLASS, limit=limit, offset=offset,
            plant=plant, material=material, f1="Y" if candidates_only else None,
        )

    # --- ledgers -------------------------------------------------------------------

    def legacy_ledger(self, *, plant=None, material=None, oar_only=True, limit=None, offset=0):
        return self._list(
            LegacyLedgerEntry, kinds.LLEDGER, limit=limit, offset=offset,
            plant=plant, material=material, oar_only=oar_only,
        )

    def legacy_entry(self, ledger_id: str, *, oar_only=True) -> LegacyLedgerEntry | None:
        return self._one(
            LegacyLedgerEntry, kinds.LLEDGER, oar_only=oar_only, extra=[R.rec_key == ledger_id]
        )

    def procurement_chain(
        self, *, material=None, plant=None, pr_number=None, po_number=None, lifecycle_status=None,
        limit=None, offset=0,
    ):
        return self._list(
            PartialLedgerEntry, kinds.CHAIN, limit=limit, offset=offset,
            material=material, plant=plant, f2=pr_number or None, f3=po_number or None,
            f1=lifecycle_status,
        )

    def reservation_ledger(
        self, *, material=None, plant=None, reservation_number=None, pr_number=None,
        lifecycle_status=None, oar_only=True, reservation_keys: set[Key] | None = None,
        limit=None, offset=0,
    ) -> tuple[list[tuple[ReservationLedgerEntry, str | None]], int]:
        """Ledger entries with their SGTXT. ``reservation_keys`` keeps only
        those (reservation, item) pairs -- the session filter."""
        extra = []
        if reservation_keys is not None:
            numbers = sorted({k[0] for k in reservation_keys})
            if not numbers:
                return [], 0
            extra.append(R.f2.in_(numbers))
        clauses = self._where(
            kinds.RLEDGER, material=material, plant=plant, oar_only=oar_only,
            f1=lifecycle_status, f2=reservation_number or None, f3=pr_number or None, extra=extra,
        )
        if reservation_keys is None:
            rows, total = self._page(clauses, [R.seq], limit, offset, columns=(R.payload, R.text1))
            return [(codec.decode(ReservationLedgerEntry, json.loads(r.payload)), r.text1) for r in rows], total
        rows = self.db.execute(select(R.payload, R.text1).where(*clauses).order_by(R.seq)).all()
        entries = [(codec.decode(ReservationLedgerEntry, json.loads(r.payload)), r.text1) for r in rows]
        entries = [(e, s) for e, s in entries if (e.reservation_number, e.reservation_item) in reservation_keys]
        end = None if limit is None else offset + limit
        return entries[offset:end], len(entries)

    def reservation_entries(self, reservation_number: str, reservation_item: str, *, oar_only=True):
        rows = self.db.execute(
            select(R.payload, R.text1)
            .where(*self._where(kinds.RLEDGER, oar_only=oar_only, f2=reservation_number))
            .order_by(R.seq)
        ).all()
        found = [(codec.decode(ReservationLedgerEntry, json.loads(r.payload)), r.text1) for r in rows]
        return [(e, s) for e, s in found if e.reservation_item == reservation_item]

    def material_ranges(self, *, material=None, plant=None, size: int = 2000) -> list[tuple[str, str]]:
        """The ledger's materials in contiguous ranges of about ``size`` --
        how a tenant-wide pass (ACT detection) walks the version in slices."""
        stmt = (
            select(R.material)
            .where(*self._where(kinds.RLEDGER, material=material, plant=plant))
            .distinct()
            .order_by(R.material)
        )
        materials = list(self.db.execute(stmt).scalars())
        return [
            (materials[i], materials[min(i + size, len(materials)) - 1]) for i in range(0, len(materials), size)
        ]

    def ledger_in_range(self, lo: str, hi: str, *, plant=None) -> list[tuple[ReservationLedgerEntry, str | None]]:
        rows = self.db.execute(
            select(R.payload, R.text1)
            .where(*self._where(kinds.RLEDGER, plant=plant, extra=[R.material >= lo, R.material <= hi]))
            .order_by(R.seq)
        ).all()
        return [(codec.decode(ReservationLedgerEntry, json.loads(r.payload)), r.text1) for r in rows]

    def watch_in_range(self, lo: str, hi: str, *, plant=None) -> list[WatchMetric]:
        rows = self.db.execute(
            select(R.payload)
            .where(*self._where(kinds.WATCH, plant=plant, extra=[R.material >= lo, R.material <= hi]))
            .order_by(R.material, R.plant)
        ).scalars()
        return _items(WatchMetric, rows)

    def ledger_for_keys(self, keys: Iterable[Key]) -> list[ReservationLedgerEntry]:
        return self._for_keys(ReservationLedgerEntry, kinds.RLEDGER, keys)

    def ledger_with_sgtxt_since(self, start: date, *, plant=None, material=None, oar_only=True):
        """(entry, SGTXT) for entries required on or after ``start``."""
        rows = self.db.execute(
            select(R.payload, R.text1)
            .where(*self._where(kinds.RLEDGER, plant=plant, material=material, oar_only=oar_only,
                                extra=[R.d1 >= start]))
            .order_by(R.seq)
        ).all()
        return [(codec.decode(ReservationLedgerEntry, json.loads(r.payload)), r.text1) for r in rows]

    def ledger_with_sgtxt_for(self, material: str, plant: str):
        rows = self.db.execute(
            select(R.payload, R.text1)
            .where(*self._where(kinds.RLEDGER, material=material, plant=plant))
            .order_by(R.seq)
        ).all()
        return [(codec.decode(ReservationLedgerEntry, json.loads(r.payload)), r.text1) for r in rows]

    # --- attribution -------------------------------------------------------------

    def attribution(
        self, *, material=None, plant=None, reservation_number=None, pr_number=None,
        oar_only=True, ledger_id: str | None = None, limit=None, offset=0,
    ):
        return self._list(
            ConsumptionAttribution, kinds.ATTRIB, limit=limit, offset=offset,
            material=material, plant=plant, oar_only=oar_only,
            f2=reservation_number or None, f3=pr_number or None,
            extra=[R.rec_key == ledger_id] if ledger_id else (),
        )

    # --- GRNI and usage -----------------------------------------------------------

    def grni(self, *, plant=None, material=None, min_days=None, oar_only=True, limit=None, offset=0):
        extra = [R.n1 >= min_days] if min_days is not None else []
        rows, total = self._page(
            self._where(kinds.GRNI, plant=plant, material=material, oar_only=oar_only, extra=extra),
            [R.n1.desc(), R.material, R.plant, R.rec_key], limit, offset,
        )
        items = []
        for r in rows:
            ledger, outstanding, days = json.loads(r.payload)
            items.append(
                GrniEntry(
                    ledger=codec.decode(ReservationLedgerEntry, ledger),
                    outstanding_quantity=Decimal(outstanding),
                    days_since_gr=days,
                )
            )
        return items, total

    def usage(self, *, plant=None, material=None, aging_band=None, oar_only=True, limit=None, offset=0):
        """(key, series, stock, scope) per material-plant, most issued first."""
        rows, total = self._page(
            self._where(kinds.USAGE, plant=plant, material=material, oar_only=oar_only,
                        f1=aging_band.upper() if aging_band else None),
            [R.n1.desc(), R.material, R.plant], limit, offset,
            columns=(R.material, R.plant, R.f2, R.payload),
        )
        return [self._usage_row(r) for r in rows], total

    def usage_for(self, material: str, plant: str):
        row = self.db.execute(
            select(R.material, R.plant, R.f2, R.payload)
            .where(*self._where(kinds.USAGE, material=material, plant=plant))
            .limit(1)
        ).first()
        return self._usage_row(row) if row else None

    @staticmethod
    def _usage_row(row):
        payload = json.loads(row.payload)
        series = tuple(
            MonthlyConsumption(month=m, issued_quantity=Decimal(i), issue_count=c, received_quantity=Decimal(r))
            for m, i, c, r in payload["s"]
        )
        stock = Decimal(payload["st"]) if payload.get("st") is not None else None
        return (row.material, row.plant), series, stock, row.f2

    def is_oar(self, material: str, plant: str) -> bool:
        return (
            self.db.execute(
                select(func.count()).select_from(R).where(*self._where(kinds.OAR_KEY, material=material, plant=plant))
            ).scalar_one()
            > 0
        )

    def watch_for_keys(self, keys: Iterable[Key]) -> dict[Key, WatchMetric]:
        return {(m.material, m.plant): m for m in self._for_keys(WatchMetric, kinds.WATCH, keys)}

    # --- the legacy exception queue, with captured plans applied -----------------

    def _captured_overlay(
        self, plans: list[ConsumptionPlan], *, plant=None, material=None
    ) -> tuple[set[Key], list[ExceptionQueueItem]]:
        """The material-plants captured plans touch, and their queue
        recomputed with every plan -- ``snapshot.exception_queue``'s rule."""
        captured = [p for p in plans if p.source is PlanSource.CAPTURED]
        keys = {
            (p.material, p.plant)
            for p in captured
            if (not plant or p.plant == plant) and (not material or p.material == material)
        }
        if not keys:
            return set(), []
        key_plans = [p for p in plans if (p.material, p.plant) in keys]
        watch = sorted(self.watch_for_keys(keys).values(), key=lambda m: (m.material, m.plant))
        items = exception_queue_from(
            self.ledger_for_keys(keys), key_plans, watch, get_i13_config(), as_of=self.reference_date
        )
        return keys, [i for i in items if (i.material, i.plant) in keys]

    def _not_keys(self, keys: set[Key]) -> list:
        return [~or_(*(and_(R.material == m, R.plant == p) for m, p in keys))] if keys else []

    def exception_queue(
        self, plans: list[ConsumptionPlan], *, plant=None, material=None,
        exception_type: str | None = None, status: str | None = None,
        limit: int | None = None, offset: int = 0,
    ) -> tuple[list[ExceptionQueueItem], int]:
        keys, overlay = self._captured_overlay(plans, plant=plant, material=material)
        wanted_type = exception_type.upper() if exception_type else None
        wanted_status = status.upper() if status else None
        overlay = [
            i for i in overlay
            if (wanted_type is None or i.type.value == wanted_type)
            and (wanted_status is None or i.status.value == wanted_status)
        ]
        base = self._where(
            kinds.EXCEPTION, plant=plant, material=material, f1=wanted_type, f2=wanted_status,
            extra=self._not_keys(keys),
        )
        if not overlay:
            rows, total = self._page(base, [R.n1, R.seq], limit, offset)
            return _items(ExceptionQueueItem, (r.payload for r in rows)), total

        # Plan breaches, then no-plan, then GRNI, as the in-memory queue: the
        # stored rows of each type, then the recomputed ones of that type.
        per_type = dict(
            self.db.execute(select(R.f1, func.count()).where(*base).group_by(R.f1)).all()
        )
        segments: list[tuple[str, int, list[ExceptionQueueItem]]] = []
        for t in sorted(ExceptionType, key=lambda t: kinds.EXCEPTION_RANK[t]):
            segments.append((t.value, per_type.get(t.value, 0), [i for i in overlay if i.type is t]))
        total = sum(n + len(extra) for _, n, extra in segments)
        end = total if limit is None else min(total, offset + limit)
        page: list[ExceptionQueueItem] = []
        position = 0
        for type_value, stored, extra in segments:
            for count, source in ((stored, "stored"), (len(extra), "overlay")):
                lo, hi = position, position + count
                take_from, take_to = max(offset, lo), min(end, hi)
                if take_from < take_to:
                    if source == "stored":
                        rows = self.db.execute(
                            select(R.payload).where(*base, R.f1 == type_value).order_by(R.seq)
                            .offset(take_from - lo).limit(take_to - take_from)
                        ).scalars()
                        page.extend(_items(ExceptionQueueItem, rows))
                    else:
                        page.extend(extra[take_from - lo : take_to - lo])
                position = hi
        return page, total

    def exception_counts(self, plans: list[ConsumptionPlan], *, plant=None, material=None) -> Counter:
        keys, overlay = self._captured_overlay(plans, plant=plant, material=material)
        counts: Counter = Counter()
        for type_value, n in self.db.execute(
            select(R.f1, func.count())
            .where(*self._where(kinds.EXCEPTION, plant=plant, material=material, extra=self._not_keys(keys)))
            .group_by(R.f1)
        ).all():
            counts[ExceptionType(type_value)] += n
        for item in overlay:
            counts[item.type] += 1
        return counts

    # --- the summary -------------------------------------------------------------

    def summary(self, plans: list[ConsumptionPlan], *, plant=None, material=None) -> I13Summary:
        bands = dict(
            self.db.execute(
                select(R.f1, func.count())
                .where(*self._where(kinds.OAR_KEY, plant=plant, material=material))
                .group_by(R.f1)
            ).all()
        )
        exceptions = self.exception_counts(plans, plant=plant, material=material)
        candidates = self.db.execute(
            select(func.count()).select_from(R).where(*self._where(kinds.RECLASS, plant=plant, material=material, f1="Y"))
        ).scalar_one()
        reference = sum(
            1 for p in self.reference_plans
            if (not plant or p.plant == plant) and (not material or p.material == material)
        )
        return I13Summary(
            total_oar_positions=sum(bands.values()),
            fast_moving_count=bands.get(AgingBand.FAST.value, 0),
            slow_moving_count=bands.get(AgingBand.SLOW.value, 0),
            non_moving_count=bands.get(AgingBand.NON_MOVING.value, 0),
            gr_not_issued_30_day_count=exceptions[ExceptionType.GR_NOT_ISSUED_30_DAY],
            plan_breach_count=exceptions[ExceptionType.PLAN_BREACH],
            no_plan_count=exceptions[ExceptionType.NO_PLAN],
            reclassification_candidate_count=candidates,
            valuation_is_mocked=True,
            reference_plan_count=reference,
        )

    # --- FR-6 validation inputs, read for the rows a report names -----------
    #
    # The in-memory snapshot kept every issue movement and every receipt date
    # for validation's sake. The store does not: validation asks for the
    # material-plants and PO lines its reports actually list, from this
    # version's indexed work tables.

    def _work(self):
        from app.initiatives.i13.snapshot_store.work_tables import WorkTables

        return WorkTables(self.version)

    def issue_events_for(self, keys: Iterable[Key]) -> dict[Key, list[dict]]:
        from sqlalchemy import bindparam, text

        from app.initiatives.i13.movements import ISSUE_TYPES, reversal_types_for
        from app.integrations.sap.postgres_movements import _to_movement_row

        wanted = sorted(set(ISSUE_TYPES) | reversal_types_for(ISSUE_TYPES))
        keys = set(keys)
        materials = sorted({m for m, _ in keys})
        found: dict[Key, list[dict]] = {}
        stmt = text(
            "SELECT material, plant, movement_type, quantity, posting_date, purchase_order, item "
            f"FROM {self._work().mov} WHERE material IN :materials AND movement_type IN :types"
        ).bindparams(bindparam("materials", expanding=True), bindparam("types", expanding=True))
        for start in range(0, len(materials), 500):
            for record in self.db.execute(stmt, {"materials": materials[start : start + 500], "types": wanted}):
                row = _to_movement_row(record)
                key = (row["Matnr"], row["Werks"])
                if key in keys:
                    found.setdefault(key, []).append(row)
        return found

    def movement_history_start(self) -> date | None:
        from sqlalchemy import text

        first = self.db.execute(text(f"SELECT MIN(posting_date) FROM {self._work().mov}")).scalar()
        return date.fromisoformat(first) if first else None

    def po_lines_for(self, lines: Iterable[Key]) -> tuple[dict[Key, str], dict[Key, frozenset[date]]]:
        """(PO, item) -> plant, and -> its 101 receipt dates, for these lines."""
        from sqlalchemy import bindparam, text

        from app.initiatives.i13.movements import RECEIPT_TYPES

        lines = set(lines)
        documents = sorted({po for po, _ in lines if po})
        plants: dict[Key, str] = {}
        receipts: dict[Key, set[date]] = {}
        work = self._work()
        po_stmt = text(
            f"SELECT purchasing_document, item, plant FROM {work.po} WHERE purchasing_document IN :docs"
        ).bindparams(bindparam("docs", expanding=True))
        gr_stmt = text(
            f"SELECT purchasing_document, item, movement_type, posting_date FROM {work.gr} "
            "WHERE purchasing_document IN :docs"
        ).bindparams(bindparam("docs", expanding=True))
        for start in range(0, len(documents), 500):
            chunk = {"docs": documents[start : start + 500]}
            for r in self.db.execute(po_stmt, chunk):
                key = (r.purchasing_document.strip(), r.item.strip())
                if key in lines:
                    plants[key] = r.plant.strip()
            for r in self.db.execute(gr_stmt, chunk):
                key = (r.purchasing_document.strip(), r.item.strip())
                if key in lines and (r.movement_type or "").strip() in RECEIPT_TYPES and r.posting_date:
                    receipts.setdefault(key, set()).add(date.fromisoformat(r.posting_date))
        return plants, {k: frozenset(v) for k, v in receipts.items()}

    def current_plans(self) -> list[ConsumptionPlan]:
        """Reference plans (as of this version) + captured plans (live)."""
        return list(self.reference_plans) + load_captured_plans(self.db)
