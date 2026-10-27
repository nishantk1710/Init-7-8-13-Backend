"""Open repair PRs with no PO yet (``requisitions.py``), without a database.

The rule is all three at once: the PR item is not deleted, no live PO points
back to it, and at least one of its repair-order (543) lines is neither deleted
nor final-issued. On the 15-Oct-2026 extract that is 108 (1300: 68, 1500: 40);
the looser readings give 156, 122 and 117. Rows here are hand-built in the
``v_resb`` / ``v_eban`` / ``v_ekpo`` / ``v_ekko`` column names.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import date
from decimal import Decimal

import pytest

from app.core.db import MSSQL
from app.initiatives.i8 import requisitions, staged
from app.initiatives.i8.config import I8Settings
from app.initiatives.i8.requisitions import (
    PR_RAISED,
    RepairRequest,
    RequestStats,
    classify_repair_requests,
    has_live_purchase_order,
    index_back_pointers,
    is_repair_reservation_line,
    is_requisition_deleted,
    is_reservation_line_closed,
    load_repair_requests,
)

CFG = I8Settings(_env_file=None)
TODAY = date(2026, 10, 15)


def resb(banfn="2000029113", bnfpo="10", **overrides) -> dict:
    row = dict(
        rsnum="1916164", rspos="1", matnr="8000003325", werks="1300", bwart="543",
        banfn=banfn, bnfpo=bnfpo, aufnr="940000004981", xloek=None, kzear=None,
    )
    row.update(overrides)
    return row


def eban(banfn="2000029113", bnfpo="10", **overrides) -> dict:
    row = dict(
        banfn=banfn, bnfpo=bnfpo, matnr="8000003325", werks="1300", txz01="BEARING ASSEMBLY",
        menge=Decimal("1.000"), meins="EA", badat=date(2026, 9, 15), lfdat=date(2026, 9, 20),
        loekz=None, statu="N", bsart="ZMNP", afnam="10305", rsnum="1916164",
    )
    row.update(overrides)
    return row


def ekpo(ebeln="4500001234", banfn="2000029113", bnfpo="10", loekz=None) -> dict:
    return dict(ebeln=ebeln, ebelp="10", banfn=banfn, bnfpo=bnfpo, loekz=loekz)


def classify(resb_rows, eban_rows, po_rows=(), headers=None, cfg=CFG):
    return classify_repair_requests(
        resb_rows, eban_rows, index_back_pointers(po_rows), headers or {}, cfg, TODAY
    )


# --- the predicates ------------------------------------------------------------


class TestPredicates:
    def test_a_repair_reservation_line_is_the_movement_type_with_a_pr(self) -> None:
        assert is_repair_reservation_line(resb(), CFG) is True
        assert is_repair_reservation_line(resb(bwart="261"), CFG) is False
        assert is_repair_reservation_line(resb(banfn=None), CFG) is False

    def test_the_movement_type_is_a_setting(self) -> None:
        cfg = I8Settings(_env_file=None, repair_reservation_movement_type="544")
        assert is_repair_reservation_line(resb(bwart="544"), cfg) is True
        assert is_repair_reservation_line(resb(), cfg) is False

    def test_a_reservation_line_is_closed_when_deleted_or_final_issued(self) -> None:
        assert is_reservation_line_closed(resb(), CFG) is False
        assert is_reservation_line_closed(resb(xloek="X"), CFG) is True
        assert is_reservation_line_closed(resb(kzear="X"), CFG) is True
        cfg = I8Settings(_env_file=None, reservation_deleted_indicator="D", reservation_final_issue_indicator="F")
        assert is_reservation_line_closed(resb(xloek="X"), cfg) is False
        assert is_reservation_line_closed(resb(kzear="F"), cfg) is True

    def test_a_pr_is_deleted_on_its_own_indicator_not_ekpo_s(self) -> None:
        assert is_requisition_deleted(eban(loekz="X"), CFG) is True
        # EKPO's deleted value means nothing on a PR.
        assert is_requisition_deleted(eban(loekz="L"), CFG) is False
        assert is_requisition_deleted(eban(), CFG) is False

    def test_live_po(self) -> None:
        key = ("2000029113", "10")

        def live(po_rows, headers=None):
            return has_live_purchase_order(key, index_back_pointers(po_rows), headers or {}, CFG)

        order = {"4500001234": {"ebeln": "4500001234", "bsart": "ZREP"}}
        rfq = {"4500001234": {"ebeln": "4500001234", "bsart": "AN"}}
        assert live([]) is False
        assert live([ekpo()], order) is True
        assert live([ekpo(loekz="L")], order) is False, "a deleted PO is not a PO"
        assert live([ekpo()], rfq) is False, "a request for quotation orders nothing"
        assert live([ekpo(loekz="S")], order) is True, "a blocked PO is still a PO"
        assert live([ekpo()], {}) is True, "a PO with no header is live"
        assert live([ekpo(bnfpo="20")], order) is False, "another item of the same PR"
        assert live([ekpo(loekz="L"), ekpo(ebeln="4500009999")], order) is True


# --- the classification ----------------------------------------------------------


class TestTheOpenRule:
    def test_an_open_repair_pr_is_found(self) -> None:
        requests, stats = classify([resb()], [eban()])
        assert [r.key for r in requests] == [("2000029113", "10")]
        assert stats == RequestStats(open_repair_requests=1, repair_reservation_lines=1)

    def test_a_deleted_pr_is_left_out(self) -> None:
        requests, stats = classify([resb()], [eban(loekz="X")])
        assert requests == ()
        assert stats.requisitions_excluded_deleted == 1

    @pytest.mark.parametrize(
        ("loekz", "bsart", "with_header", "open_"),
        [
            (None, "ZREP", True, False),  # a live PO: ordered, so not here
            ("L", "ZREP", True, True),  # deleted PO: still waiting
            (None, "AN", True, True),  # RFQ: nothing ordered
            ("S", "ZREP", True, False),  # blocked: ordered, SAP holding it
            (None, None, False, False),  # header-less: live
        ],
    )
    def test_a_po_pointing_back(self, loekz, bsart, with_header, open_) -> None:
        headers = {"4500001234": {"ebeln": "4500001234", "bsart": bsart}} if with_header else {}
        requests, stats = classify([resb()], [eban()], [ekpo(loekz=loekz)], headers)
        assert bool(requests) is open_
        assert stats.requisitions_excluded_live_po == (0 if open_ else 1)

    @pytest.mark.parametrize("closed", [{"xloek": "X"}, {"kzear": "X"}, {"xloek": "X", "kzear": "X"}])
    def test_a_closed_repair_order_line_closes_the_pr(self, closed) -> None:
        requests, stats = classify([resb(**closed)], [eban()])
        assert requests == ()
        assert stats.requisitions_excluded_reservation_closed == 1

    def test_one_open_line_of_two_keeps_it_open(self) -> None:
        lines = [resb(rspos="1", kzear="X", aufnr="940000000001"), resb(rspos="2", aufnr="940000000002")]
        requests, stats = classify(lines, [eban()])
        assert len(requests) == 1
        # The repair order is the open line's, not the closed one's.
        assert requests[0].repair_order == "940000000002"
        assert stats.repair_reservation_lines == 2

    def test_each_exclusion_is_counted_once_in_order(self) -> None:
        """Deleted PR, then live PO, then closed reservation line."""
        resb_rows = [
            resb("1", kzear="X"), resb("2", kzear="X"), resb("3", kzear="X"), resb("4"),
        ]
        eban_rows = [eban("1", loekz="X"), eban("2"), eban("3"), eban("4")]
        po_rows = [ekpo(banfn="1"), ekpo(banfn="2")]
        requests, stats = classify(resb_rows, eban_rows, po_rows)
        assert [r.purchase_requisition for r in requests] == ["4"]
        assert (
            stats.requisitions_excluded_deleted,
            stats.requisitions_excluded_live_po,
            stats.requisitions_excluded_reservation_closed,
        ) == (1, 1, 1)

    def test_only_repair_order_lines_name_a_repair_pr(self) -> None:
        """A PR no 543 line names is not a repair PR, whatever it is for."""
        requests, stats = classify([resb(bwart="261"), resb(banfn=None)], [eban()])
        assert requests == ()
        assert stats == RequestStats()

    def test_80_series_parts_only(self) -> None:
        """Exactly, in Python: a staged copy is narrowed with a superset LIKE."""
        requests, _ = classify([resb(matnr="5000000800")], [eban(matnr="5000000800")])
        assert requests == ()

    def test_a_pr_the_pr_lines_do_not_hold_is_reported(self) -> None:
        requests, stats = classify([resb("7")], [eban()])
        assert requests == ()
        assert stats.requisitions_not_found == 1


class TestTheRequest:
    def test_fields(self) -> None:
        [request], _ = classify([resb()], [eban()])
        assert request == RepairRequest(
            purchase_requisition="2000029113",
            item="10",
            material_id="8000003325",
            plant="1300",
            description="BEARING ASSEMBLY",
            quantity=Decimal("1.000"),
            unit="EA",
            requested_on=date(2026, 9, 15),
            need_date=date(2026, 9, 20),
            department="10305",
            requisition_status="N",
            document_type="ZMNP",
            repair_order="940000004981",
            reservation="1916164",
            days_open=30,
            aging_bucket="16-30",
        )
        assert request.key == ("2000029113", "10")
        assert request.row_id == "PR-2000029113-10"
        assert PR_RAISED == "PR Raised"

    def test_days_open_and_aging_follow_the_configured_bands(self) -> None:
        cfg = I8Settings(_env_file=None, aging_band_boundaries="10,20,30")
        [request], _ = classify([resb()], [eban(badat=date(2026, 9, 1))], cfg=cfg)
        assert request.days_open == 44
        assert request.aging_bucket == "30+"

    def test_unknown_stays_unknown(self) -> None:
        """None means unknown, never zero -- except a missing quantity, which
        is zero as the register's own missing MENGE is."""
        row = eban(badat=None, lfdat=None, afnam=None, menge=None, meins=None)
        [request], _ = classify([resb(aufnr=None)], [row])
        assert request.days_open is None and request.aging_bucket is None
        assert request.department is None and request.repair_order is None
        assert request.quantity == Decimal(0) and request.unit is None

    def test_the_reservation_falls_back_to_the_pr_s(self) -> None:
        [request], _ = classify([resb(rsnum=None)], [eban(rsnum="1999999")])
        assert request.reservation == "1999999"

    def test_order_is_pr_date_then_number_then_item(self) -> None:
        resb_rows = [resb("200", "20"), resb("200", "100"), resb("100"), resb("300"), resb("50")]
        eban_rows = [
            eban("200", "20"), eban("200", "100"),
            eban("100", badat=date(2026, 9, 1)),
            eban("300", badat=None),
            eban("50"),
        ]
        requests, _ = classify(resb_rows, eban_rows)
        assert [r.key for r in requests] == [
            ("100", "10"), ("50", "10"), ("200", "20"), ("200", "100"), ("300", "10"),
        ]


