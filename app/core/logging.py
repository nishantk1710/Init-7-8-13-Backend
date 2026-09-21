"""Central logging setup.

Logs go to stdout so the hosting platform (Azure App Service, a container
runtime, or a local terminal) owns collection. Application Insights can be
added later by attaching an extra handler here -- no call site has to change.
"""

import logging
import sys

from app.core.config import Settings

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

# Third-party loggers that emit at INFO and drown everything else.
#
# The Azure SDK logs a full request and response block -- URL, every header,
# body note -- for each HTTP call it makes. One `app.ingest --list` makes 21 of
# them, so at INFO the twenty-one lines a person wanted arrive buried in some
# four hundred they did not. The information is genuinely useful when a call is
# misbehaving, which is what LOG_LEVEL=DEBUG is for; it is noise the rest of
# the time.
#
# Capped rather than silenced: a warning or an error from any of these still
# comes through, so a credential or transport problem is never hidden.
_NOISY_AT_INFO = (
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.identity",
    "urllib3.connectionpool",
)


def configure_logging(settings: Settings) -> None:
    """Configure root logging for the application process."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    level = settings.log_level.upper()
    root.setLevel(level)

    # Only when we are not deliberately debugging. At DEBUG the caller has
    # asked for everything, and quietly withholding the HTTP exchange would be
    # the opposite of what they wanted.
    if level != "DEBUG":
        for name in _NOISY_AT_INFO:
            logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Prefer this over ``logging.getLogger`` directly."""
    return logging.getLogger(name)
