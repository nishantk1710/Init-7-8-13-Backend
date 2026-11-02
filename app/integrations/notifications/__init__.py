"""The shared notification service used by every module.

See ``docs/notifications.md`` for how it works and what has to be configured.

* ``service``      -- ``enqueue`` (write the outbox) and ``dispatch_pending`` (deliver it)
* ``resolver``     -- who holds a role at a plant, from the ``notification_recipients`` table
* ``templates``    -- all user-facing wording (no internal module names)
* ``port``         -- the interface a delivery adapter implements
* ``logging_adapter`` -- records an email without sending it (the default)
* ``graph_adapter``   -- Microsoft Graph, used once it is configured
* ``scheduler``    -- the background timer started with the backend
"""
