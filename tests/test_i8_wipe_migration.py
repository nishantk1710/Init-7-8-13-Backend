"""The nonprod test-row wipe (alembic c5e1a9d37f02, 28-Oct-2026).

Run on in-memory SQLite with foreign keys on, and with SQLite triggers standing
in for the append-only ones: the migration's own trigger statements are
Postgres and SQL Server SQL, so the test swaps in SQLite statements that do the
same thing (drop the guard, put it back) and checks the order around them. The
Postgres and SQL Server statements themselves are pinned as strings.

What is worth pinning:

* only the listed ids go, and a justification only when it is NEW_ACQUISITION
* an amendment goes before what it supersedes, whatever the list order
* a listed declaration that an unlisted row still amends is kept, not forced
* each guard is off only around its delete and is back afterwards
* where none of the ids exist, nothing is touched (no trigger toggled)
* the revision chain still has one head
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.pool import StaticPool

from app.assistant.models import AssistantSession, Justification
from app.initiatives.i8.models import RepairAttestation
from app.models.base import Base

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "c5e1a9d37f02_wipe_i8_nonprod_test_rows.py"
AT = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def load_migration():
    spec = importlib.util.spec_from_file_location("wipe_i8_nonprod_test_rows", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration(monkeypatch):
    """The migration, with SQLite trigger statements and a log of each toggle."""
    module = load_migration()
    toggles: list[tuple[str, bool]] = []

    def sqlite_trigger_sql(dialect, table, enabled):
        toggles.append((table, enabled))
        name = module.TRIGGERS[table]
        if enabled:
            return (
                f"CREATE TRIGGER {name} BEFORE DELETE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
            )
        return f"DROP TRIGGER {name}"

    def sqlite_trigger_exists(bind, table):
        return (
            bind.execute(
                text("SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = :n"),
                {"n": module.TRIGGERS[table]},
            ).first()
            is not None
        )

    monkeypatch.setattr(module, "trigger_sql", sqlite_trigger_sql)
    monkeypatch.setattr(module, "_trigger_exists", sqlite_trigger_exists)
    module.toggles = toggles
    return module


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, _record):  # noqa: ANN001
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(
        engine,
        tables=[AssistantSession.__table__, Justification.__table__, RepairAttestation.__table__],
    )
    with engine.begin() as conn:
        for table in ("i8_attestation", "justification"):
            conn.execute(
                text(
                    f"CREATE TRIGGER {table}_no_update_or_delete BEFORE DELETE ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
                )
            )
    return engine


def attestation(id: str, supersedes: str | None = None) -> RepairAttestation:
    return RepairAttestation(
        id=id,
        material_id="8000005632",
        plant="1300",
        quantity=Decimal("1"),
        condition_description="test1",
        fault_category="SEAL_LEAK",
        recommendation="SCRAP",
        attestor="UNAUTHENTICATED-FRONTEND",
        attested_at=AT,
        supersedes=supersedes,
        amendment_reason="corrected" if supersedes else None,
    )


def justification(id: str, kind: str = "NEW_ACQUISITION") -> Justification:
    return Justification(
        id=id,
        kind=kind,
        reason_category="URGENT_BREAKDOWN",
        free_text="vddd",
        material_id="8000005632",
        plant="1300",
        author="UNAUTHENTICATED-FRONTEND",
        recorded_at=AT,
    )


def seed(engine, *rows) -> None:
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        for row in rows:  # one at a time: an amendment needs its original first
            session.add(row)
            session.flush()
        session.commit()


def run(engine, module) -> None:
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            module.upgrade()


def ids(engine, table: str) -> set[str]:
    with engine.connect() as conn:
        return {row[0] for row in conn.execute(text(f"SELECT id FROM {table}"))}


def triggers(engine) -> set[str]:
    with engine.connect() as conn:
        return {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
        }


class TestWipe:
    @pytest.fixture(autouse=True)
    def rows(self, engine):
        seed(
            engine,
            # A chain listed original-first: deleting in list order would break
            # the foreign key, so the migration must go amendment-first.
            attestation("ATT-A7A5435102EB"),
            attestation("ATT-ADAB09C6D24F", supersedes="ATT-A7A5435102EB"),
            attestation("ATT-79C283E0544F", supersedes="ATT-ADAB09C6D24F"),
            attestation("ATT-72F3B66A8848"),
            # Listed, but a row outside the list amends it.
            attestation("ATT-309449844D87"),
            attestation("ATT-KEEP00000002", supersedes="ATT-309449844D87"),
            # ...and through a listed amendment: both listed rows stay.
            attestation("ATT-031B4C399BA0"),
            attestation("ATT-CD95DABFF42B", supersedes="ATT-031B4C399BA0"),
            attestation("ATT-KEEP00000003", supersedes="ATT-CD95DABFF42B"),
            attestation("ATT-KEEP00000001"),
            justification("JUS-30CCC1115EB8"),
            justification("JUS-0520F2167032"),
            # A listed id that is not a NEW_ACQUISITION reason is left alone.
            justification("JUS-BC8889C31AE2", kind="QUANTITY_OVERRIDE"),
            justification("JUS-KEEP00000001"),
        )

    def test_deletes_exactly_the_listed_rows(self, engine, migration) -> None:
        run(engine, migration)

        assert ids(engine, "i8_attestation") == {
            "ATT-309449844D87",
            "ATT-031B4C399BA0",
            "ATT-CD95DABFF42B",
            "ATT-KEEP00000001",
            "ATT-KEEP00000002",
            "ATT-KEEP00000003",
        }
        assert ids(engine, "justification") == {"JUS-BC8889C31AE2", "JUS-KEEP00000001"}

    def test_each_guard_is_off_only_around_its_delete(self, engine, migration) -> None:
        run(engine, migration)

        assert migration.toggles == [
            ("i8_attestation", False),
            ("i8_attestation", True),
            ("justification", False),
            ("justification", True),
        ]
        assert triggers(engine) == {
            "i8_attestation_no_update_or_delete",
            "justification_no_update_or_delete",
        }
        with pytest.raises(DatabaseError, match="append-only"):
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM justification WHERE id = 'JUS-KEEP00000001'"))

    def test_running_it_again_does_nothing(self, engine, migration) -> None:
        run(engine, migration)
        migration.toggles.clear()

        run(engine, migration)

        assert migration.toggles == []
        assert len(ids(engine, "i8_attestation")) == 6


def test_a_database_without_the_ids_is_untouched(engine, migration) -> None:
    seed(engine, attestation("ATT-KEEP00000001"), justification("JUS-KEEP00000001"))

    run(engine, migration)

    assert migration.toggles == []
    assert ids(engine, "i8_attestation") == {"ATT-KEEP00000001"}
    assert ids(engine, "justification") == {"JUS-KEEP00000001"}


def test_the_trigger_statements_per_engine() -> None:
    module = load_migration()

    assert module.trigger_sql("postgresql", "i8_attestation", False) == (
        "ALTER TABLE i8_attestation DISABLE TRIGGER i8_attestation_no_update_or_delete"
    )
    assert module.trigger_sql("postgresql", "justification", True) == (
        "ALTER TABLE justification ENABLE TRIGGER justification_no_update_or_delete"
    )
    assert module.trigger_sql("mssql", "i8_attestation", False) == (
        "DISABLE TRIGGER i8_attestation_no_update_or_delete ON i8_attestation"
    )
    assert module.trigger_sql("mssql", "justification", True) == (
        "ENABLE TRIGGER justification_no_update_or_delete ON justification"
    )
    assert module.trigger_sql("sqlite", "justification", True) is None


def test_the_trigger_names_are_the_ones_the_migrations_created() -> None:
    versions = ROOT / "alembic" / "versions"
    created = "\n".join(
        (versions / name).read_text(encoding="utf-8")
        for name in (
            "b2f4c81d5a37_i8_attestation.py",
            "e3b7c2d9f104_i8_attestation_immutability_mssql.py",
        )
    )
    assert "CREATE TRIGGER i8_attestation_no_update_or_delete" in created
    # The assistant tables' trigger names are built from the table name.
    for name in ("d8c3b1f04e75_assistant_spine.py", "5c9e2a7d4f18_assistant_immutability_mssql.py"):
        source = (versions / name).read_text(encoding="utf-8")
        assert "CREATE TRIGGER {table}_no_update_or_delete" in source
        assert '"justification"' in source


def test_the_lists_are_the_ruled_ones() -> None:
    module = load_migration()

    assert len(module.ATTESTATION_IDS) == len(set(module.ATTESTATION_IDS)) == 9
    assert len(module.JUSTIFICATION_IDS) == len(set(module.JUSTIFICATION_IDS)) == 15
    assert all(i.startswith("ATT-") for i in module.ATTESTATION_IDS)
    assert all(i.startswith("JUS-") for i in module.JUSTIFICATION_IDS)


def test_one_head_after_the_wipe() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)

    assert len(script.get_heads()) == 1
    assert script.get_revision("c5e1a9d37f02").down_revision == "a6c4e2f19b70"