# --- the loader, over a fake session -----------------------------------------------


class _Result:
    def __init__(self, rows) -> None:
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _Db:
    def __init__(self, resb_rows, eban_rows, headers) -> None:
        self.tables = {"resb": resb_rows, "eban": eban_rows, "ekko": headers}
        self.calls: list[tuple[str, dict]] = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, dict(params or {})))
        for name, rows in self.tables.items():
            if f"_{name}" in sql:
                return _Result(rows)
        raise AssertionError(f"unexpected query: {sql}")


class TestTheLoader:
    def test_it_reads_three_batches_and_classifies(self) -> None:
        db = _Db(
            [resb(), resb("2"), resb("3")],
            [eban(), eban("2"), eban("3")],
            [{"ebeln": "4500000002", "bsart": "ZREP"}, {"ebeln": "4500000003", "bsart": "AN"}],
        )
        candidates = [
            ekpo("4500000002", banfn="2"),
            ekpo("4500000003", banfn="3"),
            ekpo("4500000009", banfn="9"),  # names no repair PR: no header read for it
        ]
        requests, stats = load_repair_requests(db, CFG, reference_date=TODAY, candidates=candidates)

        # Same PR date, so by number: 3 before 2000029113.
        assert [r.purchase_requisition for r in requests] == ["3", "2000029113"]
        assert stats.requisitions_excluded_live_po == 1
        sqls = [sql for sql, _ in db.calls]
        assert len(sqls) == 3
        assert "from v_resb" in sqls[0] and db.calls[0][1] == {"movement_type": "543"}
        assert "from v_eban" in sqls[1] and sorted(db.calls[1][1]["requisitions"]) == ["2", "2000029113", "3"]
        assert "from v_ekko" in sqls[2] and db.calls[2][1]["documents"] == ["4500000002", "4500000003"]

    def test_it_reads_the_staged_copies(self) -> None:
        db = _Db([resb()], [eban()], [])
        sources = staged.Sources(views={"v_resb": "#i8s_v_resb", "v_eban": "#i8s_v_eban", "v_ekko": "#i8s_v_ekko"})
        requests, _ = load_repair_requests(db, CFG, reference_date=TODAY, candidates=[], sources=sources)
        assert len(requests) == 1
        assert "from #i8s_v_resb" in db.calls[0][0]
        assert "from #i8s_v_eban" in db.calls[1][0]

    def test_it_also_reads_the_po_lines_prs_and_hands_every_pr_back(self) -> None:
        """27-Oct-2026: the register takes each PO line's PR date and
        department from the same EBAN read."""
        db = _Db([resb()], [eban(), eban("7")], [])
        read: dict = {}
        load_repair_requests(
            db, CFG, reference_date=TODAY, candidates=[], also_read=["7", None], requisitions_out=read
        )
        assert sorted(db.calls[1][1]["requisitions"]) == ["2000029113", "7"]
        assert sorted(read) == [("2000029113", "10"), ("7", "10")]

    def test_nothing_to_read_reads_nothing_more(self) -> None:
        db = _Db([], [], [])
        requests, stats = load_repair_requests(db, CFG, reference_date=TODAY, candidates=[ekpo()])
        assert requests == () and stats == RequestStats()
        assert len(db.calls) == 1


