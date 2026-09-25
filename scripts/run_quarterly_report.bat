@echo off
REM Scheduled entry point for the I07 Quarterly Deep-Dive Report.
REM Registered as the Windows Task Scheduler task "I07 Quarterly Report"
REM (quarterly, day 1 of Jan/Apr/Jul/Oct). See app/reporting/generate_quarterly.py
REM for what --auto does: resolves the most recently completed quarter and
REM skips the run if that quarter already has a COMPLETED report.
REM
REM ALLOW_NON_AZURE_SQL=1 is set here because THIS machine's DATABASE_URL
REM points at a local Postgres dev stand-in, not Azure SQL (see
REM app/core/db.py::require_azure_sql). A real deployed environment already
REM has DATABASE_URL pointing at Azure SQL and must NOT set this -- do not
REM copy this line into a production scheduled task.
set ALLOW_NON_AZURE_SQL=1
cd /d "%~dp0.."
.venv\Scripts\python.exe -m app.reporting.generate_quarterly --auto
