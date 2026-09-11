"""The thin normalise layer I08 reads through.

Why this exists
---------------
``app/seed/manifest.py`` carries an explicit warning on every raw table: the
raw layer mirrors the July spreadsheets, not the OData contract. Column names
are the extract's business labels (``item_category``, not ``Pstyp``), every
column is ``text``, and material numbers are unpadded. Code written straight
against ``raw_*`` has to be rewritten at CPI cutover, when the same data arrives
named ``Pstyp`` and padded to 18 characters.

The normalise layer that would fix this properly is not built and has no owner.
Rather than block, I08 reads through views that are named and typed to the OData
contract. Three jobs, each done once:

1. **Rename** to the OData property names, so the code above already speaks the
   language CPI will speak.
2. **Normalise keys at the boundary** -- ruling 5.1 enforced in one place
   instead of remembered at every call site.
3. **Cast text to real types**, so ``eindt < today`` compares dates instead of
   throwing ``operator does not exist: text < date``.

At cutover the view definitions change and nothing above them does.

Views, not tables
-----------------
A second physical copy would cost a load and then drift. A view costs nothing to
build, nothing to keep in sync, and is the only thing that changes at cutover.

One consequence to know about
-----------------------------
``app/seed/loader.py`` recreates a raw table with ``DROP TABLE IF EXISTS`` --
no CASCADE, deliberately, so nothing is destroyed silently. Postgres refuses to
drop a table a view depends on, so **re-seeding a table these views read will
fail while they exist**. Drop them first and put them back after::

    python -m app.initiatives.i8.views drop
    python -m app.seed --all
    python -m app.initiatives.i8.views create

``create`` is idempotent (``CREATE OR REPLACE``), so running it again is free.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, text

from app.core.db import get_engine
from app.core.logging import get_logger

logger = get_logger(__name__)

SQL_DIR = Path(__file__).parent / "sql"

# The views this module manages, in dependency order. Dropped in reverse.
#
# Listed explicitly rather than scraped from the directory: a stray .sql file
# should not become a database object, and `drop` must know exactly what it owns
# so it can never reach something it did not create.
VIEWS: tuple[str, ...] = (
    "v_mara",
    "v_makt",
    "v_marc",
    "v_mard",
    "v_ekko",
    "v_ekpo",
    "v_eket",
    "v_ekbe",
    "v_mseg",
    "v_lfa1",
    "v_zmm065",
)

FUNCTIONS: tuple[str, ...] = ("sap_key", "sap_date", "sap_num")

# Expression indexes on the NORMALISED material key.
#
# The views compute sap_key(material) on the fly, so the seed loader's plain
# index on `material` cannot serve a lookup or a prefix scan on `matnr`. Without
# these, every I08 query sequential-scans 133,508 MARD rows and 198,170 MSEG
# rows to find the few thousand that are 80-series.
#
# text_pattern_ops is what makes `matnr LIKE '80%'` index-scannable -- the
# default opclass only supports equality unless the database collation is C.
#
# They live on the raw tables, so they disappear when the seed recreates one and
# come back with `views create`. Built in well under a second each.
INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_raw_mara_i8key", "raw_mara", "sap_key(material)"),
    ("ix_raw_makt_i8key", "raw_makt", "sap_key(material)"),
    ("ix_raw_marc_i8key", "raw_marc", "sap_key(material)"),
    ("ix_raw_mard_i8key", "raw_mard", "sap_key(material)"),
    ("ix_raw_ekpo_i8key", "raw_ekpo", "sap_key(material)"),
    ("ix_raw_mseg_i8key", "raw_mseg", "sap_key(material)"),
    ("ix_raw_zmm065_bmm_i8key", "raw_zmm065_bmm", "sap_key(mat_code)"),
    ("ix_raw_zmm065_gb_i8key", "raw_zmm065_gb", "sap_key(mat_code)"),
)


def _scripts() -> list[Path]:
    """Every .sql file, in filename order -- functions (00_) before views."""
    return sorted(SQL_DIR.glob("*.sql"))


def ensure_views(engine: Engine | None = None) -> list[str]:
    """Create or replace every function and view. Returns the view names.

    Idempotent, and safe to run against a database that already has them.
    """
    engine = engine or get_engine()
    scripts = _scripts()
    if not scripts:
        raise FileNotFoundError(f"No .sql files found in {SQL_DIR}")

    with engine.begin() as connection:
        for script in scripts:
            connection.execute(text(script.read_text(encoding="utf-8")))
        for name, table, expression in INDEXES:
            connection.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {name} "
                    f"ON {table} (({expression}) text_pattern_ops)"
                )
            )
            # Without fresh statistics the planner does not know the new index
            # is selective and keeps choosing the sequential scan.
            connection.execute(text(f"ANALYZE {table}"))

    logger.info(
        "I08: %d views and %d indexes ready (%s)",
        len(VIEWS),
        len(INDEXES),
        ", ".join(VIEWS),
    )
    return list(VIEWS)


def drop_views(engine: Engine | None = None, *, drop_functions: bool = True) -> None:
    """Drop the views (and by default the cast helpers).

    Run this before re-seeding a raw table -- see the module docstring.
    """
    engine = engine or get_engine()
    with engine.begin() as connection:
        for name in reversed(VIEWS):
            connection.execute(text(f"DROP VIEW IF EXISTS {name}"))
        # Indexes before functions: an index built on sap_key() depends on it,
        # and Postgres refuses to drop a function something still uses.
        for name, _table, _expression in INDEXES:
            connection.execute(text(f"DROP INDEX IF EXISTS {name}"))
        if drop_functions:
            for name in FUNCTIONS:
                connection.execute(text(f"DROP FUNCTION IF EXISTS {name}(text)"))
    logger.info("I08: views, indexes and cast helpers dropped")


def missing_views(engine: Engine | None = None) -> list[str]:
    """Which managed views are not present. Empty means the layer is ready."""
    engine = engine or get_engine()
    with engine.connect() as connection:
        present = {
            row[0]
            for row in connection.execute(
                text(
                    "select table_name from information_schema.views "
                    "where table_schema = 'public'"
                )
            )
        }
    return [name for name in VIEWS if name not in present]


def main(argv: list[str] | None = None) -> int:
    """``python -m app.initiatives.i8.views create|drop|status``."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m app.initiatives.i8.views",
        description="Manage the I08 normalise views over the raw extract layer.",
    )
    parser.add_argument(
        "command",
        choices=("create", "drop", "status"),
        help="create: build or replace them (idempotent). "
        "drop: remove them, which you must do before re-seeding a raw table. "
        "status: report which are present; touches no DDL.",
    )
    arguments = parser.parse_args(argv)

    if arguments.command == "create":
        names = ensure_views()
        print(f"{len(names)} views ready: {', '.join(names)}")
        return 0

    if arguments.command == "drop":
        drop_views()
        print(f"{len(VIEWS)} views dropped.")
        return 0

    missing = missing_views()
    if not missing:
        print(f"All {len(VIEWS)} I08 views are present.")
        return 0
    print(f"{len(missing)} of {len(VIEWS)} views are MISSING: {', '.join(missing)}")
    print("Run: python -m app.initiatives.i8.views create")
    return 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
