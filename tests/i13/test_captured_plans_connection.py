"""``load_captured_plans`` must finish one read before it starts the next.

SQL Server runs one statement at a time on a connection: a result still being
read blocks the next statement with "Connection is busy with results for
another command". On Azure that took /api/i13/summary, /api/i13/validation and
the I13 snapshot build down the moment the first plan was captured through the
assistant (28 Sep). No database server here: a session stand-in enforces the
same rule, which SQLite and Postgres do not.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

from app.initiatives.i13.plans import PlanSource, load_captured_plans


class _Result:
    def __init__(self, session: "_OneStatementAtATime", rows: list) -> None:
        self._session = session
        self._rows = rows

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list:
        self._session.busy = False
        return list(self._rows)

    def __iter__(self):
        yield from self._rows
        self._session.busy = False


class _OneStatementAtATime:
    """Answers execute() in order; refuses a statement while a result is unread."""

    def __init__(self, *results: list) -> None:
        self._results = list(results)
        self.busy = False

    def execute(self, _statement) -> _Result:
        if self.busy:
            raise RuntimeError("Connection is busy with results for another command")
        rows = self._results.pop(0)
        self.busy = bool(rows)
        return _Result(self, rows)


def _plan(**overrides) -> SimpleNamespace:
    values = dict(
        id="plan-1", session_id="S-1", reservation_number="", reservation_item="",
        material="2000000143", plant="1300", captured_by="requester", purpose="shutdown",
        planned_quantity=Decimal("4"), window_start=date(2026, 10, 1), window_end=date(2026, 10, 31),
        status="ACTIVE", captured_at=datetime(2026, 9, 28, 13, 30),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _link(**overrides) -> SimpleNamespace:
    values = dict(session_id="S-1", reservation_number="0012345678", reservation_item="0001",
                  material="2000000143", plant="1300")
    values.update(overrides)
    return SimpleNamespace(**values)


def test_a_captured_plan_and_a_link_load_on_a_one_statement_connection() -> None:
    db = _OneStatementAtATime([_plan()], [_link()])

    plans = load_captured_plans(db)

    assert [(p.plan_id, p.source) for p in plans] == [("plan-1", PlanSource.CAPTURED)]
    assert (plans[0].reservation_number, plans[0].reservation_item) == ("0012345678", "0001")


def test_a_link_for_another_plant_is_not_attached() -> None:
    db = _OneStatementAtATime([_plan()], [_link(plant="1500")])

    (plan,) = load_captured_plans(db)

    assert (plan.reservation_number, plan.reservation_item) == ("", "")


def test_no_captured_plans_is_an_empty_list() -> None:
    assert load_captured_plans(_OneStatementAtATime([], [])) == []
