"""Live-SAP ingestion: manifest, fetch and load.

No network and no database. The SAP client is stubbed and storage is a dict,
which is the same bargain the rest of this suite makes: the parts worth testing
are the decisions, not whether requests can reach the internet.
"""

from __future__ import annotations

import contextlib
import io
import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.core.storage import ObjectNotFoundError
from app.ingest import fetch as fetch_mod
from app.ingest import load as load_mod
from app.ingest.manifest import (
    KNOWN_EMPTY,
    Delta,
    check_delta_filters,
    spec_for,
    specs,
    table_name,
)
from app.ingest.watermarks import highest


# --- Test doubles -----------------------------------------------------------


class MemoryStorage:
    """The Storage port, backed by a dict."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    @contextlib.contextmanager
    def open_write(self, key: str):
        buffer = io.BytesIO()
        yield buffer
        self.objects[key] = buffer.getvalue()

    @contextlib.contextmanager
    def open_read(self, key: str):
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        yield io.BytesIO(self.objects[key])

    def list(self, prefix: str = ""):
        return iter(sorted(k for k in self.objects if k.startswith(prefix)))


class StubExtract:
    def __init__(self, rows, *, duplicate_keys=0, order_by=("Matnr", "Werks"),
                 degraded=False, unknown=()):
        self.rows = rows
        self.duplicate_keys = duplicate_keys
        self.order_by = order_by
        self.order_by_degraded = degraded
        self.unknown_properties = list(unknown)
        self.counted = len(rows)

    @property
    def stable(self) -> bool:
        return self.duplicate_keys == 0

    def __len__(self) -> int:
        return len(self.rows)


class StubClient:
    """Records what was asked for, and answers from a fixture or a function."""

    def __init__(self, extract=None, raises=None, answer=None):
        self._extract = extract
        self._raises = raises
        self._answer = answer
        self.calls: list[tuple[str, str | None]] = []

    def read_all(self, name, **kwargs):
        self.calls.append((name, kwargs.get("filter")))
        if self._raises:
            raise self._raises
        if self._answer is not None:
            return self._answer(name, kwargs)
        return self._extract

    @property
    def filters(self) -> list[str | None]:
        return [f for _, f in self.calls]


ROWS = [
    {"Matnr": "000000000011", "Werks": "1500", "Eisbe": "   0.000"},
    {"Matnr": "000000000012", "Werks": "1300", "Eisbe": "   2.000"},
]

RUN_DATE = date(2026, 9, 21)


@pytest.fixture
def spec():
    return spec_for("MaterialPlantSet")


@pytest.fixture
def storage():
    return MemoryStorage()


# --- Manifest ---------------------------------------------------------------


def test_table_name_strips_the_set_suffix() -> None:
    assert table_name("MaterialPlantSet") == "material_plant"


def test_table_name_handles_runs_of_capitals() -> None:
    """POHistorySet must not become p_o_history."""
    assert table_name("POHistorySet") == "po_history"
    assert table_name("POScheduleLineSet") == "po_schedule_line"


def test_every_discovered_set_has_a_spec() -> None:
    """Derived, not written down -- a set added to discovery appears here."""
    assert len(specs()) == 21
    assert {s.service for s in specs()} == {
        "ZVZI_KPI02_SHARED_SRV",
        "ZMM_KPI02_SRV",
    }


def test_tables_are_prefixed_apart_from_the_seed() -> None:
    """raw_* is the workbook route; the two carry different column names."""
    assert all(s.raw_table.startswith("odata_") for s in specs())


def test_spec_lookup_is_case_insensitive(spec) -> None:
    assert spec_for("materialplantset").name == "MaterialPlantSet"
    assert spec_for("material_plant").name == "MaterialPlantSet"


def test_unknown_set_names_the_known_ones() -> None:
    with pytest.raises(KeyError, match="MaterialPlantSet"):
        spec_for("NoSuchSet")


def test_sets_known_empty_are_still_ingested() -> None:
    """An empty table is a fact; the day SAP fills one we are already pointed at it."""
    empty = [s for s in specs() if not s.expects_rows]
    assert {s.name for s in empty} == KNOWN_EMPTY


# --- Fetch ------------------------------------------------------------------


def test_fetch_lands_jsonl_one_object_per_line(spec, storage) -> None:
    result = fetch_mod.fetch_set(
        spec, root="odata", client=StubClient(StubExtract(ROWS)),
        storage=storage, run_date=RUN_DATE,
    )

    assert result.ok
    body = storage.objects[result.data_key].decode("utf-8")
    lines = [json.loads(line) for line in body.splitlines()]
    assert lines == ROWS


def test_fetch_path_is_dated_so_a_rerun_keeps_the_evidence(spec, storage) -> None:
    result = fetch_mod.fetch_set(
        spec, root="odata", client=StubClient(StubExtract(ROWS)),
        storage=storage, run_date=RUN_DATE,
    )

    assert result.prefix == (
        "odata/ZVZI_KPI02_SHARED_SRV/MaterialPlantSet/2026-09-21"
    )


def test_fetch_writes_a_manifest_carrying_the_integrity_verdict(spec, storage) -> None:
    fetch_mod.fetch_set(
        spec, root="odata", client=StubClient(StubExtract(ROWS, degraded=True)),
        storage=storage, run_date=RUN_DATE,
    )

    manifest = fetch_mod.read_manifest(
        storage, "odata/ZVZI_KPI02_SHARED_SRV/MaterialPlantSet/2026-09-21"
    )
    assert manifest["rows"] == 2
    assert manifest["duplicate_keys"] == 0
    assert manifest["order_by_degraded"] is True
    assert manifest["usable"] is True
    assert manifest["keys"] == ["Matnr", "Werks"]


def test_columns_follow_the_contract_order(spec) -> None:
    columns = fetch_mod.columns_of(spec, ROWS)
    declared = [p.name for p in spec.entity_set.properties]

    assert columns[: len(declared)] == declared


def test_a_column_sap_added_is_appended_not_dropped(spec) -> None:
    """Silently discarding it is how a new field goes unnoticed for a month."""
    columns = fetch_mod.columns_of(spec, [{**ROWS[0], "Zznewfield": "x"}])

    assert columns[-1] == "Zznewfield"


def test_a_declared_column_missing_from_the_pull_is_still_a_column(spec) -> None:
    """Keeps the table shape stable across runs."""
    columns = fetch_mod.columns_of(spec, [{"Matnr": "1"}])

    assert "Werks" in columns


def test_an_unstable_pull_is_landed_but_marked_unusable(spec, storage) -> None:
    """The file is the evidence; the manifest is what stops it being loaded."""
    repeated = [ROWS[0], dict(ROWS[0]), ROWS[1]]
    result = fetch_mod.fetch_set(
        spec, root="odata", client=StubClient(StubExtract(repeated)),
        storage=storage, run_date=RUN_DATE,
    )

    assert result.ok is False
    assert result.data_key in storage.objects, "the bytes must survive for diagnosis"
    manifest = fetch_mod.read_manifest(storage, result.prefix)
    assert manifest["usable"] is False
    assert manifest["duplicate_keys"] == 1


def test_duplicates_are_measured_from_the_rows_not_taken_on_trust(spec, storage) -> None:
    """The client counts per request; a chunked fetch needs the whole picture."""
    repeated = [ROWS[0], dict(ROWS[0])]
    result = fetch_mod.fetch_set(
        spec, root="odata",
        client=StubClient(StubExtract(repeated, duplicate_keys=0)),
        storage=storage, run_date=RUN_DATE,
    )

    assert result.duplicate_keys == 1, "counted from the rows, not from the stub"


def test_a_failed_fetch_is_returned_not_raised(spec, storage) -> None:
    """--all must not stop at set 7 of 21."""
    result = fetch_mod.fetch_set(
        spec, root="odata", client=StubClient(raises=RuntimeError("CPI is down")),
        storage=storage, run_date=RUN_DATE,
    )

    assert result.ok is False
    assert "CPI is down" in result.error
    assert storage.objects == {}


# --- Load -------------------------------------------------------------------


def _land(storage, spec, rows=ROWS, *, usable=True, run_date=RUN_DATE):
    # Unusable means genuinely duplicated keys: fetch measures them from the
    # rows rather than believing what the client reported per request.
    landed = list(rows) if usable else [rows[0], dict(rows[0])]
    return fetch_mod.fetch_set(
        spec, root="odata", client=StubClient(StubExtract(landed)),
        storage=storage, run_date=run_date,
    )


def test_latest_prefix_picks_the_most_recent_run(spec, storage) -> None:
    _land(storage, spec, run_date=date(2026, 9, 19))
    _land(storage, spec, run_date=date(2026, 9, 21))
    _land(storage, spec, run_date=date(2026, 9, 20))

    assert load_mod.latest_prefix(storage, spec, root="odata").endswith("2026-09-21")


def test_latest_prefix_is_none_when_nothing_landed(spec, storage) -> None:
    assert load_mod.latest_prefix(storage, spec, root="odata") is None


def test_load_refuses_an_unstable_fetch(spec, storage) -> None:
    """The rows would look complete and not be."""
    _land(storage, spec, usable=False)

    result = load_mod.load_set(spec, root="odata", storage=storage)

    assert result.status == load_mod.STATUS_FAILED
    assert "unusable" in result.error


def test_load_says_what_to_do_when_nothing_landed(spec, storage) -> None:
    result = load_mod.load_set(spec, root="odata", storage=storage)

    assert result.status == load_mod.STATUS_FAILED
    assert "--fetch" in result.error


def test_rows_stream_in_column_order_preserving_nulls(spec, storage) -> None:
    """None means SAP sent no value, which is not the same as a blank."""
    _land(storage, spec, rows=[{"Matnr": "11", "Werks": "1500"}])
    key = "odata/ZVZI_KPI02_SHARED_SRV/MaterialPlantSet/2026-09-21/data.jsonl"

    rows = list(load_mod._iter_rows(storage, key, ["Werks", "Matnr", "Eisbe"]))

    assert rows == [("1500", "11", None)]


# --- Deltas: declaration ----------------------------------------------------


def test_a_delta_is_direct_or_derived_never_both() -> None:
    with pytest.raises(ValueError):
        Delta(field="Aedat", via="PurchaseOrderSet", via_key="Ebeln")
    with pytest.raises(ValueError):
        Delta()


def test_every_declared_delta_filter_is_measured_honoured() -> None:
    """The bar is higher than check_filter's: unprobed is not good enough.

    An unprobed filter that turns out to be ignored returns HTTP 200 with the
    whole set, so a 'delta' would silently pull everything and be both wrong
    and slow. This test is what stops one being added on a guess.
    """
    assert check_delta_filters() == []


def test_change_documents_have_no_delta() -> None:
    """Udate and Changenr were never probed, so they stay on full pulls."""
    assert spec_for("ChangeDocHeaderSet").delta is None
    assert spec_for("ChangeDocItemSet").delta is None


def test_movement_items_are_derived_because_their_own_filters_500() -> None:
    """Filtering MSEG by BudatMkpf or Ebeln returns HTTP 500; MKPF is the way in."""
    delta = spec_for("GoodsMovementItemSet").delta

    assert delta.field is None
    assert (delta.via, delta.via_key) == ("MaterialDocumentHeaderSet", "Mblnr")


# --- Deltas: literals and serialisation -------------------------------------


def test_datetime_literals_use_odata_syntax() -> None:
    """A date written as a plain string is a type mismatch SAP reports as 500."""
    literal = fetch_mod.odata_literal("2026-09-01T00:00:00", "Edm.DateTime")

    assert literal == "datetime'2026-09-01T00:00:00'"


def test_datetime_literals_drop_the_offset() -> None:
    """SAP rejects a literal carrying one."""
    moment = datetime(2026, 9, 1, 12, 30, tzinfo=timezone.utc)

    assert fetch_mod.odata_literal(moment, "Edm.DateTime") == (
        "datetime'2026-09-01T12:30:00'"
    )


def test_string_literals_escape_the_quote() -> None:
    assert fetch_mod.odata_literal("O'Brien", "Edm.String") == "'O''Brien'"


def test_decoded_datetimes_survive_serialisation() -> None:
    """The envelope decodes Edm.DateTime to a real datetime, which json.dumps
    cannot write. Without a default, any set carrying a date would crash."""
    assert fetch_mod._json_default(datetime(2026, 9, 1)) == "2026-09-01T00:00:00"
    assert fetch_mod._json_default(Decimal("2.000")) == "2.000"


# --- Deltas: fetching -------------------------------------------------------


@pytest.fixture
def no_watermark_io(monkeypatch):
    """Keep the watermark table out of these tests; record what was written."""
    written: list[tuple] = []
    monkeypatch.setattr(fetch_mod, "get_watermark", lambda *a, **k: None)
    monkeypatch.setattr(
        fetch_mod, "set_watermark", lambda *args: written.append(args)
    )
    return written


def test_a_direct_delta_filters_on_its_date(storage, no_watermark_io) -> None:
    po = spec_for("PurchaseOrderSet")
    client = StubClient(StubExtract([{"Ebeln": "4500001", "Aedat": "20260910"}]))

    fetch_mod.fetch_set(
        po, root="odata", client=client, storage=storage,
        run_date=RUN_DATE, mode="delta", since="2026-09-01T00:00:00",
    )

    assert client.filters == ["Aedat ge datetime'2026-09-01T00:00:00'"]


def test_a_first_delta_with_no_watermark_pulls_in_full(storage, no_watermark_io) -> None:
    """There is nothing to be incremental from yet, and it says so."""
    po = spec_for("PurchaseOrderSet")
    client = StubClient(StubExtract([{"Ebeln": "4500001"}]))

    result = fetch_mod.fetch_set(
        po, root="odata", client=client, storage=storage,
        run_date=RUN_DATE, mode="delta",
    )

    assert client.filters == [None]
    assert result.ok


def test_a_derived_delta_batches_the_parent_keys(storage, no_watermark_io) -> None:
    """The filter is a chain of `or`s in a URL, so it has to be chunked."""
    items = spec_for("PurchaseOrderItemSet")
    keys = [f"45000{n:05d}" for n in range(fetch_mod.KEY_BATCH + 10)]
    client = StubClient(StubExtract([]))

    fetch_mod.fetch_set(
        items, root="odata", client=client, storage=storage,
        run_date=RUN_DATE, mode="delta", parent_keys=keys,
    )

    assert len(client.calls) == 2, "60 keys at 50 per request is two requests"
    assert client.filters[0].count(" or ") == fetch_mod.KEY_BATCH - 1


def test_a_derived_delta_with_no_changed_parents_asks_sap_nothing(
    storage, no_watermark_io
) -> None:
    items = spec_for("PurchaseOrderItemSet")
    client = StubClient(StubExtract([]))

    result = fetch_mod.fetch_set(
        items, root="odata", client=client, storage=storage,
        run_date=RUN_DATE, mode="delta", parent_keys=[],
    )

    assert client.calls == []
    assert result.rows == 0
    assert result.ok


def test_duplicates_are_counted_across_chunks_not_within_them(
    storage, no_watermark_io
) -> None:
    """No single request would notice the same row arriving from two chunks."""
    items = spec_for("PurchaseOrderItemSet")
    same = {"Ebeln": "4500001", "Ebelp": "00010"}
    client = StubClient(StubExtract([same]))
    keys = [f"45000{n:05d}" for n in range(fetch_mod.KEY_BATCH + 1)]

    result = fetch_mod.fetch_set(
        items, root="odata", client=client, storage=storage,
        run_date=RUN_DATE, mode="delta", parent_keys=keys,
    )

    assert result.duplicate_keys == 1
    assert result.ok is False


def test_a_delta_file_is_marked_for_merging_not_replacing(
    storage, no_watermark_io
) -> None:
    """Replacing the table with an increment would leave it looking complete."""
    po = spec_for("PurchaseOrderSet")
    client = StubClient(StubExtract([{"Ebeln": "4500001", "Aedat": "20260910"}]))

    result = fetch_mod.fetch_set(
        po, root="odata", client=client, storage=storage,
        run_date=RUN_DATE, mode="delta", since="2026-09-01T00:00:00",
    )

    assert fetch_mod.read_manifest(storage, result.prefix)["load_strategy"] == "merge"


def test_a_full_file_is_marked_for_replacing(spec, storage) -> None:
    result = _land(storage, spec)

    assert fetch_mod.read_manifest(storage, result.prefix)["load_strategy"] == "replace"


# --- Deltas: the watermark --------------------------------------------------


def test_the_watermark_advances_to_the_highest_value_seen(
    storage, no_watermark_io
) -> None:
    po = spec_for("PurchaseOrderSet")
    client = StubClient(StubExtract([
        {"Ebeln": "1", "Aedat": "2026-09-10"},
        {"Ebeln": "2", "Aedat": "2026-09-14"},
        {"Ebeln": "3", "Aedat": "2026-09-12"},
    ]))

    fetch_mod.fetch_set(
        po, root="odata", client=client, storage=storage,
        run_date=RUN_DATE, mode="delta", since="2026-09-01T00:00:00",
    )

    assert no_watermark_io == [("PurchaseOrderSet", "Aedat", "2026-09-14", 3)]


def test_the_watermark_does_not_advance_on_an_unstable_pull(
    storage, no_watermark_io
) -> None:
    """Advancing past rows that were never landed loses them silently."""
    po = spec_for("PurchaseOrderSet")
    duplicated = {"Ebeln": "4500001", "Aedat": "2026-09-14"}
    client = StubClient(StubExtract([duplicated, dict(duplicated)]))

    fetch_mod.fetch_set(
        po, root="odata", client=client, storage=storage,
        run_date=RUN_DATE, mode="delta", since="2026-09-01T00:00:00",
    )

    assert no_watermark_io == []


def test_a_derived_child_never_advances_a_watermark(storage, no_watermark_io) -> None:
    """It was filtered by its parent's keys, so it measured no position of its own."""
    items = spec_for("PurchaseOrderItemSet")
    client = StubClient(StubExtract([{"Ebeln": "1", "Ebelp": "00010"}]))

    fetch_mod.fetch_set(
        items, root="odata", client=client, storage=storage,
        run_date=RUN_DATE, mode="delta", parent_keys=["4500001"],
    )

    assert no_watermark_io == []


