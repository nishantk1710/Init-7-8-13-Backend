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
    # This is the ONLY place the database is named. Local development points it
    # at Postgres; the deployed environment points it at the managed database.
    # Swapping environments is therefore a config change, never a code change:
    # nothing below this line, and nothing in app/core/db.py, knows which
    # engine it is talking to.
    #
    #   local:  postgresql+psycopg://postgres:<password>@localhost:5432/spares_ai
    #
    # NOTE: a Postgres-to-SQL-Server move is NOT config-only -- dialect, driver
    # and several types differ. Keep models on portable SQLAlchemy constructs so
    # that swap stays small. See README, "Database".
    database_url: str = ""

    # Echo every SQL statement to the log. Local debugging only.
    database_echo: bool = False

    # Object storage location. Like database_url, this is the ONLY place storage
    # is named, and the adapter is chosen from the URL scheme -- so moving from a
    # local folder to cloud storage is a config change, not a code change.
    #
    #   local:  D:/vzi-data/extracts        (a plain path is accepted)
    #           file:///D:/vzi-data/extracts
    #   cloud:  abfss://<container>@<account>.dfs.core.windows.net/<path>
    #
    # Empty by default so the app starts with no storage configured.
    storage_url: str = ""

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

    # --- Reserved for future integrations. Not read by any code yet, and not
    # --- required for startup. See app/core/security.py and app/integrations/.
    azure_tenant_id: str = ""
    azure_client_id: str = ""

    # --- Initiative 13: End-to-End Spares Utilisation Tracking. ---
    # Root directory holding the CSV-backed SAP/platform datasets (both the
    # live-set reader and the reduced mock reader read from here -- see
    # app/integrations/sap/). Relative paths resolve from the process cwd.
    i13_data_dir: str = "data-generator/generated"

    # Current VZI OAR material-scope ruling (MARC.DISMM), comma-separated.
    # See app/shared/material_scope/policy.py -- this is shared platform
    # config, not owned by any single initiative.
    i13_oar_mrp_types: str = "ND,PD"
    i13_min_max_mrp_types: str = "VB"

    # W6.2 reservation source switch (implementation plan §11): "mock" (the
    # synthetic ReservationItemSet.csv, tied to the same synthetic PR/PO/GR/GI
    # dataset LiveSapGateway reads today) or "postgres" (real RESB rows
    # already extracted into raw_resb -- see
    # app/integrations/sap/postgres_reservation.py). The rest of I13 -- the
    # ledger builder, aging, the API -- does not know or care which is active;
    # only app.integrations.sap.gateway.SapGateway reads this.
    i13_reservation_source: str = "mock"

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

    # Reconciliation tolerance for local validation against reference reports.
    i13_reconciliation_tolerance_pct: float = 5.0

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


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
