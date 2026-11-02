"""Shared notification basics: wording, links, the logging adapter and the tables.

Pure logic and in-memory SQLite -- nothing here needs Azure SQL or a network.
"""

from __future__ import annotations

import re

from sqlalchemy import inspect

from app.integrations.notifications import templates
from app.integrations.notifications.links import absolute_url, is_safe_path
from app.integrations.notifications.logging_adapter import LoggingNotificationAdapter
from app.integrations.notifications.models import (
    NotificationChannel,
    NotificationIntent,
    NotificationOutcome,
)
CONTEXT = {"material": "5000092261", "plant": "1300", "comment": "Check the lead time", "pending_role": "Plant Head", "summary": "A repair is overdue."}


def test_no_internal_module_names_in_any_user_facing_text():
    pattern = re.compile(r"\bI\s?0?(7|8|13)\b|\bACT\b", re.IGNORECASE)
    for event_type in templates.known_event_types():
        rendered = templates.render(event_type, CONTEXT)
        for text in (rendered.title, rendered.body, rendered.button_label):
            assert not pattern.search(text), f"{event_type}: {text!r}"


def test_approval_required_wording_matches_the_agreed_example():
    rendered = templates.render(templates.APPROVAL_REQUIRED, CONTEXT)
    assert rendered.title == "Approval Required – Material Recommendation"
    assert "Plant 1300" in rendered.body
    assert "Material: 5000092261" in rendered.body
    assert rendered.button_label == "Open Review Workspace"


def test_missing_detail_renders_as_a_dash_instead_of_raising():
    rendered = templates.render(templates.APPROVAL_REJECTED, {"material": "5000092261"})
    assert "Comment: -" in rendered.body


def test_unknown_event_type_uses_the_general_template():
    assert templates.render("SOMETHING_NEW", {"summary": "Hello"}).body == "Hello"


def test_absolute_url_joins_base_and_path_without_a_double_slash():
    assert absolute_url("/approvals", "https://spares.example.com/") == "https://spares.example.com/approvals"


def test_absolute_url_is_none_without_a_base_or_with_an_unsafe_path():
    assert absolute_url("/approvals", "") is None
    assert absolute_url("//evil.example.com", "https://spares.example.com") is None
    assert absolute_url("https://evil.example.com", "https://spares.example.com") is None
    assert not is_safe_path(None)


def intent(recipient):
    return NotificationIntent(
        exception_id="EX-1", channel=NotificationChannel.EMAIL, recipient=recipient, subject="Subject", body="Body"
    )


def test_logging_adapter_never_reports_sent():
    result = LoggingNotificationAdapter().send(intent("someone@example.com"))
    assert result.outcome == NotificationOutcome.LOGGED_ONLY
    assert result.outcome != NotificationOutcome.SENT


def test_logging_adapter_fails_without_a_recipient():
    assert LoggingNotificationAdapter().send(intent(None)).outcome == NotificationOutcome.FAILED


def test_i13_still_imports_the_moved_types_and_adapter():
    from app.initiatives.i13.act.domain import NotificationChannel as DomainChannel
    from app.initiatives.i13.act_notifications import LoggingNotificationAdapter as Reexported

    assert DomainChannel is NotificationChannel
    assert Reexported is LoggingNotificationAdapter


def test_shared_tables_create_on_a_portable_database(db):
    tables = set(inspect(db.get_bind()).get_table_names())
    assert {
        "notification",
        "notification_recipients",
        "notification_sla",
        "notification_reminder_state",
        "notification_config_audit",
    } <= tables
