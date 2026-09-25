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

# Resolve our own directory rather than hard-coding /home/site/wwwroot.
#
# Same result on App Service, but it also works from a test container, a local
# checkout, or a deployment slot on a different path. A hard-coded cd that is
# wrong fails as "No such file or directory" -- another opaque exit that says
# nothing about what actually went wrong.
# Find the directory that actually holds the application, rather than assuming
# it is the one this script sits in.
#
# That assumption held while the deployment shipped files straight into
# /home/site/wwwroot. It broke when Oryx switched to compressed output: the
# package is now a wwwroot/output.tar.zst extracted to /tmp/<hash>, and the
# loose files still sitting in wwwroot are leftovers from the last uncompressed
# deploy. cd'ing to wwwroot therefore landed in a directory with alembic/,
# tests/ and docs/ but no app/ at all -- "cannot import app.main", on a
# deployment that was in fact complete.
#
# Candidates in order of trust: the directory Oryx already put us in, the path
# it exports, then this script's own. First one holding app/main.py wins.
_resolve_app_root() {
    local candidate
    for candidate in "$PWD" "${APP_PATH:-}" "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; do
        if [ -n "$candidate" ] && [ -f "$candidate/app/main.py" ]; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

if APP_ROOT="$(_resolve_app_root)"; then
    cd "$APP_ROOT"
else
    echo "startup: FATAL - no app/main.py in any candidate directory." >&2
    echo "         cwd=$PWD" >&2
    echo "         APP_PATH=${APP_PATH:-<unset>}" >&2
    echo "         script dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" >&2
    echo "         If the deploy shipped wwwroot/output.tar.zst, the app lives" >&2
    echo "         in the extracted /tmp path, not beside this script." >&2
    exit 127
fi

# --- Preflight ------------------------------------------------------------
#
# Exit 127 means "command not found", and App Service reports it with no
# indication of WHICH command. These checks cost milliseconds and turn that
# into one line naming the actual problem.
if ! command -v python >/dev/null 2>&1; then
    echo "startup: FATAL - no 'python' on PATH. PATH=$PATH" >&2
    exit 127
fi

if ! python -c "import gunicorn" >/dev/null 2>&1; then
    echo "startup: FATAL - gunicorn is not importable. Did the build install" >&2
    echo "         requirements.txt? Try: python -m pip install -r requirements.txt" >&2
    exit 127
fi

if ! python -c "import app.main" >/dev/null 2>&1; then
    echo "startup: FATAL - cannot import app.main from $(pwd)" >&2
    echo "         Directory contains: $(ls -A | tr '\n' ' ')" >&2
    exit 127
fi

echo "startup: $(python -V 2>&1), cwd $(pwd)"

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
exec python -m gunicorn app.main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers "${WEB_CONCURRENCY:-2}" \
    --bind "0.0.0.0:${PORT:-8000}" \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
