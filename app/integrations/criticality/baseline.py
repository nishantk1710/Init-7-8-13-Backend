"""Import the July delivery as each plant's first criticality report.

Before monthly uploads, criticality lived only in ``raw_zmm065_bmm`` and
``raw_zmm065_gb``, seeded from the delivered workbooks. Every reader now reads
``criticality_current`` instead, so those two tables are copied in once as
``source='july_delivery'`` reports for July 2026 -- after which an upload for
August simply sits on top, and the history shows the delivery as the first row.

Safe to run any number of times, from any instance:

* ``dedupe_key = 'july_delivery:<plant>'`` is unique, so a second run finds the
  row and does nothing.
* A plant that already has *any* report for July (somebody uploaded July
  themselves) is left alone: an upload always beats the delivery.
* A raw table that is not loaded is skipped, not an error -- ZMM065 is not part
  of the SAP pull, and on a database without the seed there is nothing to copy.

The raw tables are read leniently (``RowCollector(strict=False)``): the
delivery was not chosen by anybody, so nothing in it is refused -- whatever
strict mode would have refused is logged instead.

Run on its own with ``python -m app.integrations.criticality.baseline``; the
application also runs it once at start-up.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.core.db import get_sessionmaker
from app.core.logging import get_logger
from app.integrations.criticality import reports
from app.integrations.criticality.report_parser import RowCollector
from app.models.criticality_report import SOURCE_JULY_DELIVERY, CriticalityReportUpload
from sqlalchemy import select

logger = get_logger(__name__)

#: The month the delivery is for.
DELIVERY_MONTH = date(2026, 7, 1)

#: Shown in the history's File column.
DELIVERY_LABEL = "(July delivery)"

#: Raw table -> the plant it covers. Names are module constants, never input,
#: which is what makes interpolating them into SQL below safe.
DELIVERED: dict[str, str] = {
    "raw_zmm065_bmm": "1300",
    "raw_zmm065_gb": "1500",
}


def dedupe_key(plant: str) -> str:
    return f"{SOURCE_JULY_DELIVERY}:{plant}"


def import_july_delivery(session: Session) -> dict[str, str]:
    """Import whichever delivered tables are loaded and not yet imported.
    Returns ``plant -> outcome`` for logging and the CLI."""
    outcome: dict[str, str] = {}
    present = set(inspect(session.get_bind()).get_table_names())

    for table, plant in DELIVERED.items():
        if table not in present:
            outcome[plant] = f"skipped: {table} is not loaded"
            continue
        already = session.scalar(
            select(CriticalityReportUpload.id).where(CriticalityReportUpload.dedupe_key == dedupe_key(plant))
        )
        if already is not None:
            outcome[plant] = "skipped: already imported"
            continue
        if any(
            upload.report_month == DELIVERY_MONTH for upload in reports.effective_uploads(session, plant)
        ):
            outcome[plant] = "skipped: a July report was already uploaded"
            continue

        collector = RowCollector(plant, strict=False)
        result = session.execute(
            text(
                f"SELECT mat_code, plant, criticality, material_description, mattyp, mrp_type FROM {table}"
            )
        )
        for mat_code, row_plant, criticality, description, mattyp, mrp_type in result:
            collector.add(
                mat_code, row_plant, criticality, description=description, mattyp=mattyp, mrp_type=mrp_type
            )
        parsed = collector.finish(table)
        if not parsed.rows:
            outcome[plant] = f"skipped: {table} holds no rows for plant {plant}"
            continue
        for issue in parsed.issues:
            logger.warning("July delivery %s: %s", table, issue)

        reports.store_upload(
            session,
            parsed,
            report_month=DELIVERY_MONTH,
            file_name=DELIVERY_LABEL,
            uploaded_by=None,
            source=SOURCE_JULY_DELIVERY,
            dedupe_key=dedupe_key(plant),
        )
        outcome[plant] = f"imported {len(parsed.rows):,} materials"
    return outcome


def run() -> dict[str, str]:
    """Open a session and import. Never raises for a database that is missing
    or unreachable -- the caller is start-up, which must not fail over it."""
    try:
        with get_sessionmaker()() as session:
            outcome = import_july_delivery(session)
    except Exception as exc:  # noqa: BLE001 -- start-up must not depend on this
        logger.warning("criticality baseline not imported (%s)", exc)
        return {}
    for plant, result in outcome.items():
        logger.info("criticality baseline, plant %s: %s", plant, result)
    return outcome


if __name__ == "__main__":
    for plant, result in run().items():
        print(f"{plant}: {result}")
