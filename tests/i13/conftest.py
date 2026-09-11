"""Shared fixtures for Initiative 13 tests.

Unit tests build small in-memory CSVs under a temp directory and construct a
real ``SapGateway`` against them -- exercising the exact CSV-loading /
coercion path production uses, without depending on the full generated
dataset. ``tests/i13/test_api.py`` covers the one true end-to-end path
against the real generated CSVs.
"""

import csv
from pathlib import Path

import pytest

from app.initiatives.i13.config import I13Config, build_i13_config
from app.integrations.sap.gateway import SapGateway


def write_csv(path: Path, header: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in header})


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def gateway(data_dir: Path) -> SapGateway:
    return SapGateway(data_dir)


@pytest.fixture
def i13_config() -> I13Config:
    from app.core.config import Settings

    return build_i13_config(Settings())
