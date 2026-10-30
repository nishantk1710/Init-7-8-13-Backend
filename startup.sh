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
# It stays off by default so a deploy never changes the schema by surprise.
# Instances that start together do not race: alembic/env.py holds a SQL Server
# application lock for the whole upgrade, so the second waits and then finds
# nothing to do. Turn it on with the app setting RUN_MIGRATIONS_ON_STARTUP=true,
# or run it by hand from the SSH console -- see README, "Deployment".
if [ "${RUN_MIGRATIONS_ON_STARTUP:-false}" = "true" ]; then
    echo "startup: applying migrations"
    python -m alembic upgrade head
else
    echo "startup: skipping migrations (RUN_MIGRATIONS_ON_STARTUP is not 'true')"
fi

# --- OAR text packages (optional) -----------------------------------------
#
# torch + sentence-transformers (requirements-oar-text.txt, about 1.3 GB unpacked) are not in
# requirements.txt: CI must not pull torch, and a rebuild must not reinstall it. They live in /home as
# ONE archive and are unpacked onto the container's local disk here.
#
# Why an archive and not the packages themselves in /home: /home is a network share, and torch is tens
# of thousands of small files. `import torch` from there took 1m45s (7s of CPU, the rest waiting on the
# share) -- past gunicorn's 120s --timeout for the first request that needed the model. From local disk
# it takes about 8s. One large file reads fast; many small ones do not.
#
# Build the archive once, from the App Service SSH console. The exact steps are in
# docs/i07_oar_similarity_engine.md ("Installing the model on the App Service"); in short:
#     python -m pip freeze > /tmp/constraints.txt
#     python -m pip install --target /tmp/oar-text -c /tmp/constraints.txt -r requirements-oar-text.txt
#     tar -C /tmp -cf /home/site/oar-text.tar oar-text
#
# Entirely optional and never fatal: no archive means no model, and the app runs as it always did (the
# OAR single-target refresh then uses the vectors a run stored -- see oar/refresh.py). A failure here only
# logs a warning. The archive is unpacked only when /tmp/oar-text is missing, i.e. once per container.
#
# HF_HOME defaults to a folder under /home so the ~440 MB of model weights are fetched once, not on
# every restart. Both can be overridden with app settings.
OAR_TEXT_ARCHIVE="${OAR_TEXT_ARCHIVE:-/home/site/oar-text.tar}"
OAR_TEXT_DIR=/tmp/oar-text
if [ -f "$OAR_TEXT_ARCHIVE" ]; then
    if [ ! -d "$OAR_TEXT_DIR" ]; then
        echo "startup: unpacking OAR text packages from $OAR_TEXT_ARCHIVE"
        # Unpacked beside the target and moved into place, so an interrupted unpack is never mistaken
        # for a complete one on the next start.
        if rm -rf /tmp/oar-text.unpack \
            && mkdir -p /tmp/oar-text.unpack \
            && tar -xf "$OAR_TEXT_ARCHIVE" -C /tmp/oar-text.unpack \
            && mv /tmp/oar-text.unpack/oar-text "$OAR_TEXT_DIR"; then
            rm -rf /tmp/oar-text.unpack
            echo "startup: OAR text packages unpacked"
        else
            rm -rf /tmp/oar-text.unpack "$OAR_TEXT_DIR"
            echo "startup: WARNING - could not unpack $OAR_TEXT_ARCHIVE; OAR text similarity is off on this instance" >&2
        fi
    fi
    if [ -d "$OAR_TEXT_DIR" ]; then
        export PYTHONPATH="$OAR_TEXT_DIR${PYTHONPATH:+:$PYTHONPATH}"
        export HF_HOME="${HF_HOME:-/home/site/hf}"
        echo "startup: OAR text packages on PYTHONPATH (HF_HOME=$HF_HOME)"
    fi
else
    echo "startup: no OAR text archive at $OAR_TEXT_ARCHIVE; embedding model not installed"
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
