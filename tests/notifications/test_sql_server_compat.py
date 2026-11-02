"""The notification queries must be valid on SQL Server, the production database.

SQLite accepts ``col IS 1`` and SQL Server does not (CI's SQL Server run caught exactly this in the
role lookup, the SLA lookup, the reminder job and the inbox). Every statement these flows run is
compiled for SQL Server and checked, and the source is scanned for the same comparison.
"""

from __future__ import annotations

import pathlib
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import event
from sqlalchemy.dialects import mssql

from app.initiatives.i7.reminders import run_reminders
from app.integrations.notifications import service
from app.integrations.notifications.logging_adapter import LoggingNotificationAdapter
from app.integrations.notifications.config_store import active_sla_for
from app.integrations.notifications.resolver import resolve_role
from app.integrations.notifications.models import NotificationChannel, NotificationIntent
from app.models.notification import NotificationRecipient, NotificationSla
from tests.notifications.test_i07_approvals import BASE, SUBMITTER, configure, recommendation, submit

BOOLEAN_IS = re.compile(r"\bIS\s+[01]\b", re.IGNORECASE)
OID = "11111111-1111-1111-1111-111111111111"


def record_statements_as_sql_server(db) -> list[str]:
    seen: list[str] = []

    @event.listens_for(db.get_bind(), "before_execute")
    def _record(conn, clauseelement, multiparams, params, execution_options):
        try:
            seen.append(str(clauseelement.compile(dialect=mssql.dialect(), compile_kwargs={"literal_binds": True})))
        except Exception:  # noqa: BLE001 -- statements that cannot be compiled standalone are not interesting here
            pass

    return seen


def test_no_notification_module_writes_a_boolean_is_comparison():
    roots = [
        "app/integrations/notifications",
        "app/api/notifications",
        "app/initiatives/i7/notifications.py",
        "app/initiatives/i7/reminders.py",
        "app/initiatives/i8/notifications.py",
        "app/services/notification_jobs.py",
    ]
    files = [p for root in roots for p in ([pathlib.Path(root)] if root.endswith(".py") else pathlib.Path(root).rglob("*.py"))]
    offenders = [
        f"{path}:{n}"
        for path in files
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"\.is_(not)?\((True|False)\)", line)
    ]
    assert offenders == [], "use the boolean column itself (`col` or `~col`): " + ", ".join(offenders)


def test_role_and_sla_lookups_are_valid_on_sql_server(db):
    seen = record_statements_as_sql_server(db)
    db.add(NotificationRecipient(role="Spares.PlantHead", plant="1300", user_oid=OID, active=True))
    db.add(NotificationSla(approval_route="ALL", first_reminder_minutes=1, repeat_minutes=1, max_reminders=1, active=True))
    db.commit()

    resolve_role(db, "Spares.PlantHead", "1300")
    active_sla_for(db, "OAR", "Spares.PlantHead")

    assert any("notification_recipients" in sql for sql in seen) and any("notification_sla" in sql for sql in seen)
    assert not [sql for sql in seen if BOOLEAN_IS.search(sql)], seen


def test_the_approval_reminder_and_inbox_flows_are_valid_on_sql_server(db, api):
    seen = record_statements_as_sql_server(db)
    configure(db)
    db.add(NotificationSla(approval_route="ALL", first_reminder_minutes=1, repeat_minutes=1, max_reminders=1, active=True))
    db.commit()
    rec = recommendation(db)

    submit(api, rec)                                                              # approval + enqueue + role lookup
    run_reminders(db, datetime.now(timezone.utc) + timedelta(minutes=5))          # SLA lookup + reminder state
    db.commit()
    service.dispatch_pending(db, LoggingNotificationAdapter(), base_url="https://x", retry_max=3, retry_interval_seconds=60)
    for path in ("", "?status=unread", "?status=read", "/unread-count"):
        api.get(f"/api/notifications{path}", headers={"X-Actor-Id": OID})
    api.post("/api/notifications/read-all", headers={"X-Actor-Id": OID})

    assert len(seen) > 20  # it really ran the flows
    bad = [sql for sql in seen if BOOLEAN_IS.search(sql)]
    assert not bad, bad[:3]


def test_the_i13_port_statements_are_valid_on_sql_server(db):
    seen = record_statements_as_sql_server(db)
    service.SharedNotificationPort(db).send(
        NotificationIntent(exception_id="EX-1", channel=NotificationChannel.PLATFORM_QUEUE, recipient=OID, subject="s", body="b", module="I13")
    )
    db.commit()
    assert seen and not [sql for sql in seen if BOOLEAN_IS.search(sql)]
