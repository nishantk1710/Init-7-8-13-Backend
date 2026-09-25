#!/usr/bin/env bash
# App Service startup command.
#
# Set this as the App Service "Startup Command" for app-vzi-aicom-nonprod-san:
#
#     bash /home/site/wwwroot/startup.sh
#
# Without it the Oryx builder guesses, and its guess for a FastAPI app is
# gunicorn with the default sync worker -- which cannot run an ASGI application
# and fails with an opaque worker error rather than saying so.
#
# NOTE FOR WINDOWS EDITORS: this file must keep LF line endings. A CRLF here
# produces "/usr/bin/env: 'bash\r': No such file or directory" on Linux, which
# is one of the least obvious error messages in the business. .gitattributes
# pins it; do not override that.

set -euo pipefail

cd /home/site/wwwroot

# --- Migrations -----------------------------------------------------------
#
# Deliberately opt-in and OFF by default.
#
# The GitHub runner cannot do this: sql-vzi-aicom-nonprod-san has public network
# access disabled, so `alembic upgrade head` in the pipeline would fail no matter
# what secrets it held. Inside the App Service, on the other side of the private
# endpoint, it works -- which is why it lives here rather than in deploy.yml.
#
# It stays off by default because nothing serialises it across instances: scale
# this App Service past one instance with it enabled and two workers can run the
# same migration at once. With one instance it is safe and convenient. Turn it
# on with the app setting RUN_MIGRATIONS_ON_STARTUP=true, or run it by hand from
# the SSH console -- see README, "Deployment".
if [ "${RUN_MIGRATIONS_ON_STARTUP:-false}" = "true" ]; then
    echo "startup: applying migrations"
    python -m alembic upgrade head
else
    echo "startup: skipping migrations (RUN_MIGRATIONS_ON_STARTUP is not 'true')"
fi

# --- Server ---------------------------------------------------------------
#
# gunicorn supervises; uvicorn workers actually speak ASGI. A worker that dies
# is replaced instead of taking the site down.
#
# WEB_CONCURRENCY is gunicorn's own variable and App Service sets PORT, so both
# are tunable as app settings without touching this file.
#
# ONE worker by default. The I13 screens are served from an in-memory snapshot
# held per process (app/initiatives/i13/snapshot.py): a second worker builds and
# holds its own copy (~1.3 GB, ~40 s), and a plan captured through the assistant
# refreshes the WATCH row only in the worker that handled it, so the other would
# serve that material stale until its next rebuild. Scale out by persisting the
# snapshot first (plan doc section 4.7, "Phase B"), not by raising this.
exec python -m gunicorn app.main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers "${WEB_CONCURRENCY:-1}" \
    --bind "0.0.0.0:${PORT:-8000}" \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
