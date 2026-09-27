"""MARS on the Azure SQL connection (``app.core.db.with_mars``).

Without it, reading a query while upserting on the same connection fails with
"Connection is busy with results for another command" -- which is how I07's
staging failed on its first batch against Azure SQL on 27 Sep. No database here:
only the URL the engine is built from.
"""

from __future__ import annotations

import pytest

from app.core.db import with_mars

AZURE = (
    "mssql+pyodbc://@sql-vzi-aicom-nonprod-san.database.windows.net:1433/sqldb-aicom"
    "?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&Authentication=ActiveDirectoryMsi"
)


def test_mars_is_switched_on_for_azure_sql() -> None:
    assert with_mars(AZURE).query["MARS_Connection"] == "Yes"


def test_the_rest_of_the_url_is_untouched() -> None:
    url = with_mars(AZURE)

    assert url.host == "sql-vzi-aicom-nonprod-san.database.windows.net"
    assert url.database == "sqldb-aicom"
    assert url.query["Authentication"] == "ActiveDirectoryMsi"
    assert url.query["Encrypt"] == "yes"
    assert url.query["driver"] == "ODBC Driver 18 for SQL Server"


@pytest.mark.parametrize("value", ["No", "yes"])
def test_an_explicit_choice_in_database_url_is_respected(value) -> None:
    url = with_mars(f"{AZURE}&mars_connection={value}")

    assert url.query["mars_connection"] == value
    assert "MARS_Connection" not in url.query


def test_a_password_survives() -> None:
    url = with_mars("mssql+pyodbc://user:p%40ss@host:1433/db?driver=ODBC+Driver+18+for+SQL+Server")

    assert url.password == "p@ss"
    assert url.query["MARS_Connection"] == "Yes"


def test_other_backends_are_left_alone() -> None:
    assert "MARS_Connection" not in with_mars("sqlite://").query
