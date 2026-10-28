"""wipe the I08 test declarations and test justifications on nonprod

These are the nonprod test rows the user ruled to wipe on 28-Oct-2026, before
UAT: 9 condition declarations (``i8_attestation``) and 15 NEW_ACQUISITION
justifications (``justification``) typed while the screens were being built,
with notes like ``test1``, ``vddd`` and ``njnvn``. One of the declarations is
what raised the "repaired against the assessment" finding, and they would all
be on screen at UAT.

On any other database the ids do not exist, so this is a no-op: every id is
looked up first, and nothing is touched (no trigger is disabled) when none of
them is there. Only the listed ids are deleted, and a justification only when
it is a NEW_ACQUISITION one.

Both tables are append-only, enforced by a trigger on Postgres and on SQL
Server (``i8_attestation_no_update_or_delete``, b2f4c81d5a37 / e3b7c2d9f104;
``justification_no_update_or_delete``, d8c3b1f04e75 / 5c9e2a7d4f18). Each
trigger is disabled only for its delete and enabled again straight after, in
the migration's transaction, so a failure rolls the disable back with it. A
declaration that amends another is deleted before the one it supersedes
(``i8_attestation.supersedes`` is a foreign key). A listed declaration that an
unlisted row still amends is left in place and reported, never forced.

Downgrade is a no-op: deleted audit rows cannot be put back.

Revision ID: c5e1a9d37f02
Revises: a6c4e2f19b70
Create Date: 2026-10-28 12:00:00.000000

"""
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c5e1a9d37f02"
down_revision: Union[str, Sequence[str], None] = "a6c4e2f19b70"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

#: The nonprod test declarations, as listed on 28-Oct-2026.
ATTESTATION_IDS: tuple[str, ...] = (
    "ATT-A7A5435102EB",
    "ATT-ADAB09C6D24F",
    "ATT-79C283E0544F",
    "ATT-309449844D87",
    "ATT-031B4C399BA0",
    "ATT-CD95DABFF42B",
    "ATT-87AE5410A6B8",
    "ATT-8BF057C8EB99",
    "ATT-72F3B66A8848",
)

#: The nonprod test justifications, all NEW_ACQUISITION, as listed on 28-Oct-2026.
JUSTIFICATION_IDS: tuple[str, ...] = (
    "JUS-30CCC1115EB8",
    "JUS-2259D557A01A",
    "JUS-BC8889C31AE2",
    "JUS-C19A7A656AC3",
    "JUS-A65C24B1C9E9",
    "JUS-D621FE3F4835",
    "JUS-84207FC0C561",
    "JUS-D2B27828FF59",
    "JUS-B4C5280B720F",
    "JUS-1F25BA4D7CB7",
    "JUS-A7134B6A7897",
    "JUS-A0A945A69360",
    "JUS-739EC52C4B60",
    "JUS-6FD8D58F31DA",
    "JUS-0520F2167032",
)

#: Each table's append-only trigger, by the name both engines give it.
TRIGGERS: dict[str, str] = {
    "i8_attestation": "i8_attestation_no_update_or_delete",
    "justification": "justification_no_update_or_delete",
}

_NEW_ACQUISITION = "NEW_ACQUISITION"


def _ids_param() -> sa.BindParameter:
    return sa.bindparam("ids", expanding=True)


def trigger_sql(dialect: str, table: str, enabled: bool) -> str | None:
    """The statement that disables or enables ``table``'s append-only trigger,
    or None on an engine that has none (SQLite in tests)."""
    trigger = TRIGGERS[table]
    verb = "ENABLE" if enabled else "DISABLE"
    if dialect == "postgresql":
        return f"ALTER TABLE {table} {verb} TRIGGER {trigger}"
    if dialect == "mssql":
        return f"{verb} TRIGGER {trigger} ON {table}"
    return None


def _trigger_exists(bind, table: str) -> bool:
    trigger = TRIGGERS[table]
    dialect = bind.dialect.name
    if dialect == "postgresql":
        query = "SELECT 1 FROM pg_trigger WHERE tgname = :name AND NOT tgisinternal"
    elif dialect == "mssql":
        query = "SELECT 1 FROM sys.triggers WHERE name = :name"
    else:
        return False
    return bind.execute(sa.text(query), {"name": trigger}).first() is not None


