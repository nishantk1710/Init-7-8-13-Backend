"""Environment-driven application settings.

Values are read from the process environment first and from a local ``.env``
file as a fallback, so the same code runs unchanged locally and on Azure App
Service (where settings are supplied as environment variables).

Nothing here is required for startup: every future-integration setting defaults
to empty so the app -- and the health endpoint -- come up with no SAP, database
or identity credentials present.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # The iFlow path appended to cpi_base_url. Configuration, not a constant:
    # a differently-named iFlow in another VZI landscape must not need a code
    # change.
    cpi_path: str = "/http/SAPECC/OdataConsumption"

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

    # --- Reserved for future integrations. Not read by any code yet, and not
    # --- required for startup. See app/core/security.py and app/integrations/.
    azure_tenant_id: str = ""
    azure_client_id: str = ""

    @property
    def cors_origins(self) -> list[str]:
        """Allowed CORS origins, parsed from ``FRONTEND_ORIGIN``."""
        return [origin.strip() for origin in self.frontend_origin.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
