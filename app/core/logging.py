"""Central logging setup.

Logs go to stdout so the hosting platform (Azure App Service, a container
runtime, or a local terminal) owns collection. Application Insights can be
added later by attaching an extra handler here -- no call site has to change.
"""

import logging
import sys

from app.core.config import Settings

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def configure_logging(settings: Settings) -> None:
    """Configure root logging for the application process."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Prefer this over ``logging.getLogger`` directly."""
    return logging.getLogger(name)
