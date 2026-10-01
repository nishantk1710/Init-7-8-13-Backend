# I07 Quarterly Report scheduling — what's code and what's Azure infrastructure

This document separates what ships in this repository from what someone with
Azure Portal/CLI access to VZI's subscription must still provision. **Nothing
in this document has been deployed by this change.** No Azure Logic App,
Function Timer, or WebJob exists today as a result of this PR — only the
application-side support for one to call is in place.

## Why this exists

The I07 Quarterly Deep-Dive Report was originally triggered by a Windows Task
Scheduler job ("I07 Quarterly Report", running `generate_quarterly.py --auto`)
against a Windows host. That job cannot exist on this app's current deployment
target, **Azure App Service for Linux**
(`app-vzi-aicom-nonprod-san`) — there is no Windows Task Scheduler on Linux,
and no equivalent was ever built for this platform. As a result, after the
migration, nothing triggers quarterly report generation at all; the report
can still be produced correctly on demand (see below), but never
automatically.

## What's implemented in code (this PR)

- `POST /api/v1/i7/reports/quarterly/generate` accepts an **optional**
  `quarter` field. Called with an empty body (`{}`), the backend resolves the
  latest *closed* calendar quarter itself
  (`app.initiatives.i7.reporting.period.latest_closed_quarter`) and generates
  that — so whatever calls this endpoint never has to compute a quarter.
- `python -m app.reporting.generate_quarterly` (no arguments) does the same
  resolution. The documented explicit form,
  `python -m app.reporting.generate_quarterly --quarter "Q3 2026"`, is
  unchanged.
- Both paths are **idempotent per quarter** (upsert by `quarter`, pre-existing
  behavior, not changed by this PR) — calling either twice, or retrying after
  a timeout, overwrites the same row rather than creating a duplicate. This
  makes both paths **safe to retry** from any scheduler without a dedup
  mechanism on the caller's side.
- A manual "Generate latest closed quarter" button now exists in the I07
  Quarterly Reports UI, for a human to use while no automatic trigger is
  configured (or as a one-off regeneration at any other time).

None of this starts an in-process scheduler. There is still no celery, no
apscheduler, and no `while True: sleep(...)` loop anywhere in this
application — by design, per Azure App Service's own operating model: a
container can restart or scale to multiple instances at any time, and an
in-process timer would either run zero or N times depending on which
instance(s) happen to be running at the trigger moment.

## What is NOT implemented, and must be provisioned by someone with Azure access

An external, Azure-native trigger that calls the endpoint above on a
quarterly cadence. Two concrete options, either of which satisfies the
requirement — pick one:

### Option A — Azure Logic App (recommended: no code to maintain, managed retries)

1. Portal → Create a resource → **Logic App** (Consumption plan is sufficient
   for four calls a year) in the same resource group as
   `app-vzi-aicom-nonprod-san`.
2. Add a **Recurrence** trigger: Frequency = Month, Interval = 3, start date
   anchored so it fires on the 1st of January/April/July/October (the day
   after each calendar quarter closes — generation reads "closed" quarters,
   so firing a day or two late is harmless, but firing *before* the quarter
   closes would generate the wrong one; do not point this at the last day of
   the closing quarter).
3. Add an **HTTP** action:
   - Method: `POST`
   - URI: `https://app-vzi-aicom-nonprod-san-hkhabvd6dkegfbh4.southafricanorth-01.azurewebsites.net/api/v1/i7/reports/quarterly/generate`
   - Body: `{}`
   - Headers: `Content-Type: application/json`
4. Set the HTTP action's **retry policy** (Settings → Retry Policy) — a fixed
   or exponential policy with a few retries is safe to configure, because the
   endpoint is idempotent (see above). Set the action's timeout above the
   generation time (~40-50s today against current data volumes; leave
   headroom, e.g. 5 minutes) — the default HTTP action timeout can otherwise
   cut off a slow run and trigger an unnecessary retry mid-generation (still
   harmless given idempotency, just wasteful).
5. Save, then **Run Trigger → Run now** once to confirm it reaches the app
   and generates a report (check the I07 Quarterly Reports UI or
   `GET /reports/quarterly` for the new row), before relying on the schedule.

### Option B — Azure CLI, scripted (same outcome, for someone who prefers `az` over the Portal)

