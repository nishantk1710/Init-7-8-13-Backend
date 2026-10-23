"""Raw tables no CSV extract fills: the OData set is their only route from SAP.

Every other ``raw_<table>`` is built by the CSV full pull and kept current by
merging OData into it (``csv_tables`` and ``raw_merge``). The sets here have no
CSV table behind them, so the weekly OData full pull (``ODATA_FULL_PULL_SETS``)
both creates the raw table, the first time, and keeps it current after that --
by the same merge: matched on the key, changed rows updated, new rows inserted,
nothing deleted and nothing replaced wholesale.

The table name is the one the normalise layer already reads
(``app.shared.sap_normalise.TABLES``), so a pull lands where the screens look.
A table that was loaded another way first -- ``raw_dept_codes`` from VZI's
workbook on 21 Oct 2026 -- is merged into in its own column names: the
normalise mapping lists every spelling a column has had, and the merge uses
it (``raw_merge.resolve``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OdataTable:
    """One OData set and the raw table it alone fills."""

    entity_set: str

    table: str
    """The normalise layer's name for it: ``raw_<table>`` read through ``n_<table>``."""

    plant_property: str | None = None
    """The set's plant property, when only in-scope plants belong in the table.

    The set answers for every plant of the SAP client; the platform serves two
    (``app.shared.plant_scope``). Rows for the others are left out when merged
    rather than stored and filtered by every reader."""

    note: str = ""

    @property
    def raw_table(self) -> str:
        return f"raw_{self.table}"

    @property
    def sap_table(self) -> str:
        """For messages that name the table the way the CSV route does."""
        return self.table.upper()


ODATA_TABLES: tuple[OdataTable, ...] = (
    # VZI's department list -- plant, department code, name -- exposed by SAP
    # on DEV and QA from 22-23 Oct 2026 (ZMM_KPI02_ADD_SRV, key Werks +
    # DeptCode). Until then it came from "Plant Dept Codes 1.XLSX", loaded into
    # raw_dept_codes by hand on 21 Oct; the pull merges into that same table,
    # so the assistant's department list (app/assistant/departments.py) is
    # unchanged in shape and stays current without another upload. QA answers
    # 379 codes across 13 plants, 119 of them at 1300 and 1500 -- exactly the
    # workbook's count.
    OdataTable(
        "DepartmentCodeSet", "dept_codes", plant_property="Werks",
        note="plant department codes; replaces the workbook upload",
    ),
)

BY_ENTITY_SET: dict[str, OdataTable] = {t.entity_set: t for t in ODATA_TABLES}