# --- staging -------------------------------------------------------------------------


class _StagingDb:
    """Records every statement; answers every count with 0."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement, params=None):
        assert not params, "staging never binds a parameter"
        self.statements.append(str(statement))
        return self

    def scalar_one(self):
        return 0

    def get_bind(self):
        return type("Bind", (), {"dialect": type("Dialect", (), {"name": MSSQL})()})()


def _staging_statements(monkeypatch, cfg) -> list[str]:
    import app.shared.optional_tables as optional_tables

    monkeypatch.setattr(optional_tables, "existing", lambda db, tables: [])
    db = _StagingDb()
    with staged.staged_sources(db, cfg) as sources:
        assert sources.views["v_resb"] == "#i8s_v_resb"
        assert sources.views["v_eban"] == "#i8s_v_eban"
    return db.statements


class TestStaging:
    def test_the_build_stages_resb_and_eban(self) -> None:
        assert "v_resb" in staged.VIEWS and "v_eban" in staged.VIEWS

    def test_resb_is_narrowed_on_scope_movement_type_and_series(self, monkeypatch) -> None:
        cfg = I8Settings(_env_file=None, repair_reservation_movement_type="544", job_issue_movement_type="201")
        statements = _staging_statements(monkeypatch, cfg)
        [narrow] = [s for s in statements if s.startswith("SELECT * INTO #i8b_resb FROM n_resb")]
        # The repair order's lines and, since 27-Oct-2026, every job line
        # (MISSING_SESSION_ID), both from settings.
        assert "movement_type IN ('544', '201')" in narrow
        assert "LIKE '%80%'" in narrow and "'1300'" in narrow

    def test_eban_is_narrowed_on_scope_and_series(self, monkeypatch) -> None:
        statements = _staging_statements(monkeypatch, CFG)
        [narrow] = [s for s in statements if s.startswith("SELECT * INTO #i8b_eban FROM n_eban")]
        assert "LIKE '%80%'" in narrow
        assert "movement_type" not in narrow

    def test_both_are_cleaned_up(self, monkeypatch) -> None:
        statements = _staging_statements(monkeypatch, CFG)
        # Everything after the scanned count is the cleanup.
        scanned = max(i for i, s in enumerate(statements) if s.startswith("SELECT COUNT(*) FROM n_ekpo"))
        cleanup = statements[scanned + 1:]
        for table in ("#i8b_resb", "#i8b_eban", "#i8s_v_resb", "#i8s_v_eban"):
            assert f"DROP TABLE IF EXISTS {table}" in cleanup, table


# --- no SAP value written into the logic -------------------------------------------------


def test_no_sap_value_is_compared_literally() -> None:
    """The movement type and every indicator are settings (I8Settings)."""
    tree = ast.parse(pathlib.Path(requisitions.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        literals = {
            n.value
            for n in [node.left, *node.comparators]
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
        assert not literals & {"543", "X", "L", "S", "AN", "3"}, literals
