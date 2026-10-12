"""Fixtures for the criticality-report tests: an in-memory SQLite database that
holds only the tables this feature owns, so nothing here needs Azure SQL.

The whole ``Base.metadata`` cannot be created on SQLite -- ``i7_oar_target``
declares ``ix_i7_oar_target_status`` twice -- so only the four tables the
feature reads and writes are built.
"""

from __future__ import annotations

from datetime import datetime
from io import BytesIO

import openpyxl
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.criticality_report import CriticalityCurrent, CriticalityReportRow, CriticalityReportUpload
from app.models.ingestion import IngestionRun

HEADER = ["Sl.No", "Mat.Code", "Material Descrip", "Plant", "MatTyp", "Criticality", "MRP Type"]


@pytest.fixture
def factory():
    # StaticPool keeps ONE connection, so every session sees the same in-memory
    # database; the default pool would give each checkout its own empty one.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(
        engine,
        tables=[
            CriticalityReportUpload.__table__,
            CriticalityReportRow.__table__,
            CriticalityCurrent.__table__,
            IngestionRun.__table__,
        ],
    )
    return sessionmaker(bind=engine)


@pytest.fixture
def session(factory):
    with factory() as s:
        yield s


def workbook(rows, *, plant="1300", data_sheet="Sheet2", title_row=True, header=HEADER) -> BytesIO:
    """A workbook shaped like the delivered reports: a pivot sheet first, then
    the data sheet with an empty row above its header.

    ``rows`` are ``(material, tier)`` pairs or ``(material, tier, plant)``.
    """
    book = openpyxl.Workbook()
    book.active.title = "Pivot"
    book.active.append(["Row Labels", "Count of Mat.Code"])
    sheet = book.create_sheet(data_sheet)
    if title_row:
        sheet.append([])
    sheet.append(header)
    for number, row in enumerate(rows, start=1):
        material, tier = row[0], row[1]
        row_plant = row[2] if len(row) > 2 else plant
        sheet.append([number, material, f"desc {material}", row_plant, "ZREP", tier, "PD"])
    out = BytesIO()
    book.save(out)
    out.seek(0)
    return out


def at(year: int, month: int, day: int = 1, hour: int = 9) -> datetime:
    return datetime(year, month, day, hour)
