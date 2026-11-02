"""W6.6 ``NotificationPort`` adapters -- now in the shared notification package.

``LoggingNotificationAdapter`` and ``NullNotificationAdapter`` moved to
``app.integrations.notifications.logging_adapter`` so I07, I08 and I13 share one
implementation. They are re-exported here so existing imports keep working.
"""

from app.integrations.notifications.logging_adapter import (  # noqa: F401
    LoggingNotificationAdapter,
    NullNotificationAdapter,
)
