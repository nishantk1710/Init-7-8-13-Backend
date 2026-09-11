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

    # --- Reserved for future integrations. Not read by any code yet, and not
    # --- required for startup. See app/core/security.py and app/integrations/.
    sap_base_url: str = ""
    sap_client_id: str = ""
    sap_client_secret: str = ""
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