# --- Sets SAP will not serve bare ------------------------------------------


def test_change_documents_declare_a_required_filter() -> None:
    """A bare read of either returns HTTP 400 -- measured, see probes.csv."""
    assert spec_for("ChangeDocHeaderSet").required_filter == (
        "Objectclas eq 'MATERIAL'"
    )
    assert spec_for("ChangeDocItemSet").required_filter == (
        "Objectclas eq 'MATERIAL'"
    )


def test_most_sets_need_no_predicate(spec) -> None:
    assert spec.required_filter is None


def test_a_required_filter_is_sent_on_a_full_pull(storage) -> None:
    """Without it the sweep 400s on these two and loses them."""
    cdhdr = spec_for("ChangeDocHeaderSet")
    client = StubClient(StubExtract([{"Objectclas": "MATERIAL", "Changenr": "1"}]))

    fetch_mod.fetch_set(
        cdhdr, root="odata", client=client, storage=storage, run_date=RUN_DATE
    )

    assert client.filters == ["Objectclas eq 'MATERIAL'"]


def test_the_required_filter_is_recorded_as_provenance(storage) -> None:
    """The table holds MATERIAL-class documents, not every change document."""
    cdhdr = spec_for("ChangeDocHeaderSet")
    client = StubClient(StubExtract([{"Objectclas": "MATERIAL", "Changenr": "1"}]))

    result = fetch_mod.fetch_set(
        cdhdr, root="odata", client=client, storage=storage, run_date=RUN_DATE
    )

    manifest = fetch_mod.read_manifest(storage, result.prefix)
    assert manifest["required_filter"] == "Objectclas eq 'MATERIAL'"


def test_predicates_are_bracketed_when_combined() -> None:
    """An unbracketed `or` in either half would quietly widen the result."""
    combined = fetch_mod.combine("Objectclas eq 'MATERIAL'", "A eq 1 or B eq 2")

    assert combined == "(Objectclas eq 'MATERIAL') and (A eq 1 or B eq 2)"


def test_combining_one_predicate_leaves_it_alone() -> None:
    assert fetch_mod.combine(None, "A eq 1") == "A eq 1"
    assert fetch_mod.combine(None, None) is None


def test_highest_ignores_blanks() -> None:
    rows = [{"A": "2026-09-01"}, {"A": None}, {"A": ""}, {"A": "2026-09-09"}]

    assert highest(rows, "A") == "2026-09-09"
    assert highest([{"A": None}], "A") is None
