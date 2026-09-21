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

    # --- Reserved for future integrations. Not read by any code yet, and not
    # --- required for startup. See app/core/security.py and app/integrations/.
    azure_tenant_id: str = ""
    azure_client_id: str = ""

    # --- Initiative 13: End-to-End Spares Utilisation Tracking. ---
    # Root directory for platform-owned (non-SAP) reference data -- currently
    # just consumption_plans.csv (see app/initiatives/i13/plans.py). All
    # SAP-derived I13 data reads from Postgres (see app/integrations/sap/
    # postgres_*.py); there is no CSV path for it any more.
    i13_data_dir: str = "data-generator/generated"

    # Current VZI OAR material-scope ruling (MARC.DISMM), comma-separated.
    # See app/shared/material_scope/policy.py -- this is shared platform
    # config, not owned by any single initiative.
    i13_oar_mrp_types: str = "ND,PD"
    i13_min_max_mrp_types: str = "VB"

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

    # W7.4 / I13 FR-3: the three values the FRS names as configuration for the
    # quantity suggestion. The FRS names all three and gives numbers for none
    # of them, so these are OUR defaults and are labelled as such -- see
    # app/initiatives/i13/quantity.py, which reports the values it used with
    # every suggestion so a reviewer never has to guess which ceiling produced
    # a number. Open question 10 to VZI.
    #
    # Cover ceiling: how many months of cover a reservation may take a
    # material to. Suggest enough to reach it, never more.
    i13_quantity_cover_ceiling_months: float = 12.0

    # Look-back window for the average monthly consumption the suggestion
    # divides by. Deliberately a SEPARATE value from
    # i13_consumption_window_months even though both default to 12: WATCH's
    # window is a reporting choice and this one is an arithmetic input, and
    # retuning one must not silently move the other.
    i13_quantity_lookback_months: int = 12

    # Below this many consumption events in the look-back window, NO suggestion
    # is made. An average built from one movement is not an average, and a
    # confident number derived from it is worse than no number -- the same
    # never-invent rule the rest of the platform follows.
    i13_quantity_min_history_consumptions: int = 3

    # --- W7: the reservation-time assistant (shared I08 + I13) -------------
    #
    # The session ID is read off one screen by a human and typed into another,
    # so its shape is constrained by the SAP field it lands in, not by us.
    # Bednr is Edm.String(10), so 10 is the ceiling; Wempf, the only exposed
    # alternative, is 12, which means 10 is safe either way. Width is
    # configuration so pointing at a different field later is an .env change.
    # See app/assistant/ids.py.
    assistant_session_id_length: int = 10
    assistant_session_id_prefix: str = "S"

    # How long a minted session stays valid. OUR default, not a VZI ruling --
    # open question 11. Expiry is reported on the trace and never used to
    # invalidate a session retrospectively: the reservation is already saved in
    # SAP and the platform cannot write back, so raising an exception nobody
    # can clear would be a fault, not a control.
    assistant_session_ttl_hours: int = 72

    # Whether the assistant asks the model to write the sentence around the
    # facts it has already computed. OFF by default: every number the assistant
    # states is a field we hold, the arithmetic is never the model's job, and a
    # stubbed provider must never pass placeholder prose off as advice. When
    # on, a failed or unconfigured provider degrades to the deterministic text
    # rather than failing the turn. See app/assistant/narrative.py.
    assistant_narrative_enabled: bool = False

    # Reason categories offered when a requester justifies a decision -- both
    # FRSs say "reason category plus free text" and neither lists the
    # categories. A PLACEHOLDER list until VZI's vocabulary arrives (open
    # question 9), which is why it is configuration and not an enum.
    assistant_justification_reason_categories: str = (
        "URGENT_BREAKDOWN,NO_SUITABLE_REPAIRABLE,REPAIR_TOO_SLOW,"
        "PLANNED_MAINTENANCE,SAFETY_CRITICAL,PROJECT_WORK,OTHER"
    )

    # Whether the free-text box answers anything at all. When on it routes to a
    # small, explicit set of intents over the DETERMINISTIC read models (see
    # app/assistant/intents.py) -- it is not a general question-answering agent
    # over the data, and that remains a separate scope decision.
    assistant_free_text_intents_enabled: bool = True

    @property
    def cors_origins(self) -> list[str]:
        """Allowed CORS origins, parsed from ``FRONTEND_ORIGIN``."""
        return [origin.strip() for origin in self.frontend_origin.split(",") if origin.strip()]

    @property
    def i13_oar_mrp_type_set(self) -> frozenset[str]:
        """Normalised MRP type codes that count as OAR (in-scope) materials."""
        return frozenset(code.strip().upper() for code in self.i13_oar_mrp_types.split(",") if code.strip())

    @property
    def i13_min_max_mrp_type_set(self) -> frozenset[str]:
        """Normalised MRP type codes that count as Min-Max (stocked) materials."""
        return frozenset(code.strip().upper() for code in self.i13_min_max_mrp_types.split(",") if code.strip())

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
