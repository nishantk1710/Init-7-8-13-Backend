"""Environment-driven application settings.

Values are read from the process environment first and from a local ``.env``
file as a fallback, so the same code runs unchanged locally and on Azure App
Service (where settings are supplied as environment variables).

Nothing here is required for startup: every future-integration setting defaults
to empty so the app -- and the health endpoint -- come up with no SAP, database
or identity credentials present.
"""

from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The backend repository root (the folder holding app/). Anchors relative
# filesystem settings so they do not depend on the working directory.
BACKEND_ROOT = Path(__file__).resolve().parents[2]

# SAP DEV's iFlow route, the default target. See Settings.cpi_path.
DEFAULT_CPI_PATH = "/http/SAPECC/OdataConsumption"


def normalise_cpi_path(value: object) -> str:
    """``CPI_PATH`` as one canonical string: one leading slash, none trailing,
    no surrounding whitespace -- blank meaning the default. Compared, not just
    sent: it is what picks the discovery snapshot and what the database
    records as its source system, so ``/http/SAPECCQA/OdataConsumption/`` and
    `` /http/SAPECCQA/OdataConsumption`` must be the same system."""
    text = str(value or "").strip().strip("/")
    return f"/{text}" if text else DEFAULT_CPI_PATH


class Settings(BaseSettings):
    """Application settings. Field names map to upper-case env vars."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "local"
    app_name: str = "spares-ai-backend"
    app_version: str = "0.1.0"
    api_prefix: str = "/api"
    log_level: str = "INFO"

    # Browser origin(s) allowed to call this API. Comma-separated for multiple.
    frontend_origin: str = "http://localhost:3000"

    # Database connection, as a SQLAlchemy URL. Empty by default so the app --
    # and the liveness endpoint -- still start with no database present.
    #
    # This is the ONLY place the database is named, so moving between VZI
    # environments is a config change and never a code change.
    #
    # Azure SQL, and only Azure SQL. Local Postgres was a stand-in while VZI's
    # database was being provisioned; app/core/db.py now refuses any other
    # backend rather than letting it fail obscurely further in.
    #
    #   mssql+pyodbc://<user>:<password>@sql-vzi-aicom-nonprod-san.database.windows.net:1433/sqldb-aicom
    #     ?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no
    #
    # The driver, Encrypt and TrustServerCertificate parameters are not
    # optional: without Encrypt=yes Azure SQL refuses the connection, and with
    # TrustServerCertificate=yes it would succeed while trusting anything, which
    # is the same objection as cpi_ca_bundle below.
    #
    # Unreachable outside the VNet -- public network access is disabled and the
    # server sits behind pe-sqlvziaicomnonprod-sqlserver.
    database_url: str = ""

    # Echo every SQL statement to the log. Local debugging only.
    database_echo: bool = False

    # Seconds to wait for a connection before giving up. Deliberately short: a
    # database that is not running should fail in seconds with a clear message,
    # not hang. Raise it only for a genuinely slow network path.
    database_connect_timeout_seconds: int = 5

    # Seconds after which a pooled connection is replaced rather than reused.
    # Azure SQL cuts idle connections at around 30 minutes; App Service instances
    # sit idle between requests, so a pooled connection that looks fine can be
    # dead by the next call. Recycling below that window means the replacement
    # happens on our schedule instead of inside someone's request.
    database_pool_recycle_seconds: int = 1500

    # Connections one worker keeps open, how many more it may open under a
    # burst, and how long a request waits for one before failing. Requests and
    # every background job share this pool, and each job's lock (delta, full
    # refresh, snapshot builds, the I07 pipeline) holds a connection for its
    # whole run. SQLAlchemy's own 5 + 10 ran out on nonprod on 14 Oct 2026
    # with no load running ("QueuePool limit of size 5 overflow 10 reached").
    # The ceiling is size + overflow per worker, times workers, times
    # instances: keep that well under the database tier's worker limit.
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_pool_timeout_seconds: int = 30

    # Attempts at opening a connection when Azure SQL drops it while it is being
    # set up -- "TCP Provider: Error code 0x68 (104)", a serverless database
    # waking (40613), a failover. Microsoft's guidance is to retry these: they
    # pass in seconds, and without a retry each one is an error on someone's
    # screen. Only the connect is retried, never a statement. 1 = no retry.
    database_connect_attempts: int = 3
    # Seconds before the first retry; doubled for each one after.
    database_connect_retry_seconds: float = 2.0

    # Object storage location. Like database_url, this is the ONLY place storage
    # is named, and the adapter is chosen from the URL scheme.
    #
    #   abfss://<container>@stvziaicomnonprod.dfs.core.windows.net/<path>
    #
    # abfss:// is the only scheme supported. The local-folder adapter was a
    # stand-in and has been removed, so a filesystem path is refused with an
    # explanation rather than silently treated as something else.
    #
    # Empty by default so the app starts with no storage configured.
    storage_url: str = ""

    # Where `python -m app.ingest` lands what it pulls from CPI, as a key
    # prefix inside STORAGE_URL. A setting rather than a constant so one
    # environment's landing area can be moved, or a trial run pointed at a
    # scratch prefix, without a code change.
    #
    # Writing requires 'Storage Blob Data Contributor' on the identity. Reader
    # is enough for every other path in this app, so the fetch stage is the
    # first thing here that needs the stronger grant.
    ingest_prefix: str = "odata"

    # --- Delta schedule ---------------------------------------------------
    #
    # The timer runs inside this application rather than in a Logic App or a
    # Function, because adding an Azure resource was ruled out. That makes it
    # cheap to enable and cheap to get wrong, so it is OFF by default: a fresh
    # deployment does not start pulling from SAP because somebody shipped it.
    #
    # startup.sh runs one gunicorn worker by default, but WEB_CONCURRENCY or a
    # scale-out adds more, and each would run every delta.
    # app/ingest/scheduler.py holds a SQL Server application lock to make
    # exactly one of them win.
    delta_schedule_enabled: bool = False

    # Minutes between delta cycles. Floored at 5 in the scheduler: below that
    # a cycle can still be running when the next one is due.
    delta_interval_minutes: int = 60

    # Seconds to wait after startup before the first cycle, so a deploy does
    # not begin a pull while migrations may still be applying.
    delta_initial_delay_seconds: int = 120

    # Run the same delta cycle when SAP posts a purchase requisition to
    # /api/events/pr, instead of waiting for the next timer tick. Independent of
    # DELTA_SCHEDULE_ENABLED: the timer can stay off and an event still
    # triggers. Off by default for the same reason the timer is.
    delta_on_pr_event_enabled: bool = False

    # Seconds between a PR event and the cycle it triggers. PRs arrive in
    # bursts (a release, an MRP run), and every event inside the wait rides the
    # same cycle rather than starting one each.
    delta_event_debounce_seconds: int = 120

    # After a delta cycle that loaded rows, ask the I13 and I08 snapshots to
    # re-check their source fingerprints (app/ingest/scheduler.py) instead of
    # leaving it to their periodic watchers. Same gates as the watchers: an
    # unchanged fingerprint, a build in progress, the settle wait after a
    # load and the I13 rebuild gap all still apply.
    snapshot_refresh_after_delta: bool = True

    # --- CSV increments for the tables without an OData delta ---------------
    #
    # After the OData sweep, pull each table in CSV_DELTA_TABLES over the CSV
    # route for the window [its mark, today] and merge the rows into
    # raw_<table> by key -- appended, never replaced (app/ingest/csv_delta.py).
    # Off by default. A table with no mark is skipped, never pulled whole.
    csv_delta_enabled: bool = False

    # Comma-separated SAP tables. A table whose OData set has a runnable delta
    # is left out: that route already keeps it current. RESB and CDPOS are the
    # windowed tables the OData delta cannot cover (ReservationItemSet exposes
    # no creation date; ChangeDocItemSet rejects the only usable filter).
    csv_delta_tables: str = "RESB,CDPOS"

    # How long one windowed request may take to deliver its first chunk, and
    # how long a pause after the last chunk means the delivery finished.
    # Shorter than the whole-table pull's 15 and 5 minutes: a window with no
    # changes delivers nothing, and the cycle must not wait a quarter of an
    # hour per table to learn that.
    csv_delta_first_chunk_timeout_seconds: int = 300
    csv_delta_quiet_period_seconds: int = 120

    # --- Key index on the raw tables ------------------------------------------
    #
    # Whether a CSV full load indexes raw_<table> on the set's identity key, so
    # the merges into it seek instead of reading the whole table (app/ingest/
    # csv_load.py, KEY INDEX). Off by default: every table is then built
    # exactly as it always was -- every column nvarchar(max), no index -- and
    # an environment changes only when it is switched on. Takes effect at a
    # table's next full load; a table already loaded is never altered. Turn it
    # on before the first data load of an environment that should have it.
    raw_key_index_enabled: bool = False

    # --- Full refresh schedule -----------------------------------------------
    #
    # Once a day: every table over the CSV route, loaded, then the OData sets
    # below merged into the same raw tables. This is the baseline the deltas
    # merge into and the backstop that bounds their drift to a day -- see
    # app/ingest/scheduler.py. Off by default for the same reason the delta
    # timer is. A sweep takes ~15 minutes (the two tables SAP never delivers
    # wait out their timeout) and pushes ~120 MB through /api/events/csv.
    full_refresh_enabled: bool = False

    # Hour of day, UTC, to start it. 01:00 UTC is 03:00 in South Africa.
    full_refresh_hour_utc: int = 1

    # OData sets fetched whole after the sweep and merged into raw_<table>,
    # for fields the CSV extract does not carry. Comma-separated.
    full_refresh_odata_sets: str = "MaterialPlantSet"

    # --- Weekly OData full pull ---------------------------------------------
    #
    # Sets with no date SAP filters on, refreshed whole over OData and merged
    # into raw_<table> by key, once a week: matched rows updated only where a
    # value changed, new rows inserted, nothing deleted or replaced. MBEW is
    # the case: a price table with no change date (agreed with the SAP team
    # 2026-10-15). DepartmentCodeSet is the other: VZI's department list,
    # exposed by SAP on 22-23 Oct 2026, merged into raw_dept_codes (in-scope
    # plants only; the table is created by the first pull where it does not
    # exist). Small sets only -- a full OData read of a large one is refused at
    # the read ceiling. Off by default, like the other timers.
    odata_full_pull_enabled: bool = False

    # Comma-separated entity sets.
    odata_full_pull_sets: str = "MaterialValuationSet,DepartmentCodeSet"

    # Day of the week (0 = Monday ... 6 = Sunday) and hour, UTC. Sunday
    # 02:00 UTC is 04:00 in South Africa, after the I13 build window.
    odata_full_pull_weekday: int = 6
    odata_full_pull_hour_utc: int = 2

    # --- Delta marks for tables loaded another way --------------------------
    #
    # A set whose raw_<table> holds rows but has no watermark and no
    # ingestion_run load record (the 2026-10-09 migration left neither)
    # starts its first increment from this day, YYYY-MM-DD, instead of pulling
    # the whole set over OData -- which for RESB and CDPOS is past the read
    # ceiling, refused, every cycle. Set it to the day the extract behind the
    # tables was taken, less a few days. Blank means: no floor, pull in full.
    delta_seed_floor: str = ""

    # Keep the odata_<table> archive beside raw_<table>. A delta increment is
    # merged into raw_<table> (the table the pages read) and then written to
    # odata_<table>, which accumulates what OData has delivered since the
    # first increment. False skips that second write: the increment goes into
    # raw_<table> only. What that costs: the odata_ tables stop moving, so
    # anything that reads them to tell a delta row from a CSV row (the export
    # and reset scripts in /home/live_checks) goes stale. Since 23-Oct-2026 a
    # full OData pull (--full, the weekly MBEW and department-code pull) is
    # governed too: merged into raw_<table> only, odata_<table> neither
    # created nor replaced -- except odata_material_plant, which I07 reads
    # directly (load.ARCHIVE_READ_BY_APP) and a full load keeps writing.
    odata_archive_enabled: bool = True

    # --- Normalise views ----------------------------------------------------
    #
    # Build the n_<table> views (app/shared/sap_normalise.py) and I08's views on
    # top of them at start-up, before the I13 snapshot is built from them. A
    # fresh Azure SQL database has none, and every I08/I13 read goes through
    # them. The CSV and workbook loaders rebuild them after each load
    # regardless of this; it only governs start-up. Off for a process that
    # must not run DDL when it starts.
    normalise_views_on_startup: bool = True

    # How the SAP CSV extract writes numbers, which the n_<table> views undo
    # for every CSV-shaped number column:
    #   us      2,490.00  803.70  1.000   -- grouping commas, decimal point
    #   german  2.490,00  803,70  1,000   -- grouping dots, decimal comma
    # The extract follows the SAP job user's decimal notation, and it has
    # changed: raw_mseg.MENGE held "0,989" in September 2026 and "1.000" from
    # the 9 October delivery. A view built for the wrong one reads every
    # quantity a thousand times out. Declared here, per deployment, never
    # guessed from a value ("1,000" is one in one notation and a thousand in
    # the other); measure a delivery before changing it. The OData delta
    # writes the plain shape, which the us rule reads unchanged.
    csv_number_format: str = "us"

    # --- CSV receiver (/api/events/csv) -------------------------------------
    #
    # How many pushed CSV chunks one worker decodes, parses and lands at the
    # same time. SAP pushes every table of a sweep at once, 50,000 rows a chunk.
    # Measured on one 6 MB, 50,000-row push: the handler peaked at 54 MB before
    # lean handling and 24 MB after. Twenty of the old kind together were an
    # out-of-memory kill on the shared B1 plan (2026-10-08 08:12); six of the
    # new kind is ~150 MB -- wide enough that a queue rarely forms.
    # The rest wait their turn on the event loop, which costs nothing.
    csv_upload_max_concurrent: int = 6

    # Seconds a chunk waits for a slot before it is processed anyway. The
    # SENDER waits with it: SAP's push stays open until we answer, and a sender
    # that gives up marks the chunk failed even if we then land it -- a resend
    # would land it twice. Kept short for that reason; set it below SAP's (or
    # CPI's) own HTTP timeout once they confirm it.
    csv_upload_max_wait_seconds: int = 30

    # --- CSV pull batching ----------------------------------------------------
    #
    # Tables per batch in a CSV sweep (--csv-pull --all, and the nightly full
    # refresh): fire this many, wait until each is delivered and reconciled --
    # complete, failed or timed out -- then fire the next. 2 since 2026-10-08,
    # so SAP never pushes more than two tables at the shared B1 plan at once.
    # 0 fires every table together, the shape this route was proven on
    # (21 tables, 38 deliveries); --batch-size overrides it per run.
    csv_pull_batch_size: int = 2

    # --- Workbook fallback --------------------------------------------------
    #
    # The local folder `python -m app.ingest.fallback --upload` (no arguments)
    # and `--sync` scan for the five fallback workbooks (EKKO, EKET, ZMM065 x2,
    # 30 Day GR Report). A relative path is resolved against the backend root,
    # not the working directory, so the command finds the same folder from
    # wherever it is run. The folder holds real SAP extracts and is gitignored;
    # see data/fallback/README.md for the file names it expects.
    fallback_source_dir: str = "data/fallback"

    # Optional. The Data Lake adapter authenticates with DefaultAzureCredential
    # by default -- the App Service's managed identity when deployed, the
    # developer's `az login` session locally -- so no secret is stored anywhere.
    # This exists only for a machine where neither is available. Prefer granting
    # 'Storage Blob Data Reader' to an identity over setting this.
    azure_storage_account_key: str = ""

    # The Key Vault, for `python -m app.checkup` ONLY.
    #
    # No code reads secrets from here. Secrets reach this app as App Service
    # Key Vault references -- App Service resolves
    # @Microsoft.KeyVault(SecretUri=...) into an ordinary environment variable
    # before the process starts, so Settings reads them unchanged and this
    # codebase needs no Key Vault SDK.
    #
    # That is the right design and it has one drawback: the app never talks to
    # Key Vault, so it cannot report whether Key Vault is reachable -- which is
    # one of the three things Anish asked to confirm. Setting this lets checkup
    # probe the vault directly, using the same managed identity, to answer that
    # question and nothing else.
    #
    #   https://kv-vzi-aicom-nonprod.vault.azure.net/
    key_vault_url: str = ""

    # SAP, reached through the CPI generic OData consumption endpoint.
    #
    # Named CPI_*, not SAP_*, because that is what they actually are and what
    # data-generator/.env has always called them: the platform never talks to
    # SAP directly. Every call goes to ONE CPI iFlow URL carrying APIPath and
    # APIQuery parameters -- there are no per-service OData URLs to configure.
    #
    # All empty by default. The application, its liveness endpoint and the test
    # suite all start and pass with no SAP access at all.
    cpi_base_url: str = ""
    cpi_token_url: str = ""
    cpi_client_id: str = ""
    cpi_client_secret: str = ""

    # The iFlow path appended to cpi_base_url -- and the ONE setting that says
    # which SAP system this process talks to:
    #
    #     /http/SAPECC/OdataConsumption     DEV (the default)
    #     /http/SAPECCQA/OdataConsumption   QA
    #
    # Everything that depends on the system follows it, with no code change:
    # every OData read, $count and CSV extract request (all of them go through
    # transport.CpiTransport, which appends this path); the recorded SAP
    # contract -- keys, types, filter evidence, the delta gate -- which is the
    # discovery snapshot captured FROM this path (see contract.discovery_dir);
    # and the database guard that refuses to load one system's data on top of
    # another's (app.ingest.sap_system). cpi_discovery.py and the live checks
    # read the same variable.
    cpi_path: str = DEFAULT_CPI_PATH

    # Where the discovery snapshot for this system lives. Leave empty: it is
    # found by the CPI_PATH each snapshot recorded when it was captured
    # (data-generator/discovery*/snapshot.json). Set it only to force a folder,
    # relative to the backend root or absolute.
    sap_discovery_dir: str = ""

    # Rows per page. Conservative -- SAP's real server-side limit is unproven.
    cpi_page_size: int = 1000

    # Seconds. Generous: some sets take over a minute to answer a $count.
    cpi_timeout_seconds: int = 120

    # Path to a CA bundle, for networks that terminate TLS with a corporate
    # certificate. Empty means "use the default trust store".
    #
    # There is deliberately NO setting to disable verification. On a network
    # that intercepts TLS, turning verification off does not "make it work" --
    # it makes every call trust whatever answers, including on the day the
    # interception is something else. Export the corporate root certificate and
    # point this at it. `requests` also honours REQUESTS_CA_BUNDLE natively, so
    # either mechanism works.
    cpi_ca_bundle: str = ""

    @property
    def cpi_configured(self) -> bool:
        """Whether enough is set to attempt a call. Drives skipping, not failing."""
        return bool(
            self.cpi_base_url
            and self.cpi_token_url
            and self.cpi_client_id
            and self.cpi_client_secret
        )

    # --- AI service layer (W1.5) ------------------------------------------
    #
    # Which provider is plugged in. Business logic never reads this -- it calls
    # get_llm(). Defaults to the deterministic stub so the application, its
    # tests and a developer laptop all work with no provider at all.
    #
    #   stub     deterministic, no network
    #   foundry  Microsoft Foundry
    #   openai   any OpenAI-compatible endpoint
    llm_provider: str = "stub"

    # Shared across providers.
    llm_max_tokens: int = 1024
    llm_timeout_seconds: int = 60
    llm_max_retries: int = 3

    # Foundry. Endpoint and key are the only things that wait for Azure.
    #
    # NOTE: which model family is deployed on VZI's Foundry resource is still an
    # open item on the W1.1 Day-0 checklist. The adapter is built for the
    # chat-completions shape; a Claude deployment would need the Anthropic
    # Foundry client instead. That is one adapter file, not a redesign.
    foundry_endpoint: str = ""
    foundry_api_key: str = ""
    foundry_deployment: str = ""

    # The cheaper deployment, for high-volume formulaic work. VZI has gpt-4o and
    # gpt-4o-mini; app/core/model_registry.py decides which job uses which.
    # Empty means "use foundry_deployment for everything".
    foundry_deployment_fast: str = ""
    foundry_api_version: str = "2024-10-21"

    # Which of Foundry's two request shapes the endpoint speaks.
    #
    #   v1           {endpoint}/chat/completions
    #                model in the BODY, Bearer auth, NO api-version.
    #                Endpoints ending /openai/v1 -- the newer AI Foundry surface.
    #
    #   deployments  {endpoint}/openai/deployments/{name}/chat/completions?api-version=...
    #                model in the URL, api-key header.
    #                The classic Azure OpenAI surface.
    #
    #   auto         infer from the endpoint (default): "/openai/v1" means v1.
    #
    # This matters concretely: VZI's endpoint is
    # https://oai-vzi-aicom-nonprod-san.services.ai.azure.com/openai/v1, and
    # appending the classic path to it yields a doubled /openai/ and a 404.
    # Auto-detection reads that correctly; the override exists for the day an
    # endpoint does not follow the convention.
    foundry_api_style: str = "auto"

    # The alternate provider -- any OpenAI-compatible endpoint.
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    @property
    def llm_configured(self) -> bool:
        """Whether the selected provider has what it needs.

        The stub always qualifies: "no provider configured" is a working state
        here, not a broken one.
        """
        choice = (self.llm_provider or "stub").strip().lower()
        if choice == "stub":
            return True
        if choice == "foundry":
            return bool(self.foundry_endpoint and self.foundry_api_key and self.foundry_deployment)
        if choice == "openai":
            return bool(self.llm_base_url and self.llm_api_key and self.llm_model)
        return False

    # --- Criticality (W3.4) -----------------------------------------------
    #
    # Where material criticality is read from. I07, I08 and I13 never read this
    # -- they call get_criticality_source(). Defaults to the delivered ZMM065
    # extracts, which are the only confirmed source today.
    #
    #   zmm065   the delivered ZMM065 aging reports (default)
    #   zzcritic MARC-ZZCRITIC, falling back to zmm065
    #
    # ZZCRITIC is the preferred source in principle, but the CPI service does
    # not expose it and its contents are unconfirmed, so selecting it today
    # resolves every lookup through the fallback -- visibly. See
    # app/integrations/criticality/zzcritic.py for what would change that.
    criticality_source: str = "zmm065"

    @property
    def criticality_configured(self) -> bool:
        """Whether the selected source can answer.

        ZMM065 reads the seeded raw tables, so it needs the database; ZZCRITIC
        falls back to ZMM065 and therefore needs the same.
        """
        choice = (self.criticality_source or "zmm065").strip().lower()
        if choice in ("zmm065", "zzcritic"):
            return bool(self.database_url)
        return False

    # Load a development-only mock Criticality -> Service Level matrix
    # (config/dev/i7_service_level_matrix.yaml) instead of leaving I07's
    # ServiceLevelPolicy unconfigured. False by default everywhere, including
    # production: the Solution Design's Rule 1 still requires the real,
    # Vedanta-signed matrix, and this flag exists only so local development
    # can exercise the chain that an unsigned matrix blocks (LightGBM's
    # target quantile, Z-factor, safety stock, ROP) without it. See
    # app/initiatives/i7/policy/dev_fixtures.py -- a caller must still opt in
    # explicitly by calling load_mock_service_level_policy(); this flag does
    # not, by itself, change what PolicyDocument() builds.
    i7_dev_mock_service_level: bool = False

    # Load a development-only mock Max Stock strategy
    # (config/dev/i7_max_stock_strategy.yaml) instead of leaving I07's
    # MaxStockStrategy unchosen. False by default everywhere, including
    # production: the Solution Design's Rule 2 still requires the formula to
    # be agreed with Vedanta, and this flag exists only so local development
    # can exercise the Max Stock leg -- and, through it, the OAR
    # similarity-weighted Max estimate, which borrows only from donor
    # materials whose own Phase 5 Max succeeded. The fixture selects the
    # already-implemented review-period strategy through the existing
    # MaxStockStrategy schema; no new formula is introduced. Independent of
    # i7_dev_mock_service_level, though setting this one alone changes
    # nothing in practice, because Max Stock is computed from ROP and ROP is
    # blocked by an unsigned service level.
    i7_dev_mock_max_stock: bool = False

    # --- Sign-in: Microsoft Entra ID access tokens (app/core/security.py). ---
    # Off by default, and off locally and in tests: with it off every request is
    # served as today -- the author comes from the X-Actor-Id header and no role
    # is checked -- so switching it on is a setting, not a deploy, and switching
    # it back off is the rollback.
    auth_enabled: bool = False
    # The VZI tenant, and the Application (client) ID of the API's app
    # registration (spares-ai-api-<env>). Both are required once AUTH_ENABLED
    # is true; without them every protected request answers 503 rather than
    # being let through.
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    # Extra accepted token audiences, comma-separated. The client ID and
    # api://<client ID> are always accepted: a v2 access token carries the bare
    # client ID as its audience (measured on spares-ai-api-prod, 26 Oct 2026),
    # not the api:// URI the app registration displays.
    auth_audience: str = ""
    # The delegated scope a user's token must carry (Expose an API).
    auth_required_scope: str = "access_as_user"
    # Clock difference tolerated on exp/nbf/iat, and how long Entra's signing
    # keys are cached before they are fetched again (an unknown key ID always
    # triggers a fetch, so a key rotation never waits for this).
    auth_clock_skew_seconds: int = 120
    auth_jwks_cache_seconds: int = 3600
    # SAP's calls (/api/events/pr, /api/events/csv) are switched separately:
    # they come from VZI's CPI, not from a person, and how CPI authenticates is
    # decision D2 in the RBAC plan. "off" keeps them open as today; "entra"
    # requires an application token carrying the Sap.Events.Write role.
    sap_events_auth: Literal["off", "entra"] = "off"

    # --- Initiative 13: End-to-End Spares Utilisation Tracking. ---
    # Root directory for platform-owned (non-SAP) reference data -- currently
    # just consumption_plans.csv (see app/initiatives/i13/plans.py). All
    # SAP-derived I13 data reads from the database, through the n_<table>
    # normalise views (see app/integrations/sap/postgres_*.py and
    # app/shared/sap_normalise.py); there is no CSV path for it any more.
    i13_data_dir: str = "data-generator/generated"

    # Whether consumption_plans.csv under i13_data_dir is read at all. That file
    # is generator output -- 742 fabricated plans for plants 1000-4000 that
    # match no in-scope reservation -- and it ships with every deploy because
    # the whole repository is the package. Read, every open one counts as a
    # plan breach forever. Off, the only plans are the ones requesters captured
    # through the assistant (the consumption_plan table), which is what FRS
    # sec. 2 / D8 specify. Turn on locally to exercise the breach path with
    # synthetic data; the file itself stays tracked on purpose.
    i13_reference_plans_enabled: bool = False

    # The I13 in-memory snapshot (app/initiatives/i13/snapshot.py): every I13
    # read route serves one precomputed snapshot instead of recomputing from
    # raw_* per request. False puts every route back on the live compute --
    # the escape hatch, and what `?live=true` does for a single request.
    i13_snapshot_enabled: bool = True
    # Build the snapshot in the background at start-up, so the first visitor
    # does not pay for it. Off in tests, where the first use builds it lazily.
    i13_snapshot_warm_on_startup: bool = True
    # How often (seconds, at most) the fingerprint is re-checked for a reseed
    # or a new day. One cheap grouped query over ingestion_run -- but every
    # delta cycle writes ingestion_run, so a change it finds is a full rebuild
    # (~78 minutes on the "sql" store over the production extract). On the
    # "sql" store a new day alone is a re-age, not a rebuild. Hourly,
    # to match DELTA_INTERVAL_MINUTES. A delta cycle that loaded rows asks
    # for the check itself (SNAPSHOT_REFRESH_AFTER_DELTA); this is the
    # fallback for a manual load.
    i13_snapshot_check_interval_seconds: int = 3600
    # ISO date to measure every snapshot metric as of. Empty = today, the date
    # every live route has always used (plan decision D1 left open).
    i13_snapshot_reference_date: str = ""
    # Where the snapshot lives. "memory" (the default) holds it in this
    # process, as it always has. "sql" builds it in material batches and stores
    # it in Azure SQL (app/initiatives/i13/snapshot_store/) -- for a production-
    # sized extract, where the in-memory build no longer fits the App Service.
    # The routes answer the same either way.
    i13_snapshot_store: str = "memory"
    # The I13 summary report (app/initiatives/i13/report/): a generated page
    # summarising the four I13 screens, its words written by the configured AI
    # provider. Off by default -- while off, its routes answer 404 and the
    # frontend hides the button, so deploying the code changes nothing visible.
    i13_report_enabled: bool = False
    # "sql" store only: how many distinct materials one build batch covers. A
    # batch is read, computed and written before the next one starts, so this
    # bounds the build's memory -- smaller is leaner and slower.
    i13_build_batch_materials: int = 2000
    # "sql" store only: the most rows (goods movements + reservations) one batch
    # reads. Batches are cut by this AND by I13_BUILD_BATCH_MATERIALS, whichever
    # comes first -- a handful of high-volume materials can hold more movements
    # than thousands of slow movers, so a count of materials alone does not
    # bound a batch's memory. A single material over it gets a batch of its own.
    i13_build_batch_rows: int = 150000
    # "sql" store only: automatic build attempts for the same data before the
    # store stops retrying (a manual POST /api/i13/snapshot/refresh still runs).
    # A build that fails the same way every time otherwise re-copies the large
    # tables forever.
    i13_build_max_attempts: int = 3
    # "sql" store only: the least time between one automatic build finishing
    # and the next starting. The delta runs hourly and every cycle that loads
    # rows changes the fingerprint, but a build takes about as long as a cycle,
    # so without a gap the store would rebuild continuously and keep Azure SQL
    # busy all day. 0 = no gap (every change rebuilds, as before); set 120 if
    # builds run back to back. A manual POST /api/i13/snapshot/refresh
    # ignores the gap.
    i13_rebuild_min_gap_minutes: int = 0
    # "sql" store only: the UTC hours an automatic FULL build may start in,
    # "HH:MM-HH:MM" (may wrap past midnight, e.g. "18:30-23:30" = 00:00-05:00
    # IST). Outside it a data change waits, and the served version is re-aged
    # to today instead if the day has turned -- so a full build, which loads
    # Azure SQL for over an hour, never starts while people are using the
    # site. Empty = any time (as before). A manual POST
    # /api/i13/snapshot/refresh ignores it; a new day's re-age is never held
    # back by it (it reads no SAP tables and takes minutes).
    i13_auto_build_window_utc: str = ""
    # "sql" store only: how many calendar months of monthly usage are kept per
    # material-plant (the usage-pattern screens). Aging and 12-month
    # consumption read the full movement history regardless.
    i13_usage_history_months: int = 36

    # --- Initiative 08: the in-memory snapshot (app/initiatives/i8/service.py). ---
    i8_snapshot_warm_on_startup: bool = True
    # Per statement, until its first row (not the fetch after it). 0 = no limit.
    i8_snapshot_statement_timeout_seconds: int = 120
    # Then 503 "building". Under the frontend's 20 s GET timeout.
    i8_snapshot_wait_seconds: int = 10
    # How often (seconds, at most) the source fingerprint is re-checked for a
    # reload or a new day. One cheap grouped query over ingestion_run. The
    # rebuild it triggers is a background one -- the current snapshot keeps
    # serving -- so this is how long a reload stays invisible, not how long a
    # request waits. Hourly, like I13's. A delta cycle that loaded rows asks
    # for the check itself (SNAPSHOT_REFRESH_AFTER_DELTA); this is the
    # fallback for a manual load.
    i8_snapshot_check_interval_seconds: int = 3600
    # Read narrowed copies of the I08 sources during a snapshot build instead of
    # the views over the whole extract (app/initiatives/i8/staged.py). The
    # views call a scalar function per row, which on the production extract ran
    # one build query past its limit and pulled 805k EKPO lines into the worker.
    # Off is the old path, kept as a switch while the staged one is new.
    i8_snapshot_staged: bool = True
    # Where the I08 snapshot lives. "memory" builds it in the web worker, as it
    # always has. "sql" builds it in its own process and stores it in
    # i8_snapshot_run (app/initiatives/i8/snapshot_store): the worker only loads
    # it, and a restart reloads the stored version instead of rebuilding.
    i8_snapshot_store: str = "memory"
    # "sql" store only: automatic build failures per hour before the store stops
    # retrying. `python -m app.initiatives.i8.snapshot_store build --force`
    # still runs one.
    i8_build_max_attempts: int = 3

    # Session <-> reservation linking (app/initiatives/i13/session_link.py).
    # The requester types the assistant's session ID into the reservation's item
    # text, RESB.SGTXT, which the extract loads as raw_resb.text. BEDNR was the
    # original carrier but is not in the extract (blocker B2).
    #
    # UAT only: overlay simulated reservations / SGTXT values from
    # uat_reservation_sgtxt on top of raw_resb, standing in for "the requester
    # typed it into SAP and a new extract was loaded". Never on in production:
    # raw_resb itself is never modified either way.
    i13_uat_simulation_enabled: bool = False
    # Reservations required on or after this ISO date are expected to carry a
    # session ID (the "reservations with no session" compliance count). Empty =
    # the day the first assistant session was issued.
    i13_assistant_go_live_date: str = ""

    # --- I07 plant scope ----------------------------------------------------
    #
    # Which plants I07 stages, comma-separated. VZI confirmed on 2026-10-07
    # that the programme covers Gamsberg (1500) and Black Mountain (1300)
    # only, while the extract delivers thirteen -- the other eleven produced
    # recommendations for sites nobody asked about and diluted every
    # catalogue-wide percentage the reports quote.
    #
    # EMPTY MEANS EVERY PLANT, deliberately: an unset or blank value must not
    # silently narrow the catalogue, because "no plants configured" and "all
    # plants in scope" would then look identical and the first is a mistake
    # worth noticing. Narrowing is opt-in and visible in the staging summary.
    #
    # This scopes I07's STAGING only -- raw_*, the n_<table> views, I08, I13
    # and the assistant all still see every plant. It is not an access
    # control: see auth_plant_groups for that, which is a different concern.
    #
    # Codes are strings, not integers: PDWB is a real plant in this extract.
    i7_plants: str = "1500,1300"

    # --- I07 unit price: the placeholder floor ------------------------------
    #
    # A valuation price at or below this is a placeholder (SAP keeps 0.01 and
    # 1.00 on rows that are not really valued), so the material-plant stages with
    # no unit price and the reason `price_token_value` -- counted in the staging
    # rejections and kept per row. The same floor I08 applies to its New Item
    # Price (I8_NEW_ITEM_PRICE_TOKEN_MAX), set separately so the two modules stay
    # independent. Decimal, not float: it is compared against money.
    i7_price_token_max: Decimal = Decimal("1.00")

    # --- I07 High Stockout Risk: the date its history is read as of ----------
    #
    # The card counts a material-plant at risk when its stock on hand is below
    # (average daily consumption over the 90 days ending on this date) x (lead
    # time), and takes its Fast / Slow / Non-moving band from the days between
    # this date and the last issue. The date is fixed rather than "today" because the movement
    # history is not current: 2026-05 has ~2,400 issue lines, 2026-06 has 358 and
    # July has 10, so a 90-day window ending today (or on the stray newest line,
    # 2026-09-08) is nearly empty and would read as "nothing is at risk".
    # 2026-05-31 is the last month with normal volume. The card shows this date
    # as "As of ...".
    #
    # Change it here (I7_STOCKOUT_RISK_AS_OF=YYYY-MM-DD), then rebuild the table
    # it reads: python -m app.initiatives.i7.stockout_risk. Set it BLANK to use
    # the newest issue date in the movement data instead. The card always shows
    # the date the table was last built with, so a changed setting that has not
    # been rebuilt yet never mislabels the figures.
    i7_stockout_risk_as_of: date | None = date(2026, 5, 31)

    # --- I07 pipeline watcher -----------------------------------------------
    #
    # OFF by default, and deliberately so. The watcher runs the whole I07
    # pipeline in-process, which is far heavier than the I08/I13 fingerprint
    # checks it is modelled on, and it would share one gunicorn worker with
    # I13's ~1.3 GB snapshot. Whether that is safe is a question about peak RSS
    # and run duration against the real database, and neither has been
    # measured -- see docs/i07_pipeline_automation.md. Until it has, this stays
    # false and the pipeline is run on purpose:
    #     python -m scripts.run_full_pipeline
    i7_pipeline_watch_enabled: bool = False

    # Seconds between fingerprint checks. The check itself is one grouped query
    # over ingestion_run; the PIPELINE it may start is the expensive part, and
    # that is bounded by the fingerprint changing, not by this interval.
    i7_pipeline_check_interval_seconds: int = 300

    # Seconds to wait after start-up before the first check, so a deploy does
    # not begin a pipeline run while migrations may still be applying. Matches
    # the delta scheduler's reasoning and its default.
    i7_pipeline_initial_delay_seconds: int = 120

    # The nightly slot, in UTC, when the watcher may START a run (it never stops
    # one). 20 = 22:00 in South Africa, after the working day. The watcher only
    # acts inside a slot; the rest of the day it does nothing at all. See
    # app/initiatives/i7/pipeline_scheduler.py.
    i7_pipeline_nightly_hour_utc: int = 20

    # Extra attempts after a nightly run for the same source data did not
    # succeed (failed, or interrupted by a restart), each in its own slot two
    # hours after the previous one. Counted from i7_pipeline_run, so a restart
    # does not reset it; once used up, that data version is not retried again
    # until new data arrives or someone runs the pipeline by hand.
    i7_pipeline_retries: int = 2

    # Whether the nightly watcher also saves the I07 monthly KPI snapshots (i7_kpi_snapshot) the Quarterly Report's
    # trends and quarter-end position read: after the night's run it refreshes the current month's rolling reading and
    # finalises last month's. OFF by default, and it only acts when the watcher itself is on
    # (i7_pipeline_watch_enabled) -- there is no second timer. Needs the migration a3c9e7f15b42 applied first. The same
    # capture can be run by hand: python -m app.reporting.capture_snapshot.
    i7_kpi_snapshot_enabled: bool = False

    # Whether the nightly watcher also generates the Quarterly Report for the quarter that just closed
    # (app/initiatives/i7/reporting/auto_generate.py), after the pipeline check and the snapshots. OFF by default, and it
    # only acts when the watcher itself is on (i7_pipeline_watch_enabled) -- there is no second timer. Nothing else in the
    # app generates the report on a schedule; the alternative is an Azure Logic App / WebJob calling the API (see
    # docs/i07_quarterly_report_scheduling.md). Turn on I7_KPI_SNAPSHOT_ENABLED with it, or the report has no month-end
    # snapshots to read and says so.
    i7_quarterly_report_auto_enabled: bool = False

    # Days after a quarter closes during which that automatic generation still runs. Switching it on mid-quarter must not
    # produce a report for a quarter that closed weeks ago; that one is generated by hand. 1-31.
    i7_quarterly_report_auto_window_days: int = 7

    # The same for the MONTHLY report: when on (with the nightly watcher, I7_PIPELINE_WATCH_ENABLED), the report for a
    # month that has just ended is generated automatically -- on the 1st, after the month's snapshot is finalised --
    # and a mid-month report someone generated by hand is replaced by the final one. OFF by default, independent of the
    # quarterly switch above. Turn on I7_KPI_SNAPSHOT_ENABLED with it, or the report has no month-end snapshot to read.
    i7_monthly_report_auto_enabled: bool = False

    # Days after a month ends during which that automatic generation still runs. 1-31.
    i7_monthly_report_auto_window_days: int = 7

    # Whether Auto-ARIMA runs as the CHALLENGER to SES for Smooth and Erratic
    # demand. OFF by default. On nonprod (13 Oct 2026) it was the whole cost of
    # forecasting: ~7-8 minutes per material-plant on Azure (4 s on a laptop), so
    # the 179 smooth/erratic material-plants alone would take ~22 hours, while the
    # other 5,896 (SBA + LightGBM) take ~0.2 s each. With it off, SES is the only
    # model for those classes and the segment decision records "challenger not
    # run". Intermittent and lumpy demand are unaffected. Turn it on to compare
    # the two models offline.
    i7_forecast_arima_enabled: bool = False

    # Declining-demand detection and the separate TSB forecasting path. Three
    # states, default off:
    #
    #   off     nothing is evaluated and forecasting is exactly as it was,
    #           including the placeholder TSB rows. The default.
    #   shadow  the feature build evaluates every material-plant with enough
    #           history and records the result (i7_declining_demand_evaluation),
    #           logging how many were flagged and why the rest were not.
    #           Forecasts do not change. This is how the rule is reviewed on
    #           real data before it is allowed to change a number.
    #   active  as shadow, and forecasting runs TSB directly for flagged
    #           material-plants (no challenger comparison, no backtest) while
    #           everything else keeps the normal routing. Refuses to run when
    #           the run's evaluations are missing -- see docs/i07_declining_demand.md.
    #
    # Needs the i7_declining_demand_evaluation table (an Alembic migration that
    # is applied on purpose, not by this setting).
    i7_declining_demand_mode: Literal["off", "shadow", "active"] = "off"

    # --- I07 OAR donor similarity: retrieval -------------------------------
    #
    # Operational knobs for app/initiatives/i7/oar/retrieval.py. They decide how
    # much work a run does, not what a neighbour is -- the business rules
    # (threshold, K, evidence, eligibility) live in SimilarityPolicy. See
    # docs/i07_oar_similarity_engine.md for what each one trades.
    #
    # "two_stage" (default): a vectorised pre-score over the eligible donors
    #   picks a shortlist; only the shortlist gets the exact, explainable score.
    # "exhaustive": every eligible donor gets the exact score -- the pre-2
    #   behaviour, kept as the reference and for calibration runs.
    i7_oar_retrieval_mode: str = "two_stage"

    # Shortlist channels, merged and de-duplicated: best by structured+business
    # pre-score, best by description similarity, best by the full combined
    # pre-score. Up to their sum (200 by default) per target, usually fewer.
    i7_oar_structured_pool: int = 50
    i7_oar_semantic_pool: int = 50
    i7_oar_fused_pool: int = 100

    # A criticality partition with at most this many eligible donors is scored
    # exhaustively -- retrieval would save nothing there.
    i7_oar_exhaustive_below: int = 200

    # Targets pre-scored together (one matrix product per batch). Bounds the
    # batch x donors similarity block held at once.
    i7_oar_target_batch_size: int = 256

    # Descriptions sent to the embedding model per call.
    i7_oar_embedding_batch_size: int = 64

    # Keep embeddings in i7_oar_embedding so the next run reuses them. Off means
    # every run embeds every description again (same result, more work).
    i7_oar_embedding_cache: bool = True

    # Target results written per batch, so a run never holds every neighbour
    # row of every target in memory at once.
    i7_oar_write_batch_size: int = 2000

    # Recommendation explanations requested from the AI model at the same time.
    # Each call waits on the provider (about 4 s on Foundry), so one at a time
    # takes most of a day for a full catalogue. 1 restores one-at-a-time.
    # Raise with care: the provider's rate limit is retried, then falls back to
    # the template text for that recommendation.
    i7_rationale_concurrency: int = 8

    @field_validator("i7_rationale_concurrency")
    @classmethod
    def _rationale_concurrency(cls, value: int) -> int:
        if value < 1:
            raise ValueError("I7_RATIONALE_CONCURRENCY must be at least 1")
        return value

    @field_validator("i7_oar_retrieval_mode")
    @classmethod
    def _oar_retrieval_mode(cls, value: str) -> str:
        mode = (value or "").strip().lower()
        if mode not in ("two_stage", "exhaustive"):
            raise ValueError("I7_OAR_RETRIEVAL_MODE must be 'two_stage' or 'exhaustive'")
        return mode

    @field_validator(
        "i7_oar_structured_pool",
        "i7_oar_semantic_pool",
        "i7_oar_fused_pool",
        "i7_oar_target_batch_size",
        "i7_oar_embedding_batch_size",
        "i7_oar_write_batch_size",
    )
    @classmethod
    def _oar_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("OAR retrieval sizes must be at least 1")
        return value

    @field_validator("i7_oar_exhaustive_below")
    @classmethod
    def _oar_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("I7_OAR_EXHAUSTIVE_BELOW must not be negative")
        return value

    # Current VZI OAR material-scope ruling (20 Oct 2026): the MRP controller,
    # MARC.DISPO, comma-separated. Replaces the earlier MRP type (DISMM) rule.
    # A blank DISPO, and every code not listed, is not OAR. See
    # app/shared/material_scope/policy.py -- shared platform config, read by
    # I13 and the assistant's routing.
    i13_oar_mrp_controllers: str = "OAR"
    # A material flagged for deletion in SAP -- MARC-LVORM (at the plant) or
    # MARA-LVORM (client level) = X -- is never OAR, whatever its MRP controller
    # (UAT, 19 Oct 2026). See app/shared/material_scope/deletion.py.
    i13_exclude_deleted_materials: bool = True
    # A PO line SAP has closed -- EKPO-ELIKZ = X (delivery completed) or
    # EKPO-LOEKZ set (L deleted, S blocked) -- has nothing open on it, so it
    # adds nothing to a material's open-PO quantity (UAT, 21 Oct 2026). See
    # app/initiatives/i13/watch.py. Part of the snapshot fingerprint.
    i13_open_po_respect_closed: bool = True
    # A purchase requisition item SAP has deleted -- EBAN-LOEKZ = X -- that no
    # PO line orders is not procurement: the ledger leaves it out, and a
    # reservation naming it reads "PR deleted in SAP" (UAT, 26 Oct 2026). See
    # app/initiatives/i13/po_lines.py. Part of the snapshot fingerprint.
    i13_pr_respect_deleted: bool = True
    # Purchasing document types (EKKO-BSART) that are not orders, so nothing is
    # ever open on their lines: WK value and MK quantity contracts, AN requests
    # for quotation, FPA (VZI's own agreement type, measured as an agreement:
    # 3,564 of its 4,577 lines carry no quantity). UAT, 23 Oct 2026; see
    # app/initiatives/i13/po_lines.py. Part of the snapshot fingerprint.
    i13_non_order_doc_types: str = "WK,MK,AN,FPA"

    # Aging bands (days since last goods movement).
    i13_aging_fast_max_days: int = 365
    i13_aging_slow_max_days: int = 730

    # Trailing consumption window used throughout WATCH/aging/reclassification.
    i13_consumption_window_months: int = 12

    # 30-day goods-received-not-issued exception threshold.
    i13_gr_not_issued_threshold_days: int = 30

    # Grace period after a consumption plan's planned-use window before it
    # becomes a PLAN_BREACH exception.
    i13_plan_breach_grace_days: int = 14

    # SOP indicator for OAR -> Min-Max reclassification review.
    i13_reclass_min_consumption_count: int = 4

    # SOP 3.1.1 indicator 2: which ZMM065 criticality tiers (W3.4,
    # app.core.criticality.CriticalityTier) count as "Critical" evidence for
    # reclassification. The Dev Plan says "Critical flag"; the FRS also
    # mentions "Critical or significant production impact" -- IMPACT is
    # deliberately NOT enabled by default, since broadening the rule beyond
    # the Dev Plan's own wording is a business decision, not one this code
    # should make silently. Comma-separated tier names.
    i13_reclass_critical_tiers: str = "CRITICAL"

    # Reconciliation tolerance for local validation against reference reports.
    i13_reconciliation_tolerance_pct: float = 5.0

    # W6.4: gates cost-centre enrichment in consumption attribution. Off by
    # default -- no deterministic cost-centre source (EKKN/AUFK) is loaded
    # in this codebase yet (see app/initiatives/i13/cost_centre_provider.py),
    # so leaving it on would silently do nothing; reservation/requester/order
    # attribution is never gated by this flag.
    i13_cost_centre_attribution_enabled: bool = False

    # W6.6: how many days an ACT exception's requester has to confirm before
    # it escalates to the responsible HOD (app.initiatives.i13.act.service
    # .process_escalations). The 30-day GRNI fallback for no-plan cases
    # reuses i13_gr_not_issued_threshold_days above -- it is not a second
    # constant.
    i13_requester_response_days: int = 5

    # W6.6: local/config HOD routing -- "PLANT:identity,PLANT2:identity2".
    # A placeholder for a future Entra/DOA-backed lookup (see
    # app/initiatives/i13/act_hod_provider.py); empty by default, which
    # leaves every escalation's routing explicitly PENDING rather than
    # inventing a recipient.
    i13_hod_recipients: str = ""

    # --- W7.4: reservation-time quantity suggestion -----------------------
    #
    # Master gate. Off by default, and deliberately separate from the two
    # values below: "the business has not given us the numbers yet" and "the
    # feature is switched off for this environment" are different states and
    # the engine reports them differently (NOT_CONFIGURED vs DISABLED).
    i13_qty_suggestion_enabled: bool = False

    # Cover ceiling, in months -- the months-of-cover guard rail a request is
    # nudged down to. NO DEFAULT, on purpose. A guessed ceiling would produce
    # plausible-looking but unsanctioned purchase advice, so with this unset
    # the engine returns NO_SUGGESTION/NOT_CONFIGURED rather than inventing
    # one -- the same posture i13_hod_recipients above already takes (empty
    # means routing is explicitly Pending, never a fabricated recipient).
    # Open VZI item; see FRS §10.
    #
    # Decimal, not float: every quantity this engine touches is fixed-point
    # (see app/models/i13_watch_mart.py), and a ceiling of 0.1 that is really
    # 0.100000000000000005 would put a rounding artefact into a purchase
    # figure.
    i13_qty_cover_ceiling_months: Decimal | None = None

    # Minimum consumption history before the engine will speak at all,
    # measured as a count of consumption events inside the look-back window
    # (WatchMetricMart.consumption_count_12m). Also no default, for the same
    # reason -- and this single value decides how often the engine speaks:
    # against the current WATCH mart (7,184 positions), "any consumption"
    # would cover 3,086 of them and ">= 4 in 12 months" only 459.
    i13_qty_minimum_history_count: int | None = None

    # Look-back window for the consumption rate the suggestion is built on.
    # Defaults to the same 12 months i13_consumption_window_months already
    # uses, because the AMC the engine reads IS that window's figure -- it is
    # repeated here only so a divergent W7.4 window stays a config change.
    # Setting it to anything else today is recorded on the suggestion but
    # does not re-derive AMC; see app/initiatives/i13/quantity_suggestion.py.
    i13_qty_lookback_months: int = 12

    # --- FR-3 as the assistant serves it ----------------------------------
    #
    # Read by app/initiatives/i13/quantity.py, which the reservation-time
    # conversation calls (app/assistant/session.py suggestion_for) and which
    # /api/i13/quantity-suggestion does NOT -- that endpoint runs the gated
    # W7.4 engine on the i13_qty_* values above.
    #
    # So there are deliberately two sets, and they take opposite positions on
    # the same open VZI number: the W7.4 engine ships no ceiling and declines
    # with NOT_CONFIGURED, while these carry the WS7 notes' working defaults so
    # the conversation has something to say. That divergence is a live
    # decision, not an oversight -- W7.4's plan argues a guessed ceiling is
    # unsanctioned purchase advice, and if that argument wins for the
    # assistant too, this block goes and quantity.py reads i13_qty_* instead.
    #
    # Decimal rather than float: a ceiling of 0.1 that is really
    # 0.100000000000000005 would put a rounding artefact into a purchase
    # figure, which is the same reason i13_qty_cover_ceiling_months is Decimal.
    i13_quantity_cover_ceiling_months: Decimal = Decimal("12.0")
    i13_quantity_lookback_months: int = 12
    i13_quantity_min_history_consumptions: int = 3

    # --- WS7: the shared reservation-time assistant ------------------------
    #
    # Read by app/assistant/* and app/api/assistant/router.py. Every value
    # here has a working default, so the assistant runs with nothing set.

    # Session reference format. The length is the whole string including the
    # prefix and its separator -- app/assistant/ids.py refuses a length that
    # leaves no room for a payload rather than minting a truncated reference.
    # assistant_session.id is deliberately wider than this, so raising it is
    # a config change and not a migration.
    assistant_session_id_prefix: str = "S"
    assistant_session_id_length: int = 10

    # How long a session stays OPEN before it is derived as ABANDONED. The
    # row is append-only and nothing writes a status, so this value alone
    # decides where that boundary falls -- see app/assistant/session.py.
    assistant_session_ttl_hours: int = 72

    # The optional model-written sentence (app/assistant/narrative.py). OFF BY
    # DEFAULT AND MUST STAY THAT WAY until VZI signs off: both FRSs say
    # responses come from the LLM layer, and serving one before that
    # conversation happens is the visible deviation the WS7 notes record.
    # Every number is computed either way; this only phrases them.
    assistant_narrative_enabled: bool = False

    # Justification reason categories -- configuration, not an enum, because
    # both FRSs say "a reason category plus free text" and neither lists the
    # categories (open question 9). These three are PLACEHOLDERS; the day VZI
    # supplies its own vocabulary is a .env change, not a migration. Order is
    # preserved: it is the order the options appear on the form.
    assistant_justification_reason_categories: str = (
        "URGENT_BREAKDOWN,NO_SUITABLE_REPAIRABLE,OTHER"
    )

    # The free-text box (section 4.6), answered from deterministic read models
    # by a fixed set of intents -- no model is involved. Off means the box
    # declines and says so; it never falls through to general
    # question-answering, which is separate unagreed scope.
    assistant_free_text_intents_enabled: bool = True

    # Department on a session started from the platform must be a code from
    # the list (app/assistant/departments.py: 5-digit EBAN.AFNAM values at an
    # in-scope plant). Off stores whatever is sent, as before. Sessions opened
    # from SAP are never refused either way.
    assistant_department_check_enabled: bool = True

    # Multi-material requests (app/assistant/requests.py): one request, one
    # plant, one line per material, each line an ordinary session. Off by
    # default -- the routes answer 404 until it is switched on.
    assistant_requests_enabled: bool = False
    # The request reference: same alphabet, length and check character as a
    # session ID, a different prefix so a reader can tell the two apart. Each
    # line is quoted as "<request>/<n>" on the SAP item text (SGTXT, 50 chars).
    assistant_request_id_prefix: str = "R"
    # Materials per request. Submitting re-assesses and records every line in
    # one transaction, so this bounds how long one submit holds a worker and
    # the database. Values above the hard maximum are clamped to it.
    assistant_request_max_lines: int = 20

    @field_validator("i13_qty_cover_ceiling_months", "i13_qty_minimum_history_count", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """Treat an empty environment value as "not set".

        These two are the settings most likely to appear in a .env or an App
        Service configuration with the key present and no value yet -- they
        are exactly the numbers VZI has not given us. Blank must mean the same
        as absent (the engine declines with NOT_CONFIGURED), not a startup
        crash that takes the whole application, and its health endpoint, down
        with it."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("i7_stockout_risk_as_of", mode="before")
    @classmethod
    def _blank_stockout_as_of_is_unset(cls, value: object) -> object:
        """A blank I7_STOCKOUT_RISK_AS_OF means "use the newest issue date", not a
        startup crash: clearing the setting is the documented way to switch."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("cpi_path", mode="before")
    @classmethod
    def _canonical_cpi_path(cls, value: object) -> str:
        # A stray space once broke CPI_PATH in .env (2026-10-05); the same
        # stray space must not make DEV look like a different system.
        return normalise_cpi_path(value)

    @property
    def fallback_source_path(self) -> Path:
        """``FALLBACK_SOURCE_DIR`` as an absolute path, relative ones anchored at
        the backend root."""
        path = Path(self.fallback_source_dir).expanduser()
        return path if path.is_absolute() else BACKEND_ROOT / path

    @property
    def full_refresh_odata_set_list(self) -> list[str]:
        return [name.strip() for name in self.full_refresh_odata_sets.split(",") if name.strip()]

    @property
    def odata_full_pull_set_list(self) -> list[str]:
        return [name.strip() for name in self.odata_full_pull_sets.split(",") if name.strip()]

    @property
    def csv_delta_table_list(self) -> list[str]:
        return [name.strip().upper() for name in self.csv_delta_tables.split(",") if name.strip()]

    @property
    def cors_origins(self) -> list[str]:
        """Allowed CORS origins, parsed from ``FRONTEND_ORIGIN``."""
        return [origin.strip() for origin in self.frontend_origin.split(",") if origin.strip()]

    @property
    def i7_plant_list(self) -> tuple[str, ...]:
        """``I7_PLANTS`` parsed, order preserved. Empty tuple means every plant.

        Not upper-cased: a plant code is matched against the extract exactly as
        delivered, and nothing here should decide that ``pdwb`` and ``PDWB`` are
        the same row when SAP has not said so.
        """
        return tuple(code.strip() for code in self.i7_plants.split(",") if code.strip())

    @property
    def i13_oar_mrp_controller_set(self) -> frozenset[str]:
        """Normalised MRP controller (DISPO) codes that count as OAR materials."""
        return frozenset(code.strip().upper() for code in self.i13_oar_mrp_controllers.split(",") if code.strip())

    @property
    def i13_non_order_doc_type_set(self) -> frozenset[str]:
        """Normalised EKKO-BSART codes whose lines are never open supply."""
        return frozenset(code.strip().upper() for code in self.i13_non_order_doc_types.split(",") if code.strip())

    @property
    def assistant_justification_reason_category_list(self) -> list[str]:
        """The configured reason categories, normalised, order preserved.

        A list rather than a set: the order is what the assistant offers them
        in, and a set would reorder the options on a form between restarts.
        """
        seen: dict[str, None] = {}
        for raw in self.assistant_justification_reason_categories.split(","):
            category = raw.strip().upper()
            if category:
                seen.setdefault(category, None)
        return list(seen)

    @property
    def i13_reclass_critical_tier_set(self):  # -> frozenset[CriticalityTier]
        """Configured criticality tiers that count as reclassification
        evidence. Local import to avoid a Settings <-> criticality import
        cycle (``app.core.criticality`` itself imports ``Settings``).
        Unrecognised tier names are dropped, never coerced -- the same
        no-invention rule ``parse_tier`` itself follows."""
        from app.core.criticality import parse_tier

        tiers = (parse_tier(raw) for raw in self.i13_reclass_critical_tiers.split(","))
        return frozenset(tier for tier in tiers if tier is not None)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