def _set_trigger(bind, table: str, enabled: bool) -> None:
    statement = trigger_sql(bind.dialect.name, table, enabled)
    if statement is not None:
        op.execute(statement)


def _present_attestations(bind) -> set[str]:
    rows = bind.execute(
        sa.text("SELECT id FROM i8_attestation WHERE id IN :ids").bindparams(_ids_param()),
        {"ids": list(ATTESTATION_IDS)},
    )
    return {row[0] for row in rows}


def _present_justifications(bind) -> set[str]:
    rows = bind.execute(
        sa.text(
            "SELECT id FROM justification WHERE id IN :ids AND kind = :kind"
        ).bindparams(_ids_param()),
        {"ids": list(JUSTIFICATION_IDS), "kind": _NEW_ACQUISITION},
    )
    return {row[0] for row in rows}


def _deletable_attestations(bind, present: set[str]) -> tuple[set[str], set[str]]:
    """Split ``present`` into the rows that can go and the rows that must stay.

    A listed declaration stays when a row outside the list amends it (directly,
    or through an amendment that itself has to stay): deleting it would break
    that row's ``supersedes`` foreign key, and the row is not ours to delete.
    """
    if not present:
        return set(), set()
    amended_by: dict[str, set[str]] = {}
    for amendment, original in bind.execute(
        sa.text(
            "SELECT id, supersedes FROM i8_attestation WHERE supersedes IN :ids"
        ).bindparams(_ids_param()),
        {"ids": sorted(present)},
    ):
        amended_by.setdefault(original, set()).add(amendment)

    kept: set[str] = set()
    changed = True
    while changed:
        changed = False
        for row_id in sorted(present - kept):
            if any(a not in present or a in kept for a in amended_by.get(row_id, ())):
                kept.add(row_id)
                changed = True
    return present - kept, kept


def _delete_attestations(bind, deletable: set[str]) -> int:
    """Delete ``deletable``, amendments before what they supersede.

    Each pass deletes the rows no remaining row amends; a row is then free once
    its amendment has gone. ``deletable`` is closed under amendment (see
    :func:`_deletable_attestations`), so every pass frees at least one row.
    """
    remaining = set(deletable)
    deleted = 0
    while remaining:
        amended = {
            row[0]
            for row in bind.execute(
                sa.text(
                    "SELECT supersedes FROM i8_attestation WHERE supersedes IN :ids"
                ).bindparams(_ids_param()),
                {"ids": sorted(remaining)},
            )
        }
        free = sorted(remaining - amended)
        if not free:
            break
        bind.execute(
            sa.text("DELETE FROM i8_attestation WHERE id IN :ids").bindparams(_ids_param()),
            {"ids": free},
        )
        deleted += len(free)
        remaining -= set(free)
    return deleted


def _delete_justifications(bind, present: set[str]) -> int:
    bind.execute(
        sa.text(
            "DELETE FROM justification WHERE id IN :ids AND kind = :kind"
        ).bindparams(_ids_param()),
        {"ids": sorted(present), "kind": _NEW_ACQUISITION},
    )
    return len(present)


def upgrade() -> None:
    """Delete the listed nonprod test rows, where they exist."""
    bind = op.get_bind()
    attestations, kept = _deletable_attestations(bind, _present_attestations(bind))
    justifications = _present_justifications(bind)
    if kept:
        logger.warning(
            "I08 test-row wipe: kept %s, as a declaration outside the list amends them",
            ", ".join(sorted(kept)),
        )
    if not attestations and not justifications:
        logger.info("I08 test-row wipe: none of the listed ids is here to delete; nothing to do")
        return

    if attestations:
        guarded = _trigger_exists(bind, "i8_attestation")
        if guarded:
            _set_trigger(bind, "i8_attestation", enabled=False)
        deleted = _delete_attestations(bind, attestations)
        if guarded:
            _set_trigger(bind, "i8_attestation", enabled=True)
        logger.info("I08 test-row wipe: %d test declaration(s) deleted", deleted)

    if justifications:
        guarded = _trigger_exists(bind, "justification")
        if guarded:
            _set_trigger(bind, "justification", enabled=False)
        deleted = _delete_justifications(bind, justifications)
        if guarded:
            _set_trigger(bind, "justification", enabled=True)
        logger.info("I08 test-row wipe: %d test justification(s) deleted", deleted)


def downgrade() -> None:
    """No-op: the deleted rows were test data and cannot be restored."""
