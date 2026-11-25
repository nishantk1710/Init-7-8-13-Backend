"""The Min-Max -> OAR repository, without a database.

A recording session stands in for the connection: it captures every statement and answers with canned
rows. That proves the queries are read-only SELECTs, compile for SQL Server, carry the scope and anchor
filters, and that rows are grouped and combined correctly. It does not prove they return the right rows
from the real tables -- the live validation does that.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy.dialects import mssql
from sqlalchemy.sql import Select

from app.initiatives.i7.policy.reverse_conversion import current_reverse_conversion_policy
from app.initiatives.i7.reverse_conversion import repository

POLICY = current_reverse_conversion_policy()


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0][0] if self._rows else None


class RecordingSession:
    """Answers each ``execute`` with the next canned row set and keeps the statements it was given."""

    def __init__(self, *answers):
        self._answers = list(answers)
        self.statements: list[Select] = []

    def execute(self, statement):
        self.statements.append(statement)
        return FakeResult(self._answers.pop(0) if self._answers else [])


def sql(statement) -> str:
    return " ".join(str(statement.compile(dialect=mssql.dialect())).split())


def test_every_query_is_a_read_only_select_that_compiles_for_sql_server():
    session = RecordingSession([], [], [("M1", "1300", date(2026, 1, 1), Decimal(1), 1, 0)], [(date(2026, 11, 1),)], [(datetime(2026, 11, 20, tzinfo=timezone.utc),)])
    repository.fetch_attributes(session, POLICY)
    list(repository.iter_consumption(session, POLICY, date(2026, 10, 1)))
    repository.latest_staged_month(session)
    repository.latest_extract_request_time(session)
    assert len(session.statements) == 5
    for statement in session.statements:
        assert isinstance(statement, Select)
        text = sql(statement).upper()
        assert text.startswith("SELECT")
        assert not any(word in text for word in ("INSERT ", "UPDATE ", "DELETE ", "MERGE ", "DROP ", "ALTER "))


def test_the_consumption_query_stops_at_the_anchor_and_is_scoped_and_ordered():
    session = RecordingSession([])
    list(repository.iter_consumption(session, POLICY, date(2026, 10, 1)))
    text = sql(session.statements[0])
    assert "i7_staged_consumption.period <=" in text  # nothing after the anchor is read
    assert "i7_staged_consumption.sap_plant_code IN" in text
    assert "EXISTS" in text and "i7_material_feature.mrp_type IN" in text
    assert "ORDER BY i7_staged_consumption.sap_material_number, i7_staged_consumption.sap_plant_code, i7_staged_consumption.period" in text
    params = session.statements[0].compile(dialect=mssql.dialect()).params
    assert date(2026, 10, 1) in params.values()
    assert session.statements[0].get_execution_options().get("yield_per")  # streamed, not loaded whole


def test_the_consumption_query_does_not_read_the_stored_count():
    session = RecordingSession([])
    list(repository.iter_consumption(session, POLICY, date(2026, 10, 1)))
    assert "consumption_count_12m" not in sql(session.statements[0])


def test_the_attribute_query_is_scoped_to_vm_vb_and_the_two_plants():
    session = RecordingSession([], [])
    repository.fetch_attributes(session, POLICY)
    main = sql(session.statements[1])
    assert "i7_material_feature.mrp_type IN" in main and "i7_material_feature.sap_plant_code IN" in main
    params = session.statements[1].compile(dialect=mssql.dialect()).params
    values: list = []
    for value in params.values():
        values.extend(value if isinstance(value, (list, tuple)) else [value])
    assert {"VM", "VB", "1300", "1500"} <= set(values)
    assert "ORDER BY i7_material_feature.sap_material_number, i7_material_feature.sap_plant_code" in main
    assert "consumption_count_12m" not in main


def test_consumption_rows_are_grouped_one_material_plant_at_a_time_in_month_order():
    rows = [
        ("A", "1300", date(2026, 1, 1), Decimal(2), 1, 0),
        ("A", "1300", date(2026, 3, 1), Decimal(1), 1, 0),
        ("A", "1500", date(2026, 2, 1), Decimal(4), 2, 1),
        ("B", "1300", date(2026, 5, 1), Decimal(1), 1, 0),
    ]
    grouped = list(repository.iter_consumption(RecordingSession(rows), POLICY, date(2026, 10, 1)))
    assert [key for key, _ in grouped] == [("A", "1300"), ("A", "1500"), ("B", "1300")]
    months_a = grouped[0][1]
    assert [m.period for m in months_a] == [date(2026, 1, 1), date(2026, 3, 1)]
    assert grouped[1][1][0].issue_count == 2 and grouped[1][1][0].reversal_count == 1
    assert grouped[1][1][0].net_issued_quantity == Decimal(4)


def test_no_consumption_rows_yield_nothing():
    assert list(repository.iter_consumption(RecordingSession([]), POLICY, date(2026, 10, 1))) == []


def test_attributes_combine_plant_and_client_level_deletion_flags_and_the_description():
    material_rows = [("M1", "SULPHUR PRILLS", False), ("M1", None, True), ("M2", "HOSE", None), ("M3", "VALVE", False)]
    attribute_rows = [
        # material, plant, mrp, criticality, lead, stock, price, minbe, mabst, plant-level deletion flag
        ("M1", "1300", "VM", "NORMAL", Decimal(30), Decimal(5), Decimal(10), Decimal(3), Decimal(9), False),
        ("M2", "1300", "VM", None, None, None, None, None, None, None),
        ("M3", "1500", "VB", "CRITICAL", Decimal(120), Decimal(0), None, Decimal(1), Decimal(2), True),
    ]
    rows = {(r.material, r.plant): r for r in repository.fetch_attributes(RecordingSession(material_rows, attribute_rows), POLICY)}
    m1, m2, m3 = rows[("M1", "1300")], rows[("M2", "1300")], rows[("M3", "1500")]
    assert m1.description == "SULPHUR PRILLS"  # the first non-empty description wins
    assert m1.deletion_flag is True  # the client-level flag deletes the material at every plant
    assert m2.deletion_flag is None  # never maintained at either level: not deleted, not known
    assert m3.deletion_flag is True  # the plant-level flag alone
    assert (m3.mrp_type, m3.criticality, m3.lead_time_days, m3.minbe, m3.mabst) == ("VB", "CRITICAL", Decimal(120), Decimal(1), Decimal(2))


def test_a_flag_that_says_not_deleted_at_both_levels_is_false():
    rows = repository.fetch_attributes(
        RecordingSession([("M1", "X", False)], [("M1", "1300", "VM", "NORMAL", None, None, None, None, None, False)]), POLICY
    )
    assert rows[0].deletion_flag is False


def test_the_extract_time_query_reads_only_completed_loaded_mseg_requests():
    session = RecordingSession([(datetime(2026, 11, 20, tzinfo=timezone.utc),)])
    assert repository.latest_extract_request_time(session) == datetime(2026, 11, 20, tzinfo=timezone.utc)
    text = sql(session.statements[0])
    assert "max(csv_extract_request.fired_at)" in text.lower()  # when SAP was ASKED, not when the data arrived
    params = list(session.statements[0].compile(dialect=mssql.dialect()).params.values())
    assert "MSEG" in params and "complete" in params
    assert "loaded_at IS NOT NULL" in text


def test_latest_staged_month_is_the_max_period():
    session = RecordingSession([(date(2026, 11, 1),)])
    assert repository.latest_staged_month(session) == date(2026, 11, 1)
    assert "max(i7_staged_consumption.period)" in sql(session.statements[0]).lower()