```bash
az logic workflow create \
  --resource-group <resource-group-name> \
  --location southafricanorth \
  --name i07-quarterly-report-trigger \
  --definition '{
    "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
    "triggers": {
      "Recurrence": {
        "type": "Recurrence",
        "recurrence": { "frequency": "Month", "interval": 3 }
      }
    },
    "actions": {
      "GenerateQuarterlyReport": {
        "type": "Http",
        "inputs": {
          "method": "POST",
          "uri": "https://app-vzi-aicom-nonprod-san-hkhabvd6dkegfbh4.southafricanorth-01.azurewebsites.net/api/v1/i7/reports/quarterly/generate",
          "headers": { "Content-Type": "application/json" },
          "body": {}
        }
      }
    }
  }'
```

Adjust `--resource-group` and the Recurrence's start time/timezone to land on
the 1st of Jan/Apr/Jul/Oct. This is illustrative — verify against the current
`az logic workflow` syntax for the CLI version in use, and test with
`az logic workflow trigger run` before trusting the schedule.

### Option C — Azure WebJob (if Logic Apps are not the team's preferred tool)

A scheduled (triggered) WebJob with a `settings.job` cron expression
(`0 0 0 1 1,4,7,10 *`) running a script that calls the same endpoint with
`curl` can substitute for Option A. This repo does not currently contain a
WebJob package for this purpose; one would need to be authored and deployed
alongside (or independently of) the main App Service deployment.

## Authentication — what's actually true today, not invented for this one endpoint

**Nothing in this application enforces authentication anywhere, on any
route**, including this one. `app/core/security.py` is an explicit,
documented placeholder: Azure Entra ID is the intended mechanism for
application users, but it is not implemented, and no interim mechanism
(API key, shared secret, mTLS) has been decided either. This is true of every
I07/I08/I13 route today — the quarterly-report generate endpoint is exactly
as exposed as, say, the recommendations list endpoint.

**This PR does not add bespoke auth to just this endpoint.** Doing so would
be inconsistent with the rest of the application and would pre-empt a
decision (`app/core/security.py`'s own stated plan) that belongs to the whole
app, not to one report. Until that decision is made:

- The Logic App/WebJob calling this endpoint needs no credential today — it
  will succeed exactly as an unauthenticated `curl` would.
- If this is unacceptable before auth lands app-wide, the narrowest
  Azure-native option *without inventing an in-app mechanism* is restricting
  network access: an Azure Front Door/Application Gateway rule, an App
  Service access restriction allowing only the Logic App's outbound IP range,
  or VNet integration between the Logic App and the App Service's private
  endpoint. These are infrastructure-level controls, configured outside this
  repository, and are not implemented by this PR either.
- When application-wide auth is implemented, this endpoint should require
  whatever the rest of the app requires — most likely a Logic App "Managed
  Identity" + Azure AD token acquisition, which Logic Apps support natively
  without a stored secret. Do not hard-code an API key or token into the
  Logic App definition or into this repository's source when that day comes;
  use Managed Identity, or an App Setting / Key Vault reference, matching
  this repo's existing secret-handling conventions (see `deploy.yml`'s own
  notes on `DATABASE_URL` via Key Vault reference).

## Retry / idempotency guarantee relied on above

`repository.save_report` upserts by `quarter` alone — generating the same
quarter twice (whether a deliberate manual re-run, or a Logic App retry after
a transient failure) overwrites that quarter's one row rather than creating a
duplicate. This was true before this PR and is unchanged by it; the
scheduling guidance above depends on it rather than re-implementing
deduplication at the trigger layer.

## Verifying the deployed trigger, once provisioned

```bash
# Confirm the endpoint resolves and generates without specifying a quarter:
curl -X POST https://app-vzi-aicom-nonprod-san-hkhabvd6dkegfbh4.southafricanorth-01.azurewebsites.net/api/v1/i7/reports/quarterly/generate \
  -H "Content-Type: application/json" -d '{}'

# Confirm the result is listed:
curl https://app-vzi-aicom-nonprod-san-hkhabvd6dkegfbh4.southafricanorth-01.azurewebsites.net/api/v1/i7/reports/quarterly

# Confirm explicit-quarter calls still work unchanged:
curl -X POST https://app-vzi-aicom-nonprod-san-hkhabvd6dkegfbh4.southafricanorth-01.azurewebsites.net/api/v1/i7/reports/quarterly/generate \
  -H "Content-Type: application/json" -d '{"quarter": "Q3 2026"}'
```
