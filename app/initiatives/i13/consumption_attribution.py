"""W6.4: deterministic consumption/ownership attribution.

Enriches W6.2's ``ReservationLedgerEntry`` output with WHO/WHICH business
object owns the consumption it records: reservation line, requester, order,
and (behind a config flag) cost centre. This module never re-derives the
Reservation -> PR -> PO -> GR -> GI chain -- ``ReservationLedgerEntry``
(``reservation_ledger.build_reservation_ledger``, W6.2) remains the single
source of truth for that linkage. It only *reads* the raw RESB row(s) the
same reservation repository already exposes, to pick up two fields W6.2's
ledger entry doesn't carry onto itself: ``Aufnr`` (order) and ``Wempf``
(goods recipient -- this dataset's requester proxy; RESB carries no separate
requester/employee field, and inventing one would violate the
no-fabrication rule; see ``postgres_reservation.py``'s module docstring).

Resolution order (an implementation approach, not a new VZI business rule):

1. Reservation line -- ``reservation_number``/``reservation_item`` come
   straight from the W6.2 ledger entry (its own key). They're only trusted
   as attributed ownership context once the raw ``raw_resb`` row for that
   exact (Rsnum, Rspos) is actually found by the caller -- if it isn't, W6.4
   has nothing deterministic to attach ownership context to, and the entry
   is UNATTRIBUTED.
2. Requester -- ``Wempf`` off that same raw row, when present, corroborated
   against the platform ``ConsumptionPlan.requester`` for the same exact
   (Rsnum, Rspos) when one exists (``app.initiatives.i13.plans`` -- the
   "requester/context already linked to reservation/session" source; there
   is no separate ChatSession entity in this backend, only this
   platform-owned, reservation-keyed record -- see ``plans.py``). Both are
   deterministic, RSNUM/RSPOS-keyed sources, so if they disagree that's a
   genuine conflict, not noise to average away -- see the AMBIGUOUS case
   below.
3. Order -- ``Aufnr`` off that same raw row, when present (source
   RESERVATION_ORDER -- there is no independent order repository in this
   codebase to source it from instead).
4. Cost centre -- only attempted when the caller's ``cost_centre_enabled``
   is true, via the ``CostCentreProvider`` seam (see
   ``cost_centre_provider.py``). Disabled, or enabled but unresolved, the
   field stays ``None`` and never turns an otherwise valid attribution into
   an error.

If more than one raw reservation row is found for the same (Rsnum, Rspos)
key with disagreeing Aufnr/Wempf values, or the matched ``ConsumptionPlan``
names a different requester than ``raw_resb`` does, this is reported
AMBIGUOUS rather than resolved by picking either candidate. Measured reality
is that ``raw_resb``'s own key is 100% unique (see
``test_reservation_ledger_postgres.test_raw_resb_own_key_is_unique``) and
``consumption_plans.csv``'s (Rsnum, Rspos) key is 100% unique too (measured:
742/742 rows), so both conflict paths are defensive guards against dirty/
future data, not cases this dataset produces today -- exercised in tests via
injected fakes.

No scoring, no probabilistic matching, no similarity heuristics, no LLM
output -- every field here is either a raw deterministic reference or
``None``.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from app.initiatives.i13.cost_centre_provider import CostCentreProvider, NullCostCentreProvider
from app.initiatives.i13.models import (
    ConsumptionAttribution,
    ConsumptionAttributionSource,
    ConsumptionAttributionStatus,
    ReservationLedgerEntry,
)
from app.initiatives.i13.plans import ConsumptionPlan

Row = dict[str, Any]


def _index_by_reservation_line(rows: list[Row]) -> dict[tuple[str, str], list[Row]]:
    index: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for row in rows:
        rsnum, rspos = row.get("Rsnum"), row.get("Rspos")
        if rsnum and rspos:
            index[(rsnum, rspos)].append(row)
    return index


def _index_plans_by_reservation_line(plans: list[ConsumptionPlan]) -> dict[tuple[str, str], ConsumptionPlan]:
    """(Rsnum, Rspos) -> its ``ConsumptionPlan`` -- 100% unique in the
    current fixture (742/742 rows measured); the first plan wins on the
    (unmeasured, hypothetical) chance of a future duplicate, same
    "don't fail the whole pipeline" posture as everywhere else in I13."""
    index: dict[tuple[str, str], ConsumptionPlan] = {}
    for plan in plans:
        key = (plan.reservation_number, plan.reservation_item)
        index.setdefault(key, plan)
    return index


class ConsumptionAttributionService:
    """W6.4 service: ``ReservationLedgerEntry`` (W6.2) -> ``ConsumptionAttribution``.

    Business logic only -- no session/DB access here (see
    ``consumption_attribution_mart.py`` for persistence).
    """

    def __init__(
        self,
        *,
        cost_centre_enabled: bool,
        cost_centre_provider: CostCentreProvider | None = None,
    ) -> None:
        self._cost_centre_enabled = cost_centre_enabled
        self._cost_centre_provider = cost_centre_provider or NullCostCentreProvider()

    def attribute_entries(
        self,
        entries: list[ReservationLedgerEntry],
        reservation_rows: list[Row],
        plans: list[ConsumptionPlan] | None = None,
    ) -> list[ConsumptionAttribution]:
        """Batch form. ``reservation_rows`` is the raw RESB row set already
        fetched for the same scope as ``entries`` -- typically the exact
        ``reservation_repository.get_reservations(...)`` call W6.2 itself
        made (memoized per repository instance, so reusing that instance
        costs no extra query -- see ``postgres_reservation.py``). ``plans``
        is optional platform ``ConsumptionPlan`` context (``plans.py``)."""
        index = _index_by_reservation_line(reservation_rows)
        plan_index = _index_plans_by_reservation_line(plans or [])
        return [
            self.attribute_entry(
                entry,
                index.get((entry.reservation_number, entry.reservation_item), []),
                plan_index.get((entry.reservation_number, entry.reservation_item)),
            )
            for entry in entries
        ]

    def attribute_entry(
        self,
        entry: ReservationLedgerEntry,
        reservation_rows: list[Row],
        plan: ConsumptionPlan | None = None,
    ) -> ConsumptionAttribution:
        """``reservation_rows``: every raw RESB row found for this entry's
        exact (reservation_number, reservation_item) key -- normally zero or
        one; more than one is the AMBIGUOUS/conflicting-candidate case.
        ``plan``: the platform ``ConsumptionPlan`` for this exact reservation
        line, if any -- corroborates (or conflicts with) the RESB-sourced
        requester."""
        now = datetime.now(timezone.utc)

        if not reservation_rows:
            return self._result(
                entry,
                requester_id=None,
                order_number=None,
                cost_centre=None,
                status=ConsumptionAttributionStatus.UNATTRIBUTED,
                source=ConsumptionAttributionSource.NONE,
                evidence=(
                    f"No raw reservation record found for {entry.reservation_number}/{entry.reservation_item}; "
                    "no deterministic ownership context available."
                ),
                now=now,
            )

        requesters = {row["Wempf"] for row in reservation_rows if row.get("Wempf")}
        orders = {row["Aufnr"] for row in reservation_rows if row.get("Aufnr")}
        if plan is not None and plan.requester:
            requesters.add(plan.requester)

        if len(requesters) > 1 or len(orders) > 1:
            return self._result(
                entry,
                requester_id=None,
                order_number=None,
                cost_centre=None,
                status=ConsumptionAttributionStatus.AMBIGUOUS,
                source=ConsumptionAttributionSource.NONE,
                evidence=(
                    f"Conflicting deterministic candidates for {entry.reservation_number}/{entry.reservation_item}: "
                    f"requesters={sorted(requesters)!r}, orders={sorted(orders)!r}"
                ),
                now=now,
            )

        requester_id = next(iter(requesters), None)
        order_number = next(iter(orders), None)

        cost_centre = None
        if self._cost_centre_enabled:
            cost_centre = self._cost_centre_provider.get_cost_centre(
                reservation_number=entry.reservation_number,
                reservation_item=entry.reservation_item,
                order_number=order_number,
            )

        if cost_centre:
            source = ConsumptionAttributionSource.COST_CENTRE
        elif order_number:
            source = ConsumptionAttributionSource.RESERVATION_ORDER
        else:
            source = ConsumptionAttributionSource.RESERVATION

        missing = []
        if not requester_id:
            missing.append("requester")
        if not order_number:
            missing.append("order")
        if self._cost_centre_enabled and not cost_centre:
            missing.append("cost centre")

        status = (
            ConsumptionAttributionStatus.PARTIALLY_ATTRIBUTED
            if missing
            else ConsumptionAttributionStatus.ATTRIBUTED
        )

        resolved = [f"requester {requester_id}" if requester_id else None, f"order {order_number}" if order_number else None]
        if self._cost_centre_enabled:
            resolved.append(f"cost centre {cost_centre}" if cost_centre else "cost centre unavailable")
        resolved_text = ", ".join(part for part in resolved if part)
        evidence = f"Reservation {entry.reservation_number}/{entry.reservation_item} resolved" + (
            f"; {resolved_text}" if resolved_text else " with no further deterministic context"
        )
        if missing:
            evidence += f"; missing: {', '.join(missing)}"

        return self._result(
            entry,
            requester_id=requester_id,
            order_number=order_number,
            cost_centre=cost_centre,
            status=status,
            source=source,
            evidence=evidence,
            now=now,
        )

    def _result(
        self,
        entry: ReservationLedgerEntry,
        *,
        requester_id: str | None,
        order_number: str | None,
        cost_centre: str | None,
        status: ConsumptionAttributionStatus,
        source: ConsumptionAttributionSource,
        evidence: str,
        now: datetime,
    ) -> ConsumptionAttribution:
        return ConsumptionAttribution(
            ledger_id=entry.ledger_id,
            material=entry.material,
            plant=entry.plant,
            reservation_number=entry.reservation_number,
            reservation_item=entry.reservation_item,
            requester_id=requester_id,
            order_number=order_number,
            cost_centre=cost_centre,
            status=status,
            source=source,
            evidence=evidence,
            cost_centre_attribution_enabled=self._cost_centre_enabled,
            attributed_at=now,
        )
